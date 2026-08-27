from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="gbp_management", queue="reputation", retry=2)
async def gbp_management(task_id: str, brand_id: str) -> None:
    """#14 — weekly GBP touches: posts + Q&A in brand voice, behind the
    Reputation tone gate. (Review REPLIES are not here — they arrive via the
    new-review webhook as review_response tasks.)"""
    async with task_lifecycle(task_id):
        # TODO: draft posts/Q&A -> Artifact behind tone gate -> GBP via adapter
        pass


@app.task(name="review_asks", queue="reputation", retry=2)
async def review_asks(task_id: str, brand_id: str) -> None:
    """#15 — review generation campaign: the ASKS ride the notifier adapter;
    the reviews themselves are structurally the client's customers to give."""
    async with task_lifecycle(task_id):
        # TODO: eligible recent customers -> ask drafts -> notifier adapter
        pass
