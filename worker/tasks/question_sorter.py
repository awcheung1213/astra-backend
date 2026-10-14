"""The weekly question sorter — the automatic funnel's mover
(utils/question_tiers.py has the rules; this file loads and applies them).

Fanned out per launched brand by schedule_question_sorter (Sundays, an hour
after the visibility_run scoring tick). Per brand it reads up to
LOOKBACK_WEEKS of analyzed answers for the core + watching questions (light
rows; competitor mentions only where a rule needs them), the Search Console
demand halves when GSC is connected (skipped quietly otherwise), and this
month's recorded moves (the swap budget); then plan_moves decides, and every
move is written with its plain-words reason (prompts.tier_reason +
question_moves) for the questions page and the weekly recap. Watching
questions also get their pending (unconfirmed) signal stamped, which the
rotation uses to re-check them first.

Idempotent per week: a second run within SORT_MIN_GAP_DAYS of a finished one
is a no-op, so a double cron fire can't spend a second swap."""

from datetime import date, timedelta
from typing import Any

import settings
from adapters.google_adapter import GoogleAdapter, GoogleReadError
from objects.brand import Brand
from objects.connection import Connection
from objects.prompt import SOURCE_USER, TIER_CORE, TIER_WATCHING, Prompt
from objects.question_move import QuestionMove
from objects.task import Task
from utils import answer_stats, clock
from utils.logger import logger
from utils.prompt_signoff import queue_after_tracking
from utils.question_tiers import STEADY_WEEKS, CheckSummary, Move, QuestionState, brand_checks, demand_match, demotion_signal, is_quiet, month_start, pending_signal, plan_moves, promotion_signal, question_value, record_move, swaps_used
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from worker.app import app
from worker.tasks.metrics import GSC_DATA_LAG_DAYS

LOOKBACK_WEEKS = 16  # the long tail is checked about every 5-6 weeks: two checks fit with room to spare
SORT_MIN_GAP_DAYS = 6
GSC_HALF_DAYS = 14  # two consecutive 14-day halves of the 28-day window = the demand signal's "2 checks"
GSC_QUERY_LIMIT = 100


@app.task(name="sort_questions", queue="research", retry=RETRY_WITH_BACKOFF)
async def sort_questions(task_id: str, brand_id: str) -> None:
    async with task_lifecycle(task_id):
        await run_sort_questions(brand_id, task_id)


async def run_sort_questions(brand_id: str, task_id: str | None = None) -> list[Move]:
    now = clock.now()
    recent_runs = [task for task in await Task.done_since(brand_id, ["sort_questions"], since=now - timedelta(days=SORT_MIN_GAP_DAYS)) if task.id != task_id]
    if recent_runs:
        logger.info(f"[sorter] brand {brand_id}: sorted {len(recent_runs)} time(s) in the last {SORT_MIN_GAP_DAYS} days — skipped")
        return []
    brand = await Brand.get(brand_id)
    prompts = await Prompt.by_brand(brand_id)
    engines = answer_stats.display_engines(settings.ACTIVE_ENGINES)
    tracked_ids = [prompt.id for prompt in prompts if prompt.tier in (TIER_CORE, TIER_WATCHING)]
    watching_ids = [prompt.id for prompt in prompts if prompt.tier == TIER_WATCHING]
    checks = await brand_checks(brand_id, tracked_ids, now, LOOKBACK_WEEKS, engines, mention_weeks=STEADY_WEEKS, mention_prompt_ids=watching_ids)
    recent_queries, prior_queries = await gsc_demand_queries(brand)

    states, pending = question_states(prompts, checks, recent_queries, prior_queries)
    moves = plan_moves(states, now, swaps_used(await QuestionMove.since(brand_id, month_start(now))))
    for move in moves:
        await record_move(brand_id, move.prompt_id, move.from_tier, move.to_tier, move.reason_code)

    moved = {move.prompt_id for move in moves}
    current_pending = {prompt.id: prompt.pending_signal for prompt in prompts}
    await Prompt.set_pending_signals(brand_id, {prompt_id: signal for prompt_id, signal in pending.items() if prompt_id not in moved and signal != current_pending.get(prompt_id)})
    started = [move.prompt_id for move in moves if move.to_tier in (TIER_CORE, TIER_WATCHING)]
    if started:
        await queue_after_tracking(brand_id, started, core_changed=False, prompts=prompts)  # topic labels; Thursday's collection picks them up
    logger.info(f"[sorter] brand {brand_id}: {len(moves)} moves ({', '.join(f'{move.from_tier}->{move.to_tier}:{move.reason_code}' for move in moves) or 'none'})")
    return moves


def question_states(prompts: list[Any], checks_by_id: dict[str, list[CheckSummary | None]], recent_queries: list[str], prior_queries: list[str]) -> tuple[list[QuestionState], dict[str, str | None]]:
    """The sorter's per-question read (pure): QuestionState for plan_moves +
    each watching question's pending signal."""
    states: list[QuestionState] = []
    pending: dict[str, str | None] = {}
    for prompt in prompts:
        weekly = checks_by_id.get(prompt.id, [])
        checks = [check for check in weekly if check is not None]  # newest first
        demand_recent, demand_prior = demand_match(prompt.text, recent_queries), demand_match(prompt.text, prior_queries)
        state = QuestionState(
            id=prompt.id,
            tier=prompt.tier,
            pinned=bool(prompt.pinned),
            owner_written=prompt.source == SOURCE_USER,
            created_at=prompt.created_at,
            tier_changed_at=prompt.tier_changed_at,
            value=question_value(checks[0] if checks else None, demand=demand_recent),
        )
        if prompt.tier == TIER_WATCHING:
            state.promotion = promotion_signal(checks, demand_recent, demand_prior)
            state.quiet = is_quiet(checks)
            pending[prompt.id] = None if state.promotion else pending_signal(checks, demand_recent)
        elif prompt.tier == TIER_CORE:
            state.demotion = demotion_signal(weekly)
        states.append(state)
    return states, pending


async def gsc_demand_queries(brand: Brand) -> tuple[list[str], list[str]]:
    """(recent 14 days, the 14 before) of real Google queries, brand-named
    ones dropped. ([], []) when GSC isn't connected or the read fails — the
    demand rule then simply never fires."""
    gsc_connection = next((connection for connection in await Connection.connected_for_brand(brand.id) if connection.provider == "gsc"), None)
    if gsc_connection is None:
        return [], []
    today = min(clock.today(), date.today())  # sent to Google's real API: sim time must never ask for future days
    recent_end = today - timedelta(days=GSC_DATA_LAG_DAYS)
    prior_end = recent_end - timedelta(days=GSC_HALF_DAYS)
    adapter = GoogleAdapter(gsc_connection)
    try:
        recent = await adapter.search_top_queries(recent_end - timedelta(days=GSC_HALF_DAYS - 1), recent_end, limit=GSC_QUERY_LIMIT)  # endDate is inclusive
        prior = await adapter.search_top_queries(prior_end - timedelta(days=GSC_HALF_DAYS - 1), prior_end, limit=GSC_QUERY_LIMIT)
    except GoogleReadError as error:
        logger.warning(f"[sorter] brand {brand.id}: GSC query pull failed ({error}) — sorting without search demand")
        return [], []

    def unbranded(rows: list[dict]) -> list[str]:
        return [row["query"] for row in rows if brand.name.lower() not in row["query"].lower()]

    return unbranded(recent), unbranded(prior)
