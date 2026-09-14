"""Narrow adapter to the pinned Hermes terminal policy for setup commands.

Every command uses the host's guarded background spawn exactly once. The caller
can durably record the validated handle before this adapter waits. Missing
acknowledgements and ambiguous termination are uncertainty, never a retry.
"""

from __future__ import annotations

import importlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .package import PackagePolicyError


_MAX_DIAGNOSTIC_CHARS = 8_000
_DEFAULT_WAIT_SECONDS = 600


def _visible_diagnostic(value: object) -> tuple[str, bool]:
    """Return a bounded, host-redacted diagnostic without Unicode controls."""

    text = "" if value is None else str(value)
    try:
        redact = importlib.import_module("agent.redact").redact_sensitive_text
        text = redact(text, force=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise PackagePolicyError("Hermes terminal redaction is unavailable") from exc
    text = "".join(
        char for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    truncated = len(text) > _MAX_DIAGNOSTIC_CHARS
    return text[:_MAX_DIAGNOSTIC_CHARS], truncated


class SetupHost:
    """Execute one reviewed command through a single host-owned local spawn."""

    @staticmethod
    def _module(name: str) -> Any:
        try:
            return importlib.import_module(name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("required Hermes terminal coupling is unavailable") from exc

    def execution_policy(self) -> dict[str, object]:
        """Fail closed unless the configured terminal backend is local."""

        terminal = self._module("tools.terminal_tool")
        try:
            config = terminal._get_env_config()  # pylint: disable=protected-access
            if not isinstance(config, dict):
                raise TypeError("terminal policy is not a mapping")
            backend = str(config.get("env_type") or "").casefold()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("Hermes terminal policy is unavailable") from exc
        if backend != "local":
            raise PackagePolicyError(
                "local installed-skill setup requires the local terminal backend; "
                "the configured backend was not bypassed"
            )
        return {"backend": "local", "single_spawn": True, "synchronous_wait": True}

    @staticmethod
    def _validate_inputs(command: str, workdir: Path, task_id: str) -> Path:
        if not isinstance(command, str) or not command.strip():
            raise TypeError("command must be a non-empty string")
        if not isinstance(workdir, Path):
            raise TypeError("workdir must be a Path")
        if not isinstance(task_id, str) or not task_id.strip():
            raise TypeError("task_id must be a non-empty string")
        return workdir.resolve()

    def _expected_session_key(self, task_id: str) -> str:
        approval = self._module("tools.approval")
        try:
            value = approval.get_current_session_key(default="")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("Hermes terminal ownership is unavailable") from exc
        return str(value or task_id)

    @staticmethod
    def _handle(session: object, *, task_id: str, session_key: str, workdir: Path) -> dict:
        return {
            "session_id": str(getattr(session, "id", "")),
            "task_id": task_id,
            "session_key": session_key,
            "workdir": str(workdir),
        }

    @staticmethod
    def _matches(
        session: object,
        handle: dict,
        *,
        command: str,
        workdir: Path,
        task_id: str,
    ) -> bool:
        try:
            return (
                isinstance(handle, dict)
                and getattr(session, "id") == handle["session_id"]
                and getattr(session, "owner_task_id") == task_id == handle["task_id"]
                and getattr(session, "session_key") == handle["session_key"]
                and getattr(session, "command") == command
                and Path(getattr(session, "cwd")).resolve() == workdir
                and Path(handle["workdir"]).resolve() == workdir
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return False

    def execute(  # pylint: disable=too-many-locals,too-many-return-statements
        self,
        command: str,
        *,
        workdir: Path,
        task_id: str,
        on_started: Callable[[dict], None] | None = None,
        timeout: int = _DEFAULT_WAIT_SECONDS,
    ) -> dict[str, object]:
        """Spawn once, persist via callback, then wait for authoritative exit."""

        resolved = self._validate_inputs(command, workdir, task_id)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise TypeError("timeout must be a positive integer")
        self.execution_policy()
        terminal = self._module("tools.terminal_tool")
        expected_session = self._expected_session_key(task_id)
        try:
            raw = terminal.terminal_tool(
                command=command,
                background=True,
                task_id=task_id,
                workdir=str(resolved),
                notify_on_complete=False,
            )
            value = json.loads(raw)
        except Exception:  # pylint: disable=broad-exception-caught
            # A post-spawn acknowledgement may have been lost.
            return {"state": "unknown", "reason": "launch_acknowledgement_lost"}
        if not isinstance(value, dict):
            return {"state": "unknown", "reason": "invalid_launch_acknowledgement"}
        process_id = value.get("session_id")
        if not isinstance(process_id, str) or not process_id:
            status = str(value.get("status") or "").casefold()
            if status in {"blocked", "pending_approval", "approval_required"}:
                return {"state": "blocked"}
            return {"state": "unknown", "reason": "launch_not_confirmed"}
        registry = self._module("tools.process_registry").process_registry
        try:
            session = registry.get(process_id)
        except Exception:  # pylint: disable=broad-exception-caught
            session = None
        handle = self._handle(
            session, task_id=task_id, session_key=expected_session, workdir=resolved
        ) if session is not None else {
            "session_id": process_id,
            "task_id": task_id,
            "session_key": expected_session,
            "workdir": str(resolved),
        }
        if session is None or not self._matches(
            session, handle, command=command, workdir=resolved, task_id=task_id
        ):
            return {"state": "unknown", "reason": "process_ownership_unconfirmed"}
        try:
            if on_started is not None:
                on_started(dict(handle))
        except Exception:  # pylint: disable=broad-exception-caught
            # Durable state may or may not have committed.
            return {"state": "unknown", "reason": "handle_acknowledgement_lost"}
        result = self._wait(
            registry, handle, command=command, workdir=resolved,
            task_id=task_id, timeout=timeout,
        )
        result["handle"] = handle
        return result

    def status(
        self,
        handle: dict,
        *,
        command: str,
        workdir: Path,
        task_id: str,
    ) -> dict[str, object]:
        """Read an existing owner/content-matched result without starting work."""

        resolved = self._validate_inputs(command, workdir, task_id)
        self.execution_policy()
        if not isinstance(handle, dict) or not isinstance(handle.get("session_id"), str):
            return {"state": "unknown", "reason": "missing_process_handle"}
        registry = self._module("tools.process_registry").process_registry
        try:
            session = registry.get(handle["session_id"])
        except Exception:  # pylint: disable=broad-exception-caught
            session = None
        if session is None or not self._matches(
            session, handle, command=command, workdir=resolved, task_id=task_id
        ):
            return {"state": "unknown", "reason": "process_result_unavailable"}
        return self._classify(registry.poll(handle["session_id"]), handle["session_id"])

    def _wait(  # pylint: disable=too-many-arguments
        self, registry: Any, handle: dict, *, command: str,
        workdir: Path, task_id: str, timeout: int,
    ) -> dict[str, object]:
        try:
            value = registry.wait(handle["session_id"], timeout=timeout)
            session = registry.get(handle["session_id"])
        except Exception:  # pylint: disable=broad-exception-caught
            return {"state": "unknown", "reason": "wait_unavailable"}
        if session is None or not self._matches(
            session, handle, command=command, workdir=workdir, task_id=task_id
        ):
            return {"state": "unknown", "reason": "process_ownership_changed"}
        return self._classify(value, handle["session_id"])

    @staticmethod
    def _classify(value: object, process_id: str) -> dict[str, object]:
        if not isinstance(value, dict) or value.get("session_id") not in {None, process_id}:
            return {"state": "unknown", "reason": "invalid_process_result"}
        if value.get("status") == "running":
            return {"state": "running"}
        if value.get("status") != "exited" or value.get("completion_reason") != "exited":
            return {"state": "unknown", "reason": "ambiguous_termination"}
        code = value.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            return {"state": "unknown", "reason": "missing_exit_status"}
        try:
            output, truncated = _visible_diagnostic(value.get("output"))
        except PackagePolicyError:
            return {"state": "unknown", "reason": "diagnostic_redaction_unavailable"}
        return {
            "state": "passed" if code == 0 else "failed",
            "exit_code": code,
            "output": output,
            "output_truncated": truncated,
        }
