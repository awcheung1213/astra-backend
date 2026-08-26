from utils.logger import logger
from worker.app import app
from utils.collector import Collector
from utils.database import Database, ReplicaDatabase


@app.task(name="submit_prompt_execution_batch", queue="collection")
async def submit_prompt_execution_batch() -> None:
    logger.info(f"[collection] submit_prompt_execution_batch - end", priority="medium")

    async with ReplicaDatabase().connection():
        async with Database().connection():
            await Collector.submit_prompt_execution_batch()
    
    logger.info(f"[collection] submit_prompt_execution_batch - end", priority="medium")


@app.task(name="poll_batches", queue="collection")
async def poll_batches() -> None:
    logger.info(f"[collection] poll_batches - start", priority="medium")

    async with ReplicaDatabase().connection():
            async with Database().connection():
                await Collector.poll_batches()

    logger.info(f"[collection] poll_batches - end", priority="medium")
