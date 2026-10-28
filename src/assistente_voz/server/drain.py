"""Graceful drain manager — Issue #3.

SIGTERM → broadcast {"type":"drain","reconnect_in":30} to all active WS
connections → mark server as draining (POST /v1/sessions returns 503) →
wait up to DRAIN_MAX_WAIT_S for in-flight work to finish → hand the signal
over to the server's own handler (uvicorn's ``handle_exit``) so the process
actually stops instead of waiting for the ECS stop timeout.

``ECS_CONTAINER_STOP_TIMEOUT`` must be set to 120s on the task definition
so the 90s drain window is not truncated by ECS's default 30s.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DRAIN_RECONNECT_IN: int = 30   # seconds clients should wait before reconnecting
DRAIN_MAX_WAIT_S: int = 90     # seconds to wait for in-flight work after drain starts


class DrainManager:
    """Process-level singleton for graceful-shutdown coordination.

    Call ``install_sigterm_handler(loop)`` once at startup.
    WS handlers register/unregister their drain callback via
    ``register``/``unregister``.  The POST /v1/sessions handler checks
    ``draining`` and returns 503 when True.
    """

    def __init__(self) -> None:
        self._draining: bool = False
        self._connections: dict[str, Callable[[], Awaitable[None]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def draining(self) -> bool:
        return self._draining

    def register(
        self, conn_id: str, send_drain: Callable[[], Awaitable[None]]
    ) -> None:
        """Register a WS connection's drain callback."""
        self._connections[conn_id] = send_drain

    def unregister(self, conn_id: str) -> None:
        """Unregister a WS connection (called on disconnect)."""
        self._connections.pop(conn_id, None)

    async def drain(self) -> None:
        """Broadcast drain message to all active WS connections.

        Idempotent — calling twice has no effect after the first call.
        Returns as soon as every registered callback has been invoked
        (i.e. in < 1s for any reasonable number of connections).

        Call ``wait_for_drain()`` afterwards if you need to wait for
        connections to fully wind down (e.g. in the SIGTERM handler).
        """
        if self._draining:
            return
        self._draining = True
        logger.warning(
            "Drain started — notifying %d active WS connections",
            len(self._connections),
        )
        for send in list(self._connections.values()):
            try:
                await send()
            except Exception:
                pass

    async def wait_for_drain(self) -> None:
        """Wait up to DRAIN_MAX_WAIT_S for all WS connections to unregister.

        Call after ``drain()`` in the SIGTERM handler.  WS handlers must
        call ``unregister()`` on disconnect so this can detect completion.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_MAX_WAIT_S
        while self._connections and loop.time() < deadline:
            await asyncio.sleep(0.1)

        if self._connections:
            logger.error(
                "Drain timed out — %d connection(s) still registered",
                len(self._connections),
            )

    def install_sigterm_handler(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install SIGTERM → drain() → previous handler on the event loop.

        ``loop.add_signal_handler`` *replaces* whatever handler uvicorn
        installed, so after draining we invoke the previous handler
        (uvicorn's ``handle_exit`` sets ``should_exit``). If there was no
        previous Python handler, the default action is restored and the
        signal re-sent so the process terminates.

        No-op on Windows (add_signal_handler not supported).
        """
        if sys.platform == "win32":
            return
        try:
            import signal

            previous = signal.getsignal(signal.SIGTERM)

            async def _sigterm_handler() -> None:
                await self.drain()
                await self.wait_for_drain()
                if callable(previous):
                    try:
                        previous(signal.SIGTERM, None)
                        return
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Previous SIGTERM handler failed: %s", exc)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

            loop.add_signal_handler(
                signal.SIGTERM,
                lambda: asyncio.ensure_future(_sigterm_handler()),
            )
        except (NotImplementedError, RuntimeError) as exc:
            logger.debug("Could not install SIGTERM handler: %s", exc)

    # ------------------------------------------------------------------
    # Metrics / introspection (for tests)
    # ------------------------------------------------------------------

    @property
    def active_connection_count(self) -> int:
        return len(self._connections)


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------

_instance: DrainManager | None = None


def get_drain_manager() -> DrainManager:
    global _instance
    if _instance is None:
        _instance = DrainManager()
    return _instance


def reset_drain_manager() -> None:
    """Test hook — reset the singleton between tests."""
    global _instance
    _instance = None
