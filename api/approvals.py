"""The approval gate's API half: a human approves or rejects a pending
artifact, and approval FANS OUT the follow-on work by artifact type. The
floor, the born-status decision, and the fan-out itself live in
utils/approval_flow.py — shared with the producers, which run the same
fan-out at birth for artifacts the brand's preset auto-approves. A human
verdict here and an auto-approval there set the identical work in motion.

Auth rides the router-level guard (utils/authz.py): the artifact/change set
in the path must belong to the caller's org. reviewed_by is the verified
caller (the guard's user id on request.state) — never a client-sent field."""

from fastapi import APIRouter, HTTPException, Request, status

from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, STATUS_REJECTED, Artifact
from objects.brand import Brand
from objects.change_set import STATUS_APPLIED, ChangeSet
from objects.connection import Connection
from objects.site import Site
from schemas.approval_schemas import DECISION_APPROVE, DECISION_REJECT, PitchEdits, RevertResponse, ReviewRequest, ReviewResponse, UnsendResponse
from utils import clock
from utils.approval_flow import create_and_dispatch_change_set, fan_out_approval, needs_site_connection
from utils.authz import current_user
from utils.pitch_mail import DETAIL_ALREADY_SENT, DETAIL_CONNECT_MAIL, DETAIL_NEEDS_ADDRESS, DETAIL_NOT_SET_UP, MAILBOX_ROUTES, ROUTE_NONE, apply_pitch_edits, can_review_pitch, can_unsend, clean_recipient, manual_send, mark_cancelled, new_send_id, pitch_review_mode, resolve_sender, scheduled_send
from worker.tasks.changes import invert_operations

router = APIRouter(prefix="/api/v1")


@router.post("/artifacts/{artifact_id}/review", response_model=ReviewResponse)
async def review_artifact(artifact_id: str, request: ReviewRequest, http_request: Request) -> ReviewResponse:
    """Approve or reject ONE pending artifact. 409 unless it is pending —
    verdicts are single-shot (one exception: "Try again" re-approves a pitch
    whose send failed) — and 409 on approving a site change while no
    site is connected (it would be recorded approved with nothing to apply and
    no way back to the queue). Approving a PITCH sends it (edits first, then
    422/409 refusals — see _schedule_pitch_send). Approval fans out by type (see fan_out_approval);
    rejection just records the verdict (producers regenerate on their own
    cadence, e.g. rejected review-ask templates regenerate next sweep)."""
    if request.decision not in (DECISION_APPROVE, DECISION_REJECT):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="decision must be approve or reject")
    # row lock held to commit: concurrent verdicts serialize, the losers see the winner's status and 409 (one change set, one fan-out)
    artifact = await Artifact.get_for_update(artifact_id)
    pitch_retry = artifact.type == "pitch" and can_review_pitch(artifact.status, request.decision == DECISION_APPROVE, (artifact.content or {}).get("send"))
    if artifact.status != STATUS_PENDING_REVIEW and not pitch_retry:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"artifact is {artifact.status}, not pending review")
    if request.decision == DECISION_APPROVE and await needs_site_connection(artifact.brand_id, artifact.type):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Connect your site before approving site changes — there is nothing to apply them to yet")

    send_at = None
    if artifact.type == "pitch" and request.decision == DECISION_APPROVE:
        send_at = await _schedule_pitch_send(artifact, request.edits)

    if artifact.type == "gbp_touches" and request.decision == DECISION_APPROVE and not await gbp_connected(artifact.brand_id):
        # no Google Business Profile connection: "Done, I posted them" — he posted them himself; nothing goes out from us
        artifact.content = {**(artifact.content or {}), "posted_by": "owner"}

    verdict = STATUS_APPROVED if request.decision == DECISION_APPROVE else STATUS_REJECTED
    artifact = await Artifact.review(artifact.id, status=verdict, reviewed_by=current_user(http_request).id)

    queued_work: list[str] = []
    if verdict == STATUS_APPROVED:
        queued_work = await fan_out_approval(artifact, request.approved_rows)
    return ReviewResponse.create({"artifact_id": artifact.id, "status": artifact.status, "queued_work": queued_work, "send_at": send_at})


async def _schedule_pitch_send(artifact: Artifact, edits: PitchEdits | None) -> str | None:
    """Approving a pitch SENDS it. The owner's edits land first and are
    flushed BEFORE the checks below on purpose: an HTTPException still
    commits this request's transaction (the middleware sees a response), so
    what he typed survives a 422 "add their address" or the 409 connect_mail
    detour through his mailbox's consent screen.

    No edits posted + nothing to send with (route none, or no usable
    address) = the owner's "I sent it": recorded sent by hand (route
    "manual"), no job. Otherwise: a valid recipient (422), a route (409), a
    connected mailbox when the route needs one (409 connect_mail) — and
    content["send"] is stamped scheduled (with the exact subject/body) for
    the fan-out. Returns send_at (None for a manual send)."""
    edits_given = edits is not None
    content = apply_pitch_edits(artifact.content, edits.model_dump(exclude_unset=True) if edits is not None else None)
    if content != artifact.content:
        artifact = await Artifact.set_content(artifact.id, content)

    recipient = clean_recipient(content.get("to_email"))
    subject, body = str(content.get("subject") or ""), str(content.get("pitch") or "")
    sender = await resolve_sender(await Brand.get(artifact.brand_id))
    if pitch_review_mode(edits_given, recipient, sender["route"]) == "manual":
        await Artifact.set_content(artifact.id, {**content, "send": manual_send(clock.now(), new_send_id(), recipient, subject, body)})
        return None

    if recipient is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=DETAIL_NEEDS_ADDRESS)
    if sender["route"] == ROUTE_NONE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DETAIL_NOT_SET_UP)
    if sender["route"] in MAILBOX_ROUTES and not sender["connected"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DETAIL_CONNECT_MAIL)

    send = scheduled_send(sender["route"], sender["from_address"], recipient, clock.now(), new_send_id(), subject, body)
    await Artifact.set_content(artifact.id, {**content, "to_email": recipient, "send": send})
    return send["send_at"]


@router.post("/artifacts/{artifact_id}/unsend", response_model=UnsendResponse)
async def unsend_pitch(artifact_id: str) -> UnsendResponse:
    """Take back an approved pitch inside its unsend window: the scheduled
    send is cancelled and the pitch returns to the approvals queue (his
    edits kept). 409 "Already sent." once the send has started — the
    send_pitch job takes the same row lock, so the two can't both win."""
    artifact = await Artifact.get_for_update(artifact_id)
    send: dict = (artifact.content or {}).get("send") or {}
    if artifact.type != "pitch" or not can_unsend(send):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DETAIL_ALREADY_SENT)
    await Artifact.set_content(artifact.id, {**artifact.content, "send": mark_cancelled(send, clock.now())}, status=STATUS_PENDING_REVIEW)
    return UnsendResponse.create({"ok": True})


@router.post("/change_sets/{change_set_id}/revert", response_model=RevertResponse)
async def revert_change_set(change_set_id: str) -> RevertResponse:
    """A revert IS just a new change set: the applied set's completed ops
    flipped from their stored rollback records, processed by the same engine.
    The original is marked reverted and linked; the revert set reuses the
    original's artifact so provenance stays one story. 409 unless the set is
    applied and unreverted; 422 when an op has no rollback record to flip."""
    original = await ChangeSet.get_for_update(change_set_id)  # concurrent Undo clicks serialize: one revert set
    if original.status != STATUS_APPLIED or original.reverted_by_change_set_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"change set is {original.status}{' and already reverted' if original.reverted_by_change_set_id else ''} — only applied, unreverted sets revert")

    inverse_operations = invert_operations(original)
    if inverse_operations is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="change set contains operations with no rollback record (e.g. a published page) — revert by hand")

    site = await Site.get(original.site_id)
    revert_set, queued_work = await create_and_dispatch_change_set(original.artifact_id, site, original.brand_id, inverse_operations)
    await ChangeSet.mark_reverted(original.id, reverted_by_change_set_id=revert_set.id)
    return RevertResponse.create({"change_set_id": original.id, "revert_change_set_id": revert_set.id, "queued_work": queued_work})


async def gbp_connected(brand_id: str) -> bool:
    return any(connection.provider == "gbp" for connection in await Connection.connected_for_brand(brand_id))
