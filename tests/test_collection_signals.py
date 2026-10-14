"""Pure (no DB, no client fixture): collection's enqueue path and brand
signals, plus research_prompts' reply handling. Collaborators are
monkeypatched; the real code paths run.

BUG-002 sign-off enqueues via create_task; BUG-011 in-reply duplicates;
BUG-012 whole-word brand_mentioned; BUG-013 own-domain brand_cited;
BUG-014 empty research reply fails."""

import contextlib
import json
from types import SimpleNamespace

import pytest

import api.prompts as prompts_api
import utils.prompt_signoff as prompt_signoff
import worker.tasks.collection as collection
import worker.tasks.research as research
from schemas.prompt_schemas import SignoffRequest
from utils.response_parsing import mentions_name

BRAND = {"id": "b1", "name": "Ace", "website_url": "https://www.ace-plumbing.example/"}
NOT_A_MENTION = "For a quick fix, try the place on Main St. They also replace water heaters."


@contextlib.asynccontextmanager
async def _fake_lifecycle(task_id):
    yield SimpleNamespace(id=task_id, payload={})


##############################
# BUG-012 / BUG-013 — brand signals on a fanned-out answer
##############################


def test_mentions_name_is_whole_word_case_insensitive():
    assert not mentions_name(NOT_A_MENTION, "Ace")
    assert mentions_name("1. ACE Plumbing — fast and fair", "Ace")


def test_brand_cited_by_own_domain_or_named_snippet():
    own_site = [{"url": "https://ace-plumbing.example/water-heaters", "cited_text": ""}]
    other_site = [{"url": "https://www.yelp.example/ace", "cited_text": "we replace pipes"}]
    subdomain = [{"url": "https://blog.ace-plumbing.example/", "cited_text": ""}]
    named = [{"url": "https://news.example/x", "cited_text": "Ace was the top pick"}]
    domain_only = [{"url": "", "domain": "www.ace-plumbing.example", "cited_text": ""}]
    assert collection._is_brand_cited("Ace", BRAND["website_url"], own_site)
    assert not collection._is_brand_cited("Ace", BRAND["website_url"], other_site)
    assert not collection._is_brand_cited("Ace", BRAND["website_url"], subdomain)
    assert collection._is_brand_cited("Ace", BRAND["website_url"], named)
    assert collection._is_brand_cited("Ace", BRAND["website_url"], domain_only)
    assert not collection._is_brand_cited("Ace", "", own_site)
    assert collection._is_brand_cited("Ace", "ace-plumbing.example", own_site)  # scheme-less website_url


async def test_prompt_result_flags_use_whole_word_and_own_domain(monkeypatch):
    created: list[dict] = []

    async def fake_prompt_result_create(**fields):
        created.append(fields)
        return SimpleNamespace(id="pr1", **fields)

    async def fake_citation_create(**fields):
        return SimpleNamespace(**fields)

    monkeypatch.setattr(collection.PromptResult, "create", fake_prompt_result_create)
    monkeypatch.setattr(collection.Citation, "create", fake_citation_create)
    query_response = SimpleNamespace(id="qr1", market_id="m1", engine="chatgpt", text=NOT_A_MENTION, raw={}, citations=[{"url": "https://www.ace-plumbing.example/", "cited_text": "", "title": "", "type": "", "domain": ""}])

    await collection._create_prompt_result_from_query_response(BRAND, "p1", query_response)
    assert created[0]["brand_mentioned"] is False
    assert created[0]["brand_cited"] is True


##############################
# BUG-002 — sign-off enqueues through create_task, in the request session
##############################


async def test_signoff_enqueues_collection_via_create_task(monkeypatch):
    enqueued: list[dict] = []

    async def fake_brand_get(brand_id):
        return SimpleNamespace(id=brand_id)

    async def fake_record_move(brand_id, prompt_id, from_tier, to_tier, reason_code, automatic=True, pinned=None):
        return None

    async def fake_create_task(**kwargs):
        enqueued.append(kwargs)
        return SimpleNamespace(id="t1")

    async def forbidden_defer(*args, **kwargs):
        raise AssertionError("sign-off must not defer_async() (rule 8)")

    monkeypatch.setattr(prompts_api.Brand, "get", fake_brand_get)

    async def labeled_prompts(brand_id):
        return [SimpleNamespace(id=prompt_id, tier="suggested", pinned=False, topic="Topic", created_at=None) for prompt_id in ("p1", "p2")]  # labeled -> only the collection job

    monkeypatch.setattr(prompt_signoff, "record_move", fake_record_move)
    monkeypatch.setattr(prompt_signoff.Prompt, "by_brand", labeled_prompts)
    monkeypatch.setattr(prompt_signoff, "create_task", fake_create_task)  # the shared sign-off path
    monkeypatch.setattr(collection.submit_prompt_execution_batch, "defer_async", forbidden_defer)

    response = await prompts_api.signoff_prompts("b1", SignoffRequest(prompt_ids=["p1", "p2"]))
    assert response.activated_count == 2 and response.collection_started
    assert enqueued == [{"brand_id": "b1", "name": "submit_prompt_execution_batch", "queue": "collection"}]


async def test_submit_runs_under_task_lifecycle_when_enqueued_by_create_task(monkeypatch):
    lifecycles: list[str] = []
    runs: list[bool] = []

    @contextlib.asynccontextmanager
    async def recording_lifecycle(task_id):
        lifecycles.append(task_id)
        yield SimpleNamespace(id=task_id)

    async def fake_run(**kwargs):
        runs.append(True)

    monkeypatch.setattr(collection, "task_lifecycle", recording_lifecycle)
    monkeypatch.setattr(collection, "run_submit_prompt_execution_batch", fake_run)
    await collection.submit_prompt_execution_batch.func(task_id="t1", brand_id="b1")  # create_task's job args
    assert lifecycles == ["t1"] and runs == [True]


##############################
# BUG-011 / BUG-014 — research_prompts reply handling
##############################


@pytest.fixture
def research_harness(monkeypatch):
    state: dict = {"created": [], "plans": [], "reply": [], "onboarded_at": None, "tracked": []}

    async def fake_brand_get(_):
        return SimpleNamespace(id="b1", organization_id="o1", name="Ace", website_url="https://ace.example", category="plumbing", description="", audience="", footprint="local", onboarded_at=state["onboarded_at"])

    async def fake_by_brand(_):
        return [SimpleNamespace(id="p0", text="Who fixes a leaking water heater?", is_active=False, retired_at=None, created_at=None)]

    async def fake_prompt_create(**fields):
        state["created"].append(fields["text"])
        return SimpleNamespace(id=f"new{len(state['created'])}", is_active=False, retired_at=None, **fields)

    async def fake_track(brand_id, prompts):
        state["tracked"].append(prompt_signoff.default_tracked_prompt_ids(prompts))
        return len(state["tracked"][-1])

    async def fake_first_plan(brand_id, task_id):
        state["plans"].append(task_id)
        return True

    async def no_gsc(_):
        return []

    class FakeAgent:
        async def run_agent(self, **kwargs):
            return json.dumps(state["reply"])

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research.Prompt, "create", fake_prompt_create)
    monkeypatch.setattr(research, "queue_first_plan_when_ready", fake_first_plan)
    monkeypatch.setattr(research, "_gsc_top_queries", no_gsc)
    monkeypatch.setattr(research, "ClaudeAgent", FakeAgent)
    monkeypatch.setattr(research, "track_default_prompts", fake_track)
    return state


async def test_research_prompts_dedupes_within_one_reply(research_harness):
    research_harness["reply"] = [
        {"text": "best plumber near me", "category": "recommendation"},
        {"text": "Best plumber near me?", "category": "recommendation"},
        {"text": "who fixes a leaking water heater", "category": "problem"},
        {"text": "plumber vs handyman for a clogged drain", "category": "comparison"},
    ]
    await research.research_prompts.func("t1", "b1")
    assert research_harness["created"] == ["best plumber near me", "plumber vs handyman for a clogged drain"]
    assert research_harness["plans"] == ["t1"]
    assert research_harness["tracked"] == []  # pre-launch research: launch owns the default


async def test_research_prompts_empty_reply_is_a_failed_parse(research_harness):
    research_harness["reply"] = []
    with pytest.raises(ValueError):
        await research.research_prompts.func("t2", "b1")
    assert research_harness["created"] == [] and research_harness["plans"] == []
