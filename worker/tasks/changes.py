"""The change-set engine — the ONLY thing that writes to a client's site
(rule 1), only through `sites`, only behind the approval gate.

Two halves live here:
- operations_for_artifact(): the conversion vocabulary — which approved
  artifact types become which mechanical operations (the approval endpoint
  calls this at fan-out time).
- change_set_apply: the resumable executor. Each operation carries an id +
  completion marker (rule 2); the runner COMMITS after every operation, so a
  failure mid-set keeps the finished markers and a retried apply RESUMES at
  the first incomplete op — never repeats one. The adapter reads the current
  value before writing and CHECKPOINTS it (committed on the op) before the
  write — previous_value = the rollback record, captured once, so a crash
  between write and marker can't make a retry record the NEW value; a revert
  is a NEW change set built from those. Applies to one site are serialized by
  a per-site procrastinate lock (utils/approval_flow.apply_lock).

The writes themselves live in adapters/cms/ (rule 6 — the only door to the
outside world); a platform not in APPLY_ENABLED_PLATFORMS still gets its
change set recorded at approval (durable, pending), just never enqueued."""

import html
import re
from datetime import timedelta

from sqlalchemy import select

from adapters.cms.common import Checkpoint, CmsWriteError, SeoBridgeNeeded
from adapters.cms.shopify_adapter import ShopifyAdapter
from adapters.cms.wordpress_adapter import DESCRIPTION_WRITE_FIELDS, SEO_BRIDGE_PLUGIN, WordPressAdapter
from objects.artifact import Artifact
from objects.brand import Brand
from objects.change_set import STATUS_APPLIED, STATUS_APPLYING, STATUS_FAILED, STATUS_PENDING, STATUS_REVERTED, ChangeSet
from objects.finding import STATUS_FIXED, STATUS_OPEN, Finding
from objects.notification import KIND_UPDATE, Notification
from objects.site import Site
from utils import clock
from utils.database import Database
from utils.logger import logger
from utils.receipts import CHANGE_NOTICE_BATCH_HOURS, FIX_DID_NOT_TAKE_KEY, batched_change_notice, change_notification_text, finding_fix_transition, live_fix_change_set_id, proposal_finding_ids, reopened_detail, resolved_detail
from utils.site_fetch import fetch_page
from utils.task_queue import RETRY_WITH_BACKOFF, create_task, task_lifecycle
from worker.app import app


@app.task(name="change_set_apply", queue="changes", retry=RETRY_WITH_BACKOFF)
async def change_set_apply(task_id: str, brand_id: str) -> None:
    """CHAINED from the approval endpoint (payload carries change_set_id).
    Applies the set op by op, committing each completion marker as it lands —
    already-applied and already-completed work is skipped, so retries resume.
    A write failure (CmsWriteError) re-raises so procrastinate retries;
    exhausted retries dead-letter to the operator with the set mid-applying."""
    async with task_lifecycle(task_id) as task:
        change_set_id = (task.payload or {}).get("change_set_id", "")
        change_set = await ChangeSet.get(change_set_id)
        if change_set.status in (STATUS_APPLIED, STATUS_REVERTED):
            logger.info(f"[changes] change set {change_set.id}: already {change_set.status} — nothing to do")
            return
        site = await Site.get(change_set.site_id)
        await ChangeSet.update_status(change_set.id, STATUS_APPLYING)
        await Database.session.commit()

        applied_count = 0
        for operation in change_set.operations:
            if operation.get("completed_at"):
                continue  # rule 2: a retried apply RESUMES, never repeats
            try:
                apply_result = await _apply_operation(site, operation, _checkpoint_for(change_set.id, operation["id"]))
            except SeoBridgeNeeded as needed:
                # not a failure to retry: the owner has to OK a helper plugin first
                logger.info(f"[changes] change set {change_set.id}: {needed} — parked, asking the owner")
                await park_for_seo_bridge(change_set, task.id)
                await Database.session.commit()
                return
            await ChangeSet.mark_operation_complete(change_set.id, operation["id"], apply_result)
            await Database.session.commit()  # the marker must survive a later op's failure
            applied_count += 1

        await ChangeSet.update_status(change_set.id, STATUS_APPLIED)
        # same commit as the status flip: an issue closes the moment its fix is
        # live and reopens when the undo lands — never one without the other
        await sync_findings_with_fix(change_set.artifact_id)
        await Database.session.commit()
        await after_applied(change_set)
        await Database.session.commit()
        if applied_count:
            brand = await Brand.get(brand_id)
            artifact = await Database.session.get(Artifact, change_set.artifact_id)
            original = await _reverted_original(change_set.id)
            title, body = change_notification_text(change_set, artifact, original)
            unread = await Notification.unread_change_notice(brand.organization_id, since=clock.now() - timedelta(hours=CHANGE_NOTICE_BATCH_HOURS))
            if unread is not None:
                unread.title, unread.body, unread.link_ref = batched_change_notice(unread.title, unread.body, unread.link_ref, title, change_set.id)
                unread.created_at = clock.now()
                Database.session.add(unread)
                await Database.session.flush()
            else:
                await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title=title, body=body, link_ref=f"change_sets/{change_set.id}")


##############################
# SEO BRIDGE (Yoast's description field) + LIVE VERIFICATION
##############################

SEO_BRIDGE_KIND = "seo_bridge"
VERIFY_DELAY_SECONDS = 120  # page caches (LiteSpeed etc.) purge on update; give them a moment
VERIFY_RETRY_DELAY_SECONDS = 1800
VERIFY_MAX_ATTEMPTS = 2
VERIFY_CHECKING = "checking"
VERIFY_CONFIRMED = "confirmed"
VERIFY_NOT_SHOWING = "not_showing"
VERIFY_WAITING_ON_BRIDGE = "waiting_on_you"


def seo_bridge_proposal(site_url: str, waiting_ids: list[str]) -> dict:
    """The one-tap ask: install the free helper plugin so Google descriptions
    land where Yoast reads them. Pure."""
    return {
        "kind": SEO_BRIDGE_KIND,
        "title": "Let us set your Google descriptions",
        "summary": "Your site uses Yoast for the text under your links in Google. To write it, we add a free helper plugin. You can undo it anytime.",
        "display": {"title": "Add a free helper plugin so we can set your Google descriptions"},
        "operations": [{"op": "install_plugin", "slug": SEO_BRIDGE_PLUGIN, "url": site_url}],
        "waiting_change_set_ids": waiting_ids,
    }


async def park_for_seo_bridge(change_set: ChangeSet, task_id: str) -> None:
    """Park the set (failed, verification waiting_on_you) and make sure ONE
    bridge proposal waits for the owner, listing every set it unblocks."""
    await ChangeSet.update_status(change_set.id, STATUS_FAILED)
    await ChangeSet.set_verification(change_set.id, {"status": VERIFY_WAITING_ON_BRIDGE, "checked_at": clock.now().isoformat()})
    pending = [artifact for artifact in await Artifact.pending_for_brand(change_set.brand_id) if artifact.type == "change_proposal" and (artifact.content or {}).get("kind") == SEO_BRIDGE_KIND]
    if pending:
        bridge = pending[0]
        waiting = list(dict.fromkeys([*(bridge.content.get("waiting_change_set_ids") or []), change_set.id]))
        bridge.content = {**bridge.content, "waiting_change_set_ids": waiting}
        Database.session.add(bridge)
        await Database.session.flush()
        return
    site_url = (await Brand.get(change_set.brand_id)).website_url
    await Artifact.create(task_id=task_id, brand_id=change_set.brand_id, type="change_proposal", content=seo_bridge_proposal(site_url, [change_set.id]))


async def after_applied(change_set: ChangeSet) -> None:
    """Follow-ups once a set is live: a bridge install re-queues the fixes it
    unblocked; a description write gets its live re-read scheduled."""
    artifact = await Database.session.get(Artifact, change_set.artifact_id)
    content = (artifact.content or {}) if artifact is not None else {}
    if content.get("kind") == SEO_BRIDGE_KIND and not await _reverted_original(change_set.id):
        for waiting_id in content.get("waiting_change_set_ids") or []:
            waiting = await ChangeSet.get(waiting_id)
            if waiting.status == STATUS_FAILED:
                await ChangeSet.update_status(waiting.id, STATUS_PENDING)
                await create_task(brand_id=waiting.brand_id, name="change_set_apply", queue="changes", payload={"change_set_id": waiting.id}, lock=f"site:{waiting.site_id}")
    if description_targets(change_set.operations):
        await ChangeSet.set_verification(change_set.id, {"status": VERIFY_CHECKING, "checked_at": clock.now().isoformat(), "attempts": 0})
        await create_task(brand_id=change_set.brand_id, name="change_set_verify", queue="changes", payload={"change_set_id": change_set.id, "attempt": 1}, delay_seconds=VERIFY_DELAY_SECONDS)


def description_targets(operations: list[dict]) -> list[tuple[str, str]]:
    """(page url, description) for each completed description write. Pure."""
    return [(operation["target"], str(operation.get("value") or "")) for operation in operations if operation.get("op") == "set_field" and operation.get("field") in DESCRIPTION_WRITE_FIELDS and operation.get("completed_at") and operation.get("target")]


META_DESCRIPTION_PATTERN = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']description[\"'][^>]*>", re.IGNORECASE)
CONTENT_ATTRIBUTE_PATTERN = re.compile(r"\bcontent\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)


def page_description(page_html: str) -> str | None:
    """The <meta name="description"> text Google reads, or None. Pure."""
    tag = META_DESCRIPTION_PATTERN.search(page_html or "")
    if not tag:
        return None
    content = CONTENT_ATTRIBUTE_PATTERN.search(tag.group(0))
    return html.unescape(content.group(2) if content.group(2) is not None else content.group(3)) if content else None


def shows_description(page_html: str, expected: str) -> bool:
    """Does the live page carry the description we wrote (whitespace-insensitive)? Pure."""
    found = page_description(page_html)
    squash = lambda text: " ".join(text.split()).strip()  # noqa: E731
    return found is not None and squash(found) == squash(html.unescape(expected))


@app.task(name="change_set_verify", queue="changes", retry=RETRY_WITH_BACKOFF)
async def change_set_verify(task_id: str, brand_id: str) -> None:
    """CHAINED after a description write lands: re-read each live page (cache
    busted) and record whether Google can actually see it. Not showing ->
    one later retry, then the receipt says so and the issue stays open."""
    async with task_lifecycle(task_id) as task:
        payload = task.payload or {}
        change_set = await ChangeSet.get(payload.get("change_set_id", ""))
        attempt = int(payload.get("attempt") or 1)
        if change_set.status != STATUS_APPLIED:
            return
        targets = description_targets(change_set.operations)
        results = []
        for url, expected in targets:
            response = await fetch_page(f"{url}{'&' if '?' in url else '?'}evergreen_check={int(clock.now().timestamp())}")
            results.append(response is not None and response.status_code < 400 and shows_description(response.text, expected))
        if all(results):
            await ChangeSet.set_verification(change_set.id, {"status": VERIFY_CONFIRMED, "checked_at": clock.now().isoformat(), "attempts": attempt})
            return
        if attempt < VERIFY_MAX_ATTEMPTS:
            await ChangeSet.set_verification(change_set.id, {"status": VERIFY_CHECKING, "checked_at": clock.now().isoformat(), "attempts": attempt})
            await create_task(brand_id=brand_id, name="change_set_verify", queue="changes", payload={"change_set_id": change_set.id, "attempt": attempt + 1}, delay_seconds=VERIFY_RETRY_DELAY_SECONDS)
            return
        await ChangeSet.set_verification(change_set.id, {"status": VERIFY_NOT_SHOWING, "checked_at": clock.now().isoformat(), "attempts": attempt})
        await reopen_findings_not_showing(change_set.artifact_id)
        logger.warning(f"[changes] change set {change_set.id}: description written but the live page doesn't show it")


async def reopen_findings_not_showing(artifact_id: str) -> None:
    """The fix is live in the CMS but not on the page: its issues go back to
    open with the fix_did_not_take marker ("Still showing after our fix"),
    the same state the next site check would have found."""
    artifact = await Database.session.get(Artifact, artifact_id)
    finding_ids = proposal_finding_ids(artifact.content) if artifact is not None and artifact.type == "change_proposal" else []
    for finding in await Finding.by_ids_for_update(finding_ids):
        if finding.status == STATUS_FIXED:
            await Finding.set_lifecycle(finding, STATUS_OPEN, {**(finding.detail or {}), FIX_DID_NOT_TAKE_KEY: True})


async def sync_findings_with_fix(artifact_id: str) -> list[tuple[str, str]]:
    """The fix lifecycle (marco-review R5): after any apply of a fix
    proposal's change set — the fix itself, its undo, a redo — its findings
    follow whether the fix is live NOW: open -> fixed (marked resolved by the
    live set) or, once undone, fixed -> open (only findings THIS fix closed).
    Idempotent: a second run finds nothing to transition. Returns the
    (finding_id, action) pairs it applied."""
    artifact = await Database.session.get(Artifact, artifact_id)
    finding_ids = proposal_finding_ids(artifact.content) if artifact is not None and artifact.type == "change_proposal" else []
    if not finding_ids:
        return []
    chain = await ChangeSet.for_artifact(artifact_id)
    live_id = live_fix_change_set_id(chain)
    chain_ids = {change_set.id for change_set in chain}
    applied: list[tuple[str, str]] = []
    for finding in await Finding.by_ids_for_update(finding_ids):
        action = finding_fix_transition(finding.status, finding.detail, live_id, chain_ids)
        if action == "resolve" and live_id:
            await Finding.set_lifecycle(finding, STATUS_FIXED, resolved_detail(finding.detail, live_id))
        elif action == "reopen":
            await Finding.set_lifecycle(finding, STATUS_OPEN, reopened_detail(finding.detail))
        else:
            continue
        applied.append((finding.id, action))
    if applied:
        logger.info(f"[changes] artifact {artifact_id}: findings {applied}")
    return applied


async def _reverted_original(change_set_id: str) -> ChangeSet | None:
    """The set this one undoes — a revert set is the one some original's
    reverted_by_change_set_id points at; None for a forward change."""
    rows = await Database.session.execute(select(ChangeSet).where(ChangeSet.reverted_by_change_set_id == change_set_id).limit(1))  # type: ignore
    return rows.scalars().first()


def _checkpoint_for(change_set_id: str, operation_id: str) -> Checkpoint:
    """The adapter's mid-op checkpoint: persist + COMMIT before it touches the
    site, so the rollback record outlives any crash after the write."""

    async def checkpoint(fields: dict) -> None:
        await ChangeSet.record_operation_progress(change_set_id, operation_id, fields)
        await Database.session.commit()

    return checkpoint


# a platform joins this set when its write adapter is actually built; any
# other platform's approval records the change set but never enqueues an apply
APPLY_ENABLED_PLATFORMS: set[str] = {"wordpress", "shopify"}


##############################
# CONVERSION (approved artifact -> operations)
##############################


def native_field(platform: str, field: str) -> str | None:
    """The CMS-native field a proposal's field name writes on this platform,
    or None when the platform has no such slot (or no adapter). Proposal
    validation uses this so a bad field is refused at proposal time, not at
    apply time."""
    adapter_class = ADAPTER_BY_PLATFORM.get(platform)
    if adapter_class is None:
        return None
    try:
        return adapter_class.resolve_field(field)
    except CmsWriteError:
        return None


def operations_for_artifact(artifact: Artifact) -> list[dict] | None:
    """The mechanical operations an approved artifact converts to, or None
    when the type has no mechanical form (prose judgment proposals) — those
    stay recorded-approved for a human. GBP work never comes here: it applies
    via gbp_apply (worker/tasks/reputation.py), not the site engine."""
    if artifact.type == "change_proposal" and artifact.content.get("operations"):
        # the deterministic fix tier's shape: [{field, url, value}]
        return [
            _operation(index, "install_plugin", target=proposed.get("url"), value={"slug": proposed["slug"]}) if proposed.get("op") == "install_plugin" else _operation(index, "set_field", target=proposed["url"], field=proposed["field"], value=proposed["value"])
            for index, proposed in enumerate(artifact.content["operations"])
        ]

    if artifact.type == "draft":
        page_content = {content_field: artifact.content.get(content_field) for content_field in ("title", "meta_description", "body_markdown")}
        if artifact.content.get("kind") == "refresh":
            return [_operation(0, "update_page", target=artifact.content.get("page", ""), value=page_content)]
        return [_operation(0, "publish_page", target=None, value=page_content)]

    return None  # judgment-tier prose proposals


def _operation(index: int, op: str, target: str | None, value, field: str | None = None) -> dict:
    operation = {"id": f"op-{index}", "op": op, "target": target, "value": value, "previous_value": None, "completed_at": None}
    if field:
        operation["field"] = field
    return operation


def invert_operations(change_set: ChangeSet) -> list[dict] | None:
    """The revert payload: each COMPLETED operation flipped from what its
    apply recorded, in reverse order of application — set_field/update_page
    restore previous_value OF THE SAME FIELD (the adapter contract: read the
    field, write the field, so the record always fits the slot it came from);
    publish_page becomes unpublish_page(created_ref). None when any completed
    op has nothing to flip (a pre-created_ref publish, an unpublish itself);
    the caller reports instead of guessing."""
    inverse_operations = []
    completed_operations = [operation for operation in change_set.operations if operation.get("completed_at")]
    for index, operation in enumerate(reversed(completed_operations)):
        if operation["op"] == "publish_page" and operation.get("created_ref"):
            inverse_operations.append(_operation(index, "unpublish_page", target=operation["created_ref"], value=None))
            continue
        if operation["op"] == "install_plugin" and operation.get("created_ref") and operation.get("previous_value") != "active":
            inverse_operations.append(_operation(index, "deactivate_plugin", target=operation["created_ref"], value=None))
            continue
        if operation["op"] not in ("set_field", "update_page"):
            return None
        inverse_operations.append(_operation(index, operation["op"], target=operation.get("target"), value=operation.get("previous_value"), field=operation.get("field")))
    return inverse_operations


##############################
# PLATFORM WRITE ADAPTERS
##############################


async def _apply_operation(site: Site, operation: dict, checkpoint: Checkpoint) -> dict:
    """Route one operation to the site's platform adapter (adapters/cms/).
    The adapter contract: READ the current value before writing, checkpoint
    it, and return it as previous_value (the rollback record reverts are built
    from); checkpoint + return created_ref for anything the op created."""
    adapter_class = ADAPTER_BY_PLATFORM.get(site.platform)
    if adapter_class is None:
        raise ValueError(f"no write adapter for platform {site.platform!r}")
    return await adapter_class(site).apply(operation, checkpoint)


ADAPTER_BY_PLATFORM: dict[str, type[WordPressAdapter] | type[ShopifyAdapter]] = {"wordpress": WordPressAdapter, "shopify": ShopifyAdapter}
