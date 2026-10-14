"""Pure (no DB, no client fixture): the backend-owned starting levels.

A launched brand with no leveled question gets its top CORE_SLOTS (5)
researched questions as core and the next ones as watching
(utils/question_tiers.initial_tiers) — at launch, or when research lands
after launch. Collaborators are monkeypatched."""

import contextlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import utils.prompt_signoff as prompt_signoff
import worker.tasks.research as research
from utils.prompt_signoff import default_tracked_prompt_ids
from utils.question_tiers import CORE_SLOTS, WATCHING_POOL_TARGET

T0 = datetime(2026, 10, 1, 9, 0, 0)


def _prompt(prompt_id: str, offset_us: int, tier: str = "suggested", retired: bool = False, topic: str | None = "Topic", pinned: bool = False):
    return SimpleNamespace(id=prompt_id, text=prompt_id, tier=tier, is_active=tier == "core", pinned=pinned, source="generated", topic=topic, retired_at=T0 if retired else None, created_at=T0 + timedelta(microseconds=offset_us))


def test_default_levels_are_five_core_then_watching_by_research_rank():
    prompts = [_prompt(f"p{rank}", rank) for rank in reversed(range(15))]
    assert default_tracked_prompt_ids(prompts) == [f"p{rank}" for rank in range(15)]  # rank order, not input order
    from utils.question_tiers import initial_tiers

    tiers = initial_tiers(prompts)
    assert [prompt_id for prompt_id, tier in tiers.items() if tier == "core"] == ["p0", "p1", "p2", "p3", "p4"]
    assert sum(1 for tier in tiers.values() if tier == "watching") == 10
    assert CORE_SLOTS == 5


def test_watching_default_is_capped_at_the_pool():
    prompts = [_prompt(f"p{rank}", rank) for rank in range(CORE_SLOTS + WATCHING_POOL_TARGET + 7)]
    from utils.question_tiers import initial_tiers

    assert sum(1 for tier in initial_tiers(prompts).values() if tier == "watching") == WATCHING_POOL_TARGET


def test_nothing_when_any_question_already_has_a_level():
    assert default_tracked_prompt_ids([_prompt("a", 0), _prompt("b", 1, tier="core"), _prompt("c", 2)]) == []
    assert default_tracked_prompt_ids([_prompt("a", 0), _prompt("b", 1, tier="watching")]) == []


def test_retired_rows_never_count_or_get_picked():
    prompts = [_prompt("retired-core", 0, tier="core", retired=True), _prompt("retired", 1, retired=True), _prompt("a", 2), _prompt("b", 3)]
    assert default_tracked_prompt_ids(prompts) == ["a", "b"]


def test_fewer_questions_than_slots_and_none():
    assert default_tracked_prompt_ids([_prompt("only", 0)]) == ["only"]
    assert default_tracked_prompt_ids([]) == []


def test_undated_rows_rank_last():
    undated = SimpleNamespace(id="undated", tier="suggested", retired_at=None, created_at=None)
    prompts = [undated, _prompt("x", 0), _prompt("y", 1)]
    assert default_tracked_prompt_ids(prompts) == ["x", "y", "undated"]


##############################
# the launch default + the legacy sign-off
##############################


@pytest.fixture
def signoff_calls(monkeypatch):
    calls: dict = {"tiers": [], "enqueued": [], "moves": [], "pinned": []}

    async def fake_set_tier(brand_id, prompt_id, tier, reason, pinned=None):
        calls["tiers"].append((prompt_id, tier))
        return 1

    async def fake_record_move(brand_id, prompt_id, from_tier, to_tier, reason_code, automatic=True, pinned=None):
        calls["moves"].append((prompt_id, from_tier, to_tier, reason_code, automatic, pinned))

    async def fake_set_pinned(brand_id, prompt_id, pinned):
        calls["pinned"].append((prompt_id, pinned))
        return 1

    async def fake_create_task(**kwargs):
        calls["enqueued"].append(kwargs)
        return SimpleNamespace(id="t1")

    async def brand_prompts(brand_id):
        return calls.get("brand_prompts", [])

    import utils.question_tiers as question_tiers

    monkeypatch.setattr(question_tiers.Prompt, "set_tier", fake_set_tier)
    monkeypatch.setattr(prompt_signoff.Prompt, "set_pinned", fake_set_pinned)
    monkeypatch.setattr(prompt_signoff.Prompt, "by_brand", brand_prompts)
    monkeypatch.setattr(prompt_signoff, "record_move", fake_record_move)
    monkeypatch.setattr(prompt_signoff, "create_task", fake_create_task)
    return calls


async def test_track_default_levels_the_brand_and_queues_one_fill(signoff_calls):
    prompts = [_prompt(f"p{rank}", rank) for rank in range(15)]
    assert await prompt_signoff.track_default_prompts("b1", prompts) == 15
    assert signoff_calls["tiers"][:5] == [(f"p{rank}", "core") for rank in range(5)]
    assert {tier for _, tier in signoff_calls["tiers"][5:]} == {"watching"}
    assert signoff_calls["enqueued"] == [{"brand_id": "b1", "name": "submit_prompt_execution_batch", "queue": "collection"}]


async def test_track_default_is_a_no_op_when_something_is_leveled(signoff_calls):
    assert await prompt_signoff.track_default_prompts("b1", [_prompt("a", 0, tier="core"), _prompt("b", 1)]) == 0
    assert signoff_calls["tiers"] == [] and signoff_calls["enqueued"] == []


async def test_default_levels_without_topics_queue_labeling(signoff_calls):
    prompts = [_prompt("p0", 0, topic=None), _prompt("p1", 1)]
    await prompt_signoff.track_default_prompts("b1", prompts)
    assert [call["name"] for call in signoff_calls["enqueued"]] == ["submit_prompt_execution_batch", "backfill_question_topics"]


async def test_legacy_sign_off_pins_core_up_to_the_room_left(signoff_calls):
    signoff_calls["brand_prompts"] = [_prompt(f"core{rank}", rank, tier="core") for rank in range(3)] + [_prompt(f"s{rank}", 10 + rank) for rank in range(4)]
    assert await prompt_signoff.sign_off_prompts("b1", ["s3", "s0", "s1", "foreign"]) == 3
    assert [(move[0], move[2], move[5]) for move in signoff_calls["moves"]] == [("s0", "core", True), ("s1", "core", True), ("s3", "watching", True)]
    assert all(move[4] is False for move in signoff_calls["moves"])  # owner moves never spend the swap budget


async def test_sign_off_queues_nothing_when_no_question_is_this_brands(signoff_calls):
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
        return SimpleNamespace(id=fields["text"], tier="suggested", is_active=False, retired_at=None, **fields)

    async def fake_first_plan(brand_id, task_id):
        return False

    async def no_gsc(_):
        return []

    class FakeAgent:
        async def run_agent(self, **kwargs):
            replies = [{"text": f"question {rank}", "category": "recommendation", "topic": f"Topic {rank}"} for rank in range(1, 16)]
            return json.dumps(replies + [{"text": "Is Ace any good for drains?", "category": "comparison"}])

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research.Prompt, "create", fake_prompt_create)
    monkeypatch.setattr(research, "queue_first_plan_when_ready", fake_first_plan)
    monkeypatch.setattr(research, "_gsc_top_queries", no_gsc)
    monkeypatch.setattr(research, "ClaudeAgent", FakeAgent)
    return state


async def test_research_after_launch_levels_the_reply(post_launch_research, signoff_calls):
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["tiers"][:5] == [(f"question {rank}", "core") for rank in range(1, 6)]
    assert len(signoff_calls["tiers"]) == 15  # the branded reply line was skipped by the backstop
    assert len(signoff_calls["enqueued"]) == 1


async def test_monthly_discovery_leaves_a_leveled_brand_alone(post_launch_research, signoff_calls):
    post_launch_research["committed"] = [_prompt("owner-pick", 0, tier="core")]
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["tiers"] == [] and signoff_calls["enqueued"] == []  # new questions stay suggested for the sorter


async def test_research_before_launch_levels_nothing(post_launch_research, signoff_calls):
    post_launch_research["onboarded_at"] = None
    await research.research_prompts.func("t1", "b1")
    assert signoff_calls["tiers"] == [] and signoff_calls["enqueued"] == []


def test_research_prompt_lists_leveled_questions_first_and_caps_the_list():
    existing = [_prompt(f"s{rank}", rank) for rank in range(research.RESEARCH_EXISTING_LIMIT + 10)] + [_prompt("core-q", -1, tier="core")]
    listed = research._listed_existing(existing)
    assert listed[0].id == "core-q" and len(listed) == research.RESEARCH_EXISTING_LIMIT
    assert listed[1].id == f"s{research.RESEARCH_EXISTING_LIMIT + 9}"  # newest suggestions next


def test_topic_labeling_is_one_waiting_job_per_brand():
    from utils.task_queue import PER_BRAND_QUEUEING_LOCK_TASKS

    assert "backfill_question_topics" in PER_BRAND_QUEUEING_LOCK_TASKS
