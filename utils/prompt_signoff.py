"""Question levels' write paths outside the weekly sorter — the ONE place a
question gets a level from a person or from launch.

  launch default       track_default_prompts: a launched brand with no
                       leveled question gets the top CORE_SLOTS (5) by
                       research rank as core + the next ones as watching
                       (utils/question_tiers.initial_tiers). Callers: launch
                       (api/onboarding.py) and research landing after launch
                       (worker/tasks/research.py).
  owner overrides      owner_update_question / owner_add_question /
                       owner_edit_question (api/prompts.py): pin a level,
                       stop one, add or reword a question.
  legacy sign-off      sign_off_prompts (POST /prompts/signoff): the chosen
                       questions become pinned core (overflow -> watching).

Each queues the first fill (submit_prompt_execution_batch via create_task in
the caller's session: level + job commit together, rule 8) when a question
became core, and topic labeling when a now-collected question has no topic.
The old "track 3 questions" default (DEFAULT_TRACKED_QUESTIONS) is replaced
by the funnel's CORE_SLOTS core + watching rotation."""

from typing import Any

import settings
from objects.competitor import Competitor
from objects.prompt import SOURCE_USER, TIER_CORE, TIER_SUGGESTED, TIER_WATCHING, Prompt
from objects.question_move import QuestionMove
from schemas.base_schemas import utc_iso
from utils import answer_stats, clock
from utils.logger import logger
from utils.query_normalization import query_hash
from utils.question_tiers import (
    CORE_SLOTS,
    REASON_MADE_ROOM_FOR_OWNER,
    REASON_OWNER_ADDED,
    REASON_OWNER_EDITED,
    REASON_OWNER_MOVED,
    REASON_OWNER_STOPPED,
    REASONS,
    QuestionTextError,
    assign_initial_tiers,
    clean_owner_question,
    core_values,
    initial_tiers,
    owner_tier_change,
    rank_key,
    record_move,
    replacement_for_core,
)
from utils.task_queue import create_task

OWNER_CATEGORY = "Your questions"  # topic_group for owner-written questions


class QuestionNotFound(LookupError):
    pass


class CoreFull(Exception):
    """Every weekly spot holds a question the owner picked (the API's 409)."""

    message = "All 5 weekly spots hold questions you picked. Move one to monthly checks first."


async def queue_after_tracking(brand_id: str, prompt_ids: list[str], core_changed: bool, prompts: list | None = None) -> None:
    """The follow-ups when questions start being collected: the first fill
    when core changed, topic labeling when any of them has no topic."""
    if core_changed:
        await create_task(brand_id=brand_id, name="submit_prompt_execution_batch", queue="collection")
    if prompt_ids and questions_without_topic(prompts if prompts is not None else await Prompt.by_brand(brand_id), prompt_ids):
        await create_task(brand_id=brand_id, name="backfill_question_topics", queue="research")


def questions_without_topic(prompts: list, prompt_ids: list[str]) -> bool:
    """Any of the just-tracked questions still has no topic. Pure."""
    chosen = set(prompt_ids)
    return any(prompt.id in chosen and not getattr(prompt, "topic", None) for prompt in prompts)


##############################
# LAUNCH DEFAULT
##############################


def default_tracked_prompt_ids(prompts: list) -> list[str]:
    """The questions launch levels by default (core first, then watching, in
    research rank); [] when any live question already has a level. Pure."""
    return list(initial_tiers(prompts))


async def track_default_prompts(brand_id: str, prompts: list) -> int:
    """Give a launched brand its starting levels (see initial_tiers) and queue
    the first fill. Returns how many questions got a level (0 = nothing to do)."""
    tiers = await assign_initial_tiers(brand_id, prompts)
    if not tiers:
        return 0
    await queue_after_tracking(brand_id, list(tiers), core_changed=TIER_CORE in tiers.values(), prompts=prompts)
    core_count = sum(1 for tier in tiers.values() if tier == TIER_CORE)
    logger.info(f"[prompts] brand {brand_id}: {core_count} core + {len(tiers) - core_count} watching questions by default, first fill enqueued")
    return len(tiers)


##############################
# LEGACY SIGN-OFF
##############################


async def sign_off_prompts(brand_id: str, prompt_ids: list[str]) -> int:
    """The owner's explicit picks become pinned core questions — as many as
    core has room for after the questions not picked; the rest pinned
    watching. Returns how many were leveled (0 = none of them this brand's)."""
    prompts = await Prompt.by_brand(brand_id)
    by_id = {prompt.id: prompt for prompt in prompts}
    chosen = sorted((by_id[prompt_id] for prompt_id in dict.fromkeys(prompt_ids) if prompt_id in by_id), key=rank_key)
    if not chosen:
        return 0
    chosen_ids = {prompt.id for prompt in chosen}
    room = max(0, CORE_SLOTS - sum(1 for prompt in prompts if prompt.tier == TIER_CORE and prompt.id not in chosen_ids))
    for index, prompt in enumerate(chosen):
        tier = TIER_CORE if index < room else TIER_WATCHING
        if prompt.tier != tier:
            await record_move(brand_id, prompt.id, prompt.tier, tier, REASON_OWNER_MOVED, automatic=False, pinned=True)
        elif not prompt.pinned:
            await Prompt.set_pinned(brand_id, prompt.id, True)
    await queue_after_tracking(brand_id, [prompt.id for prompt in chosen], core_changed=room > 0, prompts=prompts)
    return len(chosen)


##############################
# OWNER OVERRIDES
##############################


def question_row(prompt: Any) -> dict:
    """The row the owner endpoints return (the questions endpoint's level keys)."""
    return {
        "id": prompt.id,
        "text": prompt.text,
        "topic": prompt.topic,
        "category": prompt.category,
        "topic_group": answer_stats.topic_group(prompt),
        "source": prompt.source,
        "is_active": prompt.is_active,
        "tier": prompt.tier,
        "pinned": prompt.pinned,
        "tier_reason": prompt.tier_reason,
        "tier_changed_at": utc_iso(prompt.tier_changed_at),
        "last_checked_at": utc_iso(prompt.last_checked_at),
    }


async def _make_room_in_core(brand_id: str, prompts: list, exclude: str | None) -> Any | None:
    """Demote the lowest-value movable core question when core is full;
    None when there was room. Raises CoreFull when every core question is
    the owner's."""
    core_others = [prompt for prompt in prompts if prompt.tier == TIER_CORE and prompt.id != exclude]
    values = await core_values(brand_id, core_others, clock.now(), answer_stats.display_engines(settings.ACTIVE_ENGINES)) if len(core_others) >= CORE_SLOTS else {}
    try:
        replaced = replacement_for_core(prompts, values, exclude=exclude)
    except LookupError:
        raise CoreFull()
    if replaced is not None:
        await record_move(brand_id, replaced.id, TIER_CORE, TIER_WATCHING, REASON_MADE_ROOM_FOR_OWNER, automatic=False)
    return replaced


async def owner_update_question(brand_id: str, prompt_id: str, tier: str | None, pinned: bool | None) -> tuple[Any, Any | None]:
    """PATCH: (the updated question, the core question that made room or None).
    core/watching pins; suggested stops (pinned only if sent — the app sends
    pinned=true so the funnel never picks it back up). Putting one into a full
    core bumps the lowest-value unpinned core question; CoreFull when all
    five are the owner's. QuestionNotFound for another brand's / retired."""
    prompt = await Prompt.get_live(brand_id, prompt_id)
    if prompt is None:
        raise QuestionNotFound()
    old_tier, old_pinned = prompt.tier, prompt.pinned  # the ORM row refreshes in place below
    new_tier, new_pinned = owner_tier_change(old_tier, old_pinned, tier, pinned)
    replaced = None
    if new_tier == TIER_CORE and old_tier != TIER_CORE:
        replaced = await _make_room_in_core(brand_id, await Prompt.by_brand(brand_id), exclude=prompt.id)
    if new_tier != old_tier:
        await record_move(brand_id, prompt.id, old_tier, new_tier, REASON_OWNER_STOPPED if new_tier == TIER_SUGGESTED else REASON_OWNER_MOVED, automatic=False, pinned=new_pinned)
    elif new_pinned != old_pinned:
        await Prompt.set_pinned(brand_id, prompt.id, new_pinned)
    updated = await Prompt.get_live(brand_id, prompt_id)
    if new_tier in (TIER_CORE, TIER_WATCHING) and new_tier != old_tier:
        await queue_after_tracking(brand_id, [prompt.id], core_changed=new_tier == TIER_CORE, prompts=[updated])
    return updated, replaced


async def _business_names(brand: Any) -> list[str]:
    return [brand.name] + [competitor.name for competitor in await Competitor.get_competitors_by_brand_id(brand.id)]


async def owner_add_question(brand: Any, text: str) -> tuple[Any, Any | None]:
    """POST: (the question, the core question that made room or None). The
    text is validated and stripped of business names (QuestionTextError); it
    lands pinned in core — or pinned watching when all five weekly spots are
    the owner's. A question the brand already has (same normalized text) is
    reused and pinned to core instead of duplicated."""
    cleaned = clean_owner_question(text, await _business_names(brand))
    prompts = await Prompt.by_brand(brand.id)
    existing = next((prompt for prompt in prompts if query_hash(prompt.text) == query_hash(cleaned)), None)
    if existing is not None and existing.tier == TIER_CORE:
        if not existing.pinned:
            await Prompt.set_pinned(brand.id, existing.id, True)
        return await Prompt.get_live(brand.id, existing.id), None
    try:
        replaced = await _make_room_in_core(brand.id, prompts, exclude=existing.id if existing else None)
        tier = TIER_CORE
    except CoreFull:
        replaced, tier = None, TIER_WATCHING
    if existing is not None:
        await record_move(brand.id, existing.id, existing.tier, tier, REASON_OWNER_ADDED, automatic=False, pinned=True)
        question = await Prompt.get_live(brand.id, existing.id) or existing
    else:
        question = await Prompt.create(brand_id=brand.id, text=cleaned, category=OWNER_CATEGORY, source=SOURCE_USER, tier=tier, pinned=True, tier_reason=REASONS[REASON_OWNER_ADDED])
        await QuestionMove.create(brand_id=brand.id, prompt_id=question.id, from_tier=TIER_SUGGESTED, to_tier=tier, reason_code=REASON_OWNER_ADDED, reason=REASONS[REASON_OWNER_ADDED], automatic=False)
    await queue_after_tracking(brand.id, [question.id], core_changed=tier == TIER_CORE, prompts=[question])
    logger.info(f"[prompts] brand {brand.id}: owner added a {tier} question{' (made room: ' + replaced.id + ')' if replaced else ''}")
    return question, replaced


async def owner_edit_question(brand: Any, prompt_id: str, text: str) -> Any:
    """POST .../edit: a NEW pinned question with the old one's level; the old
    one is retired (its history stays). Same text = nothing changes (the old
    question comes back). QuestionTextError when it duplicates another question."""
    old = await Prompt.get_live(brand.id, prompt_id)
    if old is None:
        raise QuestionNotFound()
    cleaned = clean_owner_question(text, await _business_names(brand))
    if query_hash(cleaned) == query_hash(old.text):
        return old
    if any(prompt.id != old.id and query_hash(prompt.text) == query_hash(cleaned) for prompt in await Prompt.by_brand(brand.id)):
        raise QuestionTextError("You already have that question.")
    tier, category = old.tier, old.category
    await Prompt.retire(brand.id, old.id)
    question = await Prompt.create(brand_id=brand.id, text=cleaned, category=category, source=SOURCE_USER, tier=tier, pinned=True, tier_reason=REASONS[REASON_OWNER_EDITED])
    await QuestionMove.create(brand_id=brand.id, prompt_id=question.id, from_tier=TIER_SUGGESTED, to_tier=tier, reason_code=REASON_OWNER_EDITED, reason=REASONS[REASON_OWNER_EDITED], automatic=False)
    if tier in (TIER_CORE, TIER_WATCHING):
        await queue_after_tracking(brand.id, [question.id], core_changed=tier == TIER_CORE, prompts=[question])
    return question
