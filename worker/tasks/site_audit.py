from utils.task_queue import task_lifecycle
from utils.tasks.site_audit import run_site_audit
from worker.app import app


@app.task(name="site_audit", queue="audit", retry=2)
async def site_audit(task_id: str, brand_id: str) -> None:
    """#3 (ongoing) — crawl + audit rule engine over the brand's connected
    site -> typed findings. Fires at onboarding (site connect) and weekly via
    schedule_site_audits. Chains #9: each NEW actionable finding emits a
    technical_fix task (inside run_site_audit). Idempotent: re-detected
    findings dedupe, vanished ones auto-resolve — retries are safe."""
    async with task_lifecycle(task_id):
        await run_site_audit(brand_id)
