"""The weekly visibility sweep: newest analyzed answer per cell -> per-market
scores (visibility_run_markets) -> one framework-versioned trend point
(visibility_runs). Ported from geoviz's report generation, collapsed to
astra's shape: the run IS the aggregate and the trend point; components are
computed over the FULL result set (not averaged across markets — framework
1.0.0 defines it this way on purpose, don't "fix" it back to geoviz's
average-of-markets). Zero LLM calls — the sweep is $0 and safe to run any
time. No metro fan-out, no action generation (deliberately excluded)."""

from datetime import date, timedelta

from objects.brand import Brand
from objects.competitor_mention import CompetitorMention
from objects.market import SOURCE_NATIONAL, Market
from objects.prompt_result import SENTIMENT_POSITIVE, PromptResult
from objects.visibility_run import STATUS_DONE, VisibilityRun
from objects.visibility_run_market import VisibilityRunMarket
from schemas.prompt_result_schemas import CompetitorMention as CompetitorMentionSchema
from schemas.prompt_result_schemas import PromptResultWithCompetitorMentions
from utils.logger import logger

# framework 1.0.0 weights — unconditional rates over all answers, sum to 1.0
WEIGHT_DETECTION = 0.40
WEIGHT_SHARE_OF_VOICE = 0.25
WEIGHT_POSITIVE_RATE = 0.15
WEIGHT_TOP3_RATE = 0.15
WEIGHT_CITATION_RATE = 0.05

TOP_3_RANK = 3


##############################
# COMPONENT CALCULATORS (pure)
##############################


def detection_rate(results: list[PromptResultWithCompetitorMentions]) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result.brand_mentioned) / len(results)


def share_of_voice(results: list[PromptResultWithCompetitorMentions]) -> float:
    brand_mentions = sum(1 for result in results if result.brand_mentioned)
    competitor_mentions = sum(result.competitor_mention_count or 0 for result in results)
    total_mentions = brand_mentions + competitor_mentions
    return brand_mentions / total_mentions if total_mentions > 0 else 0.0


def positive_rate(results: list[PromptResultWithCompetitorMentions]) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result.brand_mentioned and result.positive_sentiment) / len(results)


def top3_rate(results: list[PromptResultWithCompetitorMentions]) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result.brand_mentioned and result.top_3_position) / len(results)


def citation_rate(results: list[PromptResultWithCompetitorMentions]) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result.brand_cited) / len(results)


def overall_score(results: list[PromptResultWithCompetitorMentions]) -> float:
    return (
        detection_rate(results) * WEIGHT_DETECTION
        + share_of_voice(results) * WEIGHT_SHARE_OF_VOICE
        + positive_rate(results) * WEIGHT_POSITIVE_RATE
        + top3_rate(results) * WEIGHT_TOP3_RATE
        + citation_rate(results) * WEIGHT_CITATION_RATE
    )


##############################
# SWEEP
##############################


def week_of_today() -> date:
    """Monday of the current week — the run's trend axis."""
    today = date.today()
    return today - timedelta(days=today.weekday())


def hydrate_prompt_result(result: PromptResult, mentions: list[CompetitorMention]) -> PromptResultWithCompetitorMentions:
    """Row(s) -> the scoring/analysis dataclass. Derived fields live HERE:
    positive_sentiment (sentiment == positive; None = no verdict), top-3
    (rank <= 3), competitor_mention_count. Shared with the collector's
    analysis step so both paths hydrate identically."""
    return PromptResultWithCompetitorMentions(
        id=result.id,
        prompt_id=result.prompt_id,
        market_id=result.market_id,
        brand_id=result.brand_id,
        engine=result.engine,
        text=result.text,
        raw=result.raw,
        brand_cited=result.brand_cited,
        brand_mentioned=result.brand_mentioned,
        brand_mention_position=result.rank,
        positive_sentiment=None if result.sentiment is None else result.sentiment == SENTIMENT_POSITIVE,
        top_3_position=result.rank is not None and result.rank <= TOP_3_RANK,
        competitor_mention_count=len(mentions),
        query_response_id=result.query_response_id,
        created_at=result.created_at,
        competitor_mentions=[CompetitorMentionSchema(id=m.id, competitor_id=m.competitor_id, position=m.position, created_at=m.created_at) for m in mentions],
    )


def _group_by_market(scored: list[PromptResultWithCompetitorMentions], national_market_ids: set[str]) -> dict[str, list[PromptResultWithCompetitorMentions]]:
    """Geo markets each get their own bucket; national (location-independent)
    results ride along in EVERY geo bucket. A brand with only national results
    gets a single national bucket so it still scores."""
    national_results = [result for result in scored if result.market_id in national_market_ids]
    by_market: dict[str, list[PromptResultWithCompetitorMentions]] = {}
    for result in scored:
        if result.market_id in national_market_ids:
            continue
        by_market.setdefault(result.market_id, []).append(result)
    if not by_market and national_results:
        by_market[national_results[0].market_id] = list(national_results)
    else:
        for market_results in by_market.values():
            market_results.extend(national_results)
    return by_market


async def run_visibility_sweep(brand_id: str) -> VisibilityRun | None:
    """One brand's weekly sweep. Idempotent per brand-week: retries resume the
    existing run; an already-done run short-circuits. Returns None when the
    brand has nothing analyzed to score (no run row — no trend gap)."""
    brand = await Brand.get(brand_id)

    prompt_results = await PromptResult.filter(brand_id=brand.id, contains_sentiment_analysis=True)
    if not prompt_results:
        logger.info(f"[visibility] brand {brand.id} has no analyzed results; skipping sweep")
        return None

    mentions_by_result: dict[str, list[CompetitorMention]] = {}
    for mention in await CompetitorMention.by_prompt_result_ids([result.id for result in prompt_results]):
        mentions_by_result.setdefault(mention.prompt_result_id, []).append(mention)
    scored = [hydrate_prompt_result(result, mentions_by_result.get(result.id, [])) for result in prompt_results]

    markets_by_id = {market.id: market for market in await Market.all()}
    national_market_ids = {market_id for market_id in {result.market_id for result in scored} if markets_by_id.get(market_id) and markets_by_id[market_id].source == SOURCE_NATIONAL}
    by_market = _group_by_market(scored, national_market_ids)

    run = await VisibilityRun.get_or_create_for_week(brand.id, week_of_today())
    if run.status == STATUS_DONE:
        logger.info(f"[visibility] run {run.id} already done for brand {brand.id} week {run.week_of}; skipping")
        return run

    await VisibilityRunMarket.delete_for_run(run.id)
    for market_id, market_results in by_market.items():
        await VisibilityRunMarket.create(visibility_run_id=run.id, market_id=market_id, overall_score=overall_score(market_results))

    run = await VisibilityRun.finish(
        run.id,
        overall_score=overall_score(scored),
        detection_rate=detection_rate(scored),
        share_of_voice=share_of_voice(scored),
        positive_rate=positive_rate(scored),
        top3_rate=top3_rate(scored),
        citation_rate=citation_rate(scored),
    )
    await PromptResult.assign_visibility_run([result.id for result in prompt_results], run.id)
    logger.info(f"[visibility] run {run.id} done for brand {brand.id}: overall={run.overall_score:.3f} over {len(scored)} results in {len(by_market)} markets")
    return run
