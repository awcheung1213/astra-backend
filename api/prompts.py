"""Question levels — the owner's one-question overrides on top of the
automatic funnel (utils/question_tiers.py), plus the legacy sign-off.

research_prompts lands generated questions as `suggested`; launch (or
research landing after it) levels them by default — the top 5 core, the next
ones watching — and the weekly sorter moves them from there. The owner never
has to sign anything off; these endpoints let him override one question:

  PATCH /brands/{id}/questions/{prompt_id}       {tier?, pinned?} -> the row (+ replaced)
  POST  /brands/{id}/questions                   {text} -> {question, replaced}
  POST  /brands/{id}/questions/{prompt_id}/edit  {text} -> {question} (a NEW id; the old one retires)
  POST  /brands/{id}/prompts/signoff             legacy: chosen -> pinned core

Errors are plain words for the owner: 422 (bad text / nothing to change),
404 (not this brand's question), 409 (all 5 weekly spots are his own picks).
A question that becomes core queues the first fill (corpus-deduped, so a
re-run is nearly free); Sunday's visibility_run scores it.

Auth rides the router-level guard (utils/authz.py): bearer token + the
brand-in-path must belong to the caller's org."""

from fastapi import APIRouter, HTTPException, status

from objects.brand import Brand
from objects.prompt import TIERS
from schemas.prompt_schemas import QuestionAddResponse, QuestionEditResponse, QuestionTextRequest, QuestionUpdateRequest, QuestionUpdateResponse, SignoffRequest, SignoffResponse
from utils.logger import logger
from utils.prompt_signoff import CoreFull, QuestionNotFound, owner_add_question, owner_edit_question, owner_update_question, question_row, sign_off_prompts
from utils.question_tiers import QuestionTextError

router = APIRouter(prefix="/api/v1")


@router.post("/brands/{brand_id}/prompts/signoff", response_model=SignoffResponse)
async def signoff_prompts(brand_id: str, request: SignoffRequest) -> SignoffResponse:
    """Legacy: the chosen questions become pinned core (overflow pinned
    watching) and collection starts. Safe to call again (a duplicate submit
    finds no stale cells thanks to the freshness + in-process guards)."""
    brand = await Brand.get(brand_id)
    if not request.prompt_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="prompt_ids is empty — sign off at least one prompt")

    # #6 — the engine-of-day batch collects the new active cells; Sunday's
    # visibility_run turns them into the baseline trend point. The shared
    # sign-off path (also launch's + research's default) queues it via
    # create_task in the request's session: activation + job commit together
    activated_count = await sign_off_prompts(brand.id, request.prompt_ids)
    if activated_count == 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no matching prompts for this brand")
    logger.info(f"[prompts] brand {brand.id}: {activated_count} questions signed off, first fill enqueued")
    return SignoffResponse.create({"brand_id": brand.id, "activated_count": activated_count, "collection_started": True})


def _replaced(prompt) -> dict | None:
    return {"id": prompt.id, "topic": prompt.topic} if prompt is not None else None


@router.patch("/brands/{brand_id}/questions/{prompt_id}", response_model=QuestionUpdateResponse)
async def update_question(brand_id: str, prompt_id: str, request: QuestionUpdateRequest) -> QuestionUpdateResponse:
    """Pin a question's level (core = weekly, watching = monthly — both pin
    it) or stop it (suggested; send pinned=true to keep it stopped for good).
    A move into a full core bumps the lowest-value unpinned core question to
    monthly (returned as `replaced`); 409 when all five are the owner's."""
    brand = await Brand.get(brand_id)
    if request.tier is None and request.pinned is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to change.")
    if request.tier is not None and request.tier not in TIERS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Pick weekly, monthly, or stop checking it.")
    try:
        question, replaced = await owner_update_question(brand.id, prompt_id, request.tier, request.pinned)
    except QuestionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that question.")
    except CoreFull as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message)
    return QuestionUpdateResponse.create({**question_row(question), "replaced": _replaced(replaced)})


@router.post("/brands/{brand_id}/questions", response_model=QuestionAddResponse)
async def add_question(brand_id: str, request: QuestionTextRequest) -> QuestionAddResponse:
    """The owner's own question: business names taken out, a topic written
    in the background, added pinned to the weekly checks. A full core moves
    its lowest-value unpinned question to monthly (`replaced`); when all five
    are the owner's, the new one is checked monthly instead."""
    brand = await Brand.get(brand_id)
    try:
        question, replaced = await owner_add_question(brand, request.text)
    except QuestionTextError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))
    return QuestionAddResponse.create({"question": question_row(question), "replaced": _replaced(replaced)})


@router.post("/brands/{brand_id}/questions/{prompt_id}/edit", response_model=QuestionEditResponse)
async def edit_question(brand_id: str, prompt_id: str, request: QuestionTextRequest) -> QuestionEditResponse:
    """Reword a question: a NEW pinned question at the same level; the old
    one is retired (its answers stay readable)."""
    brand = await Brand.get(brand_id)
    try:
        question = await owner_edit_question(brand, prompt_id, request.text)
    except QuestionNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="We couldn't find that question.")
    except QuestionTextError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))
    return QuestionEditResponse.create({"question": question_row(question)})
