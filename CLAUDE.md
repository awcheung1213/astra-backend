# CLAUDE.md

Guidance for Claude Code when working in the Evergreen backend (repo directory is
still `astra-backend/` — the product was renamed 2026-09-03; directories, compose
project, and container names were deliberately left alone). Current as of
2026-09-06 (Phases 1–4 + auth built; Phase 5 loose ends next). **This repo
deliberately follows geoviz-backend's patterns** — when in doubt about structure
or idiom, look there first.

## What this is

Evergreen: an AI-run SEO agency as a product (the pivot from GeoViz — measurement
stayed, an acting agency grew around it). One repo, one image, two Railway
services: `api` (image CMD: uvicorn; pre-deploy `alembic upgrade head`) and
`worker` (custom start command `python -m worker.run`), configured in the
Railway dashboard (no railway.json: config-as-code is deprecated). They communicate ONLY through Postgres — the typed task
queue is the contract.

Design source of truth (full column spec, queue contract, adapter layer, agent
roster): https://claude.ai/code/artifact/abf28a78-3838-4eb0-a88a-8323a0528a6c
UI mockup + onboarding prototypes are linked from its footer. Consult before
inventing structure; update it when a decision changes.

## Commands (geoviz-backend style)

`make start` (compose: db + api + worker; migrations + queue schema on boot) ·
`make check` (flake8 + mypy + black/isort --check + pytest) · `make test k=expr` ·
`make lint` / `make type` / `make format` · `make migration "msg"` ·
`make upgrade` / `make downgrade` · `make queue-schema` · `make shell` · `make purge` ·
`make tinker` (runs `tinker.py`, a scratch notebook of hand-run steps: each block
calls an API handler or a runner's `.func` directly; all blocks stay commented out —
uncomment one, run, re-comment. No CLI args, no stubs: it hits the DEV DB and real
providers, and some steps write to the connected live site. It stops the worker
container for the run so nothing double-executes. For a $0 end-to-end run use the
verification harness `../evergreen-verification/harness/ev` — separate
`evergreen_verify` DB, simulated clock (`ev warp`), stubbed agents + engines,
`ev drive cron <name>` / `ev drain` to fire crons and drain the queue in-process)

## Conventions (identical to geoviz-backend)

- **Request flow:** `main.py` → middleware stack (InternalServerError →
  DatabaseSession → Logging → Trace) → `api/` routers → `objects/` methods.
  DatabaseSessionMiddleware wraps each request in ONE transaction: methods
  `flush()`, never `commit()`. Standalone worker code opens its own
  `async with Database().connection():` and commits explicitly.
- **Sessions:** `Database.session` / `ReplicaDatabase.session` (ContextVar
  singletons in utils/database.py) — reads via replica, writes via primary;
  no session passing.
- **objects/:** one file per table; active-record static methods
  (get/filter/all/create/update/delete) on each class as they're needed
  (see objects/organization.py for the exemplar). Enums are plain string
  columns with module-level constants — no PG enum types.
- **New table:** SQLModel class in its own objects/ file → **import it in
  alembic/env.py** (or autogenerate won't see it) → `make migration "msg"` →
  `make upgrade`.
- **api/:** thin routers, one file per resource, `/api/v1/...`; register in
  main.py. schemas/ holds the pydantic request/response models.
- **settings/:** `__init__.py` execs `settings.{ENVIRONMENT}` (local default);
  base.py holds only what's identical everywhere; flat individually-keyed env
  vars, same NAMES per environment, values from .env locally / Railway vars
  deployed. local.py hardcodes the docker Postgres.
- **Formatting:** black + isort `--profile black`, line-length 300; flake8
  ignores E501/W503; mypy ignores objects attr-defined.
- Alembic uses pg8000 (env.py builds the URL from settings); the app uses asyncpg.

## Architecture rules (violations are bugs, not opinions)

1. Agents/pipelines emit ARTIFACTS; only the change-set engine touches a
   client's site, only through `sites`, only behind the approval gate.
   Change-set ops carry ids + completion markers: a retried apply RESUMES.
2. Task token budgets span the task's LIFETIME (across retries), not one
   attempt. Dead-lettered tasks (status `dead`) must surface to the operator.
3. `query_responses` = brand-less shared corpus, INSERT-ONLY, keyed by
   (query_hash, market, engine). One `markets` row per physical city.
4. Typed columns for anything filtered/aggregated; jsonb only for payloads
   read whole. The agent data-access layer depends on this.
5. Machine rows carry `source`; the system may revise its own rows
   (e.g. re-sampling `sampled` brand_markets), never a human's.
6. Every external call goes through `adapters/` and records a `cost_event`
   (org, brand, task, tokens, usd). No direct httpx to providers elsewhere.
7. The always-approve floor (outward/irreversible task types) is enforced in
   code — it must never be expressible as an approval_settings row.
8. Creating work = `utils.task_queue.create_task()` — the ONLY call site for
   enqueueing. Never `defer_async()` next to a `session.add()` (dual write:
   two pools, two commits). The helper defers via procrastinate's SQL function
   through Database.session so tasks row + job are one commit.
   ONE sanctioned exception (DEC-010): the two collection crons in
   worker/tasks/schedules.py (weekly submit, hourly poll) `defer_async()`
   directly with no tasks row — collection is system-wide and brand-less
   (one corpus-deduped batch across all brands; tasks.brand_id is NOT NULL),
   and the cron writes nothing else, so there is no dual write. Poll is
   serialized by procrastinate's `lock="poll_batches"`. Prompt sign-off is NOT
   part of the exception: it enqueues submit via create_task.

## Current state / next steps

Built well past scaffold: all runners + 17 cron schedules, engine services
(adapters/services/), connect endpoints (WP/Shopify/Google), onboarding
wizard backend, approval presets. **Auth is ported (2026-09-02)**: Supabase
bearer tokens verified locally (utils/token_verifier.py), ONE router-level
`guard` dependency (utils/authz.py — 401/403 by path-resource ownership),
one org per user on users.organization_id, `GET /me` bootstrap; local dev +
tests borrow the geoviz STAGING Supabase project via .env (tests sign in as
a dedicated runner, tests/_auth.py). Public exceptions (no bearer): Google
OAuth callback + WordPress one-tap callback (HMAC state is their auth) + review webhook, which is
HMAC-authenticated (api/webhooks.py: `X-Evergreen-Signature` = hex
HMAC-SHA256(`WEBHOOK_SECRET`, raw body), bare or `sha256=`-prefixed,
constant-time, checked before parse; 401 otherwise; `WEBHOOK_SECRET` required
in production, unset elsewhere = every delivery 401s). Remaining gaps live in
`../evergreen-onboarding-gaps-plan.md` §8 (Phase 5 loose ends: client-task
completion endpoint, pulse mark-as-read) and §9 (Phase 6 notifier via
Resend).
