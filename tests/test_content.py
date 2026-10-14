"""The content-calendar runner treats the agent as a PURE function: assembled
facts in, structured entries out.

ClaudeAgent.run_role is replaced with a recorder — no Anthropic call is ever
made. What these tests lock down:
- the agent gets NO tools/handlers (no side-effect surface),
- the prompt carries ONLY assembler facts: the APPROVED plan (never a
  pending one), the previous calendar, and the visibility gaps from the DB,
- its JSON reply is stored verbatim as entries behind the approval gate
  (pending_review — the cost gate before drafts), and
- malformed output is REJECTED: no artifact, no notification, task failed.
Parse tests are DB-free; the runner tests run in compose (make test)."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.market import Market
from objects.notification import Notification
from objects.organization import Organization
from objects.prompt import Prompt
from objects.prompt_result import PromptResult
from objects.query_response import QueryResponse
from objects.task import STATUS_FAILED as TASK_STATUS_FAILED
from objects.task import Task
from objects.visibility_run import STATUS_DONE, VisibilityRun
from utils.database import Database, ReplicaDatabase
from utils.query_normalization import query_hash
from worker.tasks.content import _parse_calendar_entries, content_calendar

CALENDAR_ENTRIES = [{"week": 1, "title": "Implant cost guide", "target_queries": ["best dentist near me"], "rationale": "closes the top visibility gap"}]
CALENDAR_JSON = '[{"week": 1, "title": "Implant cost guide", "target_queries": ["best dentist near me"], "rationale": "closes the top visibility gap"}]'


class AgentRecorder:
    """Stands in for ClaudeAgent.run_role: records every call, returns canned
    output, touches nothing."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def run_role(self, role: str, **kwargs) -> str:
        self.calls.append({"role": role, **kwargs})
        return self.reply


##############################
# ENTRY PARSING (pure, DB-free)
##############################


def test_parse_tolerates_a_stray_code_fence():
    assert _parse_calendar_entries(f"```json\n{CALENDAR_JSON}\n```") == CALENDAR_ENTRIES
    assert _parse_calendar_entries(CALENDAR_JSON) == CALENDAR_ENTRIES


def test_parse_rejects_anything_but_a_list_of_titled_entries():
    for bad_output in ('{"week": 1, "title": "not a list"}', "[]", '[{"week": 1}]', "Sure! Here is the calendar you asked for:"):
        with pytest.raises(ValueError):
            _parse_calendar_entries(bad_output)


##############################
# RUNNER (DB, mocked agent)
##############################


async def _seed_calendar_world() -> dict:
    """Org + brand with everything the assembler draws on: an approved plan, a
    NEWER pending plan (must be ignored), a previous calendar, a visibility
    run, and one silent prompt cell (= a visibility gap)."""
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        organization = Organization(name=f"test-org-{uuid.uuid4()}")
        session.add(organization)
        await session.flush()
        brand = Brand(organization_id=organization.id, name=f"Brand {uuid.uuid4().hex[:8]}", website_url="https://example.com")
        session.add(brand)
        await session.flush()

        seed_task = Task(brand_id=brand.id, name="plan_update")
        session.add(seed_task)
        await session.flush()
        session.add(Artifact(task_id=seed_task.id, brand_id=brand.id, type="plan", content={"markdown": "APPROVED PLAN: own the implant cluster"}, status=STATUS_APPROVED))
        session.add(Artifact(task_id=seed_task.id, brand_id=brand.id, type="plan", content={"markdown": "UNAPPROVED DRAFT PLAN"}, status=STATUS_PENDING_REVIEW))
        session.add(Artifact(task_id=seed_task.id, brand_id=brand.id, type="calendar", content={"entries": [{"title": "LAST MONTH: whitening guide"}]}, status=STATUS_APPROVED))
        session.add(VisibilityRun(brand_id=brand.id, week_of=date(2026, 8, 24), status=STATUS_DONE, overall_score=0.42))

        prompt = Prompt(brand_id=brand.id, text="best dentist near me", category="local", is_active=True, tier="core")
        market = Market(city=f"Testville-{uuid.uuid4().hex[:8]}", state="CA")
        session.add(prompt)
        session.add(market)
        await session.flush()
        query_response = await QueryResponse.create(query_hash=query_hash(prompt.text), query_text=prompt.text, market=market, engine="claude", text="Someone Else is best.", raw={}, citations=[])
        await PromptResult.create(prompt_id=prompt.id, market_id=market.id, brand_id=brand.id, engine="claude", text=query_response.text, raw="{}", brand_cited=False, brand_mentioned=False, query_response_id=query_response.id)

        calendar_task = Task(brand_id=brand.id, name="content_calendar")
        session.add(calendar_task)
        await session.commit()
        return {"organization_id": organization.id, "brand_id": brand.id, "task_id": calendar_task.id, "gap_query": prompt.text}


async def test_content_calendar_is_pure_facts_in_gated_entries_out(monkeypatch):
    recorder = AgentRecorder(reply=CALENDAR_JSON)
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_calendar_world()

    await content_calendar.func(task_id=seeded["task_id"], brand_id=seeded["brand_id"])

    # exactly one agent call, with no side-effect surface
    assert len(recorder.calls) == 1
    agent_call = recorder.calls[0]
    assert agent_call["tools"] == []
    assert agent_call["handlers"] == {}
    # the facts ride the prompt — and only the APPROVED plan does
    assert "APPROVED PLAN: own the implant cluster" in agent_call["prompt"]
    assert "UNAPPROVED DRAFT PLAN" not in agent_call["prompt"]
    assert "LAST MONTH: whitening guide" in agent_call["prompt"]
    assert seeded["gap_query"] in agent_call["prompt"]
    assert "0.42" in agent_call["prompt"]

    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        calendar = (await session.execute(select(Artifact).where(Artifact.task_id == seeded["task_id"]))).scalars().one()
        assert calendar.type == "calendar"
        assert calendar.status == STATUS_PENDING_REVIEW  # the cost gate before drafts
        assert calendar.content == {"entries": CALENDAR_ENTRIES}  # parsed verbatim
        assert calendar.evidence["visibility_gaps"] == [{"query": seeded["gap_query"], "mention_rate": 0.0}]

        notification = (await session.execute(select(Notification).where(Notification.organization_id == seeded["organization_id"]))).scalars().one()
        assert notification.link_ref == f"artifacts/{calendar.id}"


async def test_content_calendar_rejects_malformed_agent_output(monkeypatch):
    recorder = AgentRecorder(reply="Sure! Here's a calendar in prose form.")
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_calendar_world()

    with pytest.raises(ValueError):
        await content_calendar.func(task_id=seeded["task_id"], brand_id=seeded["brand_id"])

    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        # nothing was stored off the bad output, and the task is marked failed
        assert (await session.execute(select(Artifact).where(Artifact.task_id == seeded["task_id"]))).scalars().first() is None
        assert (await session.execute(select(Notification).where(Notification.organization_id == seeded["organization_id"]))).scalars().first() is None
        assert (await session.get(Task, seeded["task_id"])).status == TASK_STATUS_FAILED
