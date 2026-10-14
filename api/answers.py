"""The AI answers one-question screen: per assistant, did this week's answers
name the brand, at what rank, among how many businesses — plus one real
answer (city + date) and who else it named. Aggregation lives in
utils/answer_stats.py (the owner rule, pure + tested); this router only loads
and hands over. Read-only: corpus rows are looked up, never written.

Auth rides the router-level guard (utils/authz.py): the brand in the path must
belong to the caller's org; a prompt that isn't that brand's is a 404."""

from fastapi import APIRouter, HTTPException, status

import settings
from objects.brand import Brand
from objects.prompt import Prompt
from utils import answer_stats

router = APIRouter(prefix="/api/v1")


@router.get("/brands/{brand_id}/questions/{prompt_id}/answers")
async def question_answers(brand_id: str, prompt_id: str) -> dict:
    brand = await Brand.get(brand_id)
    prompt = await Prompt.get_for_brand(brand.id, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="question not found")

    engines = answer_stats.display_engines(settings.ACTIVE_ENGINES)
    results = [result for result in await answer_stats.week_results(brand.id, prompt_id=prompt.id) if result.engine in engines]
    results_by_engine: dict[str, list] = {}
    for result in results:
        results_by_engine.setdefault(result.engine, []).append(result)

    mentions_by_result_id = await answer_stats.mentions_by_result(brand.id, results)
    market_order, markets = await answer_stats.market_preference(brand.id, {result.market_id for result in results})
    corpus_by_id = {}
    for engine in engines:
        sample = answer_stats.sample_result(results_by_engine.get(engine, []), market_order)
        if sample is not None:
            answer = await answer_stats.corpus_answer(sample.query_response_id)
            if answer is not None:
                corpus_by_id[answer.id] = answer

    week = answer_stats.week_of(results)
    return {
        "prompt": {"id": prompt.id, "topic": prompt.topic, "text": prompt.text},
        "week_of": week.isoformat() if week else None,
        "engines": [answer_stats.engine_answer_row(engine, results_by_engine.get(engine, []), market_order, mentions_by_result_id, markets, corpus_by_id) for engine in engines],
    }
