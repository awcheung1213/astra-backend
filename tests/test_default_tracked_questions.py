"""Pure (no DB, no client fixture): the backend-owned default question set.

A launched brand with nothing tracked gets its top DEFAULT_TRACKED_QUESTIONS
(3) researched questions signed off through the ONE sign-off path — at launch,
or when research lands after launch. Collaborators are monkeypatched."""

import contextlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import utils.prompt_signoff as prompt_signoff
import worker.tasks.research as research
from utils.prompt_signoff import DEFAULT_TRACKED_QUESTIONS, default_tracked_prompt_ids

T0 = datetime(2026, 10, 1, 9, 0, 0)


def _prompt(prompt_id: str, offset_us: int, is_active: bool = False, retired: bool = False):
    return SimpleNamespace(id=prompt_id, text=prompt_id, is_active=is_active, retired_at=T0 if retired else None, created_at=T0 + timedelta(microseconds=offset_us))


def test_default_is_three():
    assert DEFAULT_TRACKED_QUESTIONS == 3


def test_picks_the_top_three_by_research_rank_not_input_order():
    prompts = [_prompt("fourth", 3), _prompt("second", 1), _prompt("first", 0), _prompt("fifth", 4), _prompt("third", 2)]
    assert default_tracked_prompt_ids(prompts) == ["first", "second", "third"]


def test_nothing_when_any_live_question_is_already_tracked():
    assert default_tracked_prompt_ids([_prompt("a", 0), _prompt("b", 1, is_active=True), _prompt("c", 2)]) == []


def test_retired_questions_neither_count_nor_get_picked():
    prompts = [_prompt("retired-active", 0, is_active=True, retired=True), _prompt("retired", 1, retired=True), _prompt("a", 2), _prompt("b", 3)]
    assert default_tracked_prompt_ids(prompts) == ["a", "b"]


def test_fewer_than_three_and_none_at_all():
    assert default_tracked_prompt_ids([_prompt("only", 0)]) == ["only"]
    assert default_tracked_prompt_ids([]) == []


def test_ties_keep_input_order_and_unknown_times_sort_last():
    undated = SimpleNamespace(id="undated", is_active=False, retired_at=None, created_at=None)
    prompts = [undated, _prompt("x", 0), _prompt("y", 0)]
    assert default_tracked_prompt_ids(prompts) == ["x", "y", "undated"]


##############################
# the shared sign-off path
##############################


@pytest.fixture
def signoff_calls(monkeypatch):
    calls: dict = {"activated": [], "enqueued": []}

    async def fake_activate(brand_id, prompt_ids):
        calls["activated"].append(list(prompt_ids))
        return len(prompt_ids)

    async def fake_create_task(**kwargs):
        calls["enqueued"].append(kwargs)
        return SimpleNamespace(id="t1")

    async def labeled_prompts(brand_id):
        return calls.get("brand_prompts", [])  # default: nothing unlabeled -> no topic job

    monkeypatch.setattr(prompt_signoff.Prompt, "activate", fake_activate)
    monkeypatch.setattr(prompt_signoff.Prompt, "by_brand", labeled_prompts)
    monkeypatch.setattr(prompt_signoff, "create_task", fake_create_task)
    return calls


async def test_track_default_signs_off_top_three_and_queues_one_fill(signoff_calls):
    prompts = [_prompt(f"p{rank}", rank) for rank in range(15)]
    assert await prompt_signoff.track_default_prompts("b1", prompts) == 3
    assert signoff_calls["activated"] == [["p0", "p1", "p2"]]
    assert signoff_calls["enqueued"] == [{"brand_id": "b1", "name": "submit_prompt_execution_batch", "queue": "collection"}]


async def test_track_default_is_a_no_op_when_something_is_tracked(signoff_calls):
    assert await prompt_signoff.track_default_prompts("b1", [_prompt("a", 0, is_active=True), _prompt("b", 1)]) == 0
    assert signoff_calls["activated"] == [] and signoff_calls["enqueued"] == []


async def test_sign_off_queues_nothing_when_nothing_activated(monkeypatch, signoff_calls):
    async def none_activated(brand_id, prompt_ids):
        return 0

    monkeypatch.setattr(prompt_signoff.Prompt, "activate", none_activated)
    assert await prompt_signoff.sign_off_prompts("b1", ["foreign"]) == 0
    assert signoff_calls["enqueued"] == []


##############################
# research landing after launch
##############################


@contextlib.asynccontextmanager
async def _fake_lifecycle(task_id):
    yield SimpleNamespace(id=task_id, payload={})


@pytest.fixture
def post_launch_research(monkeypatch, signoff_calls):
    state: dict = {"committed": [], "onboarded_at": T0}

    async def fake_brand_get(_):
        return SimpleNamespace(id="b1", organization_id="o1", name="Ace", website_url="https://ace.example", category="plumbing", description="", audience="", footprint="national", onboarded_at=state["onboarded_at"])

    async def fake_by_brand(_):
        return list(state["committed"])

    async def fake_prompt_create(**fields):
        return SimpleNamespace(id=fields["text"], is_active=False, retired_at=None, **fields)

    async def fake_first_plan(brand_id, task_id):
        return False

    async def no_gsc(_):
        return []

    class FakeAgent:
        async def run_agent(self, **kwargs):
            return json.dumps([{"text": f"question {rank}", "category": "recommendation"} for rank in range(1, 16)])

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research.Prompt, "create", fake_prompt_create)
    monkeypatch.setattr(research, "queue_first_plan_when_ready", fake_first_plan)
    monkeypatch.setattr(research, "_gsc_top_queries", no_gsc)
    monkeypatch.setattr(research, "ClaudeAgent", FakeAgent)
    return state


async def test_research_after_launch_tracks_the_reply_top_three(post_launch_research, signoff_calls):
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["activated"] == [["question 1", "question 2", "question 3"]]
    assert len(signoff_calls["enqueued"]) == 1


async def test_research_after_launch_leaves_an_owner_choice_alone(post_launch_research, signoff_calls):
    post_launch_research["committed"] = [_prompt("owner-pick", 0, is_active=True)]
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["activated"] == [] and signoff_calls["enqueued"] == []


async def test_research_before_launch_tracks_nothing(post_launch_research, signoff_calls):
    post_launch_research["onboarded_at"] = None
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["activated"] == [] and signoff_calls["enqueued"] == []


async def test_tracking_a_question_without_a_topic_queues_labeling(signoff_calls):
    signoff_calls["brand_prompts"] = [SimpleNamespace(id="p1", topic=None), SimpleNamespace(id="p2", topic="Emergency plumber")]
    assert await prompt_signoff.sign_off_prompts("b1", ["p1", "p2"]) == 2
    assert [call["name"] for call in signoff_calls["enqueued"]] == ["submit_prompt_execution_batch", "backfill_question_topics"]


async def test_tracking_labeled_questions_queues_no_labeling(signoff_calls):
    signoff_calls["brand_prompts"] = [SimpleNamespace(id="p1", topic="Emergency plumber")]
    await prompt_signoff.sign_off_prompts("b1", ["p1"])
    assert [call["name"] for call in signoff_calls["enqueued"]] == ["submit_prompt_execution_batch"]


def test_topic_labeling_is_one_waiting_job_per_brand():
    from utils.task_queue import PER_BRAND_QUEUEING_LOCK_TASKS

    assert "backfill_question_topics" in PER_BRAND_QUEUEING_LOCK_TASKS
