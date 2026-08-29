from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="gbp_management", queue="reputation", retry=2)
async def gbp_management(task_id: str, brand_id: str) -> None:
    """#14 — weekly GBP touches: posts + Q&A in brand voice, behind the
    Reputation tone gate. (Review REPLIES are not here — they arrive via the
    new-review webhook as review_response tasks.)"""
    async with task_lifecycle(task_id):
        # TODO(#14): recent brand activity -> Reputation agent drafts posts/Q&A ->
        #       Artifact behind the tone gate; approval posts via the GBP adapter
        pass


@app.task(name="review_asks", queue="reputation", retry=2)
async def review_asks(task_id: str, brand_id: str) -> None:
    """#15 — review generation campaign: the ASKS ride the notifier adapter;
    the reviews themselves are structurally the client's customers to give."""
    async with task_lifecycle(task_id):
        # TODO(#15): eligible recent customers -> ask drafts -> notifier adapter
        pass


# TODO(#14, the reply half) review_response — runner lands HERE, registered in
#       the same change as its producer (the new-review webhook endpoint):
#       every review gets a drafted reply in brand voice, plus a flag_request
#       artifact when it violates platform policy; replies post via the GBP
#       adapter behind the approval gate (outward -> always-approve floor).
