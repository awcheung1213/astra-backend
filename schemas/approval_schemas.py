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
