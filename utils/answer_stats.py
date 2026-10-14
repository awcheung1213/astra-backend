"""The AI answers screens' aggregation: per tracked question x assistant, does
that assistant NAME the brand this week?

Owner-approved rule (2026-10-14): per question x engine take the latest
ANALYZED result per market in the scoring week; share = markets that named
the brand / markets asked; named = share >= NAMED_SHARE_THRESHOLD; rank = the
average rank over the results that named it. A local brand asks in a handful
of markets, a national brand in ~150 — the share handles both.

The week is the questions endpoint's existing "last week" window (the 7-day
horizon PromptResult.filter reads); the previous week is the 7 days before it.
Everything below the loaders is pure (tests/test_answer_stats.py). Reads only:
corpus rows (query_responses) are looked up, never written."""

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select

import settings
from objects.brand_market import BrandMarket
from objects.competitor import Competitor
from objects.competitor_mention import CompetitorMention
from objects.market import SOURCE_NATIONAL, Market
from objects.prompt_result import PromptResult
from objects.query_response import ENGINE_CHATGPT, ENGINE_CLAUDE, ENGINE_GEMINI, QueryResponse
from schemas.base_schemas import utc_iso
from utils import clock
from utils.database import ReplicaDatabase

NAMED_SHARE_THRESHOLD = 0.5
WEEK = timedelta(days=7)  # = PromptResult.filter's horizon (the questions page's "last week")
DISPLAY_ENGINE_ORDER = (ENGINE_CHATGPT, ENGINE_GEMINI, ENGINE_CLAUDE)  # the rings' outer -> inner order
ALSO_NAMED_LIMIT = 3
SAMPLE_TEXT_LIMIT = 1500
NATIONWIDE_LABEL = "Nationwide"


##############################
# LOADERS (DB reads)
##############################


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
    rows = await ReplicaDatabase.session.execute(statement)  # type: ignore
    return list(rows.scalars().all())


async def week_results(brand_id: str, weeks_back: int = 0, prompt_id: str | None = None) -> list[PromptResult]:
    since, until = week_window(clock.now(), weeks_back)
    return await latest_analyzed_results(brand_id, since, until, prompt_id=prompt_id)


async def mentions_by_result(brand_id: str, results: list[PromptResult]) -> dict[str, list[CompetitorMention]]:
    """Competitor mentions per result id, minus names the brand has since
    excluded (the matcher skips them going forward; old rows still hold them)."""
    excluded = await Competitor.excluded_names(brand_id)
    grouped: dict[str, list[CompetitorMention]] = {}
    for mention in await CompetitorMention.by_prompt_result_ids([result.id for result in results]):
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


def engine_stats(results: Iterable[Any]) -> dict:
    """One question x one engine under the owner rule. Not asked -> named/share/rank None."""
    per_market = latest_per_market(results)
    if not per_market:
        return not_asked()
    cities_named = sum(1 for result in per_market if result.brand_mentioned)
    share = cities_named / len(per_market)
    ranks = [result.rank for result in per_market if result.brand_mentioned and result.rank is not None]
    return {
        "named": share >= NAMED_SHARE_THRESHOLD,
        "share": round(share, 2),
        "rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "cities": len(per_market),
        "cities_named": cities_named,
    }


def group_by_prompt_engine(results: Iterable[Any]) -> dict[str, dict[str, list[Any]]]:
    grouped: dict[str, dict[str, list[Any]]] = {}
    for result in results:
        grouped.setdefault(result.prompt_id, {}).setdefault(result.engine, []).append(result)
    return grouped


def question_engines(results_by_engine: dict[str, list[Any]], engines: list[str]) -> dict[str, dict]:
    return {engine: engine_stats(results_by_engine.get(engine, [])) for engine in engines}


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


def questions_stats(prompts: list[Any], current: list[Any], previous: list[Any], mentions_by_result_id: dict[str, list[Any]], engines: list[str]) -> tuple[dict[str, dict], dict]:
    """The questions endpoint's additions: ({prompt_id: {engines, named_count,
    prev_named_count}}, summary). Every prompt gets a row; the summary counts
    ACTIVE prompts only. Results from engines outside `engines` are ignored."""
    current = [result for result in current if result.engine in engines]
    previous = [result for result in previous if result.engine in engines]
    current_grouped, previous_grouped = group_by_prompt_engine(current), group_by_prompt_engine(previous)

    rows: dict[str, dict] = {}
    for prompt in prompts:
        stats = question_engines(current_grouped.get(prompt.id, {}), engines)
        rows[prompt.id] = {
            "engines": {engine: {key: stats[engine][key] for key in ("named", "share", "rank", "cities")} for engine in engines},
            "named_count": named_count(stats),
            "prev_named_count": named_count(question_engines(previous_grouped.get(prompt.id, {}), engines)),
            "_stats": stats,
        }

    active_ids = {prompt.id for prompt in prompts if prompt.is_active}
    active_stats = {prompt_id: rows[prompt_id]["_stats"] for prompt_id in active_ids}
    per_engine = per_engine_summary(active_stats, engines)
    active_current = [result for result in current if result.prompt_id in active_ids]
    has_previous = any(result.prompt_id in active_ids for result in previous)
    week = week_of(current)
    summary = {
        "week_of": week.isoformat() if week else None,
        "questions_tracked": len(active_ids),
        "questions_named": sum(1 for prompt_id in active_ids if (rows[prompt_id]["named_count"] or 0) >= 1),
        "questions_named_prev": sum(1 for prompt_id in active_ids if (rows[prompt_id]["prev_named_count"] or 0) >= 1) if has_previous else None,
        "per_engine": per_engine,
        "biggest_gap": biggest_gap(per_engine, competitor_names_by_engine(active_current, mentions_by_result_id), engines),
    }
    for row in rows.values():
        del row["_stats"]
    return rows, summary


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


def engine_answer_row(engine: str, results: list[Any], market_order: list[str], mentions_by_result_id: dict[str, list[Any]], markets: dict[str, Any], corpus_by_id: dict[str, Any]) -> dict:
    """One engine's block on the one-question screen. The sample's text and
    date come from the corpus answer (falling back to the result's own copy
    if the corpus row is missing); businesses_named/also_named describe that
    same answer, so the "#2 of 4 named" line matches the text shown."""
    stats = engine_stats(results)
    sample = sample_result(results, market_order)
    row: dict = {
        "engine": engine,
        "named": stats["named"],
        "share": stats["share"],
        "cities_asked": stats["cities"],
        "cities_named": stats["cities_named"],
        "rank": stats["rank"],
        "businesses_named": None,
        "also_named": [],
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


async def question_answer_stats(brand_id: str, prompts: list[Any]) -> tuple[dict[str, dict], dict]:
    """questions_stats over this week + last week for the questions endpoint.
    Competitor mentions are loaded only for the biggest-gap engine's answers
    (a national brand's week is thousands of results; one engine's is a third)."""
    engines = display_engines(settings.ACTIVE_ENGINES)
    current = await week_results(brand_id, weeks_back=0)
    previous = await week_results(brand_id, weeks_back=1)
    rows, summary = questions_stats(prompts, current, previous, {}, engines)
    gap = summary["biggest_gap"]
    if gap is not None:
        active_ids = {prompt.id for prompt in prompts if prompt.is_active}
        gap_results = [result for result in current if result.engine == gap["engine"] and result.prompt_id in active_ids]
        gap["top_competitor"] = top_competitor(competitor_names_by_engine(gap_results, await mentions_by_result(brand_id, gap_results)).get(gap["engine"], []))
    return rows, summary
