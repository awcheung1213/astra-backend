from schemas.base_schemas import BaseRequest, BaseResponse

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


class ReviewRequest(BaseRequest):
    decision: str  # approve | reject
    # calendar only: which entry indices were approved (omitted = all of them)
    approved_rows: list[int] | None = None
    reviewed_by: str | None = None


class ReviewResponse(BaseResponse):
    artifact_id: str
    status: str
    queued_work: list[str]  # task names the approval fanned out


class RevertResponse(BaseResponse):
    change_set_id: str  # the original, now marked reverted
    revert_change_set_id: str  # the new set that undoes it
    queued_work: list[str]  # empty when parked awaiting the CMS adapter
