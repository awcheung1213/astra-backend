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

from sqlalchemy import text

from objects.task import STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, Task
from utils.database import Database, ReplicaDatabase
from utils.logger import logger

# procrastinate 3.x ships this function in its schema exactly for callers on
# their own connection (composite type: queue, task_name, priority, lock,
# queueing_lock, args jsonb, scheduled_at)
_DEFER_SQL = text("SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task_name, :priority, NULL, :queueing_lock, CAST(:args AS jsonb), NULL)::procrastinate_job_to_defer_v1])")


async def create_task(
    brand_id: str,
    name: str,
    payload: dict | None = None,
    queue: str = "default",
    priority: int = 0,
    token_budget: int | None = None,
    queueing_lock: str | None = None,
) -> Task:
    """Create a tasks row + its queue job atomically. Returns the Task.

    Job args carry only ids (JSON-only rule); the task function loads everything else from the tasks row.
    """
    session = Database.session

    task = Task(brand_id=brand_id, name=name, payload=payload or {}, priority=priority, token_budget=token_budget)
    session.add(task)
    await session.flush()

    await session.execute(
        _DEFER_SQL,
        {
            "queue": queue,
            "task_name": name,
            "priority": priority,
            "queueing_lock": queueing_lock,
            "args": json.dumps({"task_id": task.id, "brand_id": brand_id}),
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

        @app.task(name="site_audit", queue="audit", retry=2)
        async def site_audit(task_id: str, brand_id: str) -> None:
            async with task_lifecycle(task_id):
                await run_site_audit(brand_id)
    """
    async with ReplicaDatabase().connection():
        async with Database().connection():
            task = await Task.get(task_id)
            # plain copies: rollback() expires ORM instances, and re-reading an
            # expired attribute on an async session raises MissingGreenlet
            task_name, task_brand_id = task.name, task.brand_id
            logger.info(f"[{task_name}] started (task {task_id}, brand {task_brand_id})", priority="medium")
            await Task.update_status(task_id, STATUS_RUNNING)
            await Database.session.commit()
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
                raise
