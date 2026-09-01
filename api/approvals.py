"""The approval gate's API half: a human approves or rejects a pending
artifact, and approval FANS OUT the follow-on work by artifact type. This is
the only place an artifact's verdict is decided — agents only ever propose.

The always-approve floor (architecture rule 7) is enforced HERE in code:
outward/irreversible work must never skip the human, no matter what an
approval_settings row says. Any future auto-approve path must consult
requires_human_approval() before skipping the gate.

NO AUTH YET (repo-wide) — reviewed_by rides the request body until the
geoviz token verifier is ported."""

from fastapi import APIRouter, HTTPException, status

from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED, Artifact
from objects.change_set import STATUS_APPLIED, ChangeSet
from objects.site import Site
from objects.task import Task
from schemas.approval_schemas import DECISION_APPROVE, DECISION_REJECT, RevertResponse, ReviewRequest, ReviewResponse
from utils.database import Database
from utils.logger import logger
from utils.task_queue import create_task
from worker.tasks.changes import APPLY_ENABLED_PLATFORMS, invert_operations, operations_for_artifact

router = APIRouter(prefix="/api/v1")

# rule 7: the floor is CODE, never expressible as an approval_settings row.
# Everything here either touches the client's site or leaves the building
# under the brand's name — a human always clicks.
ALWAYS_APPROVE_ARTIFACT_TYPES = {"change_proposal", "draft", "gbp_touches", "reply", "pitch", "calendar"}


def requires_human_approval(artifact_type: str) -> bool:
    """Consulted by any future auto-approve path (approval_settings MODE_AUTO)
    BEFORE skipping the gate — floor types always return True."""
    return artifact_type in ALWAYS_APPROVE_ARTIFACT_TYPES


@router.post("/artifacts/{artifact_id}/review", response_model=ReviewResponse)
async def review_artifact(artifact_id: str, request: ReviewRequest) -> ReviewResponse:
    """Approve or reject ONE pending artifact. 409 unless it is pending —
    verdicts are single-shot. Approval fans out by type (see _fan_out_approval);
    rejection just records the verdict (producers regenerate on their own
    cadence, e.g. rejected review-ask templates regenerate next sweep)."""
    if request.decision not in (DECISION_APPROVE, DECISION_REJECT):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="decision must be approve or reject")
    artifact = await Artifact.get(artifact_id)
    if artifact.status != STATUS_PENDING_REVIEW:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"artifact is {artifact.status}, not pending review")

    verdict = STATUS_APPROVED if request.decision == DECISION_APPROVE else STATUS_REJECTED
    artifact = await Artifact.review(artifact.id, status=verdict, reviewed_by=request.reviewed_by)

    queued_work: list[str] = []
    if verdict == STATUS_APPROVED:
        queued_work = await _fan_out_approval(artifact, request.approved_rows)
    return ReviewResponse.create({"artifact_id": artifact.id, "status": artifact.status, "queued_work": queued_work})


async def _fan_out_approval(artifact: Artifact, approved_rows: list[int] | None) -> list[str]:
    """What an approval sets in motion, by artifact type. Types with no entry
    need nothing: a pitch is approved-then-HUMAN-sent (the design boundary),
    and informational types never sit at pending review."""
    if artifact.type == "calendar":
        # #11 — one content_draft per approved entry (approved_rows narrows it)
        entries = artifact.content.get("entries", [])
        chosen_entries = [entries[row] for row in approved_rows if 0 <= row < len(entries)] if approved_rows is not None else entries
        for entry in chosen_entries:
            await create_task(brand_id=artifact.brand_id, name="content_draft", queue="content", payload={"entry": entry})
        return ["content_draft"] * len(chosen_entries)

    if artifact.type == "plan":
        # an approved roadmap re-grounds the target query list (#4 re-run)
        await create_task(brand_id=artifact.brand_id, name="prompt_research", queue="research")
        # #25 — workstreams the Strategist marked as needing client-collected
        # data become bare client-contribution rows (no queue job; the daily
        # chase nags them; the study write-up rides content_draft once the
        # data arrives)
        study_workstreams = [workstream for workstream in artifact.content.get("workstreams", []) if workstream.get("needs_client_data")]
        for workstream in study_workstreams:
            Database.session.add(Task(brand_id=artifact.brand_id, name="collect_study_data", payload={"workstream": workstream.get("name")}))
        if study_workstreams:
            await Database.session.flush()
        return ["prompt_research"] + ["collect_study_data"] * len(study_workstreams)

    if artifact.type in ("gbp_touches", "reply"):
        # GBP work goes live via the GBP adapter, not the site engine
        await create_task(brand_id=artifact.brand_id, name="gbp_apply", queue="reputation", payload={"artifact_id": artifact.id})
        return ["gbp_apply"]

    if artifact.type in ("change_proposal", "draft"):
        operations = operations_for_artifact(artifact)
        if operations is None:
            # prose judgment proposals — approval recorded, a human applies
            logger.info(f"[approvals] artifact {artifact.id} ({artifact.type}) approved — no mechanical form, apply by hand")
            return []
        site = await Site.by_brand(artifact.brand_id)
        if site is None:
            logger.info(f"[approvals] artifact {artifact.id} approved but the brand has no connected site — nothing to apply")
            return []
        change_set, queued_work = await _create_and_dispatch_change_set(artifact.id, site, artifact.brand_id, operations)
        return queued_work

    return []


async def _create_and_dispatch_change_set(artifact_id: str, site: Site, brand_id: str, operations: list[dict]) -> tuple[ChangeSet, list[str]]:
    """The one path a change set is born through — approval fan-out and revert
    both use it: durable row first, apply enqueued only when the platform's
    write adapter exists (parked pending otherwise)."""
    change_set = await ChangeSet.create(artifact_id=artifact_id, site_id=site.id, brand_id=brand_id, operations=operations)
    if site.platform not in APPLY_ENABLED_PLATFORMS:
        logger.info(f"[approvals] change set {change_set.id} recorded — {site.platform} write adapter not built yet, apply parked")
        return change_set, []
    await create_task(brand_id=brand_id, name="change_set_apply", queue="changes", payload={"change_set_id": change_set.id})
    return change_set, ["change_set_apply"]


@router.post("/change_sets/{change_set_id}/revert", response_model=RevertResponse)
async def revert_change_set(change_set_id: str) -> RevertResponse:
    """A revert IS just a new change set: the applied set's completed ops
    flipped from their stored rollback records, processed by the same engine.
    The original is marked reverted and linked; the revert set reuses the
    original's artifact so provenance stays one story. 409 unless the set is
    applied and unreverted; 422 when an op has no rollback record to flip."""
    original = await ChangeSet.get(change_set_id)
    if original.status != STATUS_APPLIED or original.reverted_by_change_set_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"change set is {original.status}{' and already reverted' if original.reverted_by_change_set_id else ''} — only applied, unreverted sets revert")

    inverse_operations = invert_operations(original)
    if inverse_operations is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="change set contains operations with no rollback record (e.g. a published page) — revert by hand")

    site = await Site.get(original.site_id)
    revert_set, queued_work = await _create_and_dispatch_change_set(original.artifact_id, site, original.brand_id, inverse_operations)
    await ChangeSet.mark_reverted(original.id, reverted_by_change_set_id=revert_set.id)
    return RevertResponse.create({"change_set_id": original.id, "revert_change_set_id": revert_set.id, "queued_work": queued_work})
