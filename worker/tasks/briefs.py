from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="weekly_brief", queue="am", retry=2)
async def weekly_brief(task_id: str, brand_id: str) -> None:
    """Pulse — the Account Manager's Monday Brief: serif voice over
    assembler-supplied facts (last week's movement, what shipped, what's
    waiting on the client). Emits a `brief` artifact; facts never
    hallucinated. Logic lands in utils/tasks/briefs.py."""
    async with task_lifecycle(task_id):
        # TODO: assemble facts -> AM agent -> Artifact(type="brief")
        pass


@app.task(name="client_chase", queue="am", retry=2)
async def client_chase(task_id: str, brand_id: str) -> None:
    """#1 (the chase half) + all stale client tasks — the AM's escalating
    gentleness. Scans the brand's overdue client-contribution tasks (access
    grants, SME answers, photo uploads) and NO-OPS when nothing is stale;
    otherwise drafts the nudge and sends via the notifier adapter."""
    async with task_lifecycle(task_id):
        # TODO: overdue client tasks -> no-op if none -> AM nudge -> notifier
        pass
