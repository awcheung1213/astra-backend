import logging

from settings import PROJECT_NAME
from utils.logger import logger
from utils.trace import Trace

# disable 3rd party logging
logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("procrastinate").setLevel(logging.WARNING)
logger.logger.setLevel(logging.DEBUG)  # type: ignore

# initialize trace logging
Trace.initializing_logging(f"%(trace_id)s:%(levelname)s:{PROJECT_NAME}:%(message)s")
