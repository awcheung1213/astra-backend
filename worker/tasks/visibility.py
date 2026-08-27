from utils.task_queue import task_lifecycle
from utils.tasks.visibility import run_visibility_sweep
from worker.app import app


@app.task(name="visibility_run", queue="visibility", retry=2)
async def visibility_run(task_id: str, brand_id: str) -> None:
    """#17 — one brand's weekly sweep: newest analyzed answers -> per-market
    scores -> one framework-versioned trend point. Idempotent per brand-week
    (get-or-create), so retries resume safely. $0 — score-only, no LLM calls."""
    async with task_lifecycle(task_id):
        await run_visibility_sweep(brand_id)
