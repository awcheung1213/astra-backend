from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="plan_update", queue="planning", retry=2)
async def plan_update(task_id: str, brand_id: str) -> None:
    """#24 (quarterly trigger) / #8 (first run, fired by onboarding) — the
    Strategist: diagnoses + goals -> roadmap workstreams and priorities.
    Emits a plan artifact; approved plans fan out content_draft /
    outreach_scout work."""
    async with task_lifecycle(task_id):
        # TODO: diagnoses + goals + metrics -> Strategist agent -> plan artifact
        pass
