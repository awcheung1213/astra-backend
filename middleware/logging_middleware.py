import json

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from utils.logger import logger


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # log the request
        logger.debug("Request Received", priority=1)
        logger.debug(f"method: {request.method}")
        logger.debug(f"url: {request.url}")
        request_headers = dict(request.headers)
        if "authorization" in request_headers:
            request_headers["authorization"] = "*****"
        logger.debug(f"headers: {request_headers}")
        try:
            request_body = json.loads(await request.body())
        except json.JSONDecodeError:
            request_body = {}
        logger.debug(f"body: {request_body}")

        # get the response
        response = await call_next(request)

        # get the response body
        response_body = b""
        async for chunk in response.body_iterator:
            response_body += chunk

        # log the response
        logger.debug("Response Sent", priority=1)
        logger.debug(f"status: {response.status_code}")
        logger.debug(f"headers: {dict(response.headers)}")
        logger.debug(f"body: {response_body.decode()}")

        return Response(
            content=response_body,
            status_code=response.status_code,
            headers=response.headers,
            media_type=response.media_type,
        )
