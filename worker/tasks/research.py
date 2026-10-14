"""Kickoff research (deliverable #4, and #5's bootstrap half): the analysis
tasks the onboarding launch enqueues (api/onboarding.py is the producer; plan
approvals re-fire #4 when priorities shift, and the monthly discovery cron —
worker/tasks/schedules.py schedule_question_discovery — re-runs #4 so new
questions keep arriving in the suggested pool: deduped against every live
question, brandless, each with a topic. The weekly sorter feeds them into the
watching rotation, utils/question_tiers.py).

Both are single no-tool agent calls over the brand's confirmed context —
generation, not judgment — so they use task-specific system prompts (static,
so prompt caching applies) rather than one of the seven roles."""

from datetime import date, datetime, timedelta

from adapters.agents.claude_agent import ClaudeAgent
from adapters.google_adapter import GoogleAdapter, GoogleReadError
from constants.constants import UsageModel
from objects.brand import Brand
from objects.competitor import SOURCE_AI_BOOTSTRAP, Competitor
from objects.connection import Connection
from objects.prompt import TIER_CORE, TIER_WATCHING, Prompt
from utils import clock
from utils.agent_json import parse_agent_json
from utils.first_plan import queue_first_plan_when_ready
from utils.logger import logger
from utils.prompt_signoff import track_default_prompts
from utils.query_normalization import query_hash
from utils.question_topics import TOPIC_BACKFILL_SYSTEM, clean_topic, parse_topic_reply, topic_backfill_prompt
from utils.response_parsing import mentions_name
from utils.task_queue import RETRY_WITH_BACKOFF, task_lifecycle
from worker.app import app
from worker.tasks.metrics import GSC_DATA_LAG_DAYS


@app.task(name="research_prompts", queue="research", retry=RETRY_WITH_BACKOFF)
async def research_prompts(task_id: str, brand_id: str) -> None:
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
        if not isinstance(generated, list) or not generated or not all(isinstance(entry, dict) and entry.get("text") and entry.get("category") for entry in generated):
            raise ValueError(f"prompt research output is not a list of text+category entries: {agent_output[:200]}")

        created_prompts: list[Prompt] = []
        ranked_at = clock.now()  # the reply's order is its rank: stamped 1µs apart so created_at keeps it
        for entry in generated:
            entry_query_hash = query_hash(entry["text"])
            if entry_query_hash in existing_query_hashes:
                continue  # never duplicates a prompt the brand already has — nor one earlier in this reply
            if mentions_name(entry["text"], brand.name):
                continue  # brandless backstop: the HARD RULE is in the system prompt, this catches a slip
            created_prompts.append(await Prompt.create(brand_id=brand.id, text=entry["text"], category=entry["category"], topic=clean_topic(entry.get("topic"), brand.name), created_at=ranked_at + timedelta(microseconds=len(created_prompts))))
            existing_query_hashes.add(entry_query_hash)
        logger.info(f"[research] brand {brand.id}: {len(created_prompts)} questions added to the suggested pool, {len(generated) - len(created_prompts)} duplicates/branded skipped")

        # landed after launch with nothing leveled: launch had nothing to
        # level, so the default happens here (re-read — launch may have landed
        # while the agent ran; committed rows + this run's new ones). A brand
        # that already has levels is left alone: new questions stay suggested
        # until the weekly sorter rotates them in
        if (await Brand.get(brand_id)).onboarded_at is not None:
            await track_default_prompts(brand.id, await Prompt.by_brand(brand.id) + created_prompts)
        await queue_first_plan_when_ready(brand.id, task_id)


@app.task(name="research_competitors", queue="research", retry=RETRY_WITH_BACKOFF)
async def research_competitors(task_id: str, brand_id: str) -> None:
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
            await Competitor.upsert(brand_id=brand.id, name=competitor_name.strip(), source=SOURCE_AI_BOOTSTRAP)
            seeded_count += 1
        logger.info(f"[research] brand {brand.id}: {seeded_count} competitors seeded")
        await queue_first_plan_when_ready(brand.id, task_id)


@app.task(name="backfill_question_topics", queue="research", retry=RETRY_WITH_BACKOFF)
async def backfill_question_topics(task_id: str, brand_id: str) -> None:
    """Label the brand's questions that have no topic yet (topic NULL) with a
    cheap model — one no-tool call per TOPIC_BACKFILL_BATCH questions, metered
    as cost_events. Queued automatically whenever questions are tracked and
    any lack a topic (utils/prompt_signoff.sign_off_prompts; one waiting job
    per brand via the queueing lock), and usable as a one-off backfill. Fills
    only NULLs (Prompt.set_topic), so re-running is safe and costs nothing
    once every question is labeled."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        unlabeled = [prompt for prompt in await Prompt.by_brand(brand.id) if prompt.topic is None]
        filled = 0
        for start in range(0, len(unlabeled), TOPIC_BACKFILL_BATCH):
            batch = unlabeled[start:][:TOPIC_BACKFILL_BATCH]
            agent_output = await ClaudeAgent(model=TOPIC_BACKFILL_MODEL, max_tokens=TOPIC_BACKFILL_MAX_TOKENS).run_agent(
                system=TOPIC_BACKFILL_SYSTEM,
                prompt=topic_backfill_prompt([prompt.text for prompt in batch]),
                tools=[],
                handlers={},
                operation="backfill_question_topics",
                organization_id=brand.organization_id,
                brand_id=brand.id,
                task_id=task_id,
            )
            for number, topic in parse_topic_reply(agent_output, len(batch), brand.name).items():
                filled += await Prompt.set_topic(batch[number - 1].id, topic)
        logger.info(f"[research] brand {brand.id}: {filled} of {len(unlabeled)} unlabeled questions given topics")


TARGET_PROMPT_COUNT = 15
RESEARCH_EXISTING_LIMIT = 120  # existing questions listed in the research prompt (all core + watching first, then the newest) — the pool grows monthly; dedupe still checks every one
TOPIC_BACKFILL_MODEL = UsageModel.CLAUDE_HAIKU_4_5_20251001.value  # labeling, not research: the cheap model
TOPIC_BACKFILL_BATCH = 50
TOPIC_BACKFILL_MAX_TOKENS = 4000
MAX_COMPETITOR_COUNT = 12
GSC_QUERY_WINDOW_DAYS = 28
GSC_QUERY_LIMIT = 25  # top real queries riding the research prompt


async def _gsc_top_queries(brand: Brand) -> list[dict]:
    gsc_connection = next((connection for connection in await Connection.connected_for_brand(brand.id) if connection.provider == "gsc"), None)
    if gsc_connection is None:
        return []
    try:
        today = min(clock.today(), date.today())  # sent to Google's real API: sim time must never ask for future days
        window_end = today - timedelta(days=GSC_DATA_LAG_DAYS)
        top_queries = await GoogleAdapter(gsc_connection).search_top_queries(window_end - timedelta(days=GSC_QUERY_WINDOW_DAYS - 1), window_end, limit=GSC_QUERY_LIMIT * 2)  # endDate is inclusive: exactly GSC_QUERY_WINDOW_DAYS days
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
{"text": "the consumer question", "category": "a short intent label, e.g. recommendation | comparison | problem | occasion", "topic": "2-4 plain words naming what the person wants, e.g. Emergency plumber (no brand or place names, no jargon, not a question)"}"""

RESEARCH_COMPETITORS_SYSTEM = """You identify the competitors of a brand: businesses a consumer would realistically consider instead, in the same product or service category.

Exclude retailers, marketplaces, aggregators, and directories — only direct competitors that sell or provide the same kind of thing. Prefer competitors an AI assistant would plausibly mention when recommending in this category.

Respond with ONLY a JSON array of competitor names, no prose and no code fences: ["Name One", "Name Two"]"""


def _listed_existing(existing_prompts: list[Prompt]) -> list[Prompt]:
    """The existing questions the research prompt lists: leveled ones (core,
    watching) first, then the newest suggestions, RESEARCH_EXISTING_LIMIT in all."""
    leveled = [prompt for prompt in existing_prompts if getattr(prompt, "tier", None) in (TIER_CORE, TIER_WATCHING)]
    rest = sorted((prompt for prompt in existing_prompts if getattr(prompt, "tier", None) not in (TIER_CORE, TIER_WATCHING)), key=lambda prompt: prompt.created_at or datetime.min, reverse=True)
    return (leveled + rest)[:RESEARCH_EXISTING_LIMIT]


def _research_prompt(brand: Brand, existing_prompts: list[Prompt], real_queries: list[dict]) -> str:
    existing_lines = "\n".join(f"- {existing.text}" for existing in _listed_existing(existing_prompts)) or "(none yet)"
    demand_lines = "\n".join(f"- {top_query['query']} ({top_query['clicks']} clicks)" for top_query in real_queries) or "(no Search Console data yet — generate from the brand context alone)"
    return (
        f"Generate {TARGET_PROMPT_COUNT} consumer questions for the brand's category below,\n"
        "ordered most-asked first (the first few are the ones we track by default).\n"
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
