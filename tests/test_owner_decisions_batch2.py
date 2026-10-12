"""Pure (no DB, no client fixture): collaborators are monkeypatched, or the
session is a statement-capturing fake; the real code paths run.

Owner decisions answered 2026-09-27: DEC-015 one live pitch per target;
DEC-017 Ask promises a diagnosis only when one was queued; DEC-018 a new plan
supersedes older pending plans; DEC-019 one content calendar per month;
DEC-020 audit-closed twins are 'duplicate', not 'fixed'; DEC-023 ChatGPT
answers must search; DEC-024 post-launch interview edits don't re-run
research; DEC-027 no plan_update stacks behind a running one."""

import contextlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import api.ask as ask
import api.onboarding as onboarding
import worker.tasks.content as content
import worker.tasks.outreach as outreach
import worker.tasks.planning as planning
import worker.tasks.site_audit as site_audit
from adapters.services.chatgpt_service import ChatGPTService
from objects.artifact import STATUS_SUPERSEDED, Artifact
from objects.finding import STATUS_DUPLICATE, STATUS_FIXED, Finding
from utils import task_queue
from utils.database import Database


class _Result:
    rowcount = 2

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class CaptureSession:
    def __init__(self):
        self.statements: list = []
        self.added: list = []

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        return _Result()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def refresh(self, row):
        return None

    def begin_nested(self):
        return contextlib.nullcontext()


def _sql(statement) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).split())


@pytest.fixture
def session(monkeypatch):
    fake = CaptureSession()
    monkeypatch.setattr(Database, "get_session", lambda self: fake)  # ReplicaDatabase inherits it
    return fake


@contextlib.asynccontextmanager
async def _fake_lifecycle(task_id, payload=None):
    yield SimpleNamespace(id=task_id, payload=payload or {})


##############################
# DEC-015 — one live pitch per target
##############################


def test_pitch_target_key_normalizes_urls_and_folds_venues():
    assert outreach.pitch_target_key("https://News.Example.com") == outreach.pitch_target_key("https://news.example.com/#top")
    assert outreach.pitch_target_key("  r/SmallBusiness ") == "r/smallbusiness"


def test_already_pitched_and_in_run_repeats_are_dropped():
    opportunities = [
        {"where": "https://news.example.com/", "pitch": "a"},
        {"where": "https://blog.example.com/best", "pitch": "b"},
        {"where": "https://BLOG.example.com/best#x", "pitch": "c"},
    ]
    fresh = outreach.new_pitch_opportunities(opportunities, already_pitched={outreach.pitch_target_key("https://news.example.com")})
    assert [opportunity["pitch"] for opportunity in fresh] == ["b"]


async def test_scout_reads_only_pending_and_approved_pitches(session):
    await Artifact.for_brand("b1", type="pitch", statuses=["pending_review", "approved"])
    sql = _sql(session.statements[0])
    assert "artifacts.type = 'pitch'" in sql and "IN ('pending_review', 'approved')" in sql


##############################
# DEC-017 — the promise is code's, and only when queued
##############################


def test_promise_appended_only_when_a_diagnosis_was_queued():
    assert ask.answer_with_follow_up("Here is what I see.", diagnosis_queued=True) == f"Here is what I see.\n\n{ask.DIAGNOSIS_PROMISE}"
    assert ask.answer_with_follow_up("Here is what I see. ", diagnosis_queued=False) == "Here is what I see."
    assert "diagnosis" not in ask.answer_with_follow_up("Here is what I see.", diagnosis_queued=False)


def test_ask_prompt_forbids_the_model_promising_follow_up():
    prompt = ask._ask_prompt(SimpleNamespace(name="Acme", website_url="https://acme.example"), "why did we drop?", {})
    assert "full diagnosis is coming" not in prompt and "Never promise a diagnosis" in prompt


##############################
# DEC-018 — a new plan supersedes older pending plans
##############################


async def test_supersede_retires_only_other_pending_plans(session):
    assert await Artifact.supersede_pending("b1", type="plan", keep_id="new-plan") == 2
    sql = _sql(session.statements[0])
    assert sql.startswith("UPDATE artifacts SET status='superseded'")
    assert "artifacts.status = 'pending_review'" in sql and "artifacts.id != 'new-plan'" in sql and "artifacts.type = 'plan'" in sql


async def test_plan_update_supersedes_after_birth(monkeypatch):
    calls: list = []

    async def fake_create(**kwargs):
        calls.append(("create", kwargs["type"]))
        return SimpleNamespace(id="new-plan")

    async def fake_supersede(brand_id, type, keep_id):
        calls.append(("supersede", type, keep_id))
        return 1

    async def fake_run_role(self, *args, **kwargs):
        return '{"summary_markdown": "s", "workstreams": [{"name": "n", "why": "w"}]}'

    async def noop(*args, **kwargs):
        return {}

    monkeypatch.setattr(planning, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(planning.Brand, "get", lambda brand_id: _async(SimpleNamespace(id="b1", organization_id="o1", name="Acme")))
    monkeypatch.setattr(planning, "_assemble_plan_facts", noop)
    monkeypatch.setattr(planning, "_plan_prompt", lambda brand, facts: "prompt")
    monkeypatch.setattr(planning.ClaudeAgent, "__init__", lambda self: None)
    monkeypatch.setattr(planning.ClaudeAgent, "run_role", fake_run_role)
    monkeypatch.setattr(planning.Artifact, "create", fake_create)
    monkeypatch.setattr(planning.Artifact, "supersede_pending", fake_supersede)
    monkeypatch.setattr(planning.Notification, "create", noop)
    await planning.plan_update.func(task_id="t1", brand_id="b1")
    assert calls == [("create", "plan"), ("supersede", "plan", "new-plan")]
    assert STATUS_SUPERSEDED != "pending_review"  # out of the approvals queue (pending_for_brand filters pending_review)


async def _async(value):
    return value


##############################
# DEC-019 — one content calendar per brand per month
##############################


def test_same_month():
    assert content.same_month(datetime(2026, 11, 1, 9), date(2026, 11, 29))
    assert not content.same_month(datetime(2026, 10, 31, 23), date(2026, 11, 1))
    assert not content.same_month(datetime(2025, 11, 5), date(2026, 11, 5))


async def test_calendar_refire_in_the_same_month_is_a_no_op(monkeypatch):
    async def must_not_run(self, *args, **kwargs):
        raise AssertionError("agent called for a second calendar this month")

    async def latest(brand_id, type, statuses=None):
        assert type == "calendar" and "rejected" not in statuses
        return SimpleNamespace(id="cal-1", created_at=datetime(2026, 11, 1, 10))

    monkeypatch.setattr(content, "task_lifecycle", _fake_lifecycle)
    monkeypatch.setattr(content.Brand, "get", lambda brand_id: _async(SimpleNamespace(id="b1", organization_id="o1")))
    monkeypatch.setattr(content.Artifact, "latest", latest)
    monkeypatch.setattr(content.clock, "today", lambda: date(2026, 11, 29))
    monkeypatch.setattr(content.ClaudeAgent, "__init__", lambda self: None)
    monkeypatch.setattr(content.ClaudeAgent, "run_role", must_not_run)
    await content.content_calendar.func(task_id="t1", brand_id="b1")


##############################
# DEC-020 — audit-closed twins are 'duplicate', not 'fixed'
##############################


async def test_audit_closes_twins_as_duplicate_and_counts_only_real_fixes(monkeypatch):
    older = SimpleNamespace(id="f-old", kind="missing_schema", detail={"url": "https://site.example"}, created_at=datetime(2026, 9, 1))
    newer = SimpleNamespace(id="f-new", kind="missing_schema", detail={"url": "https://site.example/"}, created_at=datetime(2026, 9, 2))
    resolved: list = []

    async def crawl(base_url):
        return [{"kind": "missing_schema", "severity": "warn", "summary": "s", "detail": {"url": "https://site.example/"}}]

    async def resolve(id, status=STATUS_FIXED):
        resolved.append((id, status))

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(site_audit.Brand, "get", lambda brand_id: _async(SimpleNamespace(id="b1", website_url="https://site.example")))
    monkeypatch.setattr(site_audit.Site, "by_brand", none)
    monkeypatch.setattr(site_audit, "_crawl", crawl)
    monkeypatch.setattr(site_audit.Finding, "open_for_brand", lambda brand_id: _async([newer, older]))
    monkeypatch.setattr(site_audit.Finding, "resolve", resolve)
    monkeypatch.setattr(site_audit, "queue_first_plan_when_ready", none)
    result = await site_audit.run_site_audit("b1")
    assert resolved == [("f-new", STATUS_DUPLICATE)]
    assert result["resolved"] == 0  # a twin closed is not a fix


async def test_resolved_counts_read_fixed_only(session):
    await Finding.resolved_since("b1", since=datetime(2026, 9, 1))
    assert "findings.status = 'fixed'" in _sql(session.statements[0])


##############################
# DEC-023 — ChatGPT answers must search
##############################


def _chatgpt(max_web_searches: int) -> ChatGPTService:
    service = ChatGPTService.__new__(ChatGPTService)  # no client: only the request payload is under test
    service.model, service.use_web_search, service.max_web_searches = "gpt-test", True, max_web_searches
    return service


def test_chatgpt_request_forces_web_search_when_enabled():
    params = _chatgpt(1)._build_params("best plumber near me", None, None)
    assert params["tools"] == [{"type": "web_search"}] and params["tool_choice"] == "required" and params["max_tool_calls"] == 1


def test_chatgpt_zero_searches_or_opt_out_sends_no_tool_and_no_forcing():
    for params in (_chatgpt(0)._build_params("q", None, None), _chatgpt(1)._build_params("q", None, None, use_web_search=False)):
        assert "tools" not in params and "tool_choice" not in params and "max_tool_calls" not in params


##############################
# DEC-024 — post-launch interview edits update facts only
##############################


def test_interview_research_only_before_launch_or_on_first_answer():
    assert onboarding.interview_queues_research(launched=False, previously_answered=False)
    assert onboarding.interview_queues_research(launched=False, previously_answered=True)  # wizard re-entry: deduped by first_queued
    assert onboarding.interview_queues_research(launched=True, previously_answered=False)  # skipped interview answered from Tasks
    assert not onboarding.interview_queues_research(launched=True, previously_answered=True)


##############################
# DEC-027 — no plan_update behind a running one
##############################


async def test_plan_update_skipped_while_one_is_running(monkeypatch, session):
    async def running(brand_id, name):
        return [SimpleNamespace(id="plan-running", status="running")]

    monkeypatch.setattr(task_queue.Task, "in_flight", running)
    assert await task_queue.create_task(brand_id="b1", name="plan_update", queue="planning") is None
    assert session.added == [] and session.statements == []


async def test_plan_update_created_when_none_in_flight(monkeypatch, session):
    async def idle(brand_id, name):
        return []

    monkeypatch.setattr(task_queue.Task, "in_flight", idle)
    task = await task_queue.create_task(brand_id="b1", name="plan_update", queue="planning")
    assert task is not None and task.name == "plan_update"


async def test_other_tasks_never_consult_in_flight(monkeypatch, session):
    async def must_not_call(brand_id, name):
        raise AssertionError("in_flight consulted for a non-DEC-027 task")

    monkeypatch.setattr(task_queue.Task, "in_flight", must_not_call)
    assert await task_queue.create_task(brand_id="b1", name="site_audit", queue="audit") is not None
