"""JWT validator — Issue #6.

Validates RS256 or ES256 JWTs against a JWKS-backed key cache, with:
- Algorithm allowlist (RS256, ES256 only; HS256 / ``none`` rejected)
- Mandatory claims: ``iss``, ``sub``, ``jti``, ``exp``, ``iat``, ``nbf``, ``aud``
- ``iss`` must be in the configured allowlist
- ``exp − iat`` window ≤ 900s
- Clock skew tolerance ±60s
- ``jti`` anti-replay via ``JTICache``

``clock`` is injectable for deterministic time-based tests.

Error codes (all map to HTTP 401 unless noted):
- AUTH_ALGORITHM_NOT_ALLOWED
- AUTH_CLAIM_MISSING
- AUTH_ISS_NOT_ALLOWED
- AUTH_JWT_WINDOW_EXCEEDED
- AUTH_JTI_REPLAY
- AUTH_JWT_INVALID   (catch-all: expired, bad signature, missing/unknown kid)
- AUTH_JWKS_UNAVAILABLE → 503 (raised as JWKSUnavailableError, handled by middleware)
"""
from __future__ import annotations

import time
from collections.abc import Callable

import jwt

from .jti_cache import JTICache
from .jwks_cache import JWKSCache, JWKSKeyNotFoundError, JWKSUnavailableError

ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"RS256", "ES256"})
FIXED_AUDIENCE: str = "urn:assistente-voz:fase2"
MAX_WINDOW_S: int = 900   # 15 min
CLOCK_SKEW_S: int = 60    # ±1 min

REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "jti", "exp", "iat", "nbf", "aud")

__all__ = [
    "ALLOWED_ALGORITHMS",
    "AuthError",
    "CLOCK_SKEW_S",
    "FIXED_AUDIENCE",
    "JWKSUnavailableError",
    "JWTValidator",
    "MAX_WINDOW_S",
    "REQUIRED_CLAIMS",
]


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class JWTValidator:
    """Stateful JWT validator backed by JWKS cache and JTI cache."""

    def __init__(
        self,
        jwks_cache: JWKSCache,
        jti_cache: JTICache,
        iss_allowlist: list[str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._jwks = jwks_cache
        self._jti = jti_cache
        self._iss_allowlist = list(iss_allowlist)
        self._clock = clock

    async def validate(self, token: str) -> dict:
        """Validate *token* and return the decoded payload claims.

        Raises:
            AuthError             — for any auth failure (401 or as noted).
            JWKSUnavailableError  — when JWKS is unreachable beyond fallback (→ 503).
        """
        # 1. Inspect header without verification to check alg.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.exceptions.DecodeError as exc:
            raise AuthError(
                "AUTH_JWT_INVALID", f"Cannot decode JWT header: {exc}"
            ) from exc

        alg = header.get("alg", "")
        if alg not in ALLOWED_ALGORITHMS:
            raise AuthError(
                "AUTH_ALGORITHM_NOT_ALLOWED",
                f"Algorithm '{alg}' is not allowed. Use RS256 or ES256.",
            )

        kid = str(header.get("kid") or "")
        if not kid:
            raise AuthError("AUTH_JWT_INVALID", "JWT header must carry a 'kid'.")

        # 2. Fetch public key via JWKS cache (may raise JWKSUnavailableError → 503).
        try:
            jwk = await self._jwks.get_key(kid)
        except JWKSKeyNotFoundError as exc:
            raise AuthError(
                "AUTH_JWT_INVALID", f"Unknown signing key kid={kid!r}."
            ) from exc

        # 3. Decode and verify signature + standard claims.
        try:
            payload: dict = jwt.decode(
                token,
                jwk.key,
                algorithms=[alg],
                audience=FIXED_AUDIENCE,
                leeway=CLOCK_SKEW_S,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.exceptions.MissingRequiredClaimError as exc:
            raise AuthError(
                "AUTH_CLAIM_MISSING", f"Required claim missing: {exc}"
            ) from exc
        except jwt.exceptions.InvalidAlgorithmError as exc:
            raise AuthError(
                "AUTH_ALGORITHM_NOT_ALLOWED", str(exc)
            ) from exc
        except jwt.exceptions.ExpiredSignatureError as exc:
            raise AuthError("AUTH_JWT_INVALID", f"Token expired: {exc}") from exc
        except jwt.exceptions.DecodeError as exc:
            raise AuthError("AUTH_JWT_INVALID", f"JWT decode error: {exc}") from exc
        except jwt.exceptions.PyJWTError as exc:
            raise AuthError("AUTH_JWT_INVALID", str(exc)) from exc

        # 4. iss allowlist (if configured; empty list = bypass for tests).
        if self._iss_allowlist and payload.get("iss") not in self._iss_allowlist:
            raise AuthError(
                "AUTH_ISS_NOT_ALLOWED",
                f"Issuer '{payload.get('iss')}' is not in the allowed list.",
            )

        # 5. Window check: exp − iat ≤ MAX_WINDOW_S.
        iat = payload.get("iat", 0)
        exp = payload.get("exp", 0)
        if (exp - iat) > MAX_WINDOW_S:
            raise AuthError(
                "AUTH_JWT_WINDOW_EXCEEDED",
                f"Token window {exp - iat}s exceeds maximum {MAX_WINDOW_S}s.",
            )

        # 6. Anti-replay jti check.
        jti: str = str(payload.get("jti") or "")
        if not jti:
            raise AuthError("AUTH_CLAIM_MISSING", "Claim 'jti' is required.")

        if await self._jti.seen(jti):
            raise AuthError(
                "AUTH_JTI_REPLAY",
                f"Token jti='{jti}' has already been used.",
            )
        await self._jti.mark(jti)

        return payload
