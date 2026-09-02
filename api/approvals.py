"""The approval gate's API half: a human approves or rejects a pending
artifact, and approval FANS OUT the follow-on work by artifact type. The
floor, the born-status decision, and the fan-out itself live in
utils/approval_flow.py — shared with the producers, which run the same
fan-out at birth for artifacts the brand's preset auto-approves. A human
verdict here and an auto-approval there set the identical work in motion.

NO AUTH YET (repo-wide) — reviewed_by rides the request body until the
geoviz token verifier is ported."""

from fastapi import APIRouter, HTTPException, status

from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED, Artifact
from objects.change_set import STATUS_APPLIED, ChangeSet
from objects.site import Site
from schemas.approval_schemas import DECISION_APPROVE, DECISION_REJECT, RevertResponse, ReviewRequest, ReviewResponse
from utils.approval_flow import create_and_dispatch_change_set, fan_out_approval
from worker.tasks.changes import invert_operations

router = APIRouter(prefix="/api/v1")


@router.post("/artifacts/{artifact_id}/review", response_model=ReviewResponse)
async def review_artifact(artifact_id: str, request: ReviewRequest) -> ReviewResponse:
    """Approve or reject ONE pending artifact. 409 unless it is pending —
    verdicts are single-shot. Approval fans out by type (see fan_out_approval);
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
        queued_work = await fan_out_approval(artifact, request.approved_rows)
    return ReviewResponse.create({"artifact_id": artifact.id, "status": artifact.status, "queued_work": queued_work})


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
    revert_set, queued_work = await create_and_dispatch_change_set(original.artifact_id, site, original.brand_id, inverse_operations)
    await ChangeSet.mark_reverted(original.id, reverted_by_change_set_id=revert_set.id)
    return RevertResponse.create({"change_set_id": original.id, "revert_change_set_id": revert_set.id, "queued_work": queued_work})
