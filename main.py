from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import bootstrap  # noqa
from middleware.database_session_middleware import DatabaseSessionMiddleware
from middleware.internal_server_error_middleware import InternalServerErrorMiddleware
from middleware.logging_middleware import LoggingMiddleware
from middleware.trace_middleware import TraceMiddleware
from schemas.index_schemas import IndexResponse
from settings import CORS_ORIGINS, ENVIRONMENT, PROJECT_NAME, REGION, VERSION
from utils.logger import logger
from worker.app import app as queue_app


@asynccontextmanager
async def lifespan(_: FastAPI):
    # procrastinate's connector is per-process: the worker opens its own in
    # worker/run.py; the api opens its own here so endpoints can defer jobs
    async with queue_app.open_async():
        yield


app = FastAPI(
    title=PROJECT_NAME.capitalize(),
    version=VERSION,
    lifespan=lifespan,
)

logger.info(f"boot: service=api env={ENVIRONMENT}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# routers register here as resources land (api/organizations.py, api/brands.py, ...)

app.add_middleware(InternalServerErrorMiddleware)
app.add_middleware(DatabaseSessionMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(TraceMiddleware)


@app.get("/")
async def index() -> IndexResponse:
    return IndexResponse.create(
        {
            "project": PROJECT_NAME,
            "environment": ENVIRONMENT,
            "region": REGION,
            "version": VERSION,
            "timestamp": datetime.now().isoformat(),
        }
    )
