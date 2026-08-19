"""Worker service entrypoint.

Railway start command:  python -m worker.run   (railway.worker.json)
Local:                  the astra-backend-worker service in docker compose
"""

import asyncio

import bootstrap  # noqa
import settings
from utils.logger import logger
from worker.app import app


async def _main() -> None:
    logger.info(f"boot: service=worker env={settings.ENVIRONMENT} concurrency={settings.WORKER_CONCURRENCY}")
    async with app.open_async():
        await app.run_worker_async(concurrency=settings.WORKER_CONCURRENCY)


if __name__ == "__main__":
    asyncio.run(_main())
