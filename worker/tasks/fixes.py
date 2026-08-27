from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="technical_fix", queue="fixes", retry=2)
async def technical_fix(task_id: str, brand_id: str) -> None:
    """#9 — fix generators: finding-kind -> fix map (schema, metas, titles).
    CHAINED, never cron: produced by the site_audit runner, one per new
    actionable finding (payload carries finding_id). Emits a change_proposal
    artifact behind the approval gate; odd cases escalate to the Technical
    agent as technical_judgment. Logic lands in utils/tasks/fixes.py."""
    async with task_lifecycle(task_id) as task:
        finding_id = (task.payload or {}).get("finding_id", "")
        # TODO: load the finding -> its kind's fix generator ->
        #       Artifact(type="change_proposal") | escalate technical_judgment
        _ = finding_id
