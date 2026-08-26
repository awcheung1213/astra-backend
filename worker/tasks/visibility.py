from utils.logger import logger
from worker.app import app
from utils.collector import Collector
from utils.database import Database, ReplicaDatabase


@app.task(name="visibility_run", queue="visibility")
async def visibility_run() -> None:
    logger.info(f"[visibility] visibility_run - start", priority="medium")

    async with ReplicaDatabase().connection():
        async with Database().connection():
            await 
    
    logger.info(f"[visibility] visibility_run - end", priority="medium")