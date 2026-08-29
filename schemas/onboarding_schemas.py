from schemas.base_schemas import BaseRequest, BaseResponse


class LaunchResponse(BaseResponse):
    brand_id: str
    client_tasks: list[str]  # #1/#7 — rows the client must complete (chased daily)
    queued_work: list[str]  # the kickoff fan-out actually enqueued


class InterviewRequest(BaseRequest):
    # question -> answer map from the AM-conducted interview; stored verbatim
    answers: dict


class InterviewResponse(BaseResponse):
    """The extraction for the client to confirm — extraction-filled,
    client-confirmed (edits ride the brand update endpoint when it lands)."""

    brand_id: str
    description: str
    category: str
    audience: str
    differentiators: list[str]
