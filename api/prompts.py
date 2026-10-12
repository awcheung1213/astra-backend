"""Prompt sign-off — #4's human half, and the #6 baseline's trigger.

research_prompts lands generated prompts INACTIVE; here the human activates
the ones they want measured. The first sign-off also starts the baseline:
collection is enqueued immediately (corpus-deduped, so re-running is nearly
free), and the next Sunday visibility_run scores the brand's first trend
point — #6 is the normal weekly machinery started early, not a special job.

Auth rides the router-level guard (utils/authz.py): bearer token + the
brand-in-path must belong to the caller's org."""

from fastapi import APIRouter, HTTPException, status

from objects.brand import Brand
from objects.prompt import Prompt
from schemas.prompt_schemas import SignoffRequest, SignoffResponse
from utils.logger import logger
from utils.task_queue import create_task

router = APIRouter(prefix="/api/v1")


@router.post("/brands/{brand_id}/prompts/signoff", response_model=SignoffResponse)
async def signoff_prompts(brand_id: str, request: SignoffRequest) -> SignoffResponse:
    """Activate the chosen prompts and start collecting. Safe to call again
    (activation is idempotent; a duplicate submit finds no stale cells thanks
    to the freshness + in-process guards). Unchosen prompts simply stay
    inactive — future prompt management (retire, add) is its own endpoint."""
    brand = await Brand.get(brand_id)
    if not request.prompt_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="prompt_ids is empty — sign off at least one prompt")

    activated_count = await Prompt.activate(brand.id, request.prompt_ids)
    if activated_count == 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no matching prompts for this brand")

    # #6 — the engine-of-day batch collects the new active cells; Sunday's
    # visibility_run turns them into the baseline trend point. create_task
    # rides the request's session: activation + job commit together
    await create_task(brand_id=brand.id, name="submit_prompt_execution_batch", queue="collection")
    logger.info(f"[prompts] brand {brand.id}: {activated_count} prompts activated, first fill enqueued")
    return SignoffResponse.create({"brand_id": brand.id, "activated_count": activated_count, "collection_started": True})
