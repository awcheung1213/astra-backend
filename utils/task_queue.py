"""The queue contract, both halves: create_task() is the ONLY way app code
creates work; task_lifecycle() is the ONLY way a worker task runs it.

Writes the tasks row (our system of record) AND enqueues the procrastinate job
in the SAME session — one commit, so state and work can never disagree (the
queue contract's transactional-enqueue guarantee).

Never call defer_async() next to a session.add(): defer_async() rides
procrastinate's own connection pool, which makes it a SEPARATE transaction —
a dual write with a crash window between the two commits. This helper defers
through procrastinate's SQL function on OUR session instead. Call sites should
never need either primitive directly: creating work = create_task().

Inside an API request the middleware owns the commit; standalone worker code
wraps this in its own `async with Database().connection():` + explicit commit.
"""

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from procrastinate import RetryStrategy
from procrastinate.manager import QUEUEING_LOCK_CONSTRAINT
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from objects.task import STATUS_DONE, STATUS_FAILED, Task
from utils.database import Database, ReplicaDatabase
from utils.logger import logger

# every runner's retry: 3 attempts (2 retries) with backoff — 30s, then 120s
# (wait + exponential_wait ** attempt), so a transient provider 429/5xx isn't
# burned in seconds (BUG-009)
RETRY_WITH_BACKOFF = RetryStrategy(max_attempts=2, wait=20, exponential_wait=10)

# periodic, idempotent-by-nature work: at most one QUEUED job per brand, so a
# launch landing next to the weekly tick (or a double cron fire) doesn't stack a
# second crawl/brief/run (BUG-008). A running job doesn't block the next one.
PER_BRAND_QUEUEING_LOCK_TASKS = {"backfill_question_topics", "site_audit", "visibility_run", "weekly_brief", "metrics_ingestion", "alert_detection", "plan_update", "content_calendar", "monthly_report"}

# work too costly to stack even behind a RUNNING job: create_task skips while
# one is queued OR running for the brand (DEC-027 — a quarter tick landing
# mid-plan would otherwise queue a second paid Strategist run)
SKIP_WHILE_IN_FLIGHT_TASKS = {"plan_update"}

# procrastinate 3.x ships this function in its schema exactly for callers on
# their own connection (composite type: queue, task_name, priority, lock,
# queueing_lock, args jsonb, scheduled_at). scheduled_at is computed on the
# DATABASE clock — the one the worker's fetch compares against (infra time,
# never utils.clock's simulated time); NULL delay -> NULL = run now.
_DEFER_SQL = text("SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task_name, :priority, :lock, :queueing_lock, CAST(:args AS jsonb), now() + CAST(:delay_seconds AS integer) * interval '1 second')::procrastinate_job_to_defer_v1])")


async def create_task(
    brand_id: str,
    name: str,
    payload: dict | None = None,
    queue: str = "default",
    priority: int = 0,
    token_budget: int | None = None,
    queueing_lock: str | None = None,
    lock: str | None = None,
    delay_seconds: int | None = None,
) -> Task | None:
    """Create a tasks row + its queue job atomically. Returns the Task, or None
    when the queueing lock says an identical job is already waiting, or a
    SKIP_WHILE_IN_FLIGHT_TASKS job is queued/running (skipped:
    neither the tasks row nor the job is written; the caller's transaction is intact).

    lock: procrastinate's EXECUTION lock — jobs sharing it run one at a time, in
    defer order (e.g. f"site:{site_id}": one change-set apply per site).
    queueing_lock: at most one job with it waiting in the queue; defaults to
    f"{name}:{brand_id}" for PER_BRAND_QUEUEING_LOCK_TASKS.
    delay_seconds: the job becomes runnable this many seconds after the
    commit's clock (procrastinate scheduled_at) — same single commit, so a
    rolled-back caller leaves neither row nor scheduled job.

    Job args carry only ids (JSON-only rule); the task function loads everything else from the tasks row.
    """
    session = Database.session
    if name in SKIP_WHILE_IN_FLIGHT_TASKS and await Task.in_flight(brand_id, name):
        logger.info(f"[{name}] brand {brand_id}: already queued or running — skipped")
        return None
    if queueing_lock is None and name in PER_BRAND_QUEUEING_LOCK_TASKS:
        queueing_lock = f"{name}:{brand_id}"
    if queueing_lock is None:
        return await _create_task_and_job(session, brand_id, name, payload, queue, priority, token_budget, None, lock, delay_seconds)
    try:
        # savepoint: a lock collision rolls back only this row + job, not the caller's transaction
        async with session.begin_nested():
            return await _create_task_and_job(session, brand_id, name, payload, queue, priority, token_budget, queueing_lock, lock, delay_seconds)
    except IntegrityError as exc:
        if QUEUEING_LOCK_CONSTRAINT not in str(exc):
            raise
        logger.info(f"[{name}] brand {brand_id}: already queued (queueing lock {queueing_lock}) — skipped")
        return None


async def _create_task_and_job(session, brand_id: str, name: str, payload: dict | None, queue: str, priority: int, token_budget: int | None, queueing_lock: str | None, lock: str | None, delay_seconds: int | None = None) -> Task:
    task = Task(brand_id=brand_id, name=name, payload=payload or {}, priority=priority, token_budget=token_budget)
    session.add(task)
    await session.flush()

    await session.execute(
        _DEFER_SQL,
        {
            "queue": queue,
            "task_name": name,
            "priority": priority,
            "lock": lock,
            "queueing_lock": queueing_lock,
            "args": json.dumps({"task_id": task.id, "brand_id": brand_id}),
            "delay_seconds": int(delay_seconds) if delay_seconds else None,
        },
    )
    return task


@asynccontextmanager
async def task_lifecycle(task_id: str) -> AsyncIterator[Task]:
    """Everything around one task run, so runner files hold only the work:
    opens the DB connections, marks the task running -> done, logs start/end —
    and on failure rolls back, marks the task failed, and re-raises so
    procrastinate retries. Yields the Task row (payload access for runners
    that dispatch on it).

        @app.task(name="site_audit", queue="audit", retry=RETRY_WITH_BACKOFF)
        async def site_audit(task_id: str, brand_id: str) -> None:
            async with task_lifecycle(task_id):
                await run_site_audit(brand_id)
    """
    async with ReplicaDatabase().connection():
        async with Database().connection():
            task = await Task.get(task_id)
            task_name, task_brand_id = task.name, task.brand_id
            logger.info(f"[{task_name}] started (task {task_id}, brand {task_brand_id})", priority="medium")
            await Task.start_attempt(task_id)  # committed before the work: survives the failure rollback below
            await Database.session.commit()
            is_last_attempt = task.attempt_count >= task.max_attempts
            try:
                yield task
                await Task.update_status(task_id, STATUS_DONE)
                await Database.session.commit()
                logger.info(f"[{task_name}] done (task {task_id})", priority="medium")
            except Exception as exc:
                await Database.session.rollback()
                await Task.update_status(task_id, STATUS_FAILED)
                await Database.session.commit()
                logger.error(f"[{task_name}] failed (task {task_id}, brand {task_brand_id}): {exc}")
                if is_last_attempt:
                    await _run_final_failure_hook(task_name, task_brand_id, task_id)
                raise


async def _run_final_failure_hook(task_name: str, brand_id: str, task_id: str) -> None:
    """Failed for good (no retry left): run the task's hook, if any (BUG-057).
    A hook error is logged, never raised over the task's own failure."""
    from utils.first_plan import FINAL_FAILURE_HOOKS  # lazy: first_plan imports create_task from here

    hook = FINAL_FAILURE_HOOKS.get(task_name)
    if hook is None:
        return
    try:
        await hook(brand_id, task_id, task_name)
        await Database.session.commit()
    except Exception as hook_error:
        await Database.session.rollback()
        logger.error(f"[{task_name}] final-failure hook failed (task {task_id}, brand {brand_id}): {hook_error}")
