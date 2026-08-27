from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="content_calendar", queue="content", retry=2)
async def content_calendar(task_id: str, brand_id: str) -> None:
    """#10 — the month's content calendar, drafted plan-aware by the Content
    agent -> calendar artifact behind the approval gate. Approved rows fan out
    content_draft tasks (#11) — from the approval signal, never from cron."""
    async with task_lifecycle(task_id):
        # TODO: plan + clusters + decay data -> Content agent -> calendar artifact
        pass
