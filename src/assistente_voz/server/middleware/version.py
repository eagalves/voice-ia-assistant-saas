from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

API_VERSION = "1.0"


class ApiVersionMiddleware(BaseHTTPMiddleware):
    """Add `X-API-Version: 1.0` to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-API-Version"] = API_VERSION
        return response
