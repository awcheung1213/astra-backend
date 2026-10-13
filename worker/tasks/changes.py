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

from datetime import timedelta

from sqlalchemy import select

from adapters.cms.common import Checkpoint, CmsWriteError
from adapters.cms.shopify_adapter import ShopifyAdapter
from adapters.cms.wordpress_adapter import WordPressAdapter
from objects.artifact import Artifact
from objects.brand import Brand
from objects.change_set import STATUS_APPLIED, STATUS_APPLYING, STATUS_REVERTED, ChangeSet
from objects.finding import STATUS_FIXED, STATUS_OPEN, Finding
from objects.notification import KIND_UPDATE, Notification
from objects.site import Site
from utils import clock
from utils.database import Database
from utils.logger import logger
from utils.receipts import CHANGE_NOTICE_BATCH_HOURS, batched_change_notice, change_notification_text, finding_fix_transition, live_fix_change_set_id, proposal_finding_ids, reopened_detail, resolved_detail
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
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
            apply_result = await _apply_operation(site, operation, _checkpoint_for(change_set.id, operation["id"]))
            await ChangeSet.mark_operation_complete(change_set.id, operation["id"], apply_result)
            await Database.session.commit()  # the marker must survive a later op's failure
            applied_count += 1

        await ChangeSet.update_status(change_set.id, STATUS_APPLIED)
        # same commit as the status flip: an issue closes the moment its fix is
        # live and reopens when the undo lands — never one without the other
        await sync_findings_with_fix(change_set.artifact_id)
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
        return [_operation(index, "set_field", target=proposed["url"], field=proposed["field"], value=proposed["value"]) for index, proposed in enumerate(artifact.content["operations"])]

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
