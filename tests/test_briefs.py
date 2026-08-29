"""The AM runners treat the agent as a PURE function: facts in, prose out.

ClaudeAgent.run_role is replaced with a recorder — no Anthropic call is ever
made. What these tests lock down:
- the agent gets NO tools/handlers (no side-effect surface),
- its prompt carries the assembler's facts from the database,
- its reply is stored VERBATIM (artifact content / notification body),
- the chase's no-op guards keep the agent from being invoked at all ($0).
Runs in compose (make test), needs the database."""

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select

from adapters.agents.claude_agent import ClaudeAgent
from objects.artifact import STATUS_AUTO_APPROVED, Artifact
from objects.brand import Brand
from objects.notification import KIND_UPDATE, Notification
from objects.organization import Organization
from objects.task import STATUS_DONE as TASK_STATUS_DONE
from objects.task import Task
from objects.visibility_run import STATUS_DONE, VisibilityRun
from utils.database import Database, ReplicaDatabase
from worker.tasks.briefs import CHASE_TITLE, client_chase, weekly_brief

AGENT_REPLY = "## Your week\n\nVisibility rose — canned agent prose."


class AgentRecorder:
    """Stands in for ClaudeAgent.run_role: records every call, returns canned
    prose, touches nothing."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_role(self, role: str, **kwargs) -> str:
        self.calls.append({"role": role, **kwargs})
        return AGENT_REPLY


async def _seed_brand(with_visibility_runs: bool = False) -> dict:
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        organization = Organization(name=f"test-org-{uuid.uuid4()}")
        session.add(organization)
        await session.flush()
        brand = Brand(organization_id=organization.id, name="Sunrise Test Dental", website_url="https://example.com")
        session.add(brand)
        await session.flush()
        if with_visibility_runs:
            session.add(VisibilityRun(brand_id=brand.id, week_of=date(2026, 8, 17), status=STATUS_DONE, overall_score=0.35))
            session.add(VisibilityRun(brand_id=brand.id, week_of=date(2026, 8, 24), status=STATUS_DONE, overall_score=0.42))
        await session.commit()
        return {"organization_id": organization.id, "brand_id": brand.id}


async def _insert_task(brand_id: str, name: str, created_at: datetime | None = None) -> str:
    """A bare tasks row (no queue job) — the runner is invoked directly."""
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        task = Task(brand_id=brand_id, name=name)
        if created_at is not None:
            task.created_at = created_at
        session.add(task)
        await session.commit()
        return task.id


async def _insert_chase_notification(organization_id: str) -> None:
    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        session.add(Notification(organization_id=organization_id, kind=KIND_UPDATE, title=CHASE_TITLE))
        await session.commit()


async def _chase_notifications(organization_id: str) -> list[Notification]:
    async with ReplicaDatabase().connection(), Database().connection():
        rows = await Database.session.execute(select(Notification).where(Notification.organization_id == organization_id).where(Notification.title == CHASE_TITLE))
        return list(rows.scalars().all())


##############################
# WEEKLY BRIEF
##############################


async def test_weekly_brief_is_pure_facts_in_verbatim_prose_out(monkeypatch):
    recorder = AgentRecorder()
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_brand(with_visibility_runs=True)
    task_id = await _insert_task(seeded["brand_id"], "weekly_brief")

    await weekly_brief.func(task_id=task_id, brand_id=seeded["brand_id"])

    # exactly one agent call, with no side-effect surface
    assert len(recorder.calls) == 1
    agent_call = recorder.calls[0]
    assert agent_call["tools"] == []
    assert agent_call["handlers"] == {}
    # the facts ride the prompt — the agent is given the numbers, never asked to find them
    assert "0.42" in agent_call["prompt"]

    async with ReplicaDatabase().connection(), Database().connection():
        session = Database.session
        artifact = (await session.execute(select(Artifact).where(Artifact.task_id == task_id))).scalars().one()
        assert artifact.type == "brief"
        assert artifact.status == STATUS_AUTO_APPROVED
        assert artifact.content == {"markdown": AGENT_REPLY}  # verbatim, never edited
        assert artifact.evidence["visibility"]["overall_score"] == 0.42

        notification = (await session.execute(select(Notification).where(Notification.organization_id == seeded["organization_id"]))).scalars().one()
        assert notification.link_ref == f"artifacts/{artifact.id}"

        assert (await session.get(Task, task_id)).status == TASK_STATUS_DONE


##############################
# CLIENT CHASE
##############################


async def test_client_chase_never_invokes_agent_when_nothing_is_overdue(monkeypatch):
    recorder = AgentRecorder()
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_brand()
    chase_task_id = await _insert_task(seeded["brand_id"], "client_chase")

    await client_chase.func(task_id=chase_task_id, brand_id=seeded["brand_id"])

    assert recorder.calls == []  # no-op guard = zero spend
    assert await _chase_notifications(seeded["organization_id"]) == []


async def test_client_chase_respects_the_cooldown(monkeypatch):
    recorder = AgentRecorder()
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_brand()
    await _insert_task(seeded["brand_id"], "grant_access", created_at=datetime.now() - timedelta(days=5))
    await _insert_chase_notification(seeded["organization_id"])  # nudged just now
    chase_task_id = await _insert_task(seeded["brand_id"], "client_chase")

    await client_chase.func(task_id=chase_task_id, brand_id=seeded["brand_id"])

    assert recorder.calls == []


async def test_client_chase_nudges_verbatim_when_overdue(monkeypatch):
    recorder = AgentRecorder()
    monkeypatch.setattr(ClaudeAgent, "run_role", recorder.run_role)
    seeded = await _seed_brand()
    await _insert_task(seeded["brand_id"], "grant_access", created_at=datetime.now() - timedelta(days=5))
    chase_task_id = await _insert_task(seeded["brand_id"], "client_chase")

    await client_chase.func(task_id=chase_task_id, brand_id=seeded["brand_id"])

    assert len(recorder.calls) == 1
    agent_call = recorder.calls[0]
    assert agent_call["tools"] == []
    assert agent_call["handlers"] == {}
    assert "grant access" in agent_call["prompt"]  # the overdue item is the fact set

    (notification,) = await _chase_notifications(seeded["organization_id"])
    assert notification.body == AGENT_REPLY  # verbatim
