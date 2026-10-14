"""Lean collection planning (utils/collection_plan.py) + its wiring in
worker/tasks/collection.py and the Saturday re-ask cron. Pure: every DB/provider
collaborator is monkeypatched; the real planning code runs."""

import contextlib
from datetime import datetime
from types import SimpleNamespace

import pytest

import worker.tasks.collection as collection
import worker.tasks.schedules as schedules
from utils import collection_plan
from utils.collection_plan import FALLBACK_ANSWER_USD, FALLBACK_NO_SEARCH_ANSWER_USD, AnswerRates, BudgetContext, CandidateCell, answer_rates, attributed_spend, borderline_pairs, is_no_search_answer, month_start, plan_cells, reask_planned_cells, trim_to_budget
from utils.database import Database
from utils.question_tiers import CollectionPlan

##############################
# PLAN SPLITTING
##############################


def test_core_questions_get_every_market_with_search_and_long_tail_the_small_panel_without():
    cells = plan_cells(core_ids=["c1", "c2"], longtail_ids=["l1", "c2"], core_market_ids=["nw", "m1", "m2"], small_market_ids=["nw", "m1"])
    core = [(cell.prompt_id, cell.market_id) for cell in cells if cell.web_search]
    longtail = [(cell.prompt_id, cell.market_id) for cell in cells if not cell.web_search]
    assert sorted(core) == sorted((prompt_id, market_id) for prompt_id in ("c1", "c2") for market_id in ("nw", "m1", "m2"))
    assert longtail == [("l1", "nw"), ("l1", "m1")]  # c2 is core: never also long-tail
    assert all(cell.priority[0] == collection_plan.TIER_CORE for cell in cells if cell.web_search)
    assert all(cell.priority[0] == collection_plan.TIER_LONGTAIL for cell in cells if not cell.web_search)
    c2_m2 = next(cell for cell in cells if (cell.prompt_id, cell.market_id) == ("c2", "m2"))
    assert c2_m2.priority == (collection_plan.TIER_CORE, 2, 1)


def test_reask_cells_are_borderline_pairs_times_extra_cities_per_engine():
    by_engine = reask_planned_cells([("p1", "claude"), ("p2", "claude"), ("p1", "gemini")], ["x1", "x2"])
    assert sorted((cell.prompt_id, cell.market_id) for cell in by_engine["claude"]) == [("p1", "x1"), ("p1", "x2"), ("p2", "x1"), ("p2", "x2")]
    assert [(cell.prompt_id, cell.market_id) for cell in by_engine["gemini"]] == [("p1", "x1"), ("p1", "x2")]
    assert all(cell.web_search for cells in by_engine.values() for cell in cells)


def test_no_search_corpus_rows_are_recognized():
    assert is_no_search_answer({"evergreen_collection": {"web_search": False}, "id": "msg"})
    assert not is_no_search_answer({"id": "msg"}) and not is_no_search_answer(None)


##############################
# BUDGET TRIMMING
##############################


def _cell(prompt_id, market_index, tier=collection_plan.TIER_CORE, question_index=0, usd=1.0, engine="claude"):
    return CandidateCell(prompt_id, f"m{market_index}", engine, tier == collection_plan.TIER_CORE, (tier, market_index, question_index), usd)


def test_budget_drops_long_tail_first_then_the_farthest_core_markets_then_lower_questions():
    cells = [
        _cell("l1", 0, tier=collection_plan.TIER_LONGTAIL, usd=0.1),
        _cell("c1", 0),
        _cell("c2", 0, question_index=1),
        _cell("c1", 1),
        _cell("c2", 1, question_index=1),
    ]
    kept, dropped = trim_to_budget(cells, 3.5)
    assert {(cell.prompt_id, cell.market_id) for cell in kept} == {("c1", "m0"), ("c2", "m0"), ("c1", "m1")}
    assert [(cell.prompt_id, cell.market_id) for cell in dropped] == [("c2", "m1"), ("l1", "m0")]
    assert sum(cell.usd for cell in kept) <= 3.5

    kept, dropped = trim_to_budget(cells, 4.05)
    assert [cell.prompt_id for cell in dropped] == ["l1"]  # long-tail is the first thing to go
    kept, dropped = trim_to_budget(cells, 10)
    assert dropped == [] and len(kept) == 5


def test_budget_never_exceeds_the_cap_and_free_cells_always_ride():
    cells = [_cell("c1", 0, usd=2.0), _cell("c1", 1, usd=2.0), _cell("c1", 2, usd=0.0)]
    kept, dropped = trim_to_budget(cells, 3.0)
    assert [cell.market_id for cell in kept] == ["m0", "m2"] and [cell.market_id for cell in dropped] == ["m1"]
    kept, dropped = trim_to_budget(cells, 0.0)
    assert [cell.market_id for cell in kept] == ["m2"]


def test_budget_context_remaining_floors_at_zero():
    context = BudgetContext(rates=AnswerRates(), spend_by_brand={"b1": 31.0, "b2": 5.0})
    assert context.remaining("b1", 30) == 0.0 and context.remaining("b2", 30) == 25.0 and context.remaining("b3", 8) == 8.0


##############################
# RATES + ATTRIBUTION
##############################


def test_answer_rates_derive_from_cost_events_and_fall_back_when_empty():
    # claude: $3.00 total, $1.00 of it web search, 100 answers (50 searched) -> tokens $0.02, search $0.02/searched answer
    rates = answer_rates([("claude", 2.0, 0.6), ("claude", 1.0, 0.4)], [("claude", True)] * 50 + [("claude", False)] * 50)
    assert rates.usd("claude", True) == pytest.approx(0.04) and rates.usd("claude", False) == pytest.approx(0.02)
    assert rates.usd("gemini", True) == FALLBACK_ANSWER_USD and rates.usd("gemini", False) == FALLBACK_NO_SEARCH_ANSWER_USD
    no_answers = answer_rates([("chatgpt", 1.0, 0.0)], [])
    assert no_answers.usd("chatgpt", True) == FALLBACK_ANSWER_USD
    only_no_search = answer_rates([("chatgpt", 0.3, 0.0)], [("chatgpt", False)] * 100)
    assert only_no_search.usd("chatgpt", False) == pytest.approx(0.003) and only_no_search.usd("chatgpt", True) == FALLBACK_ANSWER_USD


def test_spend_is_split_equally_across_a_shared_cells_subscribing_brands():
    rates = AnswerRates(by_engine={"claude": (0.02, 0.004)})
    metadata = {
        "__engine__": "claude",
        "0": {"market_id": "m1", "subscribers": [{"brand_id": "a", "prompt_id": "p"}, {"brand_id": "b", "prompt_id": "q"}]},
        "1": {"market_id": "m2", "subscribers": [{"brand_id": "a", "prompt_id": "p"}, {"brand_id": "a", "prompt_id": "p2"}]},
        "2": {"market_id": "m3", "web_search": False, "subscribers": [{"brand_id": "b", "prompt_id": "q"}]},
    }
    spend = attributed_spend([("claude", metadata), ("gemini", {"0": {"market_id": "m1", "subscribers": [{"brand_id": "a"}]}})], rates)
    assert spend["a"] == pytest.approx(0.01 + 0.02 + FALLBACK_ANSWER_USD)
    assert spend["b"] == pytest.approx(0.01 + 0.004)


def test_month_start():
    assert month_start(datetime(2026, 10, 14, 13, 5, 7, 99)) == datetime(2026, 10, 1)


##############################
# BORDERLINE SELECTION
##############################


def _result(prompt_id, engine, market_id, mentioned, created_at=datetime(2026, 10, 10)):
    return SimpleNamespace(prompt_id=prompt_id, engine=engine, market_id=market_id, brand_mentioned=mentioned, created_at=created_at, rank=None)


def _share_results(prompt_id, engine, named, asked):
    return [_result(prompt_id, engine, f"m{index}", index < named) for index in range(asked)]


def test_borderline_is_40_to_60_percent_share_per_question_and_engine():
    results = (
        _share_results("p40", "claude", 4, 10) + _share_results("p60", "claude", 6, 10) + _share_results("p50", "gemini", 1, 2) + _share_results("p39", "claude", 39, 100) + _share_results("p70", "claude", 7, 10) + _share_results("p40", "gemini", 0, 5) + _share_results("notcore", "claude", 5, 10)
    )
    pairs = borderline_pairs(results, ["p40", "p60", "p50", "p39", "p70", "never_asked"], ["claude", "gemini"])
    assert pairs == [("p40", "claude"), ("p60", "claude"), ("p50", "gemini")]


def test_borderline_uses_the_latest_result_per_market():
    old, new = datetime(2026, 10, 9), datetime(2026, 10, 11)
    results = [_result("p", "claude", "m1", True, old), _result("p", "claude", "m1", False, new), _result("p", "claude", "m2", True, new)]
    assert borderline_pairs(results, ["p"], ["claude"]) == [("p", "claude")]  # 1 of 2, not 2 of 2
    assert borderline_pairs(results[:1] + results[2:], ["p"], ["claude"]) == []


##############################
# WIRING: run_submit_prompt_execution_batch
##############################


@pytest.fixture
def lean_harness(monkeypatch):
    """A national brand: core p-core, long-tail p-tail; markets = nationwide +
    6 ranked panel cities; small panel = top 2; engines claude + gemini."""
    state: dict = {"submits": [], "batches": [], "spend": {}, "fresh_corpus": []}
    brand = SimpleNamespace(id="b1", footprint="national")
    prompts = [SimpleNamespace(id="p-core", text="best plumber", is_active=True, retired_at=None), SimpleNamespace(id="p-tail", text="plumber open sunday", is_active=False, retired_at=None)]
    markets = [SimpleNamespace(id="nw", city="-", state="-", zip=None, source="national", panel_rank=None, population=None)]
    markets += [SimpleNamespace(id=f"c{rank}", city=f"City{rank}", state="NY", zip=None, source="system", panel_rank=rank, population=1000 - rank) for rank in range(1, 7)]

    async def launched():
        return [brand]

    async def plan(brand_id, now, record=True):
        return CollectionPlan(core_ids=["p-core"], longtail_ids=["p-tail"])

    async def prompts_by_brand(brand_id):
        return prompts

    async def markets_by_brand(brand_id, brand_footprint):
        assert brand_footprint == "national"
        return markets

    async def all_markets():
        return markets

    async def nothing(**kwargs):
        return []

    async def fresh_corpus(stale_cells, engine):
        return [row for row in state["fresh_corpus"] if row.engine == engine and (row.query_hash, row.market_id) in stale_cells]

    async def no_cells():
        return {}

    async def budget(now):
        return BudgetContext(rates=AnswerRates(by_engine={"claude": (0.01, 0.002), "gemini": (0.01, 0.002)}), spend_by_brand=dict(state["spend"]))

    class FakeEngine:
        def __init__(self, engine):
            self.engine = engine

        async def submit_batch(self, query_requests):
            state["submits"].append((self.engine, list(query_requests)))
            return SimpleNamespace(provider=self.engine, batch_id=f"pb{len(state['submits'])}", status="in_progress")

    async def batch_create(**fields):
        state["batches"].append(fields)

    monkeypatch.setattr(collection.settings, "ACTIVE_ENGINES", ["claude", "gemini"])
    monkeypatch.setattr(collection.settings, "SMALL_PANEL_SIZE", 2)
    monkeypatch.setattr(collection.settings, "COLLECTION_BUDGET_NATIONAL_USD", 30.0)
    monkeypatch.setattr(collection, "_in_process_cells", no_cells)
    monkeypatch.setattr(collection, "questions_to_collect", plan)
    monkeypatch.setattr(collection.Prompt, "mark_checked", _stamps(state))
    monkeypatch.setattr(collection, "load_budget_context", budget)
    monkeypatch.setattr(collection.Brand, "launched", launched)
    monkeypatch.setattr(collection.Prompt, "by_brand", prompts_by_brand)
    monkeypatch.setattr(collection.Market, "by_brand", markets_by_brand)
    monkeypatch.setattr(collection.Market, "all", all_markets)
    monkeypatch.setattr(collection.PromptResult, "within_freshness_window", nothing)
    monkeypatch.setattr(collection.QueryResponse, "within_freshness_window", fresh_corpus)
    monkeypatch.setattr(collection, "initialize_adapter", lambda adapter: FakeEngine(adapter.removesuffix("_service")))
    monkeypatch.setattr(collection.Batch, "create", batch_create)
    monkeypatch.setattr(Database, "get_session", lambda self: SimpleNamespace(commit=_async_none))
    return state


async def _async_none(*args, **kwargs):
    return None


def _submitted(state, engine, web_search):
    for submit_engine, requests in state["submits"]:
        if submit_engine == engine and all((request.use_web_search is not False) == web_search for request in requests):
            return requests
    return []


async def test_submit_splits_core_and_long_tail_into_separate_batches_per_engine(lean_harness):
    await collection.run_submit_prompt_execution_batch()
    assert len(lean_harness["submits"]) == 4  # claude + gemini, each: web-search batch + no-search batch
    for engine in ("claude", "gemini"):
        core = _submitted(lean_harness, engine, True)
        assert {request.prompt for request in core} == {"best plumber"} and len(core) == 7  # nationwide + 6 panel cities
        assert all(request.use_web_search is None for request in core)
        assert sum(1 for request in core if request.location is None) == 1  # the nationwide pseudo-market has no city
        tail = _submitted(lean_harness, engine, False)
        assert {request.prompt for request in tail} == {"plumber open sunday"} and len(tail) == 3  # nationwide + top-2 panel cities
        assert {request.location.city for request in tail if request.location} == {"City1", "City2"}
    tail_metadata = next(batch["request_metadata"] for batch in lean_harness["batches"] if all(entry["web_search"] is False for entry in batch["request_metadata"].values()))
    assert {entry["prompt_id"] for entry in tail_metadata.values()} == {"p-tail"}
    assert all(entry["subscribers"] == [{"brand_id": "b1", "prompt_id": "p-tail"}] for entry in tail_metadata.values())


async def test_submit_trims_long_tail_first_when_the_month_is_nearly_spent(lean_harness):
    # 7 core cells x 2 engines x $0.01 = $0.14 fits in $0.141; not even one $0.002 long-tail cell does
    lean_harness["spend"] = {"b1": 30.0 - 0.141}
    await collection.run_submit_prompt_execution_batch()
    assert [engine for engine, _ in lean_harness["submits"]] == ["claude", "gemini"]
    assert all(request.use_web_search is None for _, requests in lean_harness["submits"] for request in requests)


async def test_submit_sends_nothing_once_the_cap_is_reached(lean_harness):
    lean_harness["spend"] = {"b1": 30.0}
    await collection.run_submit_prompt_execution_batch()
    assert lean_harness["submits"] == [] and lean_harness["batches"] == []


async def test_core_cell_does_not_reuse_a_fresh_no_search_corpus_answer_but_long_tail_reuses_any(lean_harness):
    from utils.query_normalization import query_hash

    lean_harness["fresh_corpus"] = [
        SimpleNamespace(engine="claude", query_hash=query_hash("best plumber"), market_id="c1", raw={"evergreen_collection": {"web_search": False}}),
        SimpleNamespace(engine="claude", query_hash=query_hash("best plumber"), market_id="c2", raw={}),
        SimpleNamespace(engine="claude", query_hash=query_hash("plumber open sunday"), market_id="c1", raw={}),
    ]
    await collection.run_submit_prompt_execution_batch()
    core = _submitted(lean_harness, "claude", True)
    assert {request.location.city for request in core if request.location} == {"City1", "City3", "City4", "City5", "City6"}  # c2 reused, c1 re-asked with search
    tail = _submitted(lean_harness, "claude", False)
    assert {request.location.city for request in tail if request.location} == {"City2"}  # c1 reused


async def test_execution_ingest_marks_no_search_answers_in_the_corpus_row(monkeypatch):
    created: list[dict] = []

    async def market_get(market_id):
        return SimpleNamespace(id=market_id)

    async def corpus_create(**fields):
        created.append(fields)
        return SimpleNamespace(id="qr")

    async def brand_get(brand_id):
        return SimpleNamespace(model_dump=lambda: {"id": brand_id, "name": "Ace"})

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(collection.Market, "get", market_get)
    monkeypatch.setattr(collection.QueryResponse, "create", corpus_create)
    monkeypatch.setattr(collection.Brand, "get", brand_get)
    monkeypatch.setattr(collection, "_create_prompt_result_from_query_response", noop)
    monkeypatch.setattr(collection, "_submit_analysis_batch", noop)
    metadata = {
        "0": {"query_hash": "h", "query_text": "q", "prompt_id": "p", "market_id": "m", "web_search": False, "subscribers": [{"brand_id": "b", "prompt_id": "p"}]},
        "1": {"query_hash": "h", "query_text": "q", "prompt_id": "p", "market_id": "m2", "web_search": True, "subscribers": [{"brand_id": "b", "prompt_id": "p"}]},
    }
    batch = SimpleNamespace(provider="claude", request_metadata=metadata)
    results = [SimpleNamespace(custom_id="0", error=None, text="a", raw={"id": "x"}, citations=[]), SimpleNamespace(custom_id="1", error=None, text="b", raw={"id": "y"}, citations=[])]
    await collection.ingest_execution_batch(batch, results)
    assert created[0]["raw"] == {"id": "x", "evergreen_collection": {"web_search": False}}
    assert created[1]["raw"] == {"id": "y"}


##############################
# WIRING: borderline re-ask
##############################


async def test_reask_asks_borderline_pairs_in_extra_cities_only(lean_harness, monkeypatch):
    extra = [SimpleNamespace(id=f"x{index}", city=f"Extra{index}", state="TX", zip=None, source="system", panel_rank=None, population=500 - index, brand_id=None, collection_market_id=None) for index in range(12)]
    week = _share_results("p-core", "claude", 1, 2) + _share_results("p-core", "gemini", 2, 2) + _share_results("p-tail", "claude", 1, 2)

    async def brand_get(brand_id):
        return SimpleNamespace(id=brand_id, footprint="national")

    async def week_results(brand_id):
        return week

    async def reask_pool():
        return extra

    async def all_markets():
        return extra

    monkeypatch.setattr(collection.Brand, "get", brand_get)
    monkeypatch.setattr(collection.answer_stats, "week_results", week_results)
    monkeypatch.setattr(collection.Market, "reask_pool", reask_pool)
    monkeypatch.setattr(collection.Market, "all", all_markets)

    async def forbidden_plan(*args, **kwargs):
        raise AssertionError("the re-ask must not pick this run's long-tail slice")

    monkeypatch.setattr(collection.settings, "REASK_EXTRA_CITIES", 10)
    monkeypatch.setattr(collection, "questions_to_collect", forbidden_plan)
    await collection.run_submit_reask_batch(task_id="t-reask", brand_id="b1")
    assert [engine for engine, _ in lean_harness["submits"]] == ["claude"]  # gemini names it everywhere: not borderline
    requests = lean_harness["submits"][0][1]
    assert {request.prompt for request in requests} == {"best plumber"}  # p-tail is not core
    assert {request.location.city for request in requests} == {f"Extra{index}" for index in range(10)}
    assert lean_harness["batches"][0]["task_id"] == "t-reask"


async def test_reask_skips_local_brands(lean_harness, monkeypatch):
    async def brand_get(brand_id):
        return SimpleNamespace(id=brand_id, footprint="local")

    async def forbidden(*args, **kwargs):
        raise AssertionError("a local brand never re-asks")

    monkeypatch.setattr(collection.Brand, "get", brand_get)
    monkeypatch.setattr(collection.Prompt, "by_brand", forbidden)
    await collection.run_submit_reask_batch(task_id="t", brand_id="b1")
    assert lean_harness["submits"] == []


async def test_submit_task_dispatches_reask_payload(monkeypatch):
    calls: list[tuple] = []

    @contextlib.asynccontextmanager
    async def lifecycle(task_id):
        yield SimpleNamespace(id=task_id, payload={"reask": True})

    async def reask(**kwargs):
        calls.append(("reask", kwargs))

    async def weekly(**kwargs):
        calls.append(("weekly", kwargs))

    monkeypatch.setattr(collection, "task_lifecycle", lifecycle)
    monkeypatch.setattr(collection, "run_submit_reask_batch", reask)
    monkeypatch.setattr(collection, "run_submit_prompt_execution_batch", weekly)
    await collection.submit_prompt_execution_batch.func(task_id="t1", brand_id="b1")
    assert calls == [("reask", {"task_id": "t1", "brand_id": "b1"})]


async def test_reask_cron_enqueues_one_task_per_launched_national_brand(monkeypatch):
    enqueued: list[dict] = []

    async def launched():
        return [SimpleNamespace(id="nat", footprint="national"), SimpleNamespace(id="loc", footprint="local")]

    async def fake_create_task(**kwargs):
        enqueued.append(kwargs)

    @contextlib.asynccontextmanager
    async def no_connection(self):
        yield None

    monkeypatch.setattr(schedules.Brand, "launched", launched)
    monkeypatch.setattr(schedules, "create_task", fake_create_task)
    monkeypatch.setattr(Database, "connection", no_connection)
    monkeypatch.setattr(Database, "get_session", lambda self: SimpleNamespace(commit=_async_none))
    await schedules.schedule_borderline_reask.func(timestamp=0)
    assert enqueued == [{"brand_id": "nat", "name": "submit_prompt_execution_batch", "queue": "collection", "payload": {"reask": True}}]


def test_reask_cron_runs_monday_morning_after_the_sweep():
    source = open(schedules.__file__).read()
    assert '@app.periodic(cron="0 6 * * 1")  # Mondays 06:00 UTC' in source  # Mon 00:00 sweep -> 06:00 re-ask; its answers re-score the week when they land


def test_only_sent_questions_count_as_checked():
    from types import SimpleNamespace

    from worker.tasks.collection import checked_ids, fully_dropped_prompt_ids

    kept = [SimpleNamespace(prompt_id="lt1"), SimpleNamespace(prompt_id="core1")]
    dropped = [SimpleNamespace(prompt_id="lt1"), SimpleNamespace(prompt_id="lt2")]
    dropped_ids = fully_dropped_prompt_ids(kept, dropped)
    assert dropped_ids == {"lt2"}  # lt1 still had a cell sent
    assert checked_ids(["core1"], ["lt1", "lt2", "lt3"], dropped_ids) == ["core1", "lt1", "lt3"]  # lt3 was all fresh: counts as checked


def _stamps(state):
    async def mark_checked(brand_id, ids, at):
        state.setdefault("checked", []).extend(ids)

    return mark_checked


def test_scoring_waits_until_nothing_of_the_brand_is_in_flight():
    from types import SimpleNamespace

    from worker.tasks.collection import batch_brand_ids, brand_has_pending_batches

    execution = SimpleNamespace(kind="execute_prompts", brand_id=None, request_metadata={"__engine__": "claude", "0": {"subscribers": [{"brand_id": "b1", "prompt_id": "p"}, {"brand_id": "b2", "prompt_id": "p"}]}})
    sentiment = SimpleNamespace(kind="sentiment_analysis", brand_id="b3", request_metadata={})
    assert batch_brand_ids(execution) == {"b1", "b2"} and batch_brand_ids(sentiment) == {"b3"}
    assert brand_has_pending_batches([execution], "b1") and not brand_has_pending_batches([sentiment], "b1")


def test_weekly_sweep_runs_on_monday():
    import inspect

    import worker.tasks.schedules as schedules

    source = inspect.getsource(schedules)
    assert '@app.periodic(cron="0 0 * * 1")  # Mondays 00:00 UTC — the weekly visibility sweep' in source
    assert '@app.periodic(cron="*/10 * * * *")' in source
