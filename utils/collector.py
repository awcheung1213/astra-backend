from utils.database import Database
from utils.llm_usage import estimate_cost, summarize_usage
from objects.batch import Batch
from adapters.registry import initialize_adapter
from constants.constants import UsageKind
import settings
from schemas.provider_schemas import QueryRequest, QueryResult, LocationContext
from objects.brand import Brand
from objects.competitor import Competitor
from objects.competitor_mention import CompetitorMention
from objects.cost_event import CostEvent
from objects.prompt import Prompt
from objects.market import Market
from objects.prompt_result import PromptResult
from objects.query_response import QueryResponse
from schemas.prompt_result_schemas import PromptResultWithCompetitorMentions
from utils.query_normalization import query_hash
from utils.logger import logger
from utils.competitor_matching import build_competitor_regex_patterns, parse_prompt_result_for_competitor_mention_rank
from utils.response_parsing import parse_prompt_result_for_mention_rank


class Collector:

    ##############################
    # BATCH SUBMISSION
    ##############################

    @staticmethod
    async def submit_prompt_execution_batch() -> None:
        in_process = await Collector._in_process_cells()
        brands = await Brand.all()
        queue_for_llm: dict[str, dict[tuple[str, str], dict]] = {engine_name: {} for engine_name in settings.ACTIVE_ENGINES}

        for brand in brands:
            active_prompts = [prompt for prompt in await Prompt.by_brand(brand_id=brand.id) if prompt.is_active]
            markets = await Market.by_brand(brand_id=brand.id, brand_footprint=brand.footprint)
            cells = set((prompt.id, market.id) for prompt in active_prompts for market in markets)
            hash_by_prompt_id = {prompt.id: dict(hash=query_hash(prompt.text), text=prompt.text) for prompt in active_prompts}

            for engine_name in settings.ACTIVE_ENGINES:
                engine_queue = queue_for_llm[engine_name]

                prompt_results_within_freshness_window = await PromptResult.within_freshness_window(brand_id=brand.id, engine=engine_name)
                prompt_result_cells_within_freshness_window = set((prompt_result.prompt_id, prompt_result.market_id) for prompt_result in prompt_results_within_freshness_window)

                stale_cells_by_id = cells - prompt_result_cells_within_freshness_window
                stale_cells_by_hashes = set((hash_by_prompt_id[prompt_id]["hash"], market_id) for prompt_id, market_id in stale_cells_by_id)

                query_responses_within_freshness_window = await QueryResponse.within_freshness_window(stale_cells=stale_cells_by_hashes, engine=engine_name)

                query_response_by_cell = {(query_response.query_hash, query_response.market_id): query_response for query_response in query_responses_within_freshness_window}
                for prompt_id, market_id in stale_cells_by_id:
                    query_response = query_response_by_cell.get((hash_by_prompt_id[prompt_id]["hash"], market_id))
                    if query_response is not None:
                        continue
                    if (hash_by_prompt_id[prompt_id]["hash"], market_id) in in_process.get(engine_name, set()):
                        continue
                    if (hash_by_prompt_id[prompt_id]["hash"], market_id) not in engine_queue:
                        engine_queue[(hash_by_prompt_id[prompt_id]["hash"], market_id)] = dict(
                            prompt_id=prompt_id,
                            market_id=market_id,
                            query_text=hash_by_prompt_id[prompt_id]["text"],
                            subscribers=[
                                dict(
                                    brand_id=brand.id,
                                    prompt_id=prompt_id,
                                )
                            ],
                        )
                    else:
                        engine_queue[(hash_by_prompt_id[prompt_id]["hash"], market_id)]["subscribers"].append(
                            dict(
                                brand_id=brand.id,
                                prompt_id=prompt_id,
                            )
                        )

        markets_by_id = {market.id: market for market in await Market.all()}

        for engine_name in settings.ACTIVE_ENGINES:
            tasks: list[QueryRequest] = []
            request_metadata: dict = {}
            for (hash, market_id), cell in queue_for_llm[engine_name].items():
                market = markets_by_id.get(market_id)
                location = LocationContext(city=market.city, state=market.state, zip=market.zip) if market.source != "national" else None
                custom_id = str(len(tasks))
                request_metadata[custom_id] = {
                    "query_hash": hash,
                    "query_text": cell["query_text"],
                    "prompt_id": cell["prompt_id"],
                    "market_id": market.id,
                    "subscribers": cell["subscribers"],
                }
                tasks.append(QueryRequest(custom_id=custom_id, prompt=cell["query_text"], location=location))

            if not tasks:
                continue

            batch_handle = await initialize_adapter(adapter=f"{engine_name}_service").submit_batch(tasks)
            await Batch.create(
                provider=batch_handle.provider,
                batch_id=batch_handle.batch_id,
                brand_id=None,
                status=batch_handle.status,
                request_metadata=request_metadata,
                kind=UsageKind.EXECUTE_PROMPTS,
            )
            await Database.session.commit()

    @staticmethod
    async def _in_process_cells() -> dict[str, set[tuple[str, str]]]:
        in_process: dict[str, set[tuple[str, str]]] = {}
        for batch in await Batch.unprocessed():
            if batch.kind != UsageKind.EXECUTE_PROMPTS:
                continue
            metadata = batch.request_metadata or {}
            batch_engine = batch.provider
            for key, meta in metadata.items():
                if key.startswith("__") or not isinstance(meta, dict) or "market_id" not in meta:
                    continue
                in_process.setdefault(batch_engine, set()).add((meta["query_hash"], meta["market_id"]))
        return in_process

    ##############################
    # BATCH POLL
    ##############################

    @staticmethod
    async def poll_batches() -> None:
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
                await CostEvent.create(
                    adapter=batch.provider,
                    operation=batch.kind,
                    brand_id=batch.brand_id,
                    task_id=batch.task_id,
                    model=engine.model,
                    input_tokens=batch.usage_summary["input_tokens"],
                    output_tokens=batch.usage_summary["output_tokens"],
                    web_search_count=batch.usage_summary["web_searches"],
                    usd=estimate_cost(batch.provider, engine.model, batch.usage_summary["input_tokens"], batch.usage_summary["output_tokens"], batch.usage_summary["web_searches"], batch=True),
                )
            # sentiment -> create prompt results
            if batch.kind == UsageKind.SENTIMENT_ANALYSIS.value:
                await Collector._ingest_sentiment_batch(batch, results)
            # execution -> create query_responses -> submit sentiment batch
            else:
                await _ingest_execution_batch(batch, results)
            await Batch.mark_processed(batch.id, status=status)
            await Database.session.commit()

    @staticmethod
    async def _ingest_sentiment_batch(batch: Batch, results: list[QueryResult]) -> None:
        sentiment_batch_outputs: list[tuple[dict, list[str], str]] = []
        prompt_result_ids_with_mentions: set[str] = set()
        for result in results:
            if result.error or result.structured is None:
                continue
            meta = batch.request_metadata[result.custom_id]
            sentiment_batch_outputs.append((result.structured, meta["prompt_result_ids"], meta.get("pack_kind", UsageKind.SENTIMENT_ANALYSIS.value)))
            prompt_result_ids_with_mentions.update(meta["prompt_result_ids"])
        sentiment_by_prompt_result_id, competitors_discovered = Collector._collect_sentiment_batch_outputs(sentiment_batch_outputs)

        prompt_results_with_sentiment = [await PromptResult.get(prompt_result_id) for prompt_result_id in prompt_result_ids_with_mentions]
        in_process = await Collector._prompt_result_ids_in_process()

        # fmt: off
        silent: list[PromptResult] = [
            prompt_result for prompt_result in await PromptResult.filter(brand_id=batch.brand_id, contains_sentiment_analysis=False)
            if prompt_result.id not in prompt_result_ids_with_mentions
            and prompt_result.id not in in_process
        ]
        # fmt: on

        brand = (await Brand.get(batch.brand_id)).model_dump()
        await Collector._analyze_prompt_results(brand, [*prompt_results_with_sentiment, *silent], sentiment_by_prompt_result_id, competitors_discovered)

    @staticmethod
    def _collect_sentiment_batch_outputs(responses_with_ids: list[tuple[dict | None, list[str], str]]) -> tuple[dict[str, dict], list[str]]:
        """Example input: (result.structured, meta["prompt_result_ids"], meta.get("pack_kind", UsageKind.SENTIMENT_ANALYSIS.value))"""
        sentiment_by_prompt_result_id: dict[str, dict] = {}
        competitors_discovered: list[str] = []
        for structured_outputs, prompt_result_ids, pack_kind in responses_with_ids:
            for prompt_result_id, structured_output in Collector._map_prompt_result_ids_to_structured_outputs(structured_outputs, prompt_result_ids):
                sentiment_by_prompt_result_id[prompt_result_id] = structured_output
                if pack_kind == UsageKind.COMPETITOR_DISCOVERY.value:
                    competitors_discovered.extend(mention["name"] for mention in structured_output.get("competitor_mentions", []))
        return sentiment_by_prompt_result_id, competitors_discovered

    @staticmethod
    def _map_prompt_result_ids_to_structured_outputs(structured: dict | None, prompt_result_ids: list[str]) -> list[tuple[str, dict]]:
        """Map a pack's structured output back to prompt_result ids via the echoed
        index. Items with a missing/out-of-range index are skipped (a miscounted
        item fails alone, not the whole pack)."""
        pairs: list[tuple[str, dict]] = []
        for item in (structured or {}).get("results", []):
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(prompt_result_ids):
                pairs.append((prompt_result_ids[index], item))
            else:
                logger.error(f"sentiment pack item with invalid index {index!r} skipped")
        return pairs

    @staticmethod
    async def _prompt_result_ids_in_process() -> set[str]:
        in_process: set[str] = set()
        for batch in await Batch.unprocessed(kind=UsageKind.SENTIMENT_ANALYSIS):
            for meta in (batch.request_metadata or {}).values():
                if isinstance(meta, dict) and "prompt_result_ids" in meta:
                    in_process.update(meta["prompt_result_ids"])
        return in_process

    @staticmethod
    async def _analyze_prompt_results(brand: dict, prompt_results: list[PromptResult], sentiment_by_id: dict[str, dict], competitors_discovered: list[str]) -> dict[str, PromptResultWithCompetitorMentions]:
        for competitor_name in competitors_discovered:
            if not Competitor.is_own_brand(brand["name"], competitor_name):
                await Competitor.upsert(brand_id=brand["id"], name=competitor_name)
        competitor_regex_patterns = build_competitor_regex_patterns(await Competitor.get_competitors_by_brand_id(brand["id"]))
        excluded_names = await Competitor.excluded_names(brand["id"])

        prompt_results_by_id: dict[str, PromptResultWithCompetitorMentions] = {}
        parsed_count = 0

        for prompt_result in prompt_results:
            sentiment_structured_output = sentiment_by_id.get(prompt_result.id)
            mention_rank = parse_prompt_result_for_mention_rank(prompt_result.text, [brand["name"]])[brand["name"]]

            if mention_rank is not None:
                parsed_count += 1
                position = mention_rank
                top_3 = mention_rank <= 3
            elif sentiment_structured_output is not None:
                position = sentiment_structured_output["brand_mention_position"]
                top_3 = sentiment_structured_output["top_3_position"]
            else:
                position = None
                top_3 = False

            # fmt: off
            competitor_mention_ranks = [
                (name, mention_rank) for name, mention_rank
                in parse_prompt_result_for_competitor_mention_rank(prompt_result.text, competitor_regex_patterns, exclude=brand["name"])
                if not Competitor.is_own_brand(brand["name"], name)
                and Competitor.normalize_name(name) not in excluded_names
            ]
            # fmt: on

            prompt_result_with_sentiment = await PromptResult.update(
                id=prompt_result.id,
                brand_mention_position=position,
                positive_sentiment=bool(sentiment_structured_output["positive_sentiment"]) if sentiment_structured_output is not None else False,
                top_3_position=top_3,
                competitor_mention_count=len(competitor_mention_ranks),
            )

            prompt_result_with_competitor_mentions = PromptResultWithCompetitorMentions(
                **prompt_result_with_sentiment.model_dump(),
                competitor_mentions=[],
            )

            for name, mention_rank in competitor_mention_ranks:
                created_competitor = await Competitor.upsert(brand_id=brand["id"], name=name)
                result_competitor_mention = await CompetitorMention.create(
                    prompt_result_id=prompt_result.id,
                    name=name,
                    normalized_name=created_competitor.normalized_name,
                    competitor_id=created_competitor.id,
                    position=mention_rank,
                )
                prompt_result_with_competitor_mentions.competitor_mentions.append(result_competitor_mention)

            prompt_results_by_id[prompt_result_with_competitor_mentions.id] = prompt_result_with_competitor_mentions

        return prompt_results_by_id