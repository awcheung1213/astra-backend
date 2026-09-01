"""Event entry points from the outside world. First tenant: new reviews —
fast reply is the point, so a review becomes a review_response task the
moment it arrives instead of waiting for any sweep.

Webhook endpoints must be idempotent: platforms re-deliver. The review's
(brand, platform, external_id) identity dedupes — a duplicate delivery finds
its existing row and queues nothing.

NO AUTH YET (repo-wide) — webhook signature verification rides the same
future pass as the token verifier port."""

from datetime import datetime

from fastapi import APIRouter

from objects.brand import Brand
from objects.review import Review
from schemas.webhook_schemas import ReviewWebhookRequest, ReviewWebhookResponse
from utils.logger import logger
from utils.task_queue import create_task

router = APIRouter(prefix="/api/v1")


@router.post("/webhooks/reviews", response_model=ReviewWebhookResponse)
async def review_webhook(request: ReviewWebhookRequest) -> ReviewWebhookResponse:
    """Ingest one new review -> reviews row + a chained review_response task
    (the producer worker/tasks/reputation.py's runner consumes). Duplicate
    deliveries no-op against the existing row."""
    brand = await Brand.get(request.brand_id)

    existing_review = await Review.get_by_external_id(brand.id, request.platform, request.external_id)
    if existing_review is not None:
        logger.info(f"[webhooks] review {request.platform}:{request.external_id} re-delivered — already ingested")
        return ReviewWebhookResponse.create({"review_id": existing_review.id, "queued_work": []})

    review = await Review.create(
        brand_id=brand.id,
        platform=request.platform,
        external_id=request.external_id,
        rating=request.rating,
        author=request.author,
        text=request.text,
        received_at=request.received_at or datetime.now(),
    )
    await create_task(brand_id=brand.id, name="review_response", queue="reputation", payload={"review_id": review.id})
    return ReviewWebhookResponse.create({"review_id": review.id, "queued_work": ["review_response"]})


# TODO(leads: orders) POST /webhooks/shopify_orders — the FIRST lead producer,
#       lands HERE: Shopify orders/create webhook -> one Lead row
#       (kind="order", value_usd from total, landing_path + channel classified
#       from the payload's landing_site/referring_site — chatgpt.com etc ->
#       ai_referral, google organic -> organic; unknown -> direct and we DON'T
#       claim it, attribution is a floor not a ceiling). Verify the webhook
#       HMAC with the site's Shopify secret. Unlocks api/reads.py's
#       attribution rollup end-to-end for Shopify brands.
# TODO(leads: calls) call-tracking webhook -> Lead(kind="call") — waits on the
#       call-tracking vendor decision (build plan section A).
# TODO(leads: forms) form-fill webhook -> Lead(kind="form") — needs the
#       tracking snippet on the client site (FE/site-side work) first.
