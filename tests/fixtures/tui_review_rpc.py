"""Reusable synchronous driver for the configured host's TUI review RPC."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class TuiReviewRpcDriver:  # pylint: disable=too-few-public-methods
    """Run a TUI command thread and resolve its optional review request."""

    def __init__(
        self,
        run_command: Callable[[str], str],
        resolve_response: Callable[[dict[str, Any]], bool],
        transport: Any,
        *,
        timeout: float,
    ) -> None:
        self._run_command = run_command
        self._resolve_response = resolve_response
        self._transport = transport
        self._timeout = timeout

    def invoke(self, raw: str, action_id: str | None) -> tuple[str, dict[str, Any] | None]:
        """Return command output and the resolved review params, when requested."""
        outputs: list[str] = []
        errors: list[BaseException] = []
        with self._transport.lock:
            frame_count = len(self._transport.frames)
        thread = threading.Thread(
            target=self._capture_command,
            args=(raw, outputs, errors),
        )
        thread.start()
        if action_id is None:
            return self._finish(thread, outputs, errors), None

        request = self._wait_for_review(frame_count)
        params = request["params"]
        accepted = self._resolve_response({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "generation": params["generation"],
                "action_id": action_id,
            },
        })
        assert accepted
        return self._finish(thread, outputs, errors), params

    def _capture_command(
        self,
        raw: str,
        outputs: list[str],
        errors: list[BaseException],
    ) -> None:
        """Capture a command result or preserve its thread exception for the caller."""
        try:
            outputs.append(self._run_command(raw))
        except BaseException as error:  # pylint: disable=broad-exception-caught
            errors.append(error)

    def _wait_for_review(self, frame_count: int) -> dict[str, Any]:
        """Wait within the bounded fixture deadline for a new review frame."""
        deadline = time.monotonic() + self._timeout
        request = None
        while request is None and time.monotonic() < deadline:
            with self._transport.lock:
                request = next(
                    (
                        frame
                        for frame in self._transport.frames[frame_count:]
                        if frame.get("method") == "plugin.review"
                    ),
                    None,
                )
            if request is None:
                time.sleep(0.01)
        assert request is not None, self._transport.frames
        return request

    def _finish(
        self,
        thread: threading.Thread,
        outputs: list[str],
        errors: list[BaseException],
    ) -> str:
        """Join the command thread and return its single result."""
        thread.join(self._timeout)
        assert not thread.is_alive()
        if errors:
            raise errors[0]
        assert len(outputs) == 1
        return outputs[0]
