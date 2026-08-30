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
  value before writing (previous_value = the rollback record; a revert is a
  NEW change set built from those).

The CMS write adapters are the remaining seam: until a platform lands in
APPLY_ENABLED_PLATFORMS, approval still records the change set (durable,
status pending) — it just is not enqueued for apply."""

from objects.artifact import Artifact
from objects.brand import Brand
from objects.change_set import STATUS_APPLIED, STATUS_APPLYING, STATUS_PENDING, STATUS_REVERTED, ChangeSet
from objects.notification import KIND_UPDATE, Notification
from objects.site import Site
from utils.database import Database
from utils.logger import logger
from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="change_set_apply", queue="changes", retry=2)
async def change_set_apply(task_id: str, brand_id: str) -> None:
    """CHAINED from the approval endpoint (payload carries change_set_id).
    Applies the set op by op, committing each completion marker as it lands —
    already-applied and already-completed work is skipped, so retries resume.
    An unbuilt platform adapter parks the set back at pending (no dead-letter
    noise); any real write failure re-raises so procrastinate retries."""
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
        try:
            for operation in change_set.operations:
                if operation.get("completed_at"):
                    continue  # rule 2: a retried apply RESUMES, never repeats
                apply_result = await _apply_operation(site, operation)
                await ChangeSet.mark_operation_complete(change_set.id, operation["id"], previous_value=apply_result.get("previous_value"))
                await Database.session.commit()  # the marker must survive a later op's failure
                applied_count += 1
        except CmsAdapterNotBuilt as missing:
            await ChangeSet.update_status(change_set.id, STATUS_PENDING)
            await Database.session.commit()
            logger.info(f"[changes] change set {change_set.id}: {missing} adapter not built — parked pending")
            return

        await ChangeSet.update_status(change_set.id, STATUS_APPLIED)
        await Database.session.commit()
        if applied_count:
            brand = await Brand.get(brand_id)
            await Notification.create(organization_id=brand.organization_id, kind=KIND_UPDATE, title="Approved changes were applied to your site", body=f"{applied_count} change(s) went live.", link_ref=f"change_sets/{change_set.id}")


# a platform joins this set when its write adapter below is actually built;
# until then approval records the change set but does not enqueue an apply
APPLY_ENABLED_PLATFORMS: set[str] = set()


class CmsAdapterNotBuilt(Exception):
    """Raised at the adapter seam — the apply parks the set, no dead-letter."""


##############################
# CONVERSION (approved artifact -> operations)
##############################


def operations_for_artifact(artifact: Artifact) -> list[dict] | None:
    """The mechanical operations an approved artifact converts to, or None
    when the type has no mechanical form (prose judgment proposals, GBP work
    awaiting its own adapter) — those stay recorded-approved for a human."""
    if artifact.type == "change_proposal" and artifact.content.get("operations"):
        # the deterministic fix tier's shape: [{field, url, value}]
        return [_operation(index, "set_field", target=proposed["url"], field=proposed["field"], value=proposed["value"]) for index, proposed in enumerate(artifact.content["operations"])]

    if artifact.type == "draft":
        page_content = {content_field: artifact.content.get(content_field) for content_field in ("title", "meta_description", "body_markdown")}
        if artifact.content.get("kind") == "refresh":
            return [_operation(0, "update_page", target=artifact.content.get("page", ""), value=page_content)]
        return [_operation(0, "publish_page", target=None, value=page_content)]

    return None  # judgment-tier prose proposals, gbp_touches, replies, ...


def _operation(index: int, op: str, target: str | None, value, field: str | None = None) -> dict:
    operation = {"id": f"op-{index}", "op": op, "target": target, "value": value, "previous_value": None, "completed_at": None}
    if field:
        operation["field"] = field
    return operation


def invert_operations(change_set: ChangeSet) -> list[dict] | None:
    """The revert payload: each COMPLETED operation flipped from its stored
    rollback record (previous_value), in reverse order of application. None
    when any completed op can't be inverted — today that's publish_page,
    which needs the created page's ref (captured once the CMS adapters
    exist); the caller reports instead of guessing."""
    inverse_operations = []
    completed_operations = [operation for operation in change_set.operations if operation.get("completed_at")]
    for index, operation in enumerate(reversed(completed_operations)):
        if operation["op"] not in ("set_field", "update_page"):
            return None
        inverse_operations.append(_operation(index, operation["op"], target=operation.get("target"), value=operation.get("previous_value"), field=operation.get("field")))
    return inverse_operations


##############################
# PLATFORM WRITE ADAPTERS (the seam)
##############################


async def _apply_operation(site: Site, operation: dict) -> dict:
    """Route one operation to the site's platform adapter. The adapter MUST
    read the current value before writing and return it as previous_value —
    that is the rollback record a revert change set is built from."""
    apply_via_platform = APPLY_BY_PLATFORM.get(site.platform)
    if apply_via_platform is None:
        raise ValueError(f"no write adapter for platform {site.platform!r}")
    return await apply_via_platform(site, operation)


async def _apply_via_wordpress(site: Site, operation: dict) -> dict:
    # TODO(change-set engine): WordPress adapter — REST API with the
    #       application password from site.credentials_encrypted. Read the
    #       current field/page first (-> previous_value), then write. Add
    #       "wordpress" to APPLY_ENABLED_PLATFORMS when this is real.
    raise CmsAdapterNotBuilt("wordpress")


async def _apply_via_shopify(site: Site, operation: dict) -> dict:
    # TODO(change-set engine): Shopify adapter — Admin API with the token from
    #       site.credentials_encrypted. Read first (-> previous_value), then
    #       write. Add "shopify" to APPLY_ENABLED_PLATFORMS when this is real.
    raise CmsAdapterNotBuilt("shopify")


APPLY_BY_PLATFORM = {"wordpress": _apply_via_wordpress, "shopify": _apply_via_shopify}
