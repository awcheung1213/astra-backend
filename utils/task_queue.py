"""create_task() — the ONLY way app code creates work.

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

from sqlalchemy import text

from objects.task import Task
from utils.database import Database

# procrastinate 3.x ships this function in its schema exactly for callers on
# their own connection (composite type: queue, task_name, priority, lock,
# queueing_lock, args jsonb, scheduled_at)
_DEFER_SQL = text("SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task_name, :priority, NULL, :queueing_lock, CAST(:args AS jsonb), NULL)::procrastinate_job_to_defer_v1])")


async def create_task(
    brand_id: str,
    type: str,
    payload: dict | None = None,
    job_name: str | None = None,
    queue: str = "default",
    priority: int = 0,
    token_budget: int | None = None,
    queueing_lock: str | None = None,
) -> Task:
    """Create a tasks row + its queue job atomically. Returns the Task.

    job_name defaults to `type` — the procrastinate task registered under that
    name (worker/tasks/) must exist. Job args carry only ids (JSON-only rule);
    the task function loads everything else from the tasks row.
    """
    session = Database.session

    task = Task(brand_id=brand_id, type=type, payload=payload or {}, priority=priority, token_budget=token_budget)
    session.add(task)
    await session.flush()

    await session.execute(
        _DEFER_SQL,
        {
            "queue": queue,
            "task_name": job_name or type,
            "priority": priority,
            "queueing_lock": queueing_lock,
            "args": json.dumps({"task_id": task.id, "brand_id": brand_id}),
        },
    )
    return task
