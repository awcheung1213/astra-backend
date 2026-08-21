"""Cadence — the Schedules band from the design doc's Level 2 diagram.

Procrastinate evaluates these cron expressions INSIDE the worker and defers
each job at the right moment (deduped, so two worker replicas can't
double-fire a period). Each periodic function is a SIGNAL CREATOR: it fans
out typed per-brand tasks; it never does the work itself (the granularity
rule — a task = unit of retry and approval, never unit of iteration).
"""

from utils.logger import logger
from worker.app import app


@app.periodic(cron="0 6 * * 1")  # Mondays 06:00 UTC
@app.task(name="schedule_weekly_prompt_executions", queue="schedules")
async def schedule_weekly_prompt_executions(timestamp: int) -> None:
    logger.info(f"[schedules] weekly prompt executions tick ts={timestamp} — stub")


@app.periodic(cron="0 * * * *")  # hourly, on the hour
@app.task(name="schedule_hourly_poll_batches", queue="schedules")
async def schedule_poll_batches(timestamp: int) -> None:
    # TODO: batches WHERE processed = false -> check provider status against its
    #       TERMINAL_STATUSES -> defer the ingest task for terminal ones.
    #       Cheap no-op (one indexed query) when nothing is in flight.
    logger.info(f"[schedules] poll batches tick ts={timestamp} — stub")


@app.periodic(cron="0 6 * * 1")  # Mondays 06:00 UTC
@app.task(name="schedule_weekly_visibility_", queue="schedules")
async def schedule_weekly_visibility_(timestamp: int) -> None:
    # TODO: for each active brand -> insert tasks row (type='visibility_sweep')
    #       + defer the sweep job, in ONE transaction (transactional enqueue)
    logger.info(f"[schedules] weekly sweep tick ts={timestamp} — stub, no brands fanned out yet")


@app.periodic(cron="0 7 * * 3")  # Wednesdays 07:00 UTC
@app.task(name="schedule_weekly_audits", queue="schedules")
async def schedule_weekly_audits(timestamp: int) -> None:
    # TODO: for each connected site -> tasks row (type='site_audit') + defer site_audit
    logger.info(f"[schedules] weekly audit tick ts={timestamp} — stub")


@app.periodic(cron="0 8 1 * *")  # 1st of the month 08:00 UTC
@app.task(name="schedule_monthly_reports", queue="schedules")
async def schedule_monthly_reports(timestamp: int) -> None:
    # TODO: for each brand -> tasks row (type='monthly_report') + defer the report assembler
    logger.info(f"[schedules] monthly report tick ts={timestamp} — stub")
