from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_PRODUCTION_ENVS = frozenset({"prod", "production", "staging", "homolog", "homologation"})


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = Field(default="dev", validation_alias="APP_ENV")

    database_url: str = Field(
        default="postgresql+asyncpg://avoz:avoz@localhost:5432/avoz",
        validation_alias="DATABASE_URL",
    )
    server_host: str = Field(default="0.0.0.0", validation_alias="SERVER_HOST")
    server_port: int = Field(default=8000, validation_alias="SERVER_PORT")
    public_ws_base: str = Field(
        default="ws://localhost:8000",
        validation_alias="SERVER_PUBLIC_WS_BASE",
    )
    default_tenant_id: str = Field(
        default=DEFAULT_TENANT_ID,
        validation_alias="DEFAULT_TENANT_ID",
    )

    # ── Redis (Issue #13) ───────────────────────────────────────────────────
    # Empty string → in-memory ``FakeEventStream`` (single process, no durability).
    # Any ``redis://`` / ``rediss://`` URL → real ``RedisEventStream``.
    redis_url: str = Field(default="", validation_alias="REDIS_URL")

    # ── STT pipeline do servidor (Issue #14) ────────────────────────────────
    # off           → frames sao aceitos e descartados (sem transcricao)
    # fake          → backends de teste deterministicos (texto fixo)
    # local_whisper → faster-whisper local (WHISPER_* do .env), batch por janela
    stt_server_backend: str = Field(default="off", validation_alias="STT_SERVER_BACKEND")
    stt_window_s: float = Field(default=4.0, validation_alias="STT_WINDOW_S")
    stt_silence_rms: float = Field(default=250.0, validation_alias="STT_SILENCE_RMS")
    stt_silence_ms: int = Field(default=800, validation_alias="STT_SILENCE_MS")

    # ── Reaper de auto-finalize (Issue #16) ─────────────────────────────────
    # 0 desliga o reaper (ex.: testes). Producao: 60s.
    session_reaper_interval_s: int = Field(
        default=60, validation_alias="SESSION_REAPER_INTERVAL_S"
    )

    # ── JWT / Auth settings (Issue #6) ────────────────────────────────────
    # Empty string means "bypass mode" (no JWKS fetching) — used in tests.
    jwt_jwks_url: str = Field(
        default="",
        validation_alias="JWT_JWKS_URL",
    )
    # Comma-separated list of allowed issuers. Empty = bypass (tests).
    jwt_iss_allowlist_raw: str = Field(
        default="",
        validation_alias="JWT_ISS_ALLOWLIST",
    )

    @property
    def jwt_iss_allowlist(self) -> list[str]:
        """Parsed issuer allowlist. Empty list = bypass mode."""
        if not self.jwt_iss_allowlist_raw.strip():
            return []
        return [s.strip() for s in self.jwt_iss_allowlist_raw.split(",") if s.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in _PRODUCTION_ENVS

    @property
    def auth_bypass_active(self) -> bool:
        """True when JWT validation is disabled (no JWKS URL configured)."""
        return not self.jwt_jwks_url.strip()


@lru_cache(maxsize=1)
def get_settings() -> ServerSettings:
    """Process-wide settings (cached).

    Settings are read once per process; ``reset_settings_cache()`` is the
    test hook for tests that change env vars between cases.
    """
    return ServerSettings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
