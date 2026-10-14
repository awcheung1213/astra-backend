"""The Monday weekly recap card: what we did for you in the last 7 days,
shown once per ISO week per user until dismissed.

  GET  /brands/{id}/recap        the numbers + should_show
  POST /brands/{id}/recap/seen   dismiss it for this ISO week (this user)

The numbers and the show-once rule are pure (utils/owner_calendar.py); this
router only fetches. Auth rides the router-level guard (utils/authz.py:
brand_id -> its org); "seen" is per signed-in user (users.recap_seen_week)."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request

from objects.artifact import STATUS_APPROVED, Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.prompt import TIER_CORE, Prompt
from objects.question_move import QuestionMove
from objects.task import STATUS_DONE, Task
from objects.user import User
from utils import answer_stats, clock, owner_calendar
from utils.authz import current_user

router = APIRouter(prefix="/api/v1")

CHANGE_SET_SCAN_LIMIT = 200  # newest-first; a week of changes fits well inside


async def questions_named_delta(brand_id: str) -> int | None:
    """How many more (or fewer) CORE questions AI assistants name the brand in
    this week vs last week (the AI answers screen's "up 2"), counting only
    moves past the noise gate (utils/answer_stats.py — the same read as the
    questions endpoint). None when there's no previous week — the FE hides it."""
    _, summary = await answer_stats.question_answer_stats(brand_id, await Prompt.by_brand(brand_id), with_gap_competitor=False)
    return named_delta(summary)


def named_delta(summary: dict) -> int | None:
    """The noise-gated up minus down; None without a previous week. Pure."""
    return summary.get("named_delta")


async def question_moves(brand_id: str, since: datetime) -> list[dict]:
    """The question moves since `since` that touch the weekly checks (into or
    out of core), oldest first — long-tail bookkeeping stays out of the recap."""
    moves = await QuestionMove.since(brand_id, since)
    topics = {prompt.id: prompt.topic for prompt in await Prompt.by_brand(brand_id)}
    return recap_moves(moves, topics)


def recap_moves(moves: list, topics: dict[str, str | None]) -> list[dict]:
    """[{prompt_id, topic, from, to, reason}] for the AUTOMATIC moves into or out
    of core — what we changed for him. His own picks, stops and edits (and the
    room they made) aren't news to him, so they stay out. Pure."""
    return [{"prompt_id": move.prompt_id, "topic": topics.get(move.prompt_id), "from": move.from_tier, "to": move.to_tier, "reason": move.reason} for move in moves if TIER_CORE in (move.from_tier, move.to_tier) and getattr(move, "automatic", True)]


@router.get("/brands/{brand_id}/recap")
async def recap(brand_id: str, request: Request) -> dict:
    """{week_of, fixes_live, emails_sent, work_days, questions_named_delta,
    question_moves, should_show} — question_moves: the last 7 days' moves into
    or out of the weekly checks, each with its plain-words reason."""
    await Brand.get(brand_id)
    done_tasks = await Task.for_brand(brand_id, statuses=[STATUS_DONE])
    change_sets = await ChangeSet.for_brand(brand_id, limit=CHANGE_SET_SCAN_LIMIT)
    pitches = await Artifact.for_brand(brand_id, type="pitch", statuses=[STATUS_APPROVED])
    payload = owner_calendar.recap(
        now=clock.now(),
        done_tasks=done_tasks,
        change_sets=change_sets,
        pitches=pitches,
        seen_week=current_user(request).recap_seen_week,
        recap_cron=owner_calendar.registered_crons().get(owner_calendar.CRON_RECAP),
        questions_named_delta=await questions_named_delta(brand_id),
    )
    payload["question_moves"] = await question_moves(brand_id, clock.now() - timedelta(days=owner_calendar.RECAP_WINDOW_DAYS))
    return payload


@router.post("/brands/{brand_id}/recap/seen")
async def recap_seen(brand_id: str, request: Request) -> dict:
    """Dismiss this ISO week's recap for the signed-in user."""
    await Brand.get(brand_id)
    week_of = owner_calendar.week_monday(clock.today())
    await User.mark_recap_seen(current_user(request).id, week_of)
    return {"week_of": week_of.isoformat(), "seen": True}
