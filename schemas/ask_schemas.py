from schemas.base_schemas import BaseRequest, BaseResponse


class AskRequest(BaseRequest):
    question: str


class AskResponse(BaseResponse):
    answer: str  # the Analyst's markdown answer, receipts included
    diagnosis_queued: bool  # a deep investigation was chained (#23); lands in Pulse
