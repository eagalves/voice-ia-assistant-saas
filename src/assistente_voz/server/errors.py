"""HTTP exception handlers and error code registry for the v1 API."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..errors import ApiError

# Field name → namespaced code emitted on Pydantic validation failure.
_FIELD_TO_CODE: dict[str, str] = {
    "session_kind": "SESSION_INVALID_KIND",
    "plan": "SESSION_INVALID_PLAN",
    "seniority": "SESSION_INVALID_SENIORITY",
    "actor_role": "SESSION_INVALID_ACTOR_ROLE",
    "chars_original": "DRAFT_FEEDBACK_INVALID",
    "chars_edited": "DRAFT_FEEDBACK_INVALID",
    "fields_corrected": "DRAFT_FEEDBACK_INVALID",
}

DEFAULT_VALIDATION_CODE = "SESSION_INVALID_BODY"


def _resolve_validation_code(loc: tuple) -> str:
    for part in reversed(loc):
        if isinstance(part, str) and part in _FIELD_TO_CODE:
            return _FIELD_TO_CODE[part]
    return DEFAULT_VALIDATION_CODE


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = exc.errors()
    if not errors:
        return JSONResponse(
            status_code=400,
            content={
                "error": DEFAULT_VALIDATION_CODE,
                "message": "Request body is invalid.",
            },
        )
    first = errors[0]
    code = _resolve_validation_code(first.get("loc", ()))
    return JSONResponse(
        status_code=400,
        content={
            "error": code,
            "message": first.get("msg", "Invalid value."),
            "details": [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
                for e in errors
            ],
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
