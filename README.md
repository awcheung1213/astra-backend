# astra-backend

Astra — the SEO agency in your pocket. One repo, one Docker image, **two Railway
services** that differ only by start command:

| Service | Start command | Config | Job |
|---|---|---|---|
| `api` | (image CMD) `uvicorn main:app --host 0.0.0.0 --port $PORT` | `railway.json` | REST endpoints, authz, Ask (sync Analyst) |
| `worker` | `python -m worker.run` | `railway.worker.json` | pipelines, agent runtime, change-set engine |

They never call each other — **Postgres is the contract** (the task queue is the
handoff). Sole exception: Ask streams through the LLM adapter directly.

Design source of truth (live artifacts):
- [System design doc](https://claude.ai/code/artifact/abf28a78-3838-4eb0-a88a-8323a0528a6c) — architecture, full column spec, queue contract
- [UI mockup](https://claude.ai/code/artifact/17c0d228-c643-4012-ae95-368d0b316d90) · [Onboarding flow](https://claude.ai/code/artifact/88b641ff-6385-4d9b-ad3c-0096daaeac5c)

## Quickstart

```bash
cp .env.example .env    # local defaults need nothing filled in
make start              # postgres :5432 + api :8000 + worker
                        # (migrations + queue schema auto-apply on boot)
make migration "initial schema"   # first run: autogenerate 0001 from objects/
make upgrade
make check              # flake8 + mypy + black/isort --check + pytest
```

Smoke check: `curl localhost:8000/` returns project/environment/version.

## Deploying to Railway

One project, three services:

1. **Postgres** — Railway's plugin; put its pieces into both services below as
   `DATABASE_HOST/NAME/USERNAME/PASSWORD/PORT` (+ `ENVIRONMENT=staging|production`).
2. **api** — this repo with `railway.json` (preDeploy runs migrations + queue schema).
3. **worker** — this repo, same image, `railway.worker.json` (start: `python -m worker.run`).

## Repo map (geoviz-backend conventions)

```
main.py            FastAPI app: CORS → routers → middleware stack (ISE → DBSession → Logging → Trace)
bootstrap.py       logging + trace init (imported first by both services)
context.py         ContextVars: database_session, trace_id
settings/          __init__.py picks settings.{ENVIRONMENT}; base.py = shared;
                   local.py hardcodes docker Postgres; staging/production read flat env vars
api/               thin routers, one file per resource, all under /api/v1
objects/           SQLModel tables, ONE FILE PER TABLE, active-record static methods
                   (reads via ReplicaDatabase.session, writes via Database.session;
                   flush inside requests, never commit — middleware owns the transaction)
schemas/           pydantic request/response models per resource
middleware/        the four-middleware stack (same as geoviz-backend)
worker/            worker entrypoint + procrastinate app + tasks/
adapters/          the ONLY door to the outside world; every call meters a cost_event
utils/             database singletons, logger, trace
exceptions/        HTTPException subclasses (404s etc.)
alembic/           migrations — new table? import it in alembic/env.py or
                   autogenerate won't see it
tests/             pytest, runs in compose (make test k=expr)
```

Gotchas (inherited from geoviz-backend, still true here):
- New SQLModel tables must be **imported in `alembic/env.py`**.
- Alembic connects via pg8000 (env.py builds the URL from settings); the app uses asyncpg.
- `isort` must run with `--profile black` (the Makefile passes it).
- Inside a request, methods `flush()` but **never `commit()`** — the
  DatabaseSessionMiddleware wraps the whole request in one transaction.
- Standalone worker code opens its own
  `async with Database().connection():` and commits explicitly.

## Rules that are architecture, not style

1. **Agents emit artifacts, never side effects.** Execution belongs to the
   change-set engine, behind the approval gate.
2. **Queue contract:** the library (Procrastinate) owns claims/retries/leases;
   app code owns idempotent apply, lifetime token budgets, per-org in-flight
   caps, and granularity (a task = unit of retry/approval, never iteration).
3. **query_responses are brand-less and insert-only**; markets are shared, one
   row per physical city — or the corpus dedup breaks.
4. **Typed columns for anything filtered or aggregated; jsonb only for payloads
   read whole.**
5. **Every machine-created row carries `source`.** The system may revise its own
   rows, never a human's.
6. **All external spend crosses the adapter layer** and logs a cost_event.
