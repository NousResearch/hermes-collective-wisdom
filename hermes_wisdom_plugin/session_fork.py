"""The one place that touches a Hermes session-fork handle.

``ctx.fork_session`` returns a live handle (or None). Everything else in this plugin sees
only the plain result of :meth:`ForkRunner.run`, ``{"fork_id", "outcome"}``, so a change to
the handle's shape changes this module alone. A handle is held only while its fork is
queued or runs, so that :meth:`ForkRunner.cancel_all` can stop it when the plugin unloads.
"""

from __future__ import annotations

import logging
import secrets
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

REFUSED = "refused"


class ForkRunner:
    """Start one fork per call, wait for it, and report plain data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, Any] = {}
        self._closed = False

    def run(self, fork: Callable[..., Any] | None, token: str, prompt: str, *,
            wait: float, **options: Any) -> dict[str, str]:
        """Fork, wait up to ``wait`` seconds, and return ``{"fork_id", "outcome"}``.

        ``outcome`` is the host's (``ok``, ``cancelled``, ``timeout``, ...) or ``refused``
        when there is no fork method, the host returned no handle, the call raised (a
        host without forks, such as an isolated plugin host) or the plugin is unloading.
        A fork still running after ``wait`` is cancelled. Never raises.
        """
        fork_id = secrets.token_hex(6)
        with self._lock:
            closed = self._closed
        if fork is None or closed:
            return {"fork_id": fork_id, "outcome": REFUSED}
        try:
            handle = fork(token, prompt, **options)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.info("Collective Wisdom: the host could not start a session fork (%s)",
                        type(error).__name__)
            handle = None
        if handle is None:
            return {"fork_id": fork_id, "outcome": REFUSED}
        with self._lock:
            closed = self._closed
            if not closed:
                self._running[fork_id] = handle
        try:
            if closed or not handle.wait(wait):
                handle.cancel()  # Unloaded meanwhile, or still deferred or stuck.
            outcome = str(getattr(handle, "outcome", "") or "error")
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom: reading a session fork failed", exc_info=True)
            outcome = "error"
        finally:
            with self._lock:
                self._running.pop(fork_id, None)
        return {"fork_id": fork_id, "outcome": outcome}

    def cancel_all(self) -> int:
        """Cancel every queued or running fork and refuse new ones; the number cancelled."""
        with self._lock:
            self._closed = True
            running = list(self._running.values())
        for handle in running:
            try:
                handle.cancel()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("Collective Wisdom: cancelling a session fork failed",
                               exc_info=True)
        return len(running)

    def running(self) -> int:
        """How many forks are queued or running now."""
        with self._lock:
            return len(self._running)
