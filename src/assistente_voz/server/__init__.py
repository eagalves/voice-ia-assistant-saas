"""Fase 2 HTTP server package — coexistence with the legacy CLI.

The CLI entry points (`voice_cli.py`, `main.py`) are unchanged. This package
exposes a FastAPI application that the telemedicine app will consume.
"""

from .app import create_app

__all__ = ["create_app"]
