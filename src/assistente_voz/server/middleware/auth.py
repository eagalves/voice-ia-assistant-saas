from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..auth.jti_cache import JTICache
from ..auth.jwks_cache import JWKSCache, JWKSUnavailableError
from ..auth.jwt_validator import AuthError, JWTValidator
from ..config import ServerSettings, get_settings

PROTECTED_PREFIX = "/v1/"


def _make_401(code: str, message: str) -> JSONResponse:
    resp = JSONResponse(
        status_code=401,
        content={"error": code, "message": message},
    )
    resp.headers["X-API-Version"] = "1.0"
    return resp


def _make_503(message: str) -> JSONResponse:
    resp = JSONResponse(
        status_code=503,
        content={"error": "AUTH_JWKS_UNAVAILABLE", "message": message},
    )
    resp.headers["X-API-Version"] = "1.0"
    return resp


class BearerPresenceMiddleware(BaseHTTPMiddleware):
    """HTTP Bearer auth middleware — Issue #6.

    When ``JWT_JWKS_URL`` is set, performs full JWT validation (RS256/ES256,
    mandatory claims, JWKS, anti-replay).

    When ``JWT_JWKS_URL`` is empty (default / tests), falls back to the
    Issue #1 stub: accepts any non-empty Bearer string without cryptographic
    verification.  This keeps all pre-Issue-#6 tests green.
    """

    def __init__(self, app, settings: ServerSettings | None = None) -> None:
        super().__init__(app)
        self._settings: ServerSettings = settings or get_settings()
        self._validator: JWTValidator | None = None
        self._init_validator()

    def _init_validator(self) -> None:
        url = self._settings.jwt_jwks_url
        if not url:
            # Bypass mode — no JWT validation.
            return

        async def _http_fetcher(jwks_url: str) -> dict:
            import httpx  # lazy import; not in prod deps yet — add if needed
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(jwks_url)
                resp.raise_for_status()
                return resp.json()

        jwks_cache = JWKSCache(url, _http_fetcher)
        jti_cache = JTICache()
        self._validator = JWTValidator(
            jwks_cache=jwks_cache,
            jti_cache=jti_cache,
            iss_allowlist=self._settings.jwt_iss_allowlist,
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        if not request.url.path.startswith(PROTECTED_PREFIX):
            return await call_next(request)

        # WebSocket upgrades authenticate via ws_token query parameter.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return _make_401(
                "AUTH_BEARER_MISSING",
                "Authorization header with Bearer token is required.",
            )

        token = auth_header[len("Bearer "):].strip()
        if not token:
            return _make_401("AUTH_BEARER_MISSING", "Bearer token must not be empty.")

        request.state.bearer_token = token

        if self._validator is None:
            # Bypass mode: accept any non-empty Bearer string.
            request.state.actor_sub = token
            request.state.jwt_payload = {}
            return await call_next(request)

        # Full JWT validation.
        try:
            payload = await self._validator.validate(token)
        except JWKSUnavailableError as exc:
            return _make_503(str(exc))
        except AuthError as exc:
            return _make_401(exc.code, exc.message)

        request.state.jwt_payload = payload
        request.state.actor_sub = payload.get("sub", token)
        return await call_next(request)

