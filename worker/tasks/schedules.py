"""Cadence — the Schedules band from the design doc's Level 2 diagram.

Procrastinate evaluates these cron expressions INSIDE the worker and defers
each job at the right moment (deduped, so two worker replicas can't
double-fire a period). Each periodic function is a SIGNAL CREATOR: it fans
out typed per-brand tasks; it never does the work itself (the granularity
rule — a task = unit of retry and approval, never unit of iteration).
"""

from datetime import datetime
from objects.brand import Brand
from utils.database import Database, ReplicaDatabase
from utils.logger import logger
from utils.task_queue import create_task
from worker.app import app
from worker.tasks.collection import poll_batches, submit_prompt_execution_batch


@app.periodic(cron="")  # Thursday 00:00 UTC
@app.task(name="schedule_submit_prompt_execution_batch", queue="schedules")
async def schedule_submit_prompt_execution_batch() -> None:
    await submit_prompt_execution_batch.defer_async()
    logger.info(f"[schedules] schedule_submit_prompt_execution_batch tick ts={datetime.now()}")


@app.periodic(cron="")  # hourly
@app.task(name="schedule_poll_batches", queue="schedules")
async def schedule_poll_batches() -> None:
    await poll_batches.defer_async()
    logger.info(f"[schedules] schedule_poll_batches tick ts={datetime.now()}")


@app.periodic(cron="")  # Sundays 00:00 UTC
@app.task(name="schedule_visibility_run", queue="schedules")
async def schedule_visibility_run() -> None:
    async with ReplicaDatabase().connection():
        async with Database().connection():
            brands = await Brand.all()
            for brand in brands:
                await create_task(brand_id=brand.id, name="visibility_run", queue="visibility")
            await Database.session.commit()
    logger.info(f"[schedules] schedule_visibility_run tick ts={datetime.now()}")


# @app.periodic(cron="")  # Sundays 01:00 UTC
# @app.task(name="weekly_site_audits", queue="schedules")
# async def weekly_site_audits() -> None:
#     logger.info(f"[schedules] weekly site audit tick ts={datetime.now()}")


# @app.periodic(cron="")  # Mondays 00:00 UTC
# @app.task(name="weekly_briefs", queue="schedules")
# async def weekly_briefs() -> None:
#     # TODO: for each active brand -> insert tasks row (type='visibility_sweep')
#     #       + defer the sweep job, in ONE transaction (transactional enqueue)
#     logger.info(f"[schedules] weekly brief tick ts={datetime.now()}")


# @app.periodic(cron="")  # 1st of the month 00:00 UTC
# @app.task(name="monthly_reports", queue="schedules")
# async def monthly_reports() -> None:
#     # TODO: for each brand -> tasks row (type='monthly_report') + defer the report assembler
#     logger.info(f"[schedules] monthly report tick ts={datetime.now()}")
