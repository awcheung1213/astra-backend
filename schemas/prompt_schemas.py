from schemas.base_schemas import BaseRequest, BaseResponse


class SignoffRequest(BaseRequest):
    prompt_ids: list[str]  # the generated prompts the human chose to activate


class SignoffResponse(BaseResponse):
    brand_id: str
    activated_count: int
    collection_started: bool  # the first fill was deferred (#6 baseline)
