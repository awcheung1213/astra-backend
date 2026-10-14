import hashlib
import json

import settings
from adapters.registry import initialize_adapter
from adapters.services.claude_service import ClaudeService
from constants.constants import UsageKind
from objects.batch import Batch
from objects.brand import FOOTPRINT_LOCAL, Brand
from objects.citation import Citation
from objects.competitor import SOURCE_DISCOVERED, Competitor
from objects.competitor_mention import CompetitorMention
from objects.cost_event import CostEvent
from objects.market import SOURCE_NATIONAL, Market
from objects.prompt import Prompt
from objects.prompt_result import SENTIMENT_NEUTRAL, SENTIMENT_POSITIVE, PromptResult
from objects.query_response import QueryResponse
from schemas.prompt_result_schemas import PromptResultWithCompetitorMentions
from schemas.provider_schemas import LocationContext, QueryRequest, QueryResult
from utils import answer_stats, clock
from utils.city_panel import reask_cities, small_panel
from utils.collection_plan import CORPUS_RAW_KEY, WEB_SEARCH_KEY, AnswerRates, BudgetContext, CandidateCell, PlannedCell, borderline_pairs, is_no_search_answer, load_budget_context, plan_cells, reask_planned_cells, trim_to_budget
from utils.competitor_matching import build_competitor_regex_patterns, parse_prompt_result_for_competitor_mention_rank
from utils.database import Database, ReplicaDatabase
from utils.llm_usage import estimate_cost, summarize_usage
from utils.logger import logger
from utils.query_normalization import query_hash
from utils.question_tiers import questions_to_collect
from utils.response_parsing import mentions_name, parse_prompt_result_for_mention_rank
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from utils.urls import domain_from_url
from worker.app import app
from worker.tasks.visibility import hydrate_prompt_result


# no retry: a retry after the provider accepted a batch but before Batch.create commits would pay for it twice
@app.task(name="submit_prompt_execution_batch", queue="collection")
async def submit_prompt_execution_batch(task_id: str | None = None, brand_id: str | None = None) -> None:
    """Collection is system-wide. A prompt sign-off enqueues it via create_task
    (task_id = the signing brand's tasks row); the weekly cron defers it bare —
    the documented rule-8 exception (brand-less system work, no tasks row).
    The Saturday borderline re-ask enqueues it per brand via create_task with
    payload {"reask": true} (no exception needed: it has a tasks row)."""
    logger.info(f"[collection] submit_prompt_execution_batch - start", priority="medium")

    if task_id is not None:
        async with task_lifecycle(task_id) as task:
            # the Saturday re-ask cron enqueues this task per national brand with payload {"reask": true}
            if (getattr(task, "payload", None) or {}).get("reask") and brand_id is not None:
                await run_submit_reask_batch(task_id=task_id, brand_id=brand_id)
            else:
                await run_submit_prompt_execution_batch(task_id=task_id, brand_id=brand_id)
    else:
        async with ReplicaDatabase().connection():
            async with Database().connection():
                await run_submit_prompt_execution_batch()

    logger.info(f"[collection] submit_prompt_execution_batch - end", priority="medium")


@app.task(name="poll_batches", queue="collection", retry=RETRY_WITH_BACKOFF)  # idempotent: a retry re-polls
async def poll_batches() -> None:
    logger.info(f"[collection] poll_batches - start", priority="medium")

    async with ReplicaDatabase().connection():
        async with Database().connection():
            await run_poll_batches()

    logger.info(f"[collection] poll_batches - end", priority="medium")


RESPONSE_TRUNCATE_CHARS = 1500
# Sentiment analysis is packed: many responses per LLM call, instead of one
# call per response. The instructions live in the (cached) system block; the
# user message carries the numbered responses.
SENTIMENT_PACK_SIZE = 15
SENTIMENT_PACK_MAX_TOKENS = 4096
DISCOVERY_SAMPLE_MODULO = 20


_PACK_ITEM_BASE_PROPERTIES = {
    "index": {"type": "integer"},
    "brand_mention_position": {"type": ["integer", "null"]},
    "positive_sentiment": {"type": "boolean"},
    "top_3_position": {"type": "boolean"},
}


SENTIMENT_ANALYSIS_SYSTEM_PROMPT = """You analyze how a brand appears in AI search responses.

You will receive a brand name and several AI responses, each labeled '### Response N'. For EACH response, return one result object carrying its index N and, judged objectively for that response alone:
- brand_mention_position (int or null): 1-indexed rank if the brand is in a ranked/ordered list; null if not mentioned or not in a ranked list.
- positive_sentiment (bool): true ONLY if the brand is framed favorably (recommended, praised, top pick). Neutral inclusion in a list is NOT positive. false if neutral, negative, or not mentioned.
- top_3_position (bool): true if the brand is in positions 1-3 of a ranked list; otherwise false.

Return exactly one result per response, in order."""


SENTIMENT_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": dict(_PACK_ITEM_BASE_PROPERTIES),
                "required": ["index", "brand_mention_position", "positive_sentiment", "top_3_position"],
            },
        },
    },
    "required": ["results"],
}


COMPETITOR_DISCOVERY_SYSTEM_PROMPT = """You analyze how a brand appears in AI search responses.

You will receive a brand name and several AI responses, each labeled '### Response N'. For EACH response, return one result object carrying its index N and, judged objectively for that response alone:
- brand_mention_position (int or null): 1-indexed rank if the brand is in a ranked/ordered list; null if not mentioned or not in a ranked list.
- positive_sentiment (bool): true ONLY if the brand is framed favorably (recommended, praised, top pick). Neutral inclusion in a list is NOT positive. false if neutral, negative, or not mentioned.
- top_3_position (bool): true if the brand is in positions 1-3 of a ranked list; otherwise false.
- competitor_mentions (array): competing brands in the same product category (exclude retailers/marketplaces) mentioned in that response (exclude the analyzed brand). For each, include name and position (1-indexed rank in ranked list, or null if not in a ranked list).

Return exactly one result per response, in order."""


COMPETITOR_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **_PACK_ITEM_BASE_PROPERTIES,
                    "competitor_mentions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "position": {"type": ["integer", "null"]},
                            },
                            "required": ["name", "position"],
                        },
                    },
                },
                "required": ["index", "brand_mention_position", "positive_sentiment", "top_3_position", "competitor_mentions"],
            },
        },
    },
    "required": ["results"],
}


##############################
# BATCH SUBMISSION
##############################


async def run_submit_prompt_execution_batch(task_id: str | None = None, brand_id: str | None = None) -> None:
    """Launched brands only (AC-L19) — an abandoned signup never buys collection.
    A sign-off (task_id + brand_id) always includes the signing brand: an
    explicit human request, even if it lands before launch.

    Lean collection (utils/collection_plan.py): per brand, the question levels
    (utils.question_tiers.questions_to_collect) pick the cells — core x the
    full market set with web search, long-tail x the small panel without —
    then the monthly budget governor trims them before they join the
    cross-brand, corpus-deduped queue. A sign-off's first fill rides the same
    plan, so it never fans out beyond the panel."""
    in_process = await _in_process_cells()
    brands = await Brand.launched()
    if brand_id is not None and all(brand.id != brand_id for brand in brands):
        brands.append(await Brand.get(brand_id))
    budget = await load_budget_context(clock.now())
    queue_for_llm: dict[str, dict[tuple[str, str], dict]] = {engine_name: {} for engine_name in settings.ACTIVE_ENGINES}

    now = clock.now()
    for brand in brands:
        # preview only: the rotation stamps (last_checked_at) are written below
        # for what was actually sent, so a long-tail pick the budget drops keeps
        # its turn instead of silently losing it
        plan = await questions_to_collect(brand.id, now, record=False)
        prompts_by_id = {prompt.id: prompt for prompt in await Prompt.by_brand(brand_id=brand.id)}
        markets = await Market.by_brand(brand_id=brand.id, brand_footprint=brand.footprint)
        small_markets = small_panel(markets, brand.footprint, settings.SMALL_PANEL_SIZE)
        planned = plan_cells(
            core_ids=[prompt_id for prompt_id in plan.core_ids if prompt_id in prompts_by_id],
            longtail_ids=[prompt_id for prompt_id in plan.longtail_ids if prompt_id in prompts_by_id],
            core_market_ids=[market.id for market in markets],
            small_market_ids=[market.id for market in small_markets],
        )
        candidates = await _stale_candidates(brand.id, {engine_name: planned for engine_name in settings.ACTIVE_ENGINES}, prompts_by_id, in_process, queue_for_llm, budget.rates)
        dropped_ids = _enqueue_within_budget(brand, candidates, prompts_by_id, queue_for_llm, budget)
        await Prompt.mark_checked(brand.id, checked_ids(plan.core_ids, plan.longtail_ids, dropped_ids), now)

    await _submit_engine_queues(queue_for_llm, task_id)


def checked_ids(core_ids: list[str], longtail_ids: list[str], dropped_ids: set[str]) -> list[str]:
    """The questions this run counts as checked: every core question, and the
    long-tail picks the budget didn't fully drop. Pure."""
    return list(core_ids) + [prompt_id for prompt_id in longtail_ids if prompt_id not in dropped_ids]


async def run_submit_reask_batch(task_id: str, brand_id: str) -> None:
    """Saturday borderline re-ask (one national brand, before Sunday's
    scoring): core questions whose share this week is 40-60% on an engine get
    asked on that engine in the next REASK_EXTRA_CITIES non-panel system
    cities. Local brands skip. Same freshness + budget rules as the weekly run."""
    brand = await Brand.get(brand_id)
    if brand.footprint == FOOTPRINT_LOCAL:
        logger.info(f"[collection] reask - brand {brand_id} is local; skipped")
        return
    # core = the is_active mirror of tier == core, read directly: calling
    # questions_to_collect here could advance the long-tail rotation (it picks
    # THIS run's slice) for a run that collects no long tail
    prompts = [prompt for prompt in await Prompt.by_brand(brand_id=brand.id) if prompt.retired_at is None]
    prompts_by_id = {prompt.id: prompt for prompt in prompts}
    core_ids = [prompt.id for prompt in prompts if prompt.is_active]
    pairs = borderline_pairs(await answer_stats.week_results(brand.id), core_ids, list(settings.ACTIVE_ENGINES))
    extra_markets = reask_cities(await Market.reask_pool(), settings.REASK_EXTRA_CITIES)
    if not pairs or not extra_markets:
        logger.info(f"[collection] reask - brand {brand_id}: {len(pairs)} borderline question x engine, {len(extra_markets)} extra cities; nothing to ask")
        return
    queue_for_llm: dict[str, dict[tuple[str, str], dict]] = {engine_name: {} for engine_name in settings.ACTIVE_ENGINES}
    budget = await load_budget_context(clock.now())
    planned_by_engine = reask_planned_cells(pairs, [market.id for market in extra_markets])
    candidates = await _stale_candidates(brand.id, planned_by_engine, prompts_by_id, await _in_process_cells(), queue_for_llm, budget.rates)
    _enqueue_within_budget(brand, candidates, prompts_by_id, queue_for_llm, budget)
    logger.info(f"[collection] reask - brand {brand_id}: {len(pairs)} borderline question x engine x {len(extra_markets)} extra cities")
    await _submit_engine_queues(queue_for_llm, task_id)


async def _stale_candidates(brand_id: str, planned_by_engine: dict[str, list[PlannedCell]], prompts_by_id: dict, in_process: dict[str, set[tuple[str, str]]], queue_for_llm: dict[str, dict[tuple[str, str], dict]], rates: AnswerRates) -> list[CandidateCell]:
    """The planned cells that still need a paid answer, priced. Skipped: a
    fresh result for the brand, a fresh corpus answer (a no-search one doesn't
    count for a web-search cell), an in-flight request. A cell another brand
    already queued this run costs this brand nothing (it subscribes)."""
    candidates: list[CandidateCell] = []
    hash_by_prompt_id = {prompt_id: query_hash(prompt.text) for prompt_id, prompt in prompts_by_id.items()}
    for engine_name, planned in planned_by_engine.items():
        if not planned or engine_name not in queue_for_llm:
            continue
        fresh_results = await PromptResult.within_freshness_window(brand_id=brand_id, engine=engine_name)
        fresh_cells = {(prompt_result.prompt_id, prompt_result.market_id) for prompt_result in fresh_results}
        stale = [cell for cell in planned if (cell.prompt_id, cell.market_id) not in fresh_cells]
        stale_hashes = {(hash_by_prompt_id[cell.prompt_id], cell.market_id) for cell in stale}
        fresh_corpus = await QueryResponse.within_freshness_window(stale_cells=stale_hashes, engine=engine_name) if stale_hashes else []
        corpus_by_cell = {(query_response.query_hash, query_response.market_id): query_response for query_response in fresh_corpus}
        engine_in_process = in_process.get(engine_name, set())
        engine_queue = queue_for_llm[engine_name]
        for cell in stale:
            key = (hash_by_prompt_id[cell.prompt_id], cell.market_id)
            corpus = corpus_by_cell.get(key)
            if corpus is not None and not (cell.web_search and is_no_search_answer(corpus.raw)):
                continue
            if key in engine_in_process:
                continue
            queued = engine_queue.get(key)
            rides_free = queued is not None and (queued["web_search"] or not cell.web_search)
            candidates.append(CandidateCell(cell.prompt_id, cell.market_id, engine_name, cell.web_search, cell.priority, 0.0 if rides_free else rates.usd(engine_name, cell.web_search)))
    return candidates


def _enqueue_within_budget(brand, candidates: list[CandidateCell], prompts_by_id: dict, queue_for_llm: dict[str, dict[tuple[str, str], dict]], budget: BudgetContext) -> set[str]:
    """Trim the brand's candidates to its remaining monthly budget (long-tail
    first, then the least important core cells), then queue the rest —
    deduped across brands by (query_hash, market) per engine."""
    cap = settings.COLLECTION_BUDGET_LOCAL_USD if brand.footprint == FOOTPRINT_LOCAL else settings.COLLECTION_BUDGET_NATIONAL_USD
    remaining = budget.remaining(brand.id, cap)
    kept, dropped = trim_to_budget(candidates, remaining)
    if dropped:
        dropped_longtail = sum(1 for cell in dropped if not cell.web_search)
        logger.warning(f"[collection] budget - brand {brand.id}: ${budget.spend_by_brand.get(brand.id, 0.0):.2f} of ${cap:.2f} spent this month; dropped {len(dropped)} of {len(candidates)} cells ({dropped_longtail} long-tail, {len(dropped) - dropped_longtail} core) to stay under the cap")
    for cell in kept:
        prompt = prompts_by_id[cell.prompt_id]
        key = (query_hash(prompt.text), cell.market_id)
        subscriber = dict(brand_id=brand.id, prompt_id=cell.prompt_id)
        queued = queue_for_llm[cell.engine].get(key)
        if queued is None:
            queue_for_llm[cell.engine][key] = dict(prompt_id=cell.prompt_id, market_id=cell.market_id, query_text=prompt.text, web_search=cell.web_search, subscribers=[subscriber])
        else:
            queued["web_search"] = queued["web_search"] or cell.web_search
            queued["subscribers"].append(subscriber)
    budget.spend_by_brand[brand.id] = budget.spend_by_brand.get(brand.id, 0.0) + sum(cell.usd for cell in kept)
    return fully_dropped_prompt_ids(kept, dropped)


def fully_dropped_prompt_ids(kept: list[CandidateCell], dropped: list[CandidateCell]) -> set[str]:
    """Questions whose every candidate cell the budget dropped (nothing of
    theirs was sent this run). Pure."""
    kept_ids = {cell.prompt_id for cell in kept}
    return {cell.prompt_id for cell in dropped} - kept_ids


async def _submit_engine_queues(queue_for_llm: dict[str, dict[tuple[str, str], dict]], task_id: str | None) -> None:
    """One provider batch per engine per search mode: web-search cells and
    no-search (long-tail) cells never share a batch, so each batch's
    cost_event prices one kind of answer. Same corpus key either way."""
    markets_by_id = {market.id: market for market in await Market.all()}
    attempted: list[str] = []
    failed: list[str] = []

    for engine_name in settings.ACTIVE_ENGINES:
        for web_search in (True, False):
            query_requests: list[QueryRequest] = []
            request_metadata: dict = {}
            for (query_hash_value, market_id), cell in queue_for_llm[engine_name].items():
                if cell["web_search"] != web_search:
                    continue
                market = markets_by_id.get(market_id)
                location = LocationContext(city=market.city, state=market.state, zip=market.zip) if market.source != SOURCE_NATIONAL else None
                custom_id = str(len(query_requests))
                request_metadata[custom_id] = {
                    "query_hash": query_hash_value,
                    "query_text": cell["query_text"],
                    "prompt_id": cell["prompt_id"],
                    "market_id": market.id,
                    WEB_SEARCH_KEY: web_search,
                    "subscribers": cell["subscribers"],
                }
                query_requests.append(QueryRequest(custom_id=custom_id, prompt=cell["query_text"], location=location, use_web_search=None if web_search else False))

            if not query_requests:
                continue
            label = f"{engine_name}{'' if web_search else ' (no web search)'}"
            attempted.append(label)
            try:
                batch_handle = await initialize_adapter(adapter=f"{engine_name}_service").submit_batch(query_requests)
            except Exception as exc:
                failed.append(label)
                logger.error(f"[collection] submit_prompt_execution_batch - {label}: submit failed for {len(query_requests)} requests: {type(exc).__name__}: {str(exc)[:300]}")
                continue
            await Batch.create(
                provider=batch_handle.provider,
                batch_id=batch_handle.batch_id,
                brand_id=None,
                status=batch_handle.status,
                request_metadata=request_metadata,
                kind=UsageKind.EXECUTE_PROMPTS,
                task_id=task_id,
            )
            await Database.session.commit()

    if failed and len(failed) == len(attempted):
        raise RuntimeError(f"submit_prompt_execution_batch: every engine failed to submit ({', '.join(failed)})")


async def _in_process_cells() -> dict[str, set[tuple[str, str]]]:
    in_process: dict[str, set[tuple[str, str]]] = {}
    for batch in await Batch.unprocessed():
        if batch.kind != UsageKind.EXECUTE_PROMPTS:
            continue
        metadata = batch.request_metadata or {}
        batch_engine = batch.provider
        for custom_id, result_metadata in metadata.items():
            if custom_id.startswith("__") or not isinstance(result_metadata, dict) or "market_id" not in result_metadata:
                continue
            in_process.setdefault(batch_engine, set()).add((result_metadata["query_hash"], result_metadata["market_id"]))
    return in_process


##############################
# BATCH POLL
##############################


async def run_poll_batches() -> None:
    """
    1. Poll all unprocessed batches (execution or sentiment)
    2. For each batch, check its status with the provider
    3. If the batch is still in-flight, update its status and continue
    4. If the batch is ended, fetch its results and ingest them
    5. If the batch is an execution batch, ingest the results into the corpus,
    fan them out to every subscribing brand, and submit one sentiment batch
    per subscribing brand for its still-unanalyzed results
    6. If the batch is a sentiment batch, persist the analysis (sentiment,
    positions, competitor mentions) - the rows are then report-ready
    7. Mark the batch processed and commit (each batch is an atomic unit of
    work)
    """
    batches = await Batch.unprocessed()

    for batch in batches:
        engine = initialize_adapter(adapter=f"{batch.provider}_service")
        status = await engine.get_batch_status(batch.batch_id)

        if status not in engine.TERMINAL_STATUSES:
            await Batch.update_status(batch.id, status)
            await Database.session.commit()
            continue

        results = await engine.get_batch_results(batch.batch_id)
        if results:
            usage = summarize_usage(results)
            await CostEvent.create(
                adapter=batch.provider,
                operation=batch.kind,
                organization_id=await _batch_cost_organization_id(batch),
                brand_id=batch.brand_id,
                task_id=batch.task_id,
                model=engine.model,
                input_tokens=usage["input_tokens"] + usage["cache_write_tokens"] + usage["cache_read_tokens"],
                output_tokens=usage["output_tokens"],
                web_search_count=usage["web_searches"],
                usd=estimate_cost(batch.provider, engine.model, usage["input_tokens"], usage["output_tokens"], usage["web_searches"], batch=True, cache_write_tokens=usage["cache_write_tokens"], cache_read_tokens=usage["cache_read_tokens"]),
            )
        # sentiment -> create prompt results
        if batch.kind == UsageKind.SENTIMENT_ANALYSIS.value:
            await ingest_sentiment_batch(batch, results)
        # execution -> create query_responses -> submit sentiment batch
        else:
            await ingest_execution_batch(batch, results)
        await Batch.mark_processed(batch.id, status=status)
        await Database.session.commit()


async def _batch_cost_organization_id(batch: Batch) -> str | None:
    """Sentiment packs are one brand's spend -> its org. Execution batches are
    brand-less corpus collection (shared COGS) and stay org-NULL."""
    if batch.kind != UsageKind.SENTIMENT_ANALYSIS.value or not batch.brand_id:
        return None
    return (await Brand.get(batch.brand_id)).organization_id


async def ingest_sentiment_batch(batch: Batch, results: list[QueryResult]) -> None:
    sentiment_batch_outputs: list[tuple[dict, list[str], str]] = []
    prompt_result_ids_with_mentions: set[str] = set()
    for result in results:
        if result.error or result.structured is None:
            continue
        result_metadata = batch.request_metadata[result.custom_id]
        sentiment_batch_outputs.append((result.structured, result_metadata["prompt_result_ids"], result_metadata.get("pack_kind", UsageKind.SENTIMENT_ANALYSIS.value)))
        prompt_result_ids_with_mentions.update(result_metadata["prompt_result_ids"])
    sentiment_by_prompt_result_id, competitors_discovered = _collect_sentiment_batch_outputs(sentiment_batch_outputs)

    prompt_results_with_sentiment = [await PromptResult.get(prompt_result_id) for prompt_result_id in prompt_result_ids_with_mentions]
    in_process = await _prompt_result_ids_in_process()

    # fmt: off
    silent: list[PromptResult] = [
        prompt_result for prompt_result in await PromptResult.filter(brand_id=batch.brand_id, contains_sentiment_analysis=False)
        if prompt_result.id not in prompt_result_ids_with_mentions
        and prompt_result.id not in in_process
    ]
    # fmt: on

    brand = (await Brand.get(batch.brand_id)).model_dump()
    await _analyze_prompt_results(brand, [*prompt_results_with_sentiment, *silent], sentiment_by_prompt_result_id, competitors_discovered)


def _collect_sentiment_batch_outputs(responses_with_ids: list[tuple[dict | None, list[str], str]]) -> tuple[dict[str, dict], list[str]]:
    """Example input: (result.structured, meta["prompt_result_ids"], meta.get("pack_kind", UsageKind.SENTIMENT_ANALYSIS.value))"""
    sentiment_by_prompt_result_id: dict[str, dict] = {}
    competitors_discovered: list[str] = []
    for structured_outputs, prompt_result_ids, pack_kind in responses_with_ids:
        for prompt_result_id, structured_output in _map_prompt_result_ids_to_structured_outputs(structured_outputs, prompt_result_ids):
            sentiment_by_prompt_result_id[prompt_result_id] = structured_output
            if pack_kind == UsageKind.COMPETITOR_DISCOVERY:
                competitors_discovered.extend(mention["name"] for mention in structured_output.get("competitor_mentions", []))
    return sentiment_by_prompt_result_id, competitors_discovered


def _map_prompt_result_ids_to_structured_outputs(structured: dict | None, prompt_result_ids: list[str]) -> list[tuple[str, dict]]:
    """Map a pack's structured output back to prompt_result ids via the echoed
    index. Items with a missing/out-of-range index are skipped (a miscounted
    item fails alone, not the whole pack)."""
    pairs: list[tuple[str, dict]] = []
    for pack_result in (structured or {}).get("results", []):
        index = pack_result.get("index")
        if isinstance(index, int) and 0 <= index < len(prompt_result_ids):
            pairs.append((prompt_result_ids[index], pack_result))
        else:
            logger.error(f"sentiment pack item with invalid index {index!r} skipped")
    return pairs


async def _prompt_result_ids_in_process() -> set[str]:
    in_process: set[str] = set()
    for batch in await Batch.unprocessed(kind=UsageKind.SENTIMENT_ANALYSIS):
        for result_metadata in (batch.request_metadata or {}).values():
            if isinstance(result_metadata, dict) and "prompt_result_ids" in result_metadata:
                in_process.update(result_metadata["prompt_result_ids"])
    return in_process


async def _analyze_prompt_results(brand: dict, prompt_results: list[PromptResult], sentiment_by_id: dict[str, dict], competitors_discovered: list[str]) -> dict[str, PromptResultWithCompetitorMentions]:
    for competitor_name in competitors_discovered:
        if not Competitor.is_own_brand(brand["name"], competitor_name):
            await Competitor.upsert(brand_id=brand["id"], name=competitor_name, source=SOURCE_DISCOVERED)
    competitor_regex_patterns = build_competitor_regex_patterns(await Competitor.get_competitors_by_brand_id(brand["id"]))
    excluded_names = await Competitor.excluded_names(brand["id"])

    prompt_results_by_id: dict[str, PromptResultWithCompetitorMentions] = {}

    for prompt_result in prompt_results:
        sentiment_structured_output = sentiment_by_id.get(prompt_result.id)
        mention_rank = parse_prompt_result_for_mention_rank(prompt_result.text, [brand["name"]])[brand["name"]]

        # rank: the free string parse wins; the pack's brand_mention_position
        # is the fallback for answers the list parser couldn't rank. Top-3
        # is DERIVED from rank downstream (rank <= 3), not stored.
        if mention_rank is not None:
            rank = mention_rank
        elif sentiment_structured_output is not None:
            rank = sentiment_structured_output["brand_mention_position"]
        else:
            rank = None

        # sentiment verdict: the pack only distinguishes positive / not
        # positive; no pack verdict (silent majority) stays None. Either
        # way analyzed_at is stamped — analysis is final.
        if sentiment_structured_output is not None:
            sentiment = SENTIMENT_POSITIVE if sentiment_structured_output.get("positive_sentiment") else SENTIMENT_NEUTRAL
        else:
            sentiment = None

        # fmt: off
        competitor_mention_ranks = [
            (name, mention_rank) for name, mention_rank
            in parse_prompt_result_for_competitor_mention_rank(prompt_result.text, competitor_regex_patterns, exclude=brand["name"])
            if not Competitor.is_own_brand(brand["name"], name)
            and Competitor.normalize_name(name) not in excluded_names
        ]
        # fmt: on

        analyzed_prompt_result = await PromptResult.update(id=prompt_result.id, rank=rank, sentiment=sentiment)

        mentions = []
        for name, mention_rank in competitor_mention_ranks:
            created_competitor = await Competitor.upsert(brand_id=brand["id"], name=name, source=SOURCE_DISCOVERED)
            mentions.append(
                await CompetitorMention.create(
                    prompt_result_id=prompt_result.id,
                    name=name,
                    normalized_name=created_competitor.normalized_name,
                    competitor_id=created_competitor.id,
                    position=mention_rank,
                )
            )

        prompt_results_by_id[prompt_result.id] = hydrate_prompt_result(analyzed_prompt_result, mentions)

    return prompt_results_by_id


async def ingest_execution_batch(batch: Batch, results: list[QueryResult]) -> None:
    from dataclasses import asdict

    subscribed_brand_ids: set[str] = set()
    for result in results:
        if result.error:
            continue
        result_metadata = batch.request_metadata[result.custom_id]
        market = await Market.get(result_metadata["market_id"])
        raw = result.raw or {}
        if result_metadata.get(WEB_SEARCH_KEY) is False:
            # a long-tail answer collected without web search: same corpus key, marked so core cells don't reuse it
            raw = {**raw, CORPUS_RAW_KEY: {WEB_SEARCH_KEY: False}}
        query_response = await QueryResponse.create(
            query_hash=result_metadata["query_hash"],
            query_text=result_metadata["query_text"],
            market=market,
            engine=batch.provider,
            text=result.text,
            raw=raw,
            citations=[asdict(citation) for citation in (result.citations or [])],
        )
        for subscriber in result_metadata["subscribers"]:
            brand = (await Brand.get(subscriber["brand_id"])).model_dump()
            await _create_prompt_result_from_query_response(brand, subscriber["prompt_id"], query_response, market_id=market.id)
            subscribed_brand_ids.add(subscriber["brand_id"])

    for subscribed_brand_id in sorted(subscribed_brand_ids):
        brand = (await Brand.get(subscribed_brand_id)).model_dump()
        await _submit_analysis_batch(brand)


async def _create_prompt_result_from_query_response(brand: dict, prompt_id: str, query_response: QueryResponse, market_id: str | None = None) -> PromptResult:
    citations = query_response.citations or []
    prompt_result = await PromptResult.create(
        prompt_id=prompt_id,
        market_id=market_id or query_response.market_id,
        brand_id=brand["id"],
        engine=query_response.engine,
        text=query_response.text,
        raw=json.dumps(query_response.raw),
        brand_cited=_is_brand_cited(brand["name"], brand.get("website_url") or "", citations),
        brand_mentioned=mentions_name(query_response.text, brand["name"]),
        query_response_id=query_response.id,
    )
    unique_citations: set[tuple[str, str]] = set()
    for citation in citations:
        citation_identity = (citation.get("url") or "", citation.get("cited_text") or "")
        if citation_identity in unique_citations:
            continue
        unique_citations.add(citation_identity)
        await Citation.create(
            brand_id=brand["id"],
            prompt_id=prompt_id,
            prompt_result_id=prompt_result.id,
            cited_text=citation.get("cited_text") or "",
            title=citation.get("title") or "",
            type=citation.get("type") or "",
            url=citation.get("url") or "",
            domain=citation.get("domain") or None,
        )
    return prompt_result


def _site_host(url: str) -> str:
    return domain_from_url(url).lower().removeprefix("www.")


def _is_brand_cited(brand_name: str, website_url: str, citations: list[dict]) -> bool:
    """A citation names the brand (whole word, like brand_mentioned) or points at the brand's own site."""
    brand_host = _site_host(website_url) if website_url else ""
    for citation in citations:
        if mentions_name(citation.get("cited_text") or "", brand_name):
            return True
        if brand_host and _site_host(citation.get("url") or citation.get("domain") or "") == brand_host:
            return True
    return False


async def _submit_analysis_batch(brand: dict) -> None:
    in_process = await _prompt_result_ids_in_process()

    prompt_results_pending_analysis: list[PromptResult] = [prompt_result for prompt_result in await PromptResult.filter(brand_id=brand["id"], contains_sentiment_analysis=False) if prompt_result.id not in in_process]

    prompt_results_for_sentiment_analysis, prompt_results_for_competitor_discovery = _categorize_into_sentiment_or_competitor_discovery_analysis(prompt_results_pending_analysis)
    prompt_results_pending_analysis_ids = {prompt_result.id for prompt_result in [*prompt_results_for_sentiment_analysis, *prompt_results_for_competitor_discovery]}
    prompt_results_without_brand_mentions = [prompt_result for prompt_result in prompt_results_pending_analysis if prompt_result.id not in prompt_results_pending_analysis_ids]

    # analyze to prevent memory leak at `prompt_results_pending_analysis`
    if prompt_results_without_brand_mentions:
        await _analyze_prompt_results(brand, prompt_results_without_brand_mentions, {}, [])

    # multiple prompt results analyzed in a single response
    query_requests: list[QueryRequest] = []
    request_metadata: dict = {}

    for prompt_results, pack_kind, system_prompt, schema in (
        (prompt_results_for_sentiment_analysis, UsageKind.SENTIMENT_ANALYSIS.value, SENTIMENT_ANALYSIS_SYSTEM_PROMPT, SENTIMENT_ANALYSIS_SCHEMA),
        (prompt_results_for_competitor_discovery, UsageKind.COMPETITOR_DISCOVERY.value, COMPETITOR_DISCOVERY_SYSTEM_PROMPT, COMPETITOR_DISCOVERY_SCHEMA),
    ):
        for chunk in _chunk_prompt_results(prompt_results):
            custom_id = str(len(query_requests))
            request_metadata[custom_id] = {"brand_id": brand["id"], "prompt_result_ids": [prompt_result.id for prompt_result in chunk], "pack_kind": pack_kind}
            query_requests.append(
                QueryRequest(
                    custom_id=custom_id,
                    prompt=_build_sentiment_analysis_prompt(brand["name"], chunk),
                    system=system_prompt,
                    schema=schema,
                    cache_system=True,
                )
            )

    if not query_requests:
        logger.info(f"brand {brand['id']} has no unanalyzed prompt results; skipping sentiment batch submission")
        return

    handle = await ClaudeService(use_web_search=False, max_tokens=SENTIMENT_PACK_MAX_TOKENS).submit_batch(query_requests)
    await Batch.create(provider=handle.provider, batch_id=handle.batch_id, brand_id=brand["id"], status=handle.status, request_metadata=request_metadata, kind=UsageKind.SENTIMENT_ANALYSIS)


def _is_discovery_sample(prompt_result_id: str) -> bool:
    """Deterministic sampling: ~5% get LLM competitor discovery"""
    return int(hashlib.md5(prompt_result_id.encode()).hexdigest(), 16) % DISCOVERY_SAMPLE_MODULO == 0


def _categorize_into_sentiment_or_competitor_discovery_analysis(prompt_results: list[PromptResult]) -> tuple[list[PromptResult], list[PromptResult]]:
    prompts_for_competitor_discovery = [prompt_result for prompt_result in prompt_results if _is_discovery_sample(prompt_result.id)]
    prompts_for_competitor_discovery_ids = {prompt_result.id for prompt_result in prompts_for_competitor_discovery}
    # prompts_for_competitor_discovery already contain the sentiment analysis
    prompts_for_sentiment_analysis = [prompt_result for prompt_result in prompt_results if prompt_result.brand_mentioned and prompt_result.id not in prompts_for_competitor_discovery_ids]
    return prompts_for_sentiment_analysis, prompts_for_competitor_discovery


def _chunk_prompt_results(prompt_results: list[PromptResult]) -> list[list[PromptResult]]:
    packs = []
    for start in range(0, len(prompt_results), SENTIMENT_PACK_SIZE):
        end = start + SENTIMENT_PACK_SIZE
        packs.append(prompt_results[start:end])
    return packs


def _build_sentiment_analysis_prompt(brand_name: str, prompt_results: list[PromptResult]) -> str:
    sections = [f"Brand: {brand_name}"]
    for index, prompt_result in enumerate(prompt_results):
        sections.append(f"### Response {index}\n{prompt_result.text[:RESPONSE_TRUNCATE_CHARS]}")
    return "\n\n".join(sections)
