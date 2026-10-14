"""The AI answers screens' aggregation: per tracked question x assistant, does
that assistant NAME the brand this week?

Owner-approved rule (2026-10-14): per question x engine take the latest
ANALYZED result per market in the scoring week; share = markets that named
the brand / markets asked; rank = the average rank over the results that
named it. A local brand asks in a handful of markets, a national brand in
~150 — the share handles both.

DEAD ZONE (owner-approved 2026-10-14, replaces pooling — results show the
same week): "named" is sticky against the PREVIOUS week's state —
  not named last week -> named once share >= NAMED_ENTER (0.55)
  named last week     -> stays named until share < NAMED_EXIT (0.45)
  no previous week    -> named at share >= NAMED_SHARE_THRESHOLD (0.5)
"Previous week" is the previous CHECK: the state chains oldest -> newest over
the HISTORY_WEEKS loaded (a week with no answers carries the last state on —
the long tail is checked about monthly). The chain starts with the plain 0.5
rule at its oldest week, so a state older than the window is forgotten.

NOISE GATE for "up/down": a question counts as up (down) only when it went
from named on no assistant to named on at least one (or back) AND on an
assistant that flipped the same way the share moved more than binomial noise
for that many cities:
    |share_now - share_before| > NOISE_Z (1.5) x sqrt(p (1 - p) / n)
with p = the two weeks' pooled share (named cities / asked cities) and n =
the cities asked this week. With p = 0 or 1 any change counts. One local city
(n = 1, a 0 -> 1 flip): pooled p = 0.5, noise 0.75 < 1 -> counts.

The questions endpoint, the one-question answers endpoint and the weekly
recap all read these functions, so they agree. The tiles/summary count CORE
questions only (utils/question_tiers.py); the long tail ("watching") gets its
own summary block, read at each question's latest check.

The week is the questions endpoint's existing "last week" window (the 7-day
horizon PromptResult.filter reads); week k back is the 7 days before that.
Everything below the loaders is pure (tests/test_answer_stats.py). Reads only:
corpus rows (query_responses) are looked up, never written."""

import math
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select

import settings
from objects.brand import FOOTPRINT_LOCAL, Brand
from objects.brand_market import BrandMarket
from objects.competitor import Competitor
from objects.competitor_mention import CompetitorMention
from objects.market import SOURCE_NATIONAL, Market
from objects.prompt import TIER_CORE, TIER_SUGGESTED, TIER_WATCHING
from objects.prompt_result import PromptResult
from objects.query_response import ENGINE_CHATGPT, ENGINE_CLAUDE, ENGINE_GEMINI, QueryResponse
from schemas.base_schemas import utc_iso
from utils import clock
from utils.city_panel import reask_cities
from utils.database import ReplicaDatabase

NAMED_SHARE_THRESHOLD = 0.5  # no previous week to compare against
NAMED_ENTER = 0.55  # not named last week -> named from here
NAMED_EXIT = 0.45  # named last week -> stays named until below this
NOISE_Z = 1.5  # the up/down gate: |delta share| > NOISE_Z x binomial standard error
HISTORY_WEEKS = 8  # weeks the questions endpoint reads: the dead-zone chain + the long tail's latest check
WEEK = timedelta(days=7)  # = PromptResult.filter's horizon (the questions page's "last week")
DISPLAY_ENGINE_ORDER = (ENGINE_CHATGPT, ENGINE_GEMINI, ENGINE_CLAUDE)  # the rings' outer -> inner order
ALSO_NAMED_LIMIT = 3
SAMPLE_TEXT_LIMIT = 1500
NATIONWIDE_LABEL = "Nationwide"
OTHER_TOPIC_GROUP = "Other"
MENTION_ID_CHUNK = 10000  # result ids per competitor-mention IN query (asyncpg caps bind params at 32767)


##############################
# LOADERS (DB reads)
##############################


async def counted_market_ids(brand_id: str) -> set[str]:
    """The cities a brand's numbers count: its CURRENT collection markets
    (national: the panel + nationwide; local: its home cities) plus, for
    national brands, the borderline re-ask cities. Answers from cities that
    were switched off (the pre-panel 151-city fills) stay stored but no longer
    count — "named in X of N cities" always means the cities we ask now."""
    brand = await Brand.get(brand_id)
    ids = {market.id for market in await Market.by_brand(brand_id=brand.id, brand_footprint=brand.footprint)}
    if brand.footprint != FOOTPRINT_LOCAL:
        ids |= {market.id for market in reask_cities(await Market.reask_pool(), settings.REASK_EXTRA_CITIES)}
    return ids


async def latest_analyzed_results(brand_id: str, since: datetime, until: datetime | None = None, prompt_id: str | None = None) -> list[PromptResult]:
    """Newest ANALYZED result per (prompt, market, engine) created in (since, until] —
    PromptResult.filter's DISTINCT ON statement with an explicit window, so the
    previous week reads the same way as this one."""
    statement = (
        select(PromptResult)
        .distinct(PromptResult.prompt_id, PromptResult.market_id, PromptResult.engine)  # type: ignore
        .where(PromptResult.brand_id == brand_id)  # type: ignore
        .where(PromptResult.analyzed_at.isnot(None))  # type: ignore
        .where(PromptResult.created_at > since)  # type: ignore
        .order_by(PromptResult.prompt_id, PromptResult.market_id, PromptResult.engine, PromptResult.created_at.desc())  # type: ignore
    )
    if until is not None:
        statement = statement.where(PromptResult.created_at <= until)  # type: ignore
    if prompt_id is not None:
        statement = statement.where(PromptResult.prompt_id == prompt_id)  # type: ignore
    statement = statement.where(PromptResult.market_id.in_(list(await counted_market_ids(brand_id))))  # type: ignore
    rows = await ReplicaDatabase.session.execute(statement)  # type: ignore
    return list(rows.scalars().all())


async def week_results(brand_id: str, weeks_back: int = 0, prompt_id: str | None = None) -> list[PromptResult]:
    since, until = week_window(clock.now(), weeks_back)
    return await latest_analyzed_results(brand_id, since, until, prompt_id=prompt_id)


async def analyzed_rows(brand_id: str, since: datetime, until: datetime | None = None, prompt_ids: Iterable[str] | None = None) -> list[Any]:
    """Every ANALYZED result created in (since, until], LIGHT columns only (no
    answer text) — the multi-week reads (dead-zone chain, long tail, sorter)
    bucket these with weekly_buckets; engine_stats keeps the newest per market."""
    statement = (
        select(PromptResult.id, PromptResult.prompt_id, PromptResult.market_id, PromptResult.engine, PromptResult.brand_mentioned, PromptResult.rank, PromptResult.created_at, PromptResult.query_response_id)  # type: ignore
        .where(PromptResult.brand_id == brand_id)  # type: ignore
        .where(PromptResult.analyzed_at.isnot(None))  # type: ignore
        .where(PromptResult.created_at > since)  # type: ignore
    )
    if until is not None:
        statement = statement.where(PromptResult.created_at <= until)  # type: ignore
    if prompt_ids is not None:
        wanted = list(prompt_ids)
        if not wanted:
            return []
        statement = statement.where(PromptResult.prompt_id.in_(wanted))  # type: ignore
    statement = statement.where(PromptResult.market_id.in_(list(await counted_market_ids(brand_id))))  # type: ignore
    rows = await ReplicaDatabase.session.execute(statement)  # type: ignore
    return list(rows.all())


async def mentions_by_result(brand_id: str, results: list[Any]) -> dict[str, list[CompetitorMention]]:
    """Competitor mentions per result id, minus names the brand has since
    excluded (the matcher skips them going forward; old rows still hold them).
    Chunked so a multi-week read never exceeds the bind-parameter cap."""
    excluded = await Competitor.excluded_names(brand_id)
    result_ids = [result.id for result in results]
    grouped: dict[str, list[CompetitorMention]] = {}
    for start in range(0, len(result_ids), MENTION_ID_CHUNK):
        for mention in await CompetitorMention.by_prompt_result_ids(result_ids[start:][:MENTION_ID_CHUNK]):
            if mention.normalized_name in excluded:
                continue
            grouped.setdefault(mention.prompt_result_id, []).append(mention)
    return grouped


async def market_preference(brand_id: str, market_ids: Iterable[str]) -> tuple[list[str], dict[str, Market]]:
    """(market ids in sample-preference order, markets by id) for the given ids:
    the brand's own subscriptions first in subscription order (home base first),
    then by population (largest first), then by label."""
    wanted = set(market_ids)
    if not wanted:
        return [], {}
    market_rows = await ReplicaDatabase.session.execute(select(Market).where(Market.id.in_(wanted)))  # type: ignore
    markets = {market.id: market for market in market_rows.scalars().all()}
    subscription_rows = await ReplicaDatabase.session.execute(select(BrandMarket.market_id).where(BrandMarket.brand_id == brand_id).order_by(BrandMarket.created_at, BrandMarket.id))  # type: ignore
    subscribed = [market_id for market_id in subscription_rows.scalars().all() if market_id in wanted]
    rest = sorted(wanted - set(subscribed), key=lambda market_id: (-(markets[market_id].population or 0) if market_id in markets else 0, market_label(markets.get(market_id)) or market_id))
    return subscribed + rest, markets


async def corpus_answer(query_response_id: str) -> QueryResponse | None:
    return await ReplicaDatabase.session.get(QueryResponse, query_response_id)  # type: ignore


##############################
# PURE AGGREGATION
##############################


def display_engines(active_engines: Iterable[str]) -> list[str]:
    """ACTIVE_ENGINES in display order (chatgpt, gemini, claude), any other
    active engine after them."""
    active = list(dict.fromkeys(active_engines))
    return [engine for engine in DISPLAY_ENGINE_ORDER if engine in active] + [engine for engine in active if engine not in DISPLAY_ENGINE_ORDER]


def week_window(now: datetime, weeks_back: int = 0) -> tuple[datetime, datetime]:
    """(since, until] of the week `weeks_back` windows before the current one."""
    until = now - WEEK * weeks_back
    return until - WEEK, until


def week_of(results: Iterable[Any]) -> date | None:
    """Monday of the newest result's week — the same axis visibility_runs use."""
    newest = max((result.created_at for result in results if result.created_at is not None), default=None)
    return None if newest is None else newest.date() - timedelta(days=newest.weekday())


def latest_per_market(results: Iterable[Any]) -> list[Any]:
    """One result per market — the newest (the loader already dedupes; this
    keeps the rule true for any caller)."""
    latest: dict[str, Any] = {}
    for result in results:
        current = latest.get(result.market_id)
        if current is None or (result.created_at or datetime.min) > (current.created_at or datetime.min):
            latest[result.market_id] = result
    return list(latest.values())


def not_asked() -> dict:
    return {"named": None, "share": None, "rank": None, "cities": 0, "cities_named": 0}


def named_state(share: float, prev_named: bool | None) -> bool:
    """The dead zone: sticky against the previous week's state (module doc)."""
    if prev_named is None:
        return share >= NAMED_SHARE_THRESHOLD
    if prev_named:
        return share >= NAMED_EXIT
    return share >= NAMED_ENTER


def engine_stats(results: Iterable[Any], prev_named: bool | None = None) -> dict:
    """One question x one engine under the owner rule + the dead zone against
    `prev_named` (the previous week's state; None = no previous week). Not
    asked -> named/share/rank None."""
    per_market = latest_per_market(results)
    if not per_market:
        return not_asked()
    cities_named = sum(1 for result in per_market if result.brand_mentioned)
    share = cities_named / len(per_market)
    ranks = [result.rank for result in per_market if result.brand_mentioned and result.rank is not None]
    return {
        "named": named_state(share, prev_named),
        "share": round(share, 2),
        "rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "cities": len(per_market),
        "cities_named": cities_named,
    }


def share_noise(pooled_share: float, cities: int) -> float:
    """NOISE_Z binomial standard errors of a share over `cities` asked."""
    if cities <= 0:
        return math.inf
    return NOISE_Z * math.sqrt(pooled_share * (1 - pooled_share) / cities)


def share_moved(before: dict, now: dict) -> bool:
    """Did the share move by more than noise between two weeks? (module doc)"""
    if before.get("share") is None or now.get("share") is None or not before.get("cities") or not now.get("cities"):
        return False
    share_before = before["cities_named"] / before["cities"]
    share_now = now["cities_named"] / now["cities"]
    pooled = (before["cities_named"] + now["cities_named"]) / (before["cities"] + now["cities"])
    return abs(share_now - share_before) > share_noise(pooled, now["cities"])


def engine_change(before: dict | None, now: dict | None) -> str | None:
    """ "up" / "down" when this engine's named state flipped AND the share
    moved more than noise; None otherwise (or when either week wasn't asked)."""
    if not before or not now or before.get("named") is None or now.get("named") is None or before["named"] == now["named"]:
        return None
    if not share_moved(before, now):
        return None
    return "up" if now["named"] else "down"


def question_change(before: dict[str, dict] | None, now: dict[str, dict] | None) -> str | None:
    """A question is "up" when it went from named on no assistant to at least
    one (and an assistant that flipped up passed the noise gate); "down" the
    reverse; None otherwise."""
    if not before or not now:
        return None
    count_before, count_now = named_count(before), named_count(now)
    if count_before is None or count_now is None:
        return None
    flips = {engine_change(before.get(engine), stats) for engine, stats in now.items()}
    if count_before == 0 and count_now >= 1 and "up" in flips:
        return "up"
    if count_before >= 1 and count_now == 0 and "down" in flips:
        return "down"
    return None


def weekly_buckets(results: Iterable[Any], now: datetime, weeks: int) -> list[list[Any]]:
    """Results split into the week windows (index 0 = (now-7d, now], k = k
    weeks before). Anything outside the `weeks` windows (or in the future) is dropped."""
    buckets: list[list[Any]] = [[] for _ in range(weeks)]
    for result in results:
        if result.created_at is None or result.created_at > now:
            continue
        index = int((now - result.created_at) // WEEK)
        if index < weeks:
            buckets[index].append(result)
    return buckets


def prompt_history(buckets: list[list[Any]], engines: list[str]) -> list[dict[str, dict]]:
    """One question's per-week {engine: stats}, index 0 = this week, with the
    dead-zone state chained oldest -> newest (a week not asked carries the
    previous state on). `buckets` = that question's results per week window."""
    history: list[dict[str, dict]] = [{} for _ in buckets]
    for engine in engines:
        prev_named: bool | None = None
        for index in reversed(range(len(buckets))):
            stats = engine_stats([result for result in buckets[index] if result.engine == engine], prev_named)
            history[index][engine] = stats
            if stats["named"] is not None:
                prev_named = stats["named"]
    return history


def asked_in(week: dict[str, dict]) -> bool:
    return any(stats["named"] is not None for stats in week.values())


def latest_checked_index(history: list[dict[str, dict]], start: int = 0) -> int | None:
    """The newest week (index >= start) in which any engine was asked."""
    return next((index for index in range(start, len(history)) if asked_in(history[index])), None)


def group_by_prompt_engine(results: Iterable[Any]) -> dict[str, dict[str, list[Any]]]:
    """{prompt_id: {engine: [results]}} (utils/collection_plan.py's re-ask reads it)."""
    grouped: dict[str, dict[str, list[Any]]] = {}
    for result in results:
        grouped.setdefault(result.prompt_id, {}).setdefault(result.engine, []).append(result)
    return grouped


def named_count(stats_by_engine: dict[str, dict]) -> int | None:
    """How many assistants name the brand (0..len(engines)); None when no engine asked."""
    asked = [stats for stats in stats_by_engine.values() if stats["named"] is not None]
    return sum(1 for stats in asked if stats["named"]) if asked else None


def per_engine_summary(stats_by_question: dict[str, dict[str, dict]], engines: list[str]) -> dict[str, dict]:
    """Per engine: how many questions it names the brand in, of how many it was
    asked this week (the ring's "6 of 15")."""
    summary = {}
    for engine in engines:
        asked = [question[engine] for question in stats_by_question.values() if question.get(engine, not_asked())["named"] is not None]
        summary[engine] = {"questions_named": sum(1 for stats in asked if stats["named"]), "of": len(asked)}
    return summary


def competitor_names_by_engine(results: Iterable[Any], mentions_by_result_id: dict[str, list[Any]]) -> dict[str, list[str]]:
    """Every competitor naming, per engine (one per answer per competitor — the
    mentions table's unique key), for the top-competitor count."""
    names: dict[str, list[str]] = {}
    for result in results:
        names.setdefault(result.engine, []).extend(mention.competitor_name for mention in mentions_by_result_id.get(result.id, []))
    return names


NAMED_INSTEAD_LIMIT = 3


def named_instead(results: list[Any], mentions_by_result_id: dict[str, list[Any]]) -> list[dict]:
    """[{name, answers}] — the businesses an engine named for this question
    across its answers this week, by how many answers named each (ties
    alphabetical), top NAMED_INSTEAD_LIMIT. Pure."""
    counts: dict[str, int] = {}
    for result in results:
        for name in {mention.competitor_name for mention in mentions_by_result_id.get(result.id, [])}:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"name": name, "answers": count} for name, count in ranked[:NAMED_INSTEAD_LIMIT]]


def top_competitor(names: Iterable[str]) -> str | None:
    """The most-named competitor; ties go alphabetical so the answer is stable."""
    counts = Counter(names)
    if not counts:
        return None
    return min(counts.items(), key=lambda item: (-item[1], item[0].lower(), item[0]))[0]


def biggest_gap(per_engine: dict[str, dict], competitor_names: dict[str, list[str]], engines: list[str]) -> dict | None:
    """The assistant naming the brand in the fewest questions (ties: lower
    share of its asked questions, then display order) plus the competitor it
    named most this week. None when nothing was asked or every asking
    assistant already names the brand everywhere — no gap, no nudge."""
    candidates = [engine for engine in engines if per_engine.get(engine, {}).get("of", 0) > 0]
    if not candidates:
        return None
    gap_engine = min(candidates, key=lambda engine: (per_engine[engine]["questions_named"], per_engine[engine]["questions_named"] / per_engine[engine]["of"], engines.index(engine)))
    if per_engine[gap_engine]["questions_named"] >= per_engine[gap_engine]["of"]:
        return None
    return {"engine": gap_engine, "top_competitor": top_competitor(competitor_names.get(gap_engine, []))}


def tier_of(prompt: Any) -> str:
    return getattr(prompt, "tier", None) or TIER_SUGGESTED


# research's intent labels -> the group names Marco reads ("problem" isn't a heading)
TOPIC_GROUP_WORDS = {
    "recommendation": "Asking who to hire",
    "recommendations": "Asking who to hire",
    "comparison": "Comparing options",
    "comparisons": "Comparing options",
    "problem": "Fixing a problem",
    "problems": "Fixing a problem",
    "occasion": "Specific situations",
    "occasions": "Specific situations",
    "local": "Near me",
    "cost": "Prices and costs",
    "price": "Prices and costs",
    "pricing": "Prices and costs",
}


def topic_group(prompt: Any) -> str:
    """The long tail's grouping label, in owner words: the question's category
    mapped through TOPIC_GROUP_WORDS (an unknown free-text category is shown
    capitalized), else "Other"."""
    category = (getattr(prompt, "category", None) or "").strip()
    if not category:
        return OTHER_TOPIC_GROUP
    return TOPIC_GROUP_WORDS.get(category.lower(), category[:1].upper() + category[1:])


def question_row_indices(prompt: Any, history: list[dict[str, dict]]) -> tuple[int, int | None]:
    """(current, previous) week index for one question's row. Core and
    suggested read THIS week vs LAST week; the long tail (checked about
    monthly) reads its latest check vs the check before it."""
    if tier_of(prompt) == TIER_WATCHING:
        current = latest_checked_index(history)
        if current is None:
            return 0, None
        return current, latest_checked_index(history, current + 1)
    return 0, (1 if len(history) > 1 else None)


def questions_stats(prompts: list[Any], current: list[Any], previous: list[Any], mentions_by_result_id: dict[str, list[Any]], engines: list[str], older: list[list[Any]] | None = None) -> tuple[dict[str, dict], dict]:
    """The questions endpoint's additions: ({prompt_id: {engines, named_count,
    prev_named_count, change}}, summary). Every prompt gets a row; the summary
    (tiles, rings, biggest gap, up/down) counts CORE prompts only. `older` =
    the weeks before `previous` (index 2, 3, ...) — they feed the dead-zone
    chain and the long tail's latest check. Results from engines outside
    `engines` are ignored."""
    weeks = [current, previous, *(older or [])]
    weeks = [[result for result in week if result.engine in engines] for week in weeks]
    by_prompt_week: dict[str, list[list[Any]]] = {}
    for index, week in enumerate(weeks):
        for result in week:
            by_prompt_week.setdefault(result.prompt_id, [[] for _ in weeks])[index].append(result)

    rows: dict[str, dict] = {}
    current_stats: dict[str, dict[str, dict]] = {}
    previous_stats: dict[str, dict[str, dict] | None] = {}
    for prompt in prompts:
        history = prompt_history(by_prompt_week.get(prompt.id, [[] for _ in weeks]), engines)
        now_index, before_index = question_row_indices(prompt, history)
        stats = history[now_index]
        before = history[before_index] if before_index is not None else None
        current_stats[prompt.id], previous_stats[prompt.id] = stats, before
        rows[prompt.id] = {
            "engines": {engine: {key: stats[engine][key] for key in ("named", "share", "rank", "cities")} for engine in engines},
            "named_count": named_count(stats),
            "prev_named_count": named_count(before) if before is not None else None,
            "change": question_change(before, stats),
        }

    core_ids = [prompt.id for prompt in prompts if tier_of(prompt) == TIER_CORE]
    per_engine = per_engine_summary({prompt_id: current_stats[prompt_id] for prompt_id in core_ids}, engines)
    core_set = set(core_ids)
    core_current = [result for result in weeks[0] if result.prompt_id in core_set]
    has_previous = any(rows[prompt_id]["prev_named_count"] is not None for prompt_id in core_ids)
    changes = [rows[prompt_id]["change"] for prompt_id in core_ids]
    newest_week = week_of(core_current) or week_of(weeks[0])
    summary = {
        "week_of": newest_week.isoformat() if newest_week else None,
        "questions_tracked": len(core_ids),
        "questions_named": sum(1 for prompt_id in core_ids if (rows[prompt_id]["named_count"] or 0) >= 1),
        "questions_named_prev": sum(1 for prompt_id in core_ids if (rows[prompt_id]["prev_named_count"] or 0) >= 1) if has_previous else None,
        "questions_up": changes.count("up"),
        "questions_down": changes.count("down"),
        "named_delta": changes.count("up") - changes.count("down") if has_previous else None,  # noise-gated (module doc)
        "per_engine": per_engine,
        "biggest_gap": biggest_gap(per_engine, competitor_names_by_engine(core_current, mentions_by_result_id), engines),
    }
    return rows, summary


def watching_summary(prompts: list[Any], rows: dict[str, dict], now: datetime, moves: Iterable[Any] = ()) -> dict:
    """The long-tail block: {count, checked_this_month, named, new_this_month,
    by_topic: [{topic_group, count, named}]}. named = named on at least one
    assistant at its latest check (rows from questions_stats). new_this_month =
    started being watched this calendar month from the suggested pool (or at
    launch) — a demotion from core isn't "new"."""
    month_start = datetime(now.year, now.month, 1)
    watching = [prompt for prompt in prompts if tier_of(prompt) == TIER_WATCHING]
    came_from: dict[str, str] = {}
    for move in sorted(moves, key=lambda move: move.created_at or datetime.min):
        if move.to_tier == TIER_WATCHING:
            came_from[move.prompt_id] = move.from_tier

    def is_named(prompt: Any) -> bool:
        return (rows.get(prompt.id, {}).get("named_count") or 0) >= 1

    groups: dict[str, dict] = {}
    for prompt in sorted(watching, key=lambda prompt: topic_group(prompt).lower()):
        group = groups.setdefault(topic_group(prompt), {"topic_group": topic_group(prompt), "count": 0, "named": 0})
        group["count"] += 1
        group["named"] += int(is_named(prompt))
    return {
        "count": len(watching),
        "checked_this_month": sum(1 for prompt in watching if prompt.last_checked_at is not None and prompt.last_checked_at >= month_start),
        "named": sum(1 for prompt in watching if is_named(prompt)),
        "new_this_month": sum(1 for prompt in watching if prompt.tier_changed_at is not None and prompt.tier_changed_at >= month_start and came_from.get(prompt.id, TIER_SUGGESTED) == TIER_SUGGESTED),
        "by_topic": sorted(groups.values(), key=lambda group: (-group["count"], group["topic_group"].lower())),
    }


def strength_sort_key(row: dict) -> tuple:
    """Strongest first: most assistants naming, then the higher mean share,
    then never-asked last; stable on the original order otherwise."""
    shares = [stats["share"] for stats in row.get("engines", {}).values() if stats.get("share") is not None]
    return (row.get("named_count") is None, -(row.get("named_count") or 0), -(sum(shares) / len(shares)) if shares else 0.0)


##############################
# PURE: ONE QUESTION'S ANSWERS
##############################


def market_label(market: Any) -> str | None:
    if market is None:
        return None
    if getattr(market, "source", None) == SOURCE_NATIONAL:
        return NATIONWIDE_LABEL
    return f"{market.city}, {market.state}"


def sample_result(results: Iterable[Any], market_order: list[str]) -> Any | None:
    """The answer to show for one engine: where the brand ranked best; then a
    mention without a rank; never named -> the most-preferred market
    (market_order: the brand's home/first market first). None if not asked."""
    per_market = latest_per_market(results)
    if not per_market:
        return None
    position = {market_id: index for index, market_id in enumerate(market_order)}

    def key(result: Any) -> tuple:
        if result.brand_mentioned and result.rank is not None:
            tier, rank = 0, result.rank
        elif result.brand_mentioned:
            tier, rank = 1, 0
        else:
            tier, rank = 2, 0
        return (tier, rank, position.get(result.market_id, len(market_order)), result.market_id)

    return min(per_market, key=key)


def ordered_mention_names(mentions: Iterable[Any]) -> list[str]:
    """Distinct competitor names in answer order (listed position, unranked last)."""
    names: dict[str, str] = {}
    for mention in sorted(mentions, key=lambda mention: (mention.position is None, mention.position or 0, mention.competitor_name.lower())):
        names.setdefault(mention.normalized_name, mention.competitor_name)
    return list(names.values())


def businesses_named(result: Any, competitor_names: list[str]) -> int:
    """How many businesses the answer named: the brand (if named) + matched
    competitors, floored at the brand's listed rank (rank N means at least N were listed)."""
    return max(int(bool(result.brand_mentioned)) + len(competitor_names), result.rank or 0)


def truncate_answer(text: str, limit: int = SAMPLE_TEXT_LIMIT) -> str:
    """At most ~limit chars, cut at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.8:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def engine_answer_row(engine: str, results: list[Any], market_order: list[str], mentions_by_result_id: dict[str, list[Any]], markets: dict[str, Any], corpus_by_id: dict[str, Any], prev_named: bool | None = None, previous: dict | None = None) -> dict:
    """One engine's block on the one-question screen. The sample's text and
    date come from the corpus answer (falling back to the result's own copy
    if the corpus row is missing); businesses_named/also_named describe that
    same answer, so the "#2 of 4 named" line matches the text shown.
    prev_named / previous: the previous check's state and stats (the dead zone
    and the noise-gated `change` — the same reads as the questions endpoint)."""
    stats = engine_stats(results, prev_named)
    sample = sample_result(results, market_order)
    row: dict = {
        "engine": engine,
        "named": stats["named"],
        "share": stats["share"],
        "cities_asked": stats["cities"],
        "cities_named": stats["cities_named"],
        "rank": stats["rank"],
        "change": engine_change(previous, stats),
        "businesses_named": None,
        "also_named": [],
        # who this assistant names INSTEAD, across every city this week (most first) — Marco's first question
        "named_instead": named_instead(results, mentions_by_result_id),
        "sample": None,
    }
    if sample is None:
        return row
    competitor_names = ordered_mention_names(mentions_by_result_id.get(sample.id, []))
    answer = corpus_by_id.get(sample.query_response_id)
    collected_at = answer.collected_at if answer is not None else sample.created_at
    row["businesses_named"] = businesses_named(sample, competitor_names)
    row["also_named"] = competitor_names[:ALSO_NAMED_LIMIT]
    row["sample"] = {
        "market": market_label(markets.get(sample.market_id)),
        "collected_at": utc_iso(collected_at),
        "text": truncate_answer(answer.text if answer is not None else sample.text or ""),
        "rank": sample.rank if sample.brand_mentioned else None,
    }
    return row


async def question_answer_stats(brand_id: str, prompts: list[Any], with_gap_competitor: bool = True) -> tuple[dict[str, dict], dict]:
    """questions_stats over the last HISTORY_WEEKS weeks (light rows, one
    read) for the questions endpoint and the recap. Competitor mentions are
    loaded only for the biggest-gap engine's core answers this week (a
    national brand's week is thousands of results; one engine's is a third)."""
    engines = display_engines(settings.ACTIVE_ENGINES)
    now = clock.now()
    rows = await analyzed_rows(brand_id, since=now - WEEK * HISTORY_WEEKS, until=now)
    weeks = weekly_buckets(rows, now, HISTORY_WEEKS)
    question_rows, summary = questions_stats(prompts, weeks[0], weeks[1], {}, engines, older=weeks[2:])
    gap = summary["biggest_gap"]
    if gap is not None and with_gap_competitor:
        core_ids = {prompt.id for prompt in prompts if tier_of(prompt) == TIER_CORE}
        gap_results = [result for result in weeks[0] if result.engine == gap["engine"] and result.prompt_id in core_ids]
        gap["top_competitor"] = top_competitor(competitor_names_by_engine(gap_results, await mentions_by_result(brand_id, gap_results)).get(gap["engine"], []))
    return question_rows, summary
