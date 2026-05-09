from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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


def get_settings() -> ServerSettings:
    return ServerSettings()
