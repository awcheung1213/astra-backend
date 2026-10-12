"""BUG-057: a first-plan input that fails FOR GOOD must not strand the first
roadmap. task_lifecycle runs utils/first_plan.FINAL_FAILURE_HOOKS on the last
attempt only; the hook re-runs the readiness check (the failed row is settled)
and raises one owner notification. Pure: every DB touch is faked."""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from utils import first_plan, task_queue

BRAND_ID = "brand-1"


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeDatabase:
    session = FakeSession()

    @asynccontextmanager
    async def connection(self):
        yield self.session


@pytest.fixture
def lifecycle(monkeypatch):
    """task_lifecycle over a fake session + Task row; records hook calls."""
    task = SimpleNamespace(id="task-1", name="research_competitors", brand_id=BRAND_ID, attempt_count=0, max_attempts=3)
    hook_calls: list[tuple] = []
    FakeDatabase.session = FakeSession()

    async def get(task_id):
        return task

    async def start_attempt(task_id):
        task.attempt_count += 1

    async def update_status(task_id, status):
        task.status = status

    async def hook(brand_id, task_id, task_name):
        hook_calls.append((brand_id, task_id, task_name))

    monkeypatch.setattr(task_queue, "Database", FakeDatabase)
    monkeypatch.setattr(task_queue, "ReplicaDatabase", FakeDatabase)
    monkeypatch.setattr(task_queue.Task, "get", staticmethod(get))
    monkeypatch.setattr(task_queue.Task, "start_attempt", staticmethod(start_attempt))
    monkeypatch.setattr(task_queue.Task, "update_status", staticmethod(update_status))
    monkeypatch.setattr(first_plan, "FINAL_FAILURE_HOOKS", {"research_competitors": hook})
    return SimpleNamespace(task=task, hook_calls=hook_calls)


async def _fail_once():
    with pytest.raises(RuntimeError):
        async with task_queue.task_lifecycle("task-1"):
            raise RuntimeError("provider down")


async def test_hook_runs_only_on_the_last_attempt(lifecycle):
    await _fail_once()
    await _fail_once()
    assert lifecycle.hook_calls == []  # retries left: nothing settled yet
    await _fail_once()
    assert lifecycle.task.status == "failed"
    assert lifecycle.hook_calls == [(BRAND_ID, "task-1", "research_competitors")]


async def test_success_on_last_attempt_never_runs_the_hook(lifecycle):
    lifecycle.task.attempt_count = 2
    async with task_queue.task_lifecycle("task-1"):
        pass
    assert lifecycle.task.status == "done" and lifecycle.hook_calls == []


async def test_unregistered_task_has_no_hook(lifecycle):
    lifecycle.task.name, lifecycle.task.attempt_count = "weekly_brief", 2
    await _fail_once()
    assert lifecycle.hook_calls == []


async def test_a_failing_hook_never_masks_the_task_error(lifecycle, monkeypatch):
    async def broken_hook(brand_id, task_id, task_name):
        raise ValueError("hook blew up")

    monkeypatch.setattr(first_plan, "FINAL_FAILURE_HOOKS", {"research_competitors": broken_hook})
    lifecycle.task.attempt_count = 2
    await _fail_once()  # still the task's RuntimeError, not the hook's ValueError
    assert FakeDatabase.session.rollbacks == 2


def test_every_first_plan_input_is_registered_with_owner_copy():
    assert set(first_plan.FINAL_FAILURE_HOOKS) == set(first_plan.FIRST_PLAN_INPUTS)
    assert set(first_plan.FIRST_PLAN_INPUT_FAILURE_COPY) == set(first_plan.FIRST_PLAN_INPUTS)


@pytest.fixture
def planner(monkeypatch):
    """first_plan over faked Brand/Artifact/Task/create_task/Notification."""
    state = SimpleNamespace(onboarded_at=datetime(2026, 9, 1), plan=None, in_flight={}, created=[], notes=[])

    async def brand_get(brand_id):
        return SimpleNamespace(id=brand_id, organization_id="org-1", onboarded_at=state.onboarded_at)

    async def latest(brand_id, type):
        return state.plan

    async def in_flight(brand_id, name):
        return state.in_flight.get(name, [])

    async def create_task(**kwargs):
        state.created.append(kwargs["name"])

    async def notify(**kwargs):
        state.notes.append(kwargs)

    monkeypatch.setattr(first_plan.Brand, "get", staticmethod(brand_get))
    monkeypatch.setattr(first_plan.Artifact, "latest", staticmethod(latest))
    monkeypatch.setattr(first_plan.Task, "in_flight", staticmethod(in_flight))
    monkeypatch.setattr(first_plan, "create_task", create_task)
    monkeypatch.setattr(first_plan.Notification, "create", staticmethod(notify))
    return state


async def test_permanent_failure_queues_the_plan_and_tells_the_owner(planner):
    assert await first_plan.first_plan_input_failed(BRAND_ID, "task-1", "research_competitors") is True
    assert planner.created == ["plan_update"]
    [note] = planner.notes
    assert note["organization_id"] == "org-1" and note["urgency"] == "normal"
    assert note["title"] == "We couldn't finish researching your competitors"
    assert note["body"].startswith("Your first roadmap is being built without it.")


async def test_other_input_still_running_waits_but_still_tells_the_owner(planner):
    planner.in_flight = {"site_audit": [SimpleNamespace(id="audit-1")]}
    assert await first_plan.first_plan_input_failed(BRAND_ID, "task-1", "research_competitors") is False
    assert planner.created == []  # the audit queues the plan when it lands
    assert len(planner.notes) == 1


async def test_pre_launch_failure_queues_nothing(planner):
    planner.onboarded_at = None
    assert await first_plan.first_plan_input_failed(BRAND_ID, "task-1", "research_prompts") is False
    assert planner.created == [] and planner.notes == []


async def test_existing_plan_is_never_duplicated(planner):
    planner.plan = SimpleNamespace(id="plan-1")
    assert await first_plan.first_plan_input_failed(BRAND_ID, "task-1", "site_audit") is False
    assert planner.created == [] and planner.notes == []


async def test_plan_update_already_queued_is_not_stacked(planner):
    planner.in_flight = {"plan_update": [SimpleNamespace(id="plan-task-1")]}
    await first_plan.first_plan_input_failed(BRAND_ID, "task-1", "research_competitors")
    assert planner.created == []
