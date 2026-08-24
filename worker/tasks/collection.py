from utils.logger import logger
from worker.app import app
from objects.organization import Organization



@app.task(name="submit_prompt_execution_batch", queue="collection")
async def submit_prompt_execution_batch() -> None:
    logger.info(f"[collection] submit_prompt_execution_batch - end", priority="medium")


    logger.info(f"[collection] submit_prompt_execution_batch - end", priority="medium")


@app.task(name="poll_batches", queue="collection")
async def poll_batches() -> None:
    logger.info(f"[collection] poll_batches - start", priority="medium")
    logger.info(f"[collection] poll_batches - end", priority="medium")


# @app.task(name="ingest_execution_batch", queue="collection")
# async def ingest_execution_batch() -> None:
#     logger.info(f"[collection] ingest_execution_batch - end", priority="medium")


# @app.task(name="ingest_sentiment_batch", queue="collection")
# async def ingest_sentiment_batch() -> None:
#     logger.info(f"[collection] ingest_sentiment_batch - end", priority="medium")
