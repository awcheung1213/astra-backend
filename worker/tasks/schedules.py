"""Cadence — the Schedules band from the design doc's Level 2 diagram.

Procrastinate evaluates these cron expressions INSIDE the worker and defers
each job at the right moment (deduped, so two worker replicas can't
double-fire a period). Each periodic function is a SIGNAL CREATOR: it fans
out typed per-brand tasks; it never does the work itself (the granularity
rule — a task = unit of retry and approval, never unit of iteration).

One schedule per deliverable (#N = the 26-deliverables card in the design
doc), so the cadence roster reads straight off this file. Deliverables that
deliberately do NOT ride cron are listed at the bottom with reasons.
"""

from datetime import datetime

from objects.brand import Brand
from objects.site import Site
from utils.database import Database, ReplicaDatabase
from utils.logger import logger
from utils.task_queue import create_task
from worker.app import app
from worker.tasks.collection import poll_batches, submit_prompt_execution_batch


async def _create_task_per_active_brand(name: str, queue: str, payload: dict | None = None) -> int:
    """One typed task per brand — tasks row + job in ONE commit (create_task)."""
    async with ReplicaDatabase().connection():
        async with Database().connection():
            brands = await Brand.all()
            for brand in brands:
                await create_task(brand_id=brand.id, name=name, queue=queue, payload=payload)
            await Database.session.commit()
    return len(brands)


##############################
# COLLECTION & MONITORING (#18 rank + AI-answer movement)
##############################


@app.periodic(cron="")  # Thursday 00:00 UTC
@app.task(name="schedule_submit_prompt_execution_batch", queue="schedules")
async def schedule_submit_prompt_execution_batch() -> None:
    """#18 — collect the week's AI answers: one corpus-deduped batch per engine."""
    await submit_prompt_execution_batch.defer_async()
    logger.info(f"[schedules] schedule_submit_prompt_execution_batch tick ts={datetime.now()}")


@app.periodic(cron="")  # hourly
@app.task(name="schedule_poll_batches", queue="schedules")
async def schedule_poll_batches() -> None:
    """#18 — ingest finished provider batches (execution -> corpus, sentiment -> analysis).

    The execution lock serializes polls: an hourly tick can fire while the
    previous poll is still ingesting, and two concurrent polls would both see
    the same unprocessed batch and double-ingest it. Locked, the second job
    waits its turn and then finds nothing unprocessed."""
    await poll_batches.configure(lock="poll_batches").defer_async()
    logger.info(f"[schedules] schedule_poll_batches tick ts={datetime.now()}")


##############################
# WEEKLY SWEEPS
##############################


@app.periodic(cron="")  # Sundays 00:00 UTC
@app.task(name="schedule_visibility_run", queue="schedules")
async def schedule_visibility_run() -> None:
    """#17 — score the week's analyzed answers into one trend point per brand."""
    count = await _create_task_per_active_brand(name="visibility_run", queue="visibility")
    logger.info(f"[schedules] schedule_visibility_run tick ts={datetime.now()}: {count} sweeps")


@app.periodic(cron="")  # Sundays 01:00 UTC
@app.task(name="schedule_site_audits", queue="schedules")
async def schedule_site_audits() -> None:
    """#3 (ongoing) — crawl + audit rule engine over every CONNECTED site -> typed findings."""
    async with ReplicaDatabase().connection():
        async with Database().connection():
            sites = await Site.connected()
            for site in sites:
                await create_task(brand_id=site.brand_id, name="site_audit", queue="audit")
            await Database.session.commit()
    logger.info(f"[schedules] schedule_site_audits tick ts={datetime.now()}: {len(sites)} audits")


@app.periodic(cron="")  # Mondays 00:00 UTC — the source-poller sweep
@app.task(name="schedule_metrics_ingestion", queue="schedules")
async def schedule_metrics_ingestion() -> None:
    """#18 (dashboards) — orders/calls/form entries + the trailing week of daily
    GA4/GSC rows + GSC messages (no push API exists) -> one normalized store.
    Runner: worker/tasks/metrics.py task "metrics_ingestion"."""
    count = await _create_task_per_active_brand(name="metrics_ingestion", queue="metrics")
    logger.info(f"[schedules] schedule_metrics_ingestion tick ts={datetime.now()}: {count} ingestions")


@app.periodic(cron="")  # Mondays 02:00 UTC — after the sweep's ingestion lands
@app.task(name="schedule_alert_detection", queue="schedules")
async def schedule_alert_detection() -> None:
    """#19 — thresholds & change-points over fresh metrics (index drops, CWV
    regressions, site down) -> Pulse + new typed tasks (incl. #23 diagnosis).
    Runner: worker/tasks/metrics.py task "alert_detection"."""
    count = await _create_task_per_active_brand(name="alert_detection", queue="metrics")
    logger.info(f"[schedules] schedule_alert_detection tick ts={datetime.now()}: {count} detections")


@app.periodic(cron="")  # Mondays 06:00 UTC — the Pulse week-opener
@app.task(name="schedule_weekly_briefs", queue="schedules")
async def schedule_weekly_briefs() -> None:
    """Pulse — the Account Manager's Brief: serif voice over assembler facts.
    Runner: worker/tasks/briefs.py task "weekly_brief"."""
    count = await _create_task_per_active_brand(name="weekly_brief", queue="am")
    logger.info(f"[schedules] schedule_weekly_briefs tick ts={datetime.now()}: {count} briefs")


@app.periodic(cron="")  # Tuesdays 09:00 UTC
@app.task(name="schedule_link_prospecting", queue="schedules")
async def schedule_link_prospecting() -> None:
    """#13 — citation targets & PR placement prospecting; scout drafts, human sends.
    Runner: worker/tasks/outreach.py task "outreach_scout"."""
    count = await _create_task_per_active_brand(name="outreach_scout", queue="outreach", payload={"scan": "citation_targets"})
    logger.info(f"[schedules] schedule_link_prospecting tick ts={datetime.now()}: {count} prospect scans")


@app.periodic(cron="")  # Tuesdays 10:00 UTC
@app.task(name="schedule_community_scans", queue="schedules")
async def schedule_community_scans() -> None:
    """#21 — community thread opportunities (Reddit/Quora); scout finds, human posts."""
    count = await _create_task_per_active_brand(name="outreach_scout", queue="outreach", payload={"scan": "community_threads"})
    logger.info(f"[schedules] schedule_community_scans tick ts={datetime.now()}: {count} community scans")


@app.periodic(cron="")  # Tuesdays 11:00 UTC
@app.task(name="schedule_unlinked_mention_scans", queue="schedules")
async def schedule_unlinked_mention_scans() -> None:
    """#22 — unlinked mention reclamation; scout finds + drafts, human sends."""
    count = await _create_task_per_active_brand(name="outreach_scout", queue="outreach", payload={"scan": "unlinked_mentions"})
    logger.info(f"[schedules] schedule_unlinked_mention_scans tick ts={datetime.now()}: {count} mention scans")


@app.periodic(cron="")  # Wednesdays 10:00 UTC
@app.task(name="schedule_gbp_touches", queue="schedules")
async def schedule_gbp_touches() -> None:
    """#14 — GBP posts / Q&A behind the Reputation tone gate.
    Runner: worker/tasks/reputation.py task "gbp_management"."""
    count = await _create_task_per_active_brand(name="gbp_management", queue="reputation")
    logger.info(f"[schedules] schedule_gbp_touches tick ts={datetime.now()}: {count} touches")


@app.periodic(cron="")  # Wednesdays 11:00 UTC
@app.task(name="schedule_review_asks", queue="schedules")
async def schedule_review_asks() -> None:
    """#15 — review generation campaign: the ASKS ride the notifier; the
    reviews themselves are structurally the client's customers.
    Runner: worker/tasks/reputation.py task "review_asks"."""
    count = await _create_task_per_active_brand(name="review_asks", queue="reputation")
    logger.info(f"[schedules] schedule_review_asks tick ts={datetime.now()}: {count} campaigns")


##############################
# DAILY
##############################


@app.periodic(cron="")  # daily 09:00 UTC
@app.task(name="schedule_client_chases", queue="schedules")
async def schedule_client_chases() -> None:
    """#1 (the chase half) + all stale client tasks — the AM's escalating
    gentleness; the runner no-ops when nothing is overdue.
    Runner: worker/tasks/briefs.py task "client_chase"."""
    count = await _create_task_per_active_brand(name="client_chase", queue="am")
    logger.info(f"[schedules] schedule_client_chases tick ts={datetime.now()}: {count} chases")


@app.periodic(cron="")  # daily 13:00 UTC — same-day deadlines
@app.task(name="schedule_journalist_scans", queue="schedules")
async def schedule_journalist_scans() -> None:
    """#20 — journalist/source request scan; scout flags + drafts, human sends.
    Shallow until the Connectively/Qwoted adapters land — see outreach.py's
    TODO(#20)."""
    count = await _create_task_per_active_brand(name="outreach_scout", queue="outreach", payload={"scan": "journalist_requests"})
    logger.info(f"[schedules] schedule_journalist_scans tick ts={datetime.now()}: {count} scans")


##############################
# MONTHLY / QUARTERLY
##############################


@app.periodic(cron="")  # 1st of the month 07:00 UTC — before the report tick
@app.task(name="schedule_content_calendars", queue="schedules")
async def schedule_content_calendars() -> None:
    """#10 — the month's content calendar, drafted plan-aware; approved calendar
    rows fan out content_draft tasks (#11), not cron.
    Runner: worker/tasks/content.py task "content_calendar"."""
    count = await _create_task_per_active_brand(name="content_calendar", queue="content")
    logger.info(f"[schedules] schedule_content_calendars tick ts={datetime.now()}: {count} calendars")


@app.periodic(cron="")  # 1st of the month 08:00 UTC
@app.task(name="schedule_monthly_reports", queue="schedules")
async def schedule_monthly_reports() -> None:
    """#16 — report assembler fills every number; AM prose is written over it.
    Runner: worker/tasks/reports.py task "monthly_report"."""
    count = await _create_task_per_active_brand(name="monthly_report", queue="reports")
    logger.info(f"[schedules] schedule_monthly_reports tick ts={datetime.now()}: {count} reports")


@app.periodic(cron="")  # 1st of Jan/Apr/Jul/Oct 10:00 UTC
@app.task(name="schedule_quarterly_reviews", queue="schedules")
async def schedule_quarterly_reviews() -> None:
    """#24 — the Strategist's cadence: diagnoses + goals -> roadmap workstreams.
    Runner: worker/tasks/planning.py task "plan_update"."""
    count = await _create_task_per_active_brand(name="plan_update", queue="planning")
    logger.info(f"[schedules] schedule_quarterly_reviews tick ts={datetime.now()}: {count} plan updates")


# ============================================================================
# NOT cron-driven — every deliverable absent above, and why:
#
# Onboarding events (fire once when a brand is created/connected, from the
# onboarding flow — not from cron):
#   #1  Access provisioning — a CLIENT action: tasks rows with no queue job;
#       the daily chase (#above) is what nags them to completion.
#   #2  Kickoff interview — the AM conducts it at brand creation. AM (not a
#       dedicated worker) because the design doc assigns interviews to the AM
#       role: it is the client-facing voice, and the interview is relationship
#       surface (extracting margins/seasonality/constraints from the owner),
#       not analysis — the same voice that writes the Brief and chases.
#   #4  Keyword + AI-prompt research — runs at kickoff, re-runs on plan updates.
#   #5  Competitor & gap analysis — bootstrapped at kickoff; grown continuously
#       by run-time discovery (~5% sentiment-pack sampling).
#   #6  Baseline report — the brand's FIRST visibility_run + report, kicked off
#       by onboarding; the weekly/monthly schedules take over from there.
#   #7  Conversion tracking setup — onboarding config task + a client
#       verification task (event fires, human confirms).
#   #8  6-month roadmap — the first plan_update, fired by onboarding;
#       schedule_quarterly_reviews owns the refresh cadence.
#
# Chained from other work (the producing worker calls create_task; scheduling
# them on cron would double-run them):
#   #9  technical_fix — the site_audit runner (#3, Sundays) emits one per
#       actionable finding as it lands; the fix generators consume them.
#   #12 content_refresh — the alert detector (#19, Mondays) emits one per
#       decayed/underperforming page it flags.
#   #11 content_draft — fans out from the APPROVED content calendar (#10).
#   #23 algorithm-update triage — the alert detector (#19) emits diagnosis
#       tasks when it sees an update-shaped change-point; "as needed" has no cron.
#   technical_judgment — escalated by the fix generators (#9) per finding.
#
# Event-driven (webhooks / client actions on the API side):
#   review_response — new-review webhooks; fast reply is the point.
#   approvals — client approves an artifact -> change_set_apply.
#   Ask — free-text -> one bounded classify -> typed task; Analyst runs
#       synchronously in the request path.
#
# Covered by an existing cadence (no separate schedule on purpose):
#   #25 original data study — plan-driven (#24 emits it); the data must be
#       genuinely collected by the client.
#   #26 competitor re-baseline — continuous discovery sampling makes a
#       quarterly re-run redundant; revisit if discovery is ever turned off.
# ============================================================================
