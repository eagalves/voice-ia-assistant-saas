"""Draft generation kill switch — Issue #9.

The kill switch stops draft generation while keeping transcription running.
It activates automatically when ``correction_ratio > 0.50`` in 2 consecutive
feedback submissions from the same ``actor_sub``.

It can also be disabled globally via the ``DRAFT_GENERATION_ENABLED=0`` env
var (checked on every call, no restart needed).

Usage::

    ks = get_kill_switch()
    activated = ks.record(actor_sub, correction_ratio)
    if ks.is_active:
        # skip draft generation; transcription continues
        ...

Testing::

    reset_kill_switch()   # creates a fresh instance for isolation
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

_logger = logging.getLogger(__name__)

_KILL_SWITCH_THRESHOLD: float = 0.50
_CONSECUTIVE_REQUIRED: int = 2
_ENV_FLAG: str = "DRAFT_GENERATION_ENABLED"


class DraftKillSwitch:
    """Tracks correction ratios per actor_sub and activates when thresholds breach."""

    def __init__(self) -> None:
        self._active: bool = False
        self._history: dict[str, list[float]] = defaultdict(list)

    @property
    def is_active(self) -> bool:
        """True if kill switch is active (env flag OR automatic activation)."""
        if os.getenv(_ENV_FLAG, "1") != "1":
            return True
        return self._active

    def record(self, actor_sub: str, correction_ratio: float) -> bool:
        """Record a correction ratio for *actor_sub*.

        Keeps only the last ``_CONSECUTIVE_REQUIRED`` observations.

        Returns True if the kill switch just activated for this actor_sub,
        False otherwise.
        """
        history = self._history[actor_sub]
        history.append(correction_ratio)
        # Trim to window
        if len(history) > _CONSECUTIVE_REQUIRED:
            del history[: len(history) - _CONSECUTIVE_REQUIRED]

        should_activate = (
            len(history) >= _CONSECUTIVE_REQUIRED
            and all(r > _KILL_SWITCH_THRESHOLD for r in history[-_CONSECUTIVE_REQUIRED:])
        )

        if should_activate and not self._active:
            self._active = True
            _logger.warning(
                "Draft kill switch activated: actor_sub=%s last_ratios=%s",
                actor_sub,
                history[-_CONSECUTIVE_REQUIRED:],
            )
            return True

        return False


# ── Singleton ─────────────────────────────────────────────────────────────────

_kill_switch: DraftKillSwitch = DraftKillSwitch()


def get_kill_switch() -> DraftKillSwitch:
    """Return the process-wide DraftKillSwitch instance."""
    return _kill_switch


def reset_kill_switch() -> None:
    """Replace the singleton with a fresh instance (test isolation)."""
    global _kill_switch
    _kill_switch = DraftKillSwitch()
