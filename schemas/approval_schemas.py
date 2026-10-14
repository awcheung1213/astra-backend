from schemas.base_schemas import BaseRequest, BaseResponse

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


class PitchEdits(BaseRequest):
    """The owner's edits to a pending pitch, applied before approval. Only
    the keys sent change (to_name may be sent as null to drop the name)."""

    to_email: str | None = None
    to_name: str | None = None
    subject: str | None = None
    body: str | None = None


class ReviewRequest(BaseRequest):
    decision: str  # approve | reject
    # calendar only: which entry indices were approved (omitted = all of them)
    approved_rows: list[int] | None = None
    reviewed_by: str | None = None
    # pitch only: edits applied to the pending pitch before it is approved (and sent)
    edits: PitchEdits | None = None


class ReviewResponse(BaseResponse):
    artifact_id: str
    status: str
    queued_work: list[str]  # task names the approval fanned out
    send_at: str | None = None  # pitch only: when the approved email goes out (ISO, UTC) — unsend works until then


class UnsendResponse(BaseResponse):
    ok: bool


class RevertResponse(BaseResponse):
    change_set_id: str  # the original, now marked reverted
    revert_change_set_id: str  # the new set that undoes it
    queued_work: list[str]  # empty when parked awaiting the CMS adapter
