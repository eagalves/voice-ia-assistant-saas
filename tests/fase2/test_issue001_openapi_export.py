from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from assistente_voz.server.cli.export_openapi import (
    DEFAULT_OUTPUT,
    main,
    render_openapi,
)


def test_render_openapi_includes_v1_endpoints() -> None:
    schema = yaml.safe_load(render_openapi())
    paths = schema.get("paths", {})
    assert "/v1/sessions" in paths
    assert "/v1/sessions/{session_id}/close" in paths
    assert "/v1/sessions/{session_id}/draft" in paths
    assert "/v1/sessions/{session_id}/draft/retry" in paths
    assert "/v1/sessions/{session_id}/draft/feedback" in paths
    assert "/v1/sessions/{session_id}/token/refresh" in paths
    assert schema["info"]["version"] == "1.0.0"


def test_export_openapi_writes_yaml(tmp_path: Path) -> None:
    target = tmp_path / "v1.yaml"
    rc = main(["--output", str(target)])
    assert rc == 0
    assert target.exists()
    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "paths" in parsed and "/v1/sessions" in parsed["paths"]


def test_committed_openapi_matches_live_schema() -> None:
    """Drift check — committed `schemas/openapi/v1.yaml` must match live."""
    if not DEFAULT_OUTPUT.exists():
        pytest.skip(
            f"{DEFAULT_OUTPUT} not committed yet; run "
            "`python -m assistente_voz.server.cli.export_openapi` to generate."
        )
    rc = main(["--check", "--output", str(DEFAULT_OUTPUT)])
    assert rc == 0, "Committed OpenAPI is out of date — regenerate it."
