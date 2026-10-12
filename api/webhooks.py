"""Event entry points from the outside world. First tenant: new reviews —
fast reply is the point, so a review becomes a review_response task the
moment it arrives instead of waiting for any sweep.

Webhook endpoints must be idempotent: platforms re-deliver. The review's
(brand, platform, external_id) identity dedupes — a duplicate delivery finds
its existing row and queues nothing.

Deliberately registered WITHOUT the bearer guard — platforms call this, not
signed-in users. The HMAC IS the auth (evergreen-onboarding-gaps-plan.md §8):
`X-Evergreen-Signature` must carry hex HMAC-SHA256(WEBHOOK_SECRET, raw request
body), either bare (`<64 hex>`) or prefixed (`sha256=<64 hex>`), hex case-
insensitive. It is checked over the RAW bytes, in constant time, BEFORE any JSON
parsing or DB access. Missing, malformed or wrong -> 401. WEBHOOK_SECRET unset
-> every delivery 401s (fails closed; production requires it)."""

import hashlib
import hmac
import re

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

import settings
from objects.brand import Brand
from objects.review import Review
from schemas.webhook_schemas import ReviewWebhookRequest, ReviewWebhookResponse
from utils import clock
from utils.logger import logger
from utils.task_queue import create_task

router = APIRouter(prefix="/api/v1")

SIGNATURE_HEADER = "X-Evergreen-Signature"
SIGNATURE_PATTERN = re.compile(r"^(?:sha256=)?([0-9a-fA-F]{64})$")


def webhook_signature(body: bytes, secret: str) -> str:
    """The hex digest a sender puts in X-Evergreen-Signature."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header_value: str | None) -> bool:
    secret = settings.WEBHOOK_SECRET
    if not secret or not header_value:
        return False
    match = SIGNATURE_PATTERN.match(header_value.strip())
    if match is None:
        return False
    return hmac.compare_digest(webhook_signature(body, secret), match.group(1).lower())


@router.post(
    "/webhooks/reviews",
    response_model=ReviewWebhookResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": ReviewWebhookRequest.model_json_schema()}}}},
)
async def review_webhook(http_request: Request) -> ReviewWebhookResponse:
    """Ingest one new review -> reviews row + a chained review_response task
    (the producer worker/tasks/reputation.py's runner consumes). Duplicate
    deliveries no-op against the existing row. The body is read raw and
    signature-checked before it is parsed."""
    raw_body = await http_request.body()
    if not verify_signature(raw_body, http_request.headers.get(SIGNATURE_HEADER)):
        logger.info("[webhooks] review delivery rejected — missing or invalid signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook signature")
    try:
        request = ReviewWebhookRequest.model_validate_json(raw_body)
    except ValidationError as error:
        raise RequestValidationError(error.errors(include_url=False))

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
        received_at=request.received_at or clock.now(),
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
