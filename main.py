import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import bootstrap  # noqa
from middleware.database_session_middleware import DatabaseSessionMiddleware
from middleware.internal_server_error_middleware import InternalServerErrorMiddleware
from middleware.logging_middleware import LoggingMiddleware, RedactAccessLogFilter
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
logging.getLogger("uvicorn.access").addFilter(RedactAccessLogFilter())  # one-tap WordPress's password rides a query string

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# routers register here as resources land (api/organizations.py, api/brands.py, ...)
from fastapi import Depends  # noqa: E402

from api.approvals import router as approvals_router  # noqa: E402
from api.ask import router as ask_router  # noqa: E402
from api.brands import router as brands_router  # noqa: E402
from api.competitors import router as competitors_router  # noqa: E402
from api.connections import public_router as connections_public_router  # noqa: E402
from api.connections import router as connections_router  # noqa: E402
from api.me import router as me_router  # noqa: E402
from api.onboarding import router as onboarding_router  # noqa: E402
from api.prompts import router as prompts_router  # noqa: E402
from api.reads import router as reads_router  # noqa: E402
from api.webhooks import router as webhooks_router  # noqa: E402
from utils.authz import guard  # noqa: E402

GUARDED = [Depends(guard)]
app.include_router(approvals_router, dependencies=GUARDED)
app.include_router(ask_router, dependencies=GUARDED)
app.include_router(brands_router, dependencies=GUARDED)
app.include_router(competitors_router, dependencies=GUARDED)
app.include_router(connections_router, dependencies=GUARDED)
app.include_router(connections_public_router)
app.include_router(me_router, dependencies=GUARDED)
app.include_router(onboarding_router, dependencies=GUARDED)
app.include_router(prompts_router, dependencies=GUARDED)
app.include_router(reads_router, dependencies=GUARDED)
app.include_router(webhooks_router)

# last added = outermost. InternalServerError must wrap DatabaseSession so an
# unhandled exception reaches the transaction (rollback) before becoming a 500
app.add_middleware(DatabaseSessionMiddleware)
app.add_middleware(InternalServerErrorMiddleware)
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
