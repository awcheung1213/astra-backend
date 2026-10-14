"""The Monday weekly recap card: what we did for you in the last 7 days,
shown once per ISO week per user until dismissed.

  GET  /brands/{id}/recap        the numbers + should_show
  POST /brands/{id}/recap/seen   dismiss it for this ISO week (this user)

The numbers and the show-once rule are pure (utils/owner_calendar.py); this
router only fetches. Auth rides the router-level guard (utils/authz.py:
brand_id -> its org); "seen" is per signed-in user (users.recap_seen_week)."""

from fastapi import APIRouter, Request

import settings
from objects.artifact import STATUS_APPROVED, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.prompt import Prompt
from objects.task import STATUS_DONE, Task
from objects.user import User
from utils import answer_stats, clock, owner_calendar
from utils.authz import current_user

router = APIRouter(prefix="/api/v1")

CHANGE_SET_SCAN_LIMIT = 200  # newest-first; a week of changes fits well inside


async def questions_named_delta(brand_id: str) -> int | None:
    """How many more (or fewer) tracked questions AI assistants named the
    brand in this week vs last week (the AI answers screen's "up 2"). None
    when there's no previous week to compare — the FE hides the line."""
    prompts = await Prompt.by_brand(brand_id)
    engines = answer_stats.display_engines(settings.ACTIVE_ENGINES)
    current = await answer_stats.week_results(brand_id, weeks_back=0)
    previous = await answer_stats.week_results(brand_id, weeks_back=1)
    _, summary = answer_stats.questions_stats(prompts, current, previous, {}, engines)
    return named_delta(summary)


def named_delta(summary: dict) -> int | None:
    """this week's questions_named minus last week's; None without a previous week. Pure."""
    if summary.get("questions_named_prev") is None:
        return None
    return summary["questions_named"] - summary["questions_named_prev"]


@router.get("/brands/{brand_id}/recap")
async def recap(brand_id: str, request: Request) -> dict:
    """{week_of, fixes_live, emails_sent, work_days, questions_named_delta, should_show}."""
    await Brand.get(brand_id)
    done_tasks = await Task.for_brand(brand_id, statuses=[STATUS_DONE])
    change_sets = await ChangeSet.for_brand(brand_id, limit=CHANGE_SET_SCAN_LIMIT)
    pitches = await Artifact.for_brand(brand_id, type="pitch", statuses=[STATUS_APPROVED])
    return owner_calendar.recap(
        now=clock.now(),
        done_tasks=done_tasks,
        change_sets=change_sets,
        pitches=pitches,
        seen_week=current_user(request).recap_seen_week,
        recap_cron=owner_calendar.registered_crons().get(owner_calendar.CRON_RECAP),
        questions_named_delta=await questions_named_delta(brand_id),
    )


@router.post("/brands/{brand_id}/recap/seen")
async def recap_seen(brand_id: str, request: Request) -> dict:
    """Dismiss this ISO week's recap for the signed-in user."""
    await Brand.get(brand_id)
    week_of = owner_calendar.week_monday(clock.today())
    await User.mark_recap_seen(current_user(request).id, week_of)
    return {"week_of": week_of.isoformat(), "seen": True}
