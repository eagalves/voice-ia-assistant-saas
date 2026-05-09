from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

PROTECTED_PREFIX = "/v1/"


class BearerPresenceMiddleware(BaseHTTPMiddleware):
    """Issue #1 stub auth — only checks that a Bearer token is present.

    Issue #6 replaces this with full JWT validation (algorithms, claims,
    JWKS, anti-replay). Until then, any non-empty Bearer string is
    accepted; missing or malformed → 401 AUTH_BEARER_MISSING.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(PROTECTED_PREFIX):
            return await call_next(request)

        # WebSocket upgrades authenticate via ws_token query parameter;
        # Bearer header is not available during the HTTP→WS handshake.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "AUTH_BEARER_MISSING",
                    "message": "Authorization header with Bearer token is required.",
                },
            )
            response.headers["X-API-Version"] = "1.0"
            return response

        token = auth_header[len("Bearer ") :].strip()
        if not token:
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "AUTH_BEARER_MISSING",
                    "message": "Bearer token must not be empty.",
                },
            )
            response.headers["X-API-Version"] = "1.0"
            return response

        request.state.bearer_token = token
        return await call_next(request)
