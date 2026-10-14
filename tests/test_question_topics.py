"""Short question topics (utils/question_topics.py) + their two writers:
research_prompts stamping them on create, and the one-off
backfill_question_topics task. Pure: the agent, DB objects and task
lifecycle are monkeypatched."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from utils import question_topics
from worker.tasks import research


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Emergency plumber", "Emergency plumber"),
        ("  emergency   plumber. ", "Emergency plumber"),
        ('"Water heater repair"', "Water heater repair"),
        ("Emergency plumber?", "Emergency plumber"),
        ("HVAC tune-up", "HVAC tune-up"),
        ("Plumber", "Plumber"),
        ("Who is the best emergency plumber?", None),  # 6 words
        ("best plumber? or handyman", None),  # a question
        ("Ace Plumbing deals", None),  # names the brand
        ("ace plumbing", None),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
        ("Line one\nline two", None),
        ("Supercalifragilistic expialidocious cleaning", None),  # > 40 chars
    ],
)
def test_clean_topic(raw, expected):
    assert question_topics.clean_topic(raw, brand_name="Ace Plumbing") == expected


def test_clean_topic_brand_match_is_whole_word():
    assert question_topics.clean_topic("Acetaminophen delivery", brand_name="Ace") == "Acetaminophen delivery"


def test_parse_topic_reply_keys_on_n_and_drops_bad_entries():
    reply = json.dumps(
        [
            {"n": 1, "topic": "Emergency plumber"},
            {"n": 2, "topic": "a much too long topic label here"},
            {"n": 3, "topic": "Drain cleaning"},
            {"n": 3, "topic": "Duplicate n"},
            {"n": 9, "topic": "Out of range"},
            {"n": "4", "topic": "String n"},
            {"n": True, "topic": "Bool n"},
            "not an object",
        ]
    )
    assert question_topics.parse_topic_reply(reply, 4) == {1: "Emergency plumber", 3: "Drain cleaning"}


def test_parse_topic_reply_tolerates_code_fence_and_rejects_non_array():
    assert question_topics.parse_topic_reply('```json\n[{"n": 1, "topic": "Leak repair"}]\n```', 1) == {1: "Leak repair"}
    with pytest.raises(ValueError):
        question_topics.parse_topic_reply('{"n": 1}', 1)
    with pytest.raises(ValueError):
        question_topics.parse_topic_reply("sure! here you go", 1)


def test_backfill_prompt_numbers_from_one():
    assert question_topics.topic_backfill_prompt(["a?", "b?"]).endswith("1. a?\n2. b?")


@asynccontextmanager
async def _fake_lifecycle(task_id):
    yield SimpleNamespace(id=task_id, payload={})


def _brand():
    return SimpleNamespace(id="b1", organization_id="o1", name="Ace", website_url="https://ace.example", category="plumbing", description="", audience="", footprint="local", onboarded_at=None)


async def test_research_prompts_stores_clean_topic_or_none(monkeypatch):
    created: list[dict] = []

    async def fake_brand_get(_):
        return _brand()

    async def fake_by_brand(_):
        return []

    async def fake_create(**fields):
        created.append(fields)
        return SimpleNamespace(id=f"p{len(created)}", is_active=False, retired_at=None, **fields)

    async def fake_first_plan(brand_id, task_id):
        return True

    async def no_gsc(_):
        return []

    class FakeAgent:
        async def run_agent(self, **kwargs):
            return json.dumps(
                [
                    {"text": "best plumber near me", "category": "recommendation", "topic": "best plumber"},
                    {"text": "who fixes a burst pipe at night", "category": "problem", "topic": "Ace burst pipes"},
                    {"text": "plumber vs handyman for a clog", "category": "comparison"},
                ]
            )

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research.Prompt, "create", fake_create)
    monkeypatch.setattr(research, "queue_first_plan_when_ready", fake_first_plan)
    monkeypatch.setattr(research, "_gsc_top_queries", no_gsc)
    monkeypatch.setattr(research, "ClaudeAgent", FakeAgent)

    await research.research_prompts.func("t1", "b1")
    assert [fields["topic"] for fields in created] == ["Best plumber", None, None]  # brand-named and missing topics land NULL
    assert '"topic"' in research.RESEARCH_PROMPTS_SYSTEM


async def test_backfill_fills_only_null_topics_with_the_cheap_model(monkeypatch):
    prompts = [
        SimpleNamespace(id="p1", text="best plumber near me", topic=None),
        SimpleNamespace(id="p2", text="already labeled", topic="Leak repair"),
        SimpleNamespace(id="p3", text="who unclogs drains", topic=None),
    ]
    agent_calls: list[dict] = []
    written: list[tuple[str, str]] = []

    async def fake_brand_get(_):
        return _brand()

    async def fake_by_brand(_):
        return prompts

    async def fake_set_topic(prompt_id, topic):
        written.append((prompt_id, topic))
        return True

    class FakeAgent:
        def __init__(self, **kwargs):
            agent_calls.append({"init": kwargs})

        async def run_agent(self, **kwargs):
            agent_calls[-1]["run"] = kwargs
            return json.dumps([{"n": 1, "topic": "Local plumber"}, {"n": 2, "topic": "Drain cleaning"}])

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research.Prompt, "set_topic", fake_set_topic)
    monkeypatch.setattr(research, "ClaudeAgent", FakeAgent)

    await research.backfill_question_topics.func("t1", "b1")
    assert written == [("p1", "Local plumber"), ("p3", "Drain cleaning")]
    assert len(agent_calls) == 1
    assert agent_calls[0]["init"]["model"].startswith("claude-haiku")
    run = agent_calls[0]["run"]
    assert run["operation"] == "backfill_question_topics" and run["brand_id"] == "b1" and run["organization_id"] == "o1" and run["task_id"] == "t1"  # metered: cost_event per turn
    assert "already labeled" not in run["prompt"]


async def test_backfill_with_nothing_unlabeled_makes_no_call(monkeypatch):
    async def fake_brand_get(_):
        return _brand()

    async def fake_by_brand(_):
        return [SimpleNamespace(id="p1", text="q", topic="Done")]

    class ExplodingAgent:
        def __init__(self, **kwargs):
            raise AssertionError("no agent call expected")

    monkeypatch.setattr(research, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(research.Brand, "get", fake_brand_get)
    monkeypatch.setattr(research.Prompt, "by_brand", fake_by_brand)
    monkeypatch.setattr(research, "ClaudeAgent", ExplodingAgent)
    await research.backfill_question_topics.func("t1", "b1")
