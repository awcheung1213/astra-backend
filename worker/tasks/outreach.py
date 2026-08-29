from utils.logger import logger
from utils.task_queue import task_lifecycle
from worker.app import app

# payload["scan"] selects the scan; one scout, four hunting grounds:
SCAN_JOURNALIST_REQUESTS = "journalist_requests"  # #20 — daily, same-day deadlines
SCAN_CITATION_TARGETS = "citation_targets"  # #13 — weekly link/PR prospecting
SCAN_COMMUNITY_THREADS = "community_threads"  # #21 — weekly Reddit/Quora digest
SCAN_UNLINKED_MENTIONS = "unlinked_mentions"  # #22 — weekly reclamation


@app.task(name="outreach_scout", queue="outreach", retry=2)
async def outreach_scout(task_id: str, brand_id: str) -> None:
    """#13/#20/#21/#22 — the Outreach scout: finds & drafts, NEVER sends.
    Emits pitch + talking-point artifacts routed to the human sender —
    sending stays outside the system by design."""
    async with task_lifecycle(task_id) as task:
        scan = (task.payload or {}).get("scan", "")
        # TODO(#13/#20/#21/#22): dispatch on `scan` -> scout agent (ROLE_OUTREACH_SCOUT,
        #       web search ON) -> one Artifact(type="pitch") per opportunity + a Pulse
        #       notification for #20 (same-day deadlines ride urgency=urgent)
        logger.info(f"[outreach_scout] scan={scan} for brand {brand_id} — stub")
