"""The one place that touches a Hermes session-fork handle.

``ctx.fork_session`` returns a live handle (or None). Everything else in this plugin sees
only the plain result of :meth:`ForkRunner.run`, ``{"fork_id", "outcome"}``, so a change to
the handle's shape changes this module alone. A handle is held only while its fork runs.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable

logger = logging.getLogger(__name__)

REFUSED = "refused"


class ForkRunner:  # pylint: disable=too-few-public-methods
    """Start one fork per call, wait for it, and report plain data."""

    def run(self, fork: Callable[..., Any] | None, token: str, prompt: str, *,
            wait: float, **options: Any) -> dict[str, str]:
        """Fork, wait up to ``wait`` seconds, and return ``{"fork_id", "outcome"}``.

        ``outcome`` is the host's (``ok``, ``cancelled``, ``timeout``, ...) or ``refused``
        when there is no fork method, the host returned no handle, or the call raised
        (a host without forks, such as an isolated plugin host). A fork still running
        after ``wait`` is cancelled. Never raises.
        """
        fork_id = secrets.token_hex(6)
        if fork is None:
            return {"fork_id": fork_id, "outcome": REFUSED}
        try:
            handle = fork(token, prompt, **options)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.info("Collective Wisdom: the host could not start a session fork (%s)",
                        type(error).__name__)
            handle = None
        if handle is None:
            return {"fork_id": fork_id, "outcome": REFUSED}
        try:
            if not handle.wait(wait):
                handle.cancel()  # Still deferred or stuck: give up; a later due turn may retry.
            outcome = str(getattr(handle, "outcome", "") or "error")
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom: reading a session fork failed", exc_info=True)
            outcome = "error"
        return {"fork_id": fork_id, "outcome": outcome}
