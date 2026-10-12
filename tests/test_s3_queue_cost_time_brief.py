"""BUG-054 brief pending counts come from artifacts, not runs; BUG-053 cached
input is priced; BUG-044 API timestamps carry a UTC offset; BUG-008 per-brand
queueing locks skip duplicate periodic work; BUG-009 runners retry with
backoff. Pure (monkeypatched collaborators, no DB)."""

import importlib
import pathlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

import adapters.agents.claude_agent as claude_agent
import worker.tasks.briefs as briefs
from adapters.services.chatgpt_service import ChatGPTService
from adapters.services.gemini_service import GeminiService
from schemas.base_schemas import utc_iso
from utils import llm_usage, task_queue
from utils.database import Database
from worker.app import app

##############################
# BUG-054: pending approvals counted from artifacts
##############################


def test_pending_counts_come_from_pending_artifacts_by_type():
    pending = [SimpleNamespace(type="change_proposal"), SimpleNamespace(type="draft"), SimpleNamespace(type="draft"), SimpleNamespace(type="new_kind")]
    assert briefs.pending_approval_counts(pending) == {"total": 4, "by_type": {"site-fix proposals": 1, "article or page drafts": 2, "new kind": 1}}
    assert briefs.pending_approval_counts([]) == {"total": 0, "by_type": {}}


def test_activity_labels_never_read_as_still_pending():
    facts = briefs.describe_work({"technical_fix": 4, "content_draft": 1, "plan_update": 1, "review_response": 2}, [], datetime(2026, 10, 5), None, gbp_connected=False)
    labels = {entry["what_happened"]: entry["times"] for entry in facts["activity"]}
    technical_fix = next(label for label in labels if label.startswith("site-fix runs"))
    assert labels[technical_fix] == 4 and "not a proposal count" in technical_fix
    for label in list(briefs.WORK_OUTCOMES.values()) + list(labels):
        assert "for approval" not in label and "awaiting approval" not in label and "wait for approval" not in label
    assert "never state how many items await approval" in facts["how_to_read"]


async def test_brief_facts_carry_live_pending_counts(monkeypatch):
    async def empty(*args, **kwargs):
        return []

    async def counts(*args, **kwargs):
        return {"technical_fix": 4}

    async def pending(brand_id):
        return [SimpleNamespace(type="change_proposal")]

    monkeypatch.setattr(briefs.VisibilityRun, "latest", empty)
    monkeypatch.setattr(briefs.Finding, "open_for_brand", empty)
    monkeypatch.setattr(briefs.ChangeSet, "for_brand", empty)
    monkeypatch.setattr(briefs.Connection, "connected_for_brand", empty)
    monkeypatch.setattr(briefs, "completed_task_counts", counts)
    monkeypatch.setattr(briefs, "overdue_client_tasks", empty)
    monkeypatch.setattr(briefs.Artifact, "pending_for_brand", pending)

    facts = await briefs._assemble_brief_facts(SimpleNamespace(id="b1"))
    assert facts["waiting_on_your_approval_now"] == {"total": 1, "by_type": {"site-fix proposals": 1}}
    assert "waiting_on_your_approval_now" in briefs._brief_prompt(SimpleNamespace(name="B", website_url="https://b.com"), facts)


##############################
# BUG-053: cached input tokens are priced
##############################


def test_estimate_cost_prices_cache_writes_and_reads():
    base = llm_usage.estimate_cost("claude", "claude-haiku-4-5", 1_000_000, 0)
    assert base == 1.0
    assert llm_usage.estimate_cost("claude", "claude-haiku-4-5", 0, 0, cache_write_tokens=1_000_000) == 1.25
    assert llm_usage.estimate_cost("claude", "claude-haiku-4-5", 0, 0, cache_read_tokens=1_000_000) == 0.1
    assert llm_usage.estimate_cost("claude", "claude-haiku-4-5", 0, 0, cache_read_tokens=1_000_000, batch=True) == 0.05


def test_summarize_usage_totals_cache_fields():
    results = [
        SimpleNamespace(usage={"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5, "server_tool_use": {"web_search_requests": 1}}),
        SimpleNamespace(usage={"input_tokens": 20, "output_tokens": 5}),
        SimpleNamespace(usage=None),
    ]
    assert llm_usage.summarize_usage(results) == {"input_tokens": 30, "cache_write_tokens": 100, "cache_read_tokens": 1000, "output_tokens": 10, "web_searches": 1}


def test_openai_and_gemini_split_cached_tokens_out_of_input():
    openai_usage = ChatGPTService._extract_usage({"usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800}, "output_tokens": 50}, "output": []})
    assert openai_usage is not None and openai_usage["input_tokens"] == 200 and openai_usage["cache_read_input_tokens"] == 800
    gemini_response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=1000, cached_content_token_count=600, candidates_token_count=40), candidates=[])
    gemini_usage = GeminiService._extract_usage(gemini_response)
    assert gemini_usage is not None and gemini_usage["input_tokens"] == 400 and gemini_usage["cache_read_input_tokens"] == 600
    no_cache = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10, cached_content_token_count=None, candidates_token_count=1), candidates=[])
    assert GeminiService._extract_usage(no_cache)["cache_read_input_tokens"] == 0  # type: ignore[index]


def test_agent_metering_shares_the_cache_multipliers():
    assert claude_agent.CACHE_WRITE_MULTIPLIER is llm_usage.CACHE_WRITE_MULTIPLIER and claude_agent.CACHE_READ_MULTIPLIER is llm_usage.CACHE_READ_MULTIPLIER


##############################
# BUG-044: timestamps carry an explicit UTC offset
##############################


def test_utc_iso_marks_naive_utc_and_converts_aware():
    assert utc_iso(datetime(2026, 9, 24, 12, 30)) == "2026-09-24T12:30:00+00:00"
    assert utc_iso(datetime(2026, 9, 24, 5, 30, tzinfo=timezone(timedelta(hours=-7)))) == "2026-09-24T12:30:00+00:00"
    assert utc_iso(None) is None


def test_api_never_serializes_a_timestamp_without_its_offset():
    # date-only fields (week_of, period, deadline) keep date.isoformat()
    api_dir = pathlib.Path(__file__).resolve().parent.parent / "api"
    naive = [f"{path.name}: {match}" for path in api_dir.glob("*.py") for match in re.findall(r"\w+_at\.isoformat\(\)", path.read_text())]
    assert naive == []


##############################
# BUG-008: per-brand queueing locks
##############################


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _QueueSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.executed: list = []
        self.added: list = []
        self.error = error

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        pass

    def begin_nested(self):
        return _Savepoint()

    async def execute(self, statement, params=None):
        self.executed.append(params)
        if self.error is not None:
            raise self.error


async def _nothing_in_flight(brand_id: str, name: str) -> list:
    return []


def _unique_violation(constraint: str) -> IntegrityError:
    return IntegrityError("SELECT procrastinate_defer_jobs_v1(...)", {}, Exception(f'duplicate key value violates unique constraint "{constraint}"'))


async def test_periodic_tasks_get_a_per_brand_queueing_lock_and_others_none(monkeypatch):
    session = _QueueSession()
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    monkeypatch.setattr(task_queue.Task, "in_flight", _nothing_in_flight)  # plan_update's DEC-027 read
    for name in task_queue.PER_BRAND_QUEUEING_LOCK_TASKS:
        assert await task_queue.create_task(brand_id="b1", name=name) is not None
        assert session.executed[-1]["queueing_lock"] == f"{name}:b1"
    await task_queue.create_task(brand_id="b1", name="technical_fix", queue="fixes")
    assert session.executed[-1]["queueing_lock"] is None


async def test_duplicate_enqueue_is_skipped_without_raising(monkeypatch):
    session = _QueueSession(error=_unique_violation("procrastinate_jobs_queueing_lock_idx_v1"))
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    assert await task_queue.create_task(brand_id="b1", name="site_audit", queue="audit") is None


async def test_other_integrity_errors_still_raise(monkeypatch):
    session = _QueueSession(error=_unique_violation("tasks_pkey"))
    monkeypatch.setattr(Database, "get_session", lambda self: session)
    with pytest.raises(IntegrityError):
        await task_queue.create_task(brand_id="b1", name="site_audit", queue="audit")


##############################
# BUG-009: retries back off; collection submit never retries
##############################


def test_every_runner_retries_with_backoff_and_submit_does_not():
    for module in app.import_paths:
        importlib.import_module(module)
    runners = {name: task for name, task in app.tasks.items() if not name.startswith(("schedule_", "builtin:", "procrastinate."))}
    assert "poll_batches" in runners and "site_audit" in runners
    for name, task in runners.items():
        if name == "submit_prompt_execution_batch":
            assert task.retry_strategy is None
            continue
        strategy = task.retry_strategy
        assert strategy is task_queue.RETRY_WITH_BACKOFF, name
    first = task_queue.RETRY_WITH_BACKOFF.get_retry_decision(exception=RuntimeError(), job=SimpleNamespace(attempts=0))  # type: ignore[arg-type]
    second = task_queue.RETRY_WITH_BACKOFF.get_retry_decision(exception=RuntimeError(), job=SimpleNamespace(attempts=1))  # type: ignore[arg-type]
    assert first is not None and second is not None and first.retry_at is not None and second.retry_at is not None
    now = datetime.now(timezone.utc)
    assert round((first.retry_at - now).total_seconds()) == 30 and round((second.retry_at - now).total_seconds()) == 120
    assert task_queue.RETRY_WITH_BACKOFF.get_retry_decision(exception=RuntimeError(), job=SimpleNamespace(attempts=2)) is None  # type: ignore[arg-type]
