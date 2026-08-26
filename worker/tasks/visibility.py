from objects.task import STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, Task
from utils.database import Database, ReplicaDatabase
from utils.logger import logger
from utils.visibility import run_visibility_sweep
from worker.app import app


@app.task(name="visibility_run", queue="visibility", retry=2)
async def visibility_run(task_id: str, brand_id: str) -> None:
    logger.info(f"[visibility] visibility_run - start (task {task_id}, brand {brand_id})", priority="medium")

    async with ReplicaDatabase().connection():
        async with Database().connection():
            await Task.update_status(task_id, STATUS_RUNNING)
            await Database.session.commit()
            try:
                await run_visibility_sweep(brand_id)
                await Task.update_status(task_id, STATUS_DONE)
                await Database.session.commit()
            except Exception as exc:
                await Database.session.rollback()
                await Task.update_status(task_id, STATUS_FAILED)
                await Database.session.commit()
                logger.error(f"[visibility] visibility_run failed for brand {brand_id}: {exc}")
                raise  # let procrastinate retry; the sweep resumes its run row

    logger.info(f"[visibility] visibility_run - end (task {task_id})", priority="medium")
