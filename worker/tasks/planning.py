from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="plan_update", queue="planning", retry=2)
async def plan_update(task_id: str, brand_id: str) -> None:
    """#24 (quarterly trigger) / #8 (first run, fired by onboarding) — the
    Strategist: diagnoses + goals -> roadmap workstreams and priorities.
    Emits a plan artifact; approved plans fan out content_draft /
    outreach_scout work."""
    async with task_lifecycle(task_id):
        # TODO(#24/#8): diagnoses + goals + metrics -> Strategist agent -> plan
        #       artifact behind the gate; approval fans out workstreams
        #       (content_draft, outreach emphasis, #25 client data tasks, #4 re-run)
        pass


# TODO(#23) diagnosis — runner lands HERE, registered in the same change as its
#       producer (alert_detection; the Ask endpoint also emits it): Analyst
#       agent over metrics + the change ledger -> diagnosis artifact with
#       receipts (every causal claim tied to a dated data point); feeds the
#       next plan_update.
