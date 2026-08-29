from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="monthly_report", queue="reports", retry=2)
async def monthly_report(task_id: str, brand_id: str) -> None:
    """#16 — the monthly client document: the report ASSEMBLER fills every
    number (visibility runs, metrics, shipped change sets, findings closed);
    AM prose is written OVER the facts, never by them. Emits the reports row
    (immutable once published)."""
    async with task_lifecycle(task_id):
        # TODO(#16): assemble facts (visibility runs, metric points, shipped change
        #       sets, findings closed, completed tasks) -> AM narrative OVER them
        #       (briefs.py's assembler pattern) -> immutable report artifact + Pulse
        pass
