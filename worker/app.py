"""Procrastinate app — the queue library over our Postgres.

The queue contract (design doc) splits responsibility:
- library: SKIP LOCKED claims, retries/backoff, max-attempts, scheduling,
  stalled-job recovery
- app code (ours): idempotent change-set apply, LIFETIME token budgets,
  per-org in-flight caps, the granularity rule (a task = unit of retry and
  approval, never unit of iteration)

The queue schema is applied on compose boot (and via `make queue-schema`).

Connections: this App's connector is procrastinate's OWN psycopg pool,
per-process (worker opens it in worker/run.py; the api in main.py's lifespan).
That pool is separate from utils.database's SQLAlchemy sessions — so a
defer_async() here runs in its OWN transaction, NOT the request's. For the
strict transactional-enqueue guarantee (tasks row + job in ONE commit), the
tasks-table glue must defer via procrastinate's SQL defer function THROUGH
Database.session instead of calling defer_async(). Build the glue that way.
"""

import procrastinate

import settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(
        conninfo=f"postgresql://{settings.DATABASE_USERNAME}:{settings.DATABASE_PASSWORD}@{settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}",
    ),
    import_paths=[
        "worker.tasks.schedules",
        "worker.tasks.collection",
        "worker.tasks.site_audit",
        "worker.tasks.visibility",
        "worker.tasks.metrics",
        "worker.tasks.briefs",
        "worker.tasks.outreach",
        "worker.tasks.reputation",
        "worker.tasks.content",
        "worker.tasks.reports",
        "worker.tasks.planning",
        "worker.tasks.fixes",
        "worker.tasks.research",
        "worker.tasks.changes",
    ],
)
