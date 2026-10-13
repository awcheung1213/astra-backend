"""Prompt sign-off — the ONE path that turns questions on and starts collecting.

Three callers: the owner's explicit sign-off (api/prompts.py), launch's
default (api/onboarding.py), and research landing after launch
(worker/tasks/research.py). The backend owns the default: a launched brand
with no active question gets its top DEFAULT_TRACKED_QUESTIONS tracked —
the owner's production decision ("reduce to 3 questions", ~$40/week). The
old blanket sign-off of every drafted question cost a national brand
~15 questions x ~150 markets x 3 engines (~6,800 paid queries) on the first
fill alone, and tracked nothing at all when research finished after launch."""

from datetime import datetime

from objects.prompt import Prompt
from utils.logger import logger
from utils.task_queue import create_task

DEFAULT_TRACKED_QUESTIONS = 3


async def sign_off_prompts(brand_id: str, prompt_ids: list[str]) -> int:
    """Activate the chosen prompts and queue the first fill. create_task rides
    the caller's session, so activation + job commit together (rule 8).
    Returns how many prompts were activated; nothing is queued for zero."""
    activated_count = await Prompt.activate(brand_id, prompt_ids)
    if activated_count:
        await create_task(brand_id=brand_id, name="submit_prompt_execution_batch", queue="collection")
    return activated_count


def default_tracked_prompt_ids(prompts: list) -> list[str]:
    """The questions to track by default: none when any live question is
    already active (the owner or an earlier default chose), otherwise the
    first DEFAULT_TRACKED_QUESTIONS live ones in research rank. Research
    stamps created_at in its reply order, most-asked first, so created_at
    IS the rank (ties keep input order). Pure."""
    live = [prompt for prompt in prompts if prompt.retired_at is None]
    if any(prompt.is_active for prompt in live):
        return []
    ranked = sorted(live, key=lambda prompt: prompt.created_at or datetime.max)
    return [prompt.id for prompt in ranked[:DEFAULT_TRACKED_QUESTIONS]]


async def track_default_prompts(brand_id: str, prompts: list) -> int:
    """Sign off the default questions (see default_tracked_prompt_ids) over the
    brand's prompts. Returns how many were activated (0 = nothing to do)."""
    prompt_ids = default_tracked_prompt_ids(prompts)
    if not prompt_ids:
        return 0
    activated_count = await sign_off_prompts(brand_id, prompt_ids)
    logger.info(f"[prompts] brand {brand_id}: {activated_count} default questions tracked, first fill enqueued")
    return activated_count
