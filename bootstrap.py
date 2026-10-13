import logging

from settings import LOG_LEVEL, PROJECT_NAME
from utils.logger import logger
from utils.trace import Trace

# disable 3rd party logging
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("procrastinate").setLevel(logging.WARNING)
# app log level: DEBUG locally (request bodies/headers), INFO in production (settings.LOG_LEVEL)
logger.logger.setLevel(getattr(logging, str(LOG_LEVEL).upper(), logging.INFO))  # type: ignore

# initialize trace logging
Trace.initializing_logging(f"%(trace_id)s:%(levelname)s:{PROJECT_NAME}:%(message)s")
