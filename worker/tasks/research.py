"""Kickoff research (deliverable #4, and #5's bootstrap half): the one-shot
analysis tasks the onboarding launch enqueues (api/onboarding.py is the
producer; plan updates re-fire #4 when priorities shift).

Both are single no-tool agent calls over the brand's confirmed context —
generation, not judgment — so they use task-specific system prompts (static,
so prompt caching applies) rather than one of the seven roles."""

from datetime import date, timedelta

from adapters.agents.claude_agent import ClaudeAgent
from adapters.google_adapter import GoogleAdapter, GoogleReadError
from objects.brand import Brand
from objects.competitor import Competitor
from objects.connection import Connection
from objects.prompt import Prompt
from utils.agent_json import parse_agent_json
from utils.logger import logger
from utils.query_normalization import query_hash
from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="research_prompts", queue="research", retry=2)
async def research_prompts(task_id: str, brand_id: str) -> None:
    """#4 — AI-prompt research -> the brand's target query list. Generated
    prompts land INACTIVE (source=generated) for human sign-off; activation
    is the prompt sign-off endpoint's job (which also starts the #6 baseline
    collection). Re-runs dedupe against existing prompt text, so a plan-update
    re-fire only ADDS."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        existing_prompts = await Prompt.by_brand(brand_id)
        existing_query_hashes = {query_hash(existing.text) for existing in existing_prompts}
        real_queries = await _gsc_top_queries(brand)

        agent_output = await ClaudeAgent().run_agent(
            system=RESEARCH_PROMPTS_SYSTEM,
            prompt=_research_prompt(brand, existing_prompts, real_queries),
            tools=[],
            handlers={},
            operation="research_prompts",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        generated = parse_agent_json(agent_output)
        if not isinstance(generated, list) or not all(isinstance(entry, dict) and entry.get("text") and entry.get("category") for entry in generated):
            raise ValueError(f"prompt research output is not a list of text+category entries: {agent_output[:200]}")

        created_count = 0
        for entry in generated:
            if query_hash(entry["text"]) in existing_query_hashes:
                continue  # a re-run never duplicates a prompt the brand already has
            await Prompt.create(brand_id=brand.id, text=entry["text"], category=entry["category"])
            created_count += 1
        logger.info(f"[research] brand {brand.id}: {created_count} prompts generated (inactive, awaiting sign-off), {len(generated) - created_count} duplicates skipped")


@app.task(name="research_competitors", queue="research", retry=2)
async def research_competitors(task_id: str, brand_id: str) -> None:
    """#5 (bootstrap half) — one call seeding the brand's competitor list so
    matching works from day one; run-time discovery (collection.py's ~5%
    sampling) grows it from here. Upserts, so a retry never duplicates."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)

        agent_output = await ClaudeAgent().run_agent(
            system=RESEARCH_COMPETITORS_SYSTEM,
            prompt=_bootstrap_prompt(brand),
            tools=[],
            handlers={},
            operation="research_competitors",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        competitor_names = parse_agent_json(agent_output)
        if not isinstance(competitor_names, list) or not all(isinstance(name, str) and name.strip() for name in competitor_names):
            raise ValueError(f"competitor bootstrap output is not a list of names: {agent_output[:200]}")

        seeded_count = 0
        for competitor_name in competitor_names:
            if Competitor.is_own_brand(brand.name, competitor_name):
                continue
            await Competitor.upsert(brand_id=brand.id, name=competitor_name.strip())
            seeded_count += 1
        logger.info(f"[research] brand {brand.id}: {seeded_count} competitors seeded")


TARGET_PROMPT_COUNT = 15
MAX_COMPETITOR_COUNT = 12
GSC_QUERY_WINDOW_DAYS = 28
GSC_QUERY_LIMIT = 25  # top real queries riding the research prompt


async def _gsc_top_queries(brand: Brand) -> list[dict]:
    """#4's keyword half: the brand's real GSC queries ground generation in
    actual demand. Best-effort — research works fine without GSC, so a
    missing connection or a failed pull degrades to an empty list. Branded
    queries are filtered mechanically: the generation rule is brandless."""
    gsc_connection = next((connection for connection in await Connection.connected_for_brand(brand.id) if connection.provider == "gsc"), None)
    if gsc_connection is None:
        return []
    try:
        top_queries = await GoogleAdapter(gsc_connection).search_top_queries(date.today() - timedelta(days=GSC_QUERY_WINDOW_DAYS), date.today() - timedelta(days=2), limit=GSC_QUERY_LIMIT * 2)
    except GoogleReadError as error:
        logger.warning(f"[research] brand {brand.id}: GSC query pull failed ({error}) — generating without it")
        return []
    return [top_query for top_query in top_queries if brand.name.lower() not in top_query["query"].lower()][:GSC_QUERY_LIMIT]


##############################
# SYSTEM + TASK PROMPTS
##############################

# STATIC on purpose (cache_control): brand specifics ride the task prompt.
RESEARCH_PROMPTS_SYSTEM = """You research how consumers ask AI assistants (Claude, ChatGPT, Gemini) for recommendations in a product or service category.

You produce realistic consumer questions — the questions real people actually type, in their words, covering different intents: recommendations ("best X near me"), comparisons, problem-first questions ("who fixes X"), and occasion/need-based asks.

HARD RULES:
- Never include any brand, business, or company name. Consumers asking for a recommendation don't name brands — and branded text breaks visibility measurement.
- Never include a specific city, state, or neighborhood. Prompts are location-agnostic templates; the pipeline injects each market's city at collection time. Generic phrasing like "near me" is fine.

Respond with ONLY a JSON array, no prose and no code fences. Each entry:
{"text": "the consumer question", "category": "a short intent label, e.g. recommendation | comparison | problem | occasion"}"""

RESEARCH_COMPETITORS_SYSTEM = """You identify the competitors of a brand: businesses a consumer would realistically consider instead, in the same product or service category.

Exclude retailers, marketplaces, aggregators, and directories — only direct competitors that sell or provide the same kind of thing. Prefer competitors an AI assistant would plausibly mention when recommending in this category.

Respond with ONLY a JSON array of competitor names, no prose and no code fences: ["Name One", "Name Two"]"""


def _research_prompt(brand: Brand, existing_prompts: list[Prompt], real_queries: list[dict]) -> str:
    existing_lines = "\n".join(f"- {existing.text}" for existing in existing_prompts) or "(none yet)"
    demand_lines = "\n".join(f"- {top_query['query']} ({top_query['clicks']} clicks)" for top_query in real_queries) or "(no Search Console data yet — generate from the brand context alone)"
    return (
        f"Generate {TARGET_PROMPT_COUNT} consumer questions for the brand's category below.\n"
        "Ground them in the REAL search demand listed — the questions behind those queries — but phrase them\n"
        "the way someone asks an AI assistant, not a search box.\n"
        "Do NOT restate or trivially rephrase the existing prompts listed — only genuinely new angles.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Footprint: {brand.footprint}\n\n"
        f"Real queries people used to find the brand (last {GSC_QUERY_WINDOW_DAYS} days):\n{demand_lines}\n\n"
        f"Existing prompts:\n{existing_lines}"
    )


def _bootstrap_prompt(brand: Brand) -> str:
    return (
        f"List up to {MAX_COMPETITOR_COUNT} competitors of the brand below.\n"
        "For a local brand, prefer competitors of the same scale a consumer would actually weigh it against\n"
        "(plus any dominant national players in the category). Exclude the brand itself.\n\n"
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Footprint: {brand.footprint}"
    )
