"""Stable error codes shared by CLI and HTTP server.

Legacy codes (preserved as-is, never rename — break observability and tests):
- CONFIG_ENV_MISSING, CONFIG_ENV_INVALID
- PROFILE_ENV_MISSING, PROFILE_NOT_FOUND, PROFILE_YAML_INVALID,
  PROFILE_SCHEMA_INVALID, PROFILE_ID_DUPLICATE, PROFILE_PARAM_INVALID
- STT_EMPTY, STT_LOW_CONFIDENCE, STT_EXCEPTION
- LLM_ERR_UNAVAILABLE, LLM_ERR_LOGICAL, LLM_EXCEPTION

Fase 2 introduces new codes for the HTTP API. They follow the namespace
convention <DOMAIN>_<REASON> grouped by prefix (SESSION_*, AUTH_*, DRAFT_*,
IDEMPOTENCY_*, STT_*, AUTH_*).
"""


class ConfigError(Exception):
    """Configuration bootstrap error with stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ApiError(Exception):
    """HTTP-aware error carrying a stable code and a target status.

    The router layer maps this to a JSONResponse via the registered handler.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        extra: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}

    def to_payload(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        payload.update(self.extra)
        return payload

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
