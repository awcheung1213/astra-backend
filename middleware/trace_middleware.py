from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from utils.trace import Trace


class TraceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        Trace.start_trace(request.headers.get("X-Trace-ID"))
        response = await call_next(request)
        response.headers["X-Trace-ID"] = Trace.get_trace_id()
        return response
