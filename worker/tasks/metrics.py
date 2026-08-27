from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="metrics_ingestion", queue="metrics", retry=2)
async def metrics_ingestion(task_id: str, brand_id: str) -> None:
    """#18 (dashboards) — the Monday source-poller sweep: orders/calls/form
    entries + the trailing week of daily GA4/GSC rows + GSC messages (no push
    API exists) -> one normalized metric_points store (upsert on re-sync).
    Logic lands in utils/tasks/metrics.py; external reads via the Google adapter."""
    async with task_lifecycle(task_id):
        # TODO: GSC/GA4/calls/orders -> MetricPoint upserts
        pass


@app.task(name="alert_detection", queue="metrics", retry=2)
async def alert_detection(task_id: str, brand_id: str) -> None:
    """#19 — thresholds & change-points over the freshly ingested metrics
    (index drops, CWV regressions, site down) -> Pulse notifications + chained
    tasks: content_refresh per decayed page (#12) and diagnosis on
    update-shaped change-points (#23). Logic lands in utils/tasks/metrics.py."""
    async with task_lifecycle(task_id):
        # TODO: thresholds/change-points -> notifications
        #       + create_task(name="content_refresh" | "diagnosis", ...)
        pass
