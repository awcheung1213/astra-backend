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
from schemas.approval_schemas import DECISION_APPROVE, DECISION_REJECT, ReviewRequest, ReviewResponse
from utils.logger import logger
from utils.task_queue import create_task

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
        # TODO(#25): when a workstream calls for an original data study, fan out
        #       the client-contribution data-collection task rows here (the
        #       workstream schema needs a marker field first).
        return ["prompt_research"]

    if artifact.type in ("change_proposal", "draft", "gbp_touches", "reply"):
        # TODO(change-set engine): convert the approved artifact into a
        #       change_sets row + a change_set_apply task — the ONLY site
        #       toucher (rule 1). Until it lands, approval is recorded and the
        #       change is applied by hand.
        logger.info(f"[approvals] artifact {artifact.id} ({artifact.type}) approved — change-set engine not built yet, apply by hand")
        return []

    return []
