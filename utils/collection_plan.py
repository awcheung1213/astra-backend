"""Lean collection planning (owner decision 2026-10-14): which cells to ask
this run, and the hard monthly budget per brand.

  core questions     every active engine x the brand's full market set
                     (national: nationwide + the 20-city panel; local: home
                     cities), web search on, the usual freshness window.
  long-tail          the rotation slice utils.question_tiers hands us, on the
                     small panel (national: nationwide + top 5 panel cities;
                     local: home cities), web search OFF.
  borderline re-ask  Saturday: a core question x engine whose share this week
                     is 40-60% gets REASK_EXTRA_CITIES more (non-panel) cities.

BUDGET GOVERNOR. Each brand has a hard monthly cap
(settings.COLLECTION_BUDGET_{NATIONAL,LOCAL}_USD). Before submitting, the run's
cells are priced at a per-answer estimate per engine (derived from the trailing
RATE_WINDOW of execute_prompts cost_events over the answers those batches asked;
fallback FALLBACK_ANSWER_USD / FALLBACK_NO_SEARCH_ANSWER_USD) and trimmed to
what's left of the month: long-tail cells go first, then core cells from the
least important end (farthest market first, then the lowest question).

COST ATTRIBUTION. Execution cost_events are batch totals with org/brand NULL
(shared corpus COGS) and no batch link, so month-to-date spend is ATTRIBUTED,
not read: every execute_prompts request created this month (processed or still
in flight — in-flight is committed spend) charges each distinct subscribing
brand an equal share (1/n) of its estimated per-answer cost. The run's own
estimate is conservative instead: a cell costs the brand its full per-answer
price unless another brand already queued it this run (then 0 — it rides free).
So attributed spend never exceeds what the governor allowed.

Everything above the LOADERS section is PURE (tests/test_collection_plan.py)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from sqlalchemy import select

from constants.constants import UsageKind
from objects.batch import Batch
from objects.cost_event import CostEvent
from utils.answer_stats import group_by_prompt_engine, latest_per_market
from utils.database import ReplicaDatabase
from utils.llm_usage import estimate_cost

TIER_CORE = 0
TIER_LONGTAIL = 1

FALLBACK_ANSWER_USD = 0.014
# tokens only (no web search): ~1k output tokens at batch rates, rounded up
FALLBACK_NO_SEARCH_ANSWER_USD = 0.003
RATE_WINDOW = timedelta(days=30)

REASK_SHARE_LOW = 0.40
REASK_SHARE_HIGH = 0.60

# request_metadata flag on each execute request (absent = web search on, the
# pre-Lean default); ingest stamps no-search answers into the corpus row's raw
WEB_SEARCH_KEY = "web_search"
CORPUS_RAW_KEY = "evergreen_collection"


##############################
# PLAN (pure)
##############################


@dataclass(frozen=True)
class PlannedCell:
    prompt_id: str
    market_id: str
    web_search: bool
    priority: tuple  # lower = more important: (tier, market index, question index)


@dataclass(frozen=True)
class CandidateCell:
    """One cell for one engine, priced for the budget governor."""

    prompt_id: str
    market_id: str
    engine: str
    web_search: bool
    priority: tuple
    usd: float


def plan_cells(core_ids: list[str], longtail_ids: list[str], core_market_ids: list[str], small_market_ids: list[str]) -> list[PlannedCell]:
    """Core questions x the full market set (search on); long-tail questions x
    the small panel (search off). A question in both lists is core. List
    order = importance (questions as given, markets most important first)."""
    cells = [PlannedCell(prompt_id, market_id, True, (TIER_CORE, market_index, question_index)) for question_index, prompt_id in enumerate(dict.fromkeys(core_ids)) for market_index, market_id in enumerate(core_market_ids)]
    core = set(core_ids)
    longtail = [prompt_id for prompt_id in dict.fromkeys(longtail_ids) if prompt_id not in core]
    cells += [PlannedCell(prompt_id, market_id, False, (TIER_LONGTAIL, market_index, question_index)) for question_index, prompt_id in enumerate(longtail) for market_index, market_id in enumerate(small_market_ids)]
    return cells


def reask_planned_cells(borderline: list[tuple[str, str]], market_ids: list[str]) -> dict[str, list[PlannedCell]]:
    """{engine: cells} for borderline (prompt, engine) pairs x the extra cities
    (search on — they are core questions). Priority: market, then question."""
    question_index = {prompt_id: index for index, prompt_id in enumerate(dict.fromkeys(prompt_id for prompt_id, _ in borderline))}
    by_engine: dict[str, list[PlannedCell]] = {}
    for prompt_id, engine in borderline:
        for market_index, market_id in enumerate(market_ids):
            by_engine.setdefault(engine, []).append(PlannedCell(prompt_id, market_id, True, (TIER_CORE, market_index, question_index[prompt_id])))
    return by_engine


def is_no_search_answer(raw: dict | None) -> bool:
    """A corpus row collected with web search off (long-tail) — not a fresh
    answer for a core cell."""
    return ((raw or {}).get(CORPUS_RAW_KEY) or {}).get(WEB_SEARCH_KEY) is False


##############################
# BUDGET (pure)
##############################


@dataclass
class AnswerRates:
    """Estimated USD per answer, per engine: (web search on, web search off)."""

    by_engine: dict[str, tuple[float, float]] = field(default_factory=dict)

    def usd(self, engine: str, web_search: bool) -> float:
        search_on, search_off = self.by_engine.get(engine, (FALLBACK_ANSWER_USD, FALLBACK_NO_SEARCH_ANSWER_USD))
        return search_on if web_search else search_off


def request_entries(metadata: dict | None) -> Iterator[dict]:
    """An execute batch's per-request entries (skips '__' decoder-ring keys)."""
    for custom_id, entry in (metadata or {}).items():
        if not custom_id.startswith("__") and isinstance(entry, dict) and "market_id" in entry:
            yield entry


def answer_rates(cost_rows: Iterable[tuple[str, float, float]], answer_rows: Iterable[tuple[str, bool]]) -> AnswerRates:
    """cost_rows: (engine, usd, web-search usd) per execute cost_event in the
    window; answer_rows: (engine, web_search) per request those batches asked.

    token cost per answer = (usd - search usd) / answers; search cost per
    search-on answer = search usd / search-on answers. Search-off = tokens
    only (blended over both kinds — slightly high, the safe side for a cap).
    An engine with no spend or no answers keeps the fallbacks."""
    usd: dict[str, float] = {}
    search_usd: dict[str, float] = {}
    for engine, row_usd, row_search_usd in cost_rows:
        usd[engine] = usd.get(engine, 0.0) + float(row_usd)
        search_usd[engine] = search_usd.get(engine, 0.0) + float(row_search_usd)
    answers: dict[str, int] = {}
    search_answers: dict[str, int] = {}
    for engine, web_search in answer_rows:
        answers[engine] = answers.get(engine, 0) + 1
        if web_search:
            search_answers[engine] = search_answers.get(engine, 0) + 1
    rates = AnswerRates()
    for engine, total in usd.items():
        if total <= 0 or not answers.get(engine):
            continue
        token_per_answer = max(0.0, total - search_usd[engine]) / answers[engine]
        search_on = token_per_answer + search_usd[engine] / search_answers[engine] if search_answers.get(engine) else FALLBACK_ANSWER_USD
        rates.by_engine[engine] = (search_on, token_per_answer)
    return rates


def attributed_spend(batches: Iterable[tuple[str, dict]], rates: AnswerRates) -> dict[str, float]:
    """{brand id: attributed USD} over execute batches (engine, request_metadata):
    each request's estimated cost split equally across its distinct
    subscribing brands."""
    spend: dict[str, float] = {}
    for engine, metadata in batches:
        for entry in request_entries(metadata):
            brand_ids = sorted({subscriber["brand_id"] for subscriber in entry.get("subscribers") or [] if subscriber.get("brand_id")})
            if not brand_ids:
                continue
            share = rates.usd(engine, entry.get(WEB_SEARCH_KEY, True) is not False) / len(brand_ids)
            for brand_id in brand_ids:
                spend[brand_id] = spend.get(brand_id, 0.0) + share
    return spend


def trim_to_budget(cells: list[CandidateCell], remaining_usd: float) -> tuple[list[CandidateCell], list[CandidateCell]]:
    """(kept, dropped). Kept = the most important prefix (by priority, so all
    core before any long-tail; within core the nearest markets and top
    questions first) whose cost fits remaining_usd — never over it. Free
    cells (usd 0: already queued by another brand) are always kept."""
    kept: list[CandidateCell] = []
    dropped: list[CandidateCell] = []
    spent = 0.0
    full = False
    for cell in sorted(cells, key=lambda cell: (cell.priority, cell.engine)):
        if cell.usd <= 0:
            kept.append(cell)
        elif not full and spent + cell.usd <= remaining_usd + 1e-9:
            kept.append(cell)
            spent += cell.usd
        else:
            full = True
            dropped.append(cell)
    return kept, dropped


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


##############################
# BORDERLINE RE-ASK (pure)
##############################


def borderline_pairs(results: Iterable[Any], core_ids: list[str], engines: list[str], low: float = REASK_SHARE_LOW, high: float = REASK_SHARE_HIGH) -> list[tuple[str, str]]:
    """(prompt id, engine) for core questions whose share this week (markets
    that named the brand / markets asked, latest analyzed result per market)
    is within [low, high]. Ordered by question then engine (input orders)."""
    grouped = group_by_prompt_engine(results)
    pairs: list[tuple[str, str]] = []
    for prompt_id in dict.fromkeys(core_ids):
        for engine in engines:
            per_market = latest_per_market(grouped.get(prompt_id, {}).get(engine, []))
            if not per_market:
                continue
            share = sum(1 for result in per_market if result.brand_mentioned) / len(per_market)
            if low - 1e-9 <= share <= high + 1e-9:
                pairs.append((prompt_id, engine))
    return pairs


##############################
# LOADERS (DB reads)
##############################


@dataclass
class BudgetContext:
    rates: AnswerRates
    spend_by_brand: dict[str, float]

    def remaining(self, brand_id: str, cap_usd: float) -> float:
        return max(0.0, cap_usd - self.spend_by_brand.get(brand_id, 0.0))


async def load_budget_context(now: datetime) -> BudgetContext:
    """Per-answer rates over the trailing RATE_WINDOW + month-to-date
    attributed spend per brand (see the module docstring)."""
    rate_since = now - RATE_WINDOW
    cost_rows = await ReplicaDatabase.session.execute(select(CostEvent.adapter, CostEvent.model, CostEvent.usd, CostEvent.web_search_count).where(CostEvent.operation == UsageKind.EXECUTE_PROMPTS.value, CostEvent.occurred_at > rate_since))  # type: ignore
    costs = [(adapter, float(usd or 0), estimate_cost(adapter, model or "", 0, 0, web_searches=web_search_count or 0)) for adapter, model, usd, web_search_count in cost_rows.all()]

    since = min(rate_since, month_start(now))
    batch_rows = await ReplicaDatabase.session.execute(select(Batch.provider, Batch.request_metadata, Batch.processed, Batch.created_at).where(Batch.kind == UsageKind.EXECUTE_PROMPTS.value, Batch.created_at >= since))  # type: ignore
    batches = batch_rows.all()
    answers = [(provider, entry.get(WEB_SEARCH_KEY, True) is not False) for provider, metadata, processed, created_at in batches if processed and created_at > rate_since for entry in request_entries(metadata)]
    rates = answer_rates(costs, answers)
    this_month = [(provider, metadata) for provider, metadata, _, created_at in batches if created_at >= month_start(now)]
    return BudgetContext(rates=rates, spend_by_brand=attributed_spend(this_month, rates))
