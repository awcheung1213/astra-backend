from datetime import datetime

from schemas.base_schemas import BaseRequest, BaseResponse


class ReviewWebhookRequest(BaseRequest):
    brand_id: str
    platform: str  # google | trustpilot | g2 | yelp
    external_id: str  # the platform's own review id — the dedupe identity
    rating: int
    author: str | None = None
    text: str | None = None
    received_at: datetime | None = None  # platform timestamp; defaults to now


class ReviewWebhookResponse(BaseResponse):
    review_id: str
    queued_work: list[str]  # empty on a re-delivered (duplicate) webhook
