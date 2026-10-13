from schemas.base_schemas import BaseRequest, BaseResponse


class AskRequest(BaseRequest):
    question: str


class AskResponse(BaseResponse):
    answer: str  # the Analyst's markdown answer, receipts included
    answer_summary: str  # the answer's first line — the direct answer, markdown stripped
    diagnosis_queued: bool  # a deep investigation was chained (#23); lands in Pulse
