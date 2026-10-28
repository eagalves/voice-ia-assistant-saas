"""JWKS cache with refresh-on-miss and 60-minute fallback — Issue #6.

Normal TTL: 10 minutes (``CACHE_TTL_S``).
Fallback TTL after failed refresh: up to 60 minutes (``FALLBACK_TTL_S``).
If more than 60 minutes pass with no valid JWKS, raises ``JWKSUnavailableError``
(→ 503 ``AUTH_JWKS_UNAVAILABLE``).

``kid`` not in cache → immediate refresh (one attempt); if the refreshed
JWKS still does not contain it, ``JWKSKeyNotFoundError`` is raised — that is
a *client* problem (→ 401), not an outage. Refresh-on-miss is rate limited
(``MIN_REFRESH_INTERVAL_S``) so a flood of tokens with bogus ``kid`` values
cannot turn the IdP into a DoS target.

``JWKSFetcher`` is injectable for tests (avoids real HTTP calls).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from jwt import PyJWK

CACHE_TTL_S: float = 600.0     # 10 min
FALLBACK_TTL_S: float = 3600.0  # 60 min
MIN_REFRESH_INTERVAL_S: float = 5.0  # throttle refresh-on-miss


class JWKSUnavailableError(Exception):
    """Raised when JWKS cannot be fetched and fallback TTL is exceeded."""


class JWKSKeyNotFoundError(Exception):
    """Raised when a fresh JWKS does not contain the requested ``kid``."""


# Callable that takes a JWKS URL and returns the raw JWKS dict.
JWKSFetcher = Callable[[str], Awaitable[dict]]


class JWKSCache:
    """Asyncio-friendly JWKS cache with kid-indexed PyJWK objects."""

    def __init__(
        self,
        jwks_url: str,
        fetcher: JWKSFetcher,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_interval_s: float = MIN_REFRESH_INTERVAL_S,
    ) -> None:
        self._url = jwks_url
        self._fetcher = fetcher
        self._clock = clock
        self._min_refresh_interval_s = min_refresh_interval_s

        # kid → PyJWK
        self._keys: dict[str, PyJWK] = {}
        # When cache was last successfully refreshed (monotonic)
        self._last_refresh_at: float | None = None

    def _is_cache_fresh(self) -> bool:
        if self._last_refresh_at is None:
            return False
        return (self._clock() - self._last_refresh_at) < CACHE_TTL_S

    def _is_within_fallback(self) -> bool:
        if self._last_refresh_at is None:
            return False
        return (self._clock() - self._last_refresh_at) < FALLBACK_TTL_S

    def _refreshed_recently(self) -> bool:
        if self._last_refresh_at is None:
            return False
        return (self._clock() - self._last_refresh_at) < self._min_refresh_interval_s

    async def _refresh(self) -> None:
        """Fetch JWKS and repopulate the key index."""
        raw: dict[str, Any] = await self._fetcher(self._url)
        new_keys: dict[str, PyJWK] = {}
        for jwk_data in raw.get("keys", []):
            try:
                jwk = PyJWK(jwk_data)
                kid = jwk_data.get("kid", "")
                if kid:
                    new_keys[kid] = jwk
            except Exception:
                continue  # skip malformed keys
        self._keys = new_keys
        self._last_refresh_at = self._clock()

    async def get_key(self, kid: str) -> PyJWK:
        """Return PyJWK for *kid*.

        Refresh strategy:
        1. Cache fresh and kid present → return cached key.
        2. kid missing but the cache was refreshed < MIN_REFRESH_INTERVAL_S
           ago → ``JWKSKeyNotFoundError`` (no refetch).
        3. Cache stale OR kid missing → try refresh (one attempt).
        4. Refresh fails:
           a. Within 60min of last good cache → use stale cache if kid present.
           b. Beyond 60min, or kid absent from the stale cache →
              ``JWKSUnavailableError``.
        5. Refresh succeeded but kid still absent → ``JWKSKeyNotFoundError``.
        """
        # Fast path: fresh cache and key exists.
        if self._is_cache_fresh() and kid in self._keys:
            return self._keys[kid]

        if kid not in self._keys and self._refreshed_recently():
            raise JWKSKeyNotFoundError(kid)

        # Slow path: refresh.
        try:
            await self._refresh()
        except Exception:
            # Refresh failed — try fallback.
            if self._is_within_fallback() and kid in self._keys:
                return self._keys[kid]
            raise JWKSUnavailableError(
                f"JWKS unavailable and fallback TTL exceeded (kid={kid!r})"
            )

        if kid in self._keys:
            return self._keys[kid]

        raise JWKSKeyNotFoundError(kid)
