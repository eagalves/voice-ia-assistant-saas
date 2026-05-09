"""Dump the live FastAPI OpenAPI schema to ``schemas/openapi/v1.yaml``.

Used by both developers (refresh the committed contract) and CI (drift check
against the committed file).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ..app import create_app

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT = REPO_ROOT / "schemas" / "openapi" / "v1.yaml"


def render_openapi() -> str:
    app = create_app()
    schema = app.openapi()
    return yaml.safe_dump(schema, sort_keys=True, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Destination YAML path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare against the existing file; exit 1 if drifted.",
    )
    args = parser.parse_args(argv)

    rendered = render_openapi()
    target: Path = args.output

    if args.check:
        if not target.exists():
            print(f"OpenAPI file missing: {target}", file=sys.stderr)
            return 1
        existing = target.read_text(encoding="utf-8")
        if existing.strip() != rendered.strip():
            print(
                f"OpenAPI drift detected at {target}. "
                "Run `python -m assistente_voz.server.cli.export_openapi` to refresh.",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI committed file matches live schema: {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"Wrote OpenAPI schema to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
