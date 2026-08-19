from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from utils.database import Database, ReplicaDatabase


class DatabaseSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # create database connection
        async with ReplicaDatabase().connection():
            async with Database().connection():
                async with Database.session.transaction():  # type: ignore
                    response = await call_next(request)

        return response
