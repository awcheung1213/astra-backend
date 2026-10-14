from schemas.base_schemas import BaseRequest, BaseResponse


class SignoffRequest(BaseRequest):
    prompt_ids: list[str]  # the generated prompts the human chose to activate


class SignoffResponse(BaseResponse):
    brand_id: str
    activated_count: int
    collection_started: bool  # the first fill was deferred (#6 baseline)


class QuestionUpdateRequest(BaseRequest):
    tier: str | None = None  # core | watching | suggested (core/watching pin it; suggested stops it)
    pinned: bool | None = None


class QuestionTextRequest(BaseRequest):
    text: str  # the customer's question, 5-300 characters; business names are taken out


class ReplacedQuestion(BaseResponse):
    id: str
    topic: str | None = None


class QuestionRow(BaseResponse):
    id: str
    text: str
    topic: str | None = None
    category: str
    topic_group: str
    source: str
    is_active: bool  # mirrors tier == core (older FE builds)
    tier: str
    pinned: bool
    tier_reason: str | None = None
    tier_changed_at: str | None = None
    last_checked_at: str | None = None


class QuestionUpdateResponse(QuestionRow):
    replaced: ReplacedQuestion | None = None  # the core question that moved to monthly to make room


class QuestionAddResponse(BaseResponse):
    question: QuestionRow
    replaced: ReplacedQuestion | None = None


class QuestionEditResponse(BaseResponse):
    question: QuestionRow
