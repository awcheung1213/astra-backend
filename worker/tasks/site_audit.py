"""First pipeline stub: the site audit.

Fires when a site connects (onboarding step 4) and weekly thereafter.
Created ONLY via utils.task_queue.create_task() (tasks row + job, one commit).
TODO (in order):
  1. glue to the `tasks` table: load the row by task_id, update status
     (running -> done/failed/dead), enforce lifetime token budget across retries
  2. crawl via the site adapter, diff against last snapshot
  3. run the audit rule engine -> typed findings
  4. findings map to fix-generator tasks; odd cases -> `technical-judgment`
"""

from worker.app import app


@app.task(name="site_audit", queue="audit")
async def site_audit(task_id: str, brand_id: str) -> dict:
    # Stub — proves the queue round-trips. Replace with the real pipeline.
    print(f"[site_audit] task={task_id} brand={brand_id} — stub ran")
    return {"task_id": task_id, "brand_id": brand_id, "status": "stub"}
