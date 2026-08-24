from utils.database import Database, ReplicaDatabase
from objects.batch import Batch
from constants.constants import UsageKind
import settings
from objects.brand import Brand
from objects.prompt import Prompt
from objects.market import Market
from objects.prompt_result import PromptResult
from utils.query_normalization import query_hash


async def submit_prompt_execution_batch():
    async with ReplicaDatabase().connection():
        async with Database().connection():
            in_process = await _in_process_cells()
            brands = await Brand.all()
            queue_for_llm: dict[str, dict[tuple[str, str], dict]] = {engine_name: {} for engine_name in settings.ACTIVE_ENGINES}
    
            for brand in brands:
                active_prompts = [prompt for prompt in await Prompt.by_brand(brand_id=brand.id) if prompt.is_active]
                markets = await Market.by_brand(brand_id=brand.id, brand_footprint=brand.footprint)
                cells = set((prompt.id, market.id) for prompt in active_prompts for market in markets)
                hash_by_prompt_id = {prompt.id: dict(hash=query_hash(prompt.text), text=prompt.text) for prompt in active_prompts}

                for engine_name in settings.ACTIVE_ENGINES:
                    engine_queue = queue_for_llm[engine_name]

                    prompt_results_within_freshness_window = await PromptResult.within_freshness_window(brand_id=brand.id, engine=engine_name, freshness_window_hours=freshness_window_hours)
                    prompt_result_cells_within_freshness_window = set((prompt_result.prompt_id, prompt_result.market_id) for prompt_result in prompt_results_within_freshness_window)

                    stale_cells_by_id = cells - prompt_result_cells_within_freshness_window
                    stale_cells_by_hashes = set((hash_by_prompt_id[prompt_id]["hash"], market_id) for prompt_id, market_id in stale_cells_by_id)

                    query_responses_within_freshness_window = await QueryResponse.within_freshness_window(stale_cells=stale_cells_by_hashes, engine=engine_name, freshness_window_hours=freshness_window_hours)

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
