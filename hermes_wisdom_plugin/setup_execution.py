"""Receipt-bound setup planning, execution, status, and explicit recovery."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .credential_scan import scan_credentials
from .errors import WisdomAuthError, WisdomConflict
from .installed_setup import inspect_installed_setup
from .package import PackagePolicyError
from .setup_lock import managed_operation_guard
from .store import utc_now


KIND = "wisdom_setup"
_TERMINAL_STATES = frozenset({"passed", "failed", "blocked", "abandoned"})


class SetupStep(BaseModel):
    """One exact human-selected setup action."""

    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=False, hide_input_in_errors=True
    )
    phase: Literal["prerequisite", "setup", "verify"]
    index: int = Field(default=0, ge=0, le=39)
    command: str = Field(default="", max_length=1200)

    @model_validator(mode="after")
    def _safe_exact_command(self):
        if self.phase == "prerequisite":
            if self.command:
                raise ValueError("prerequisite acknowledgements do not contain commands")
        elif not self.command.strip():  # pylint: disable=no-member
            raise ValueError("supply the exact setup command for separate review")
        if self.phase == "verify" and self.index != 0:
            raise ValueError("verification has index zero")
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in self.command):
            raise ValueError("setup commands must be one visible line")
        if self.command and scan_credentials(
            [{"path": "setup-command", "content": self.command}]
        ):
            raise ValueError("setup command contains a credential-shaped string")
        return self


def _operation_payload(row: dict) -> dict:
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    try:
        value = json.loads(row["payload_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WisdomConflict("setup operation journal is invalid") from exc
    if not isinstance(value, dict):
        raise WisdomConflict("setup operation journal is invalid")
    return value


def _rows(store: Any, skill_id: str) -> list[dict]:
    with store.transaction() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM operation_journal WHERE kind=? AND entity_id=? ORDER BY rowid",
                (KIND, skill_id),
            ).fetchall()
        ]


def _transition(
    store: Any,
    row: dict,
    phase: str,
    payload: dict,
    *,
    done: bool = False,
) -> bool:
    old_payload = row.get("payload_json")
    if old_payload is None:
        old_payload = json.dumps(_operation_payload(row), sort_keys=True)
    encoded = json.dumps(payload, sort_keys=True)
    with store.transaction() as db:
        cursor = db.execute(
            "UPDATE operation_journal SET phase=?,state=?,payload_json=?,updated_at=? "
            "WHERE id=? AND phase=? AND state=? AND payload_json=?",
            (
                phase,
                "done" if done else "pending",
                encoded,
                utc_now(),
                row["id"],
                row["phase"],
                row["state"],
                old_payload,
            ),
        )
    return cursor.rowcount == 1


class SetupController:  # pylint: disable=too-many-instance-attributes
    """Shared terminal setup service; every mutation revalidates its receipt."""

    def __init__(
        self,
        store: Any,
        receipts: Any,
        inspection_host: Any,
        execution_host: Any,
        owner: Callable[[], dict],
    ) -> None:
        self.store = store
        self.receipts = receipts
        self.inspection_host = inspection_host
        self.execution_host = execution_host
        self.owner = owner

    def _owner(self) -> dict:
        value = self.owner()
        if not isinstance(value, dict):
            raise WisdomAuthError("setup owner is unavailable")
        return value

    @staticmethod
    def _require_current(may_apply: Callable[[], bool] | None) -> None:
        if may_apply is not None and may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")

    def _recovery_snapshot(self, skill_id: str, row: dict) -> dict:
        """Validate and project one journal row without polling or mutation."""

        if row.get("kind") != KIND or row.get("entity_id") != skill_id:
            raise PackagePolicyError("setup recovery operation was not found")
        payload = _operation_payload(row)
        if payload.get("owner") != self._owner():
            raise WisdomAuthError("setup recovery belongs to another enrolment")
        receipt = payload.get("receipt")
        if not isinstance(receipt, str) or not receipt:
            raise WisdomConflict("setup recovery receipt is invalid")
        plan = self.receipts.load(receipt, allow_started=True)
        identity_fields = (
            "owner", "version", "content_hash", "target_path", "setup_key", "step"
        )
        if plan.get("skill_id") != skill_id or any(
            plan.get(field) != payload.get(field) for field in identity_fields
        ):
            raise WisdomConflict("setup recovery no longer matches its reviewed content")
        try:
            step = SetupStep.model_validate(payload.get("step"), strict=True).model_dump()
        except (TypeError, ValueError) as exc:
            raise WisdomConflict("setup recovery step is invalid") from exc
        task_id = payload.get("task_id")
        if task_id != "wisdom-setup:" + receipt:
            raise WisdomConflict("setup recovery task identity is invalid")
        handle = payload.get("handle")
        if handle is not None and not isinstance(handle, dict):
            raise WisdomConflict("setup recovery process handle is invalid")
        result = payload.get("result")
        if result is not None and not isinstance(result, dict):
            raise WisdomConflict("setup recovery result is invalid")
        journal_state = str(row.get("state"))
        if journal_state not in {"pending", "done"}:
            raise WisdomConflict("setup recovery journal state is invalid")
        return {
            "kind": KIND,
            "skill_id": skill_id,
            "operation_id": str(row["id"]),
            "receipt": receipt,
            "journal_phase": str(row["phase"]),
            "journal_state": journal_state,
            "version": payload.get("version"),
            "content_hash": payload.get("content_hash"),
            "target_path": payload.get("target_path"),
            "setup_key": payload.get("setup_key"),
            "step": step,
            "task_id": task_id,
            "handle": dict(handle) if isinstance(handle, dict) else None,
            "result": dict(result) if isinstance(result, dict) else None,
            "can_recover": journal_state != "done",
        }

    def recovery_status(self, skill_id: str, operation_id: str) -> dict:
        """Inspect one exact setup journal without polling or changing it."""

        row = self.store.operation(operation_id)
        if row is None:
            raise PackagePolicyError("setup recovery operation was not found")
        return self._recovery_snapshot(skill_id, row)

    @staticmethod
    def _assert_expected_recovery(expected: dict, current: dict) -> None:
        if not isinstance(expected, dict) or expected != current:
            raise WisdomConflict("setup recovery changed after it was reviewed")

    @staticmethod
    def _assert_recovery_identity(expected: dict, current: dict) -> None:
        identity_fields = (
            "kind", "skill_id", "operation_id", "receipt", "version",
            "content_hash", "target_path", "setup_key", "step", "task_id", "handle",
        )
        if any(expected.get(field) != current.get(field) for field in identity_fields):
            raise WisdomConflict("setup recovery changed after it was reviewed")

    def inspect(self, skill_id: str, *, version: int | None = None) -> dict:
        """Return owner-bound installed setup state without execution."""

        return inspect_installed_setup(
            self.store, self.inspection_host, self._owner(), skill_id, version=version
        )

    def status(self, skill_id: str, *, version: int | None = None) -> dict:
        """Poll only recorded handles and return current setup state."""

        with managed_operation_guard(Path(self.store.root), skill_id):
            info = self.inspect(skill_id, version=version)
            scope = {
                "owner": self._owner(),
                "version": info["version"],
                "content_hash": info["content_hash"],
                "target_path": info["target_path"],
            }
            for row in self._pending(skill_id):
                payload = _operation_payload(row)
                if all(payload.get(key) == value for key, value in scope.items()):
                    self._resume(row, payload)
            return self.inspect(skill_id, version=version)

    def plan(
        self,
        skill_id: str,
        *,
        version: int,
        phase: str,
        index: int = 0,
        command: str = "",
    ) -> dict:
        """Create a kind-separated receipt for one exact setup action."""

        step = SetupStep.model_validate(
            {"phase": phase, "index": index, "command": command}, strict=True
        )
        info = self.inspect(skill_id, version=version)
        if self._pending(skill_id):
            raise PackagePolicyError(
                "a setup operation is unfinished; inspect or recover it first"
            )
        plan = self._build_plan(info, step)
        return self.receipts.create(plan)

    def _build_plan(self, info: dict, step: SetupStep) -> dict:
        guidance = info.get("guidance")
        if not isinstance(guidance, dict):
            raise PackagePolicyError("this installed version has no valid setup guidance")
        latest = {
            (row["phase"], row["index"]): row
            for row in info.get("setup_progress", [])
        }
        if step.phase == "prerequisite":
            requirements = info["prerequisites"]
            if step.index >= len(requirements):
                raise PackagePolicyError("prerequisite is not in the installed guidance")
            requirement = requirements[step.index]
            allowed = requirement["status"] != "missing"
            instruction = requirement["purpose"]
        elif step.phase == "setup":
            instructions = guidance["setup_instructions"]
            if step.index >= len(instructions):
                raise PackagePolicyError("setup step is not in the installed guidance")
            if any(
                latest.get(("setup", prior), {}).get("state") != "passed"
                for prior in range(step.index)
            ):
                raise PackagePolicyError("finish earlier setup steps first")
            requirement = None
            allowed = True
            instruction = instructions[step.index]
        else:
            prerequisites_ready = all(
                item["status"] == "present"
                or (item["status"] == "acknowledgement_required"
                    and latest.get(("prerequisite", index), {}).get("state") == "passed")
                for index, item in enumerate(info["prerequisites"])
            )
            setup_ready = all(
                latest.get(("setup", index), {}).get("state") == "passed"
                for index in range(len(guidance["setup_instructions"]))
            )
            if not prerequisites_ready or not setup_ready:
                raise PackagePolicyError(
                    "finish declared prerequisites and setup before verification"
                )
            requirement = None
            allowed = True
            instruction = guidance["verification_step"]
        prior = [
            row["operation_id"] for row in info.get("setup_progress", [])
            if row["phase"] != "verify"
        ] if step.phase == "verify" else []
        identity = {
            "owner": self._owner(),
            "skill_id": info["skill_id"],
            "version": info["version"],
            "content_hash": info["content_hash"],
            "target_path": info["target_path"],
            "step": step.model_dump(),
            "prior_progress": prior,
        }
        result = {
            **identity,
            "setup_key": hashlib.sha256(
                json.dumps(identity, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
            "setup_instruction": instruction,
            "allowed": allowed,
        }
        if requirement is not None:
            result["setup_requirement"] = requirement
        return result

    def apply(
        self, receipt: str, *, may_apply: Callable[[], bool] | None = None
    ) -> dict:
        """Apply one setup receipt using at most one host background spawn."""

        plan = self.receipts.load(receipt, allow_started=True)
        outcome = self.receipts.outcome(receipt)
        if outcome is not None:
            return outcome
        skill_id = plan["skill_id"]
        with managed_operation_guard(Path(self.store.root), skill_id):
            existing = self._pending(skill_id)
            if existing:
                row = existing[0]
                payload = _operation_payload(row)
                if payload.get("receipt") != receipt or payload.get("owner") != self._owner():
                    raise PackagePolicyError(
                        "another setup operation is unfinished for this skill"
                    )
                self._require_current(may_apply)
                self._bind_receipt(receipt, row["id"])
                return self._resume(row, payload, may_apply=may_apply)
            if any(
                row["entity_id"] == skill_id
                and row["kind"] in {"install", "uninstall", "update"}
                for row in self.store.pending_operations()
            ):
                raise PackagePolicyError(
                    "an install, update, or uninstall operation is unfinished"
                )
            info = self.inspect(skill_id, version=plan["version"])
            fresh = self._build_plan(info, SetupStep.model_validate(plan["step"], strict=True))
            if fresh["setup_key"] != plan.get("setup_key") or not fresh["allowed"]:
                raise PackagePolicyError(
                    "installed package, prerequisites, or reviewed setup step changed"
                )
            self._require_current(may_apply)
            lease = self.store.acquire_operation_lock(skill_id)
            if not lease:
                raise PackagePolicyError("another managed operation owns this skill")
            try:
                return self._start(plan, receipt, may_apply=may_apply)
            finally:
                self.store.release_operation_lock(skill_id, lease)

    def _start(
        self, plan: dict, receipt: str, *, may_apply: Callable[[], bool] | None = None
    ) -> dict:
        payload = {
            "owner": self._owner(),
            "receipt": receipt,
            "version": plan["version"],
            "content_hash": plan["content_hash"],
            "target_path": plan["target_path"],
            "setup_key": plan["setup_key"],
            "step": plan["step"],
            "task_id": "wisdom-setup:" + receipt,
            "handle": None,
        }
        self._require_current(may_apply)
        operation_id = self.store.journal(KIND, plan["skill_id"], "starting", payload)
        self._bind_receipt(receipt, operation_id)
        row = self.store.operation(operation_id)
        if plan["step"]["phase"] == "prerequisite":
            return self._finish(row, payload, {"state": "passed"})

        def started(handle: dict) -> None:
            payload["handle"] = handle
            current = self.store.operation(operation_id)
            if current is None or not _transition(
                self.store, current, "running", payload
            ):
                raise WisdomConflict("setup journal changed while recording its process")

        self._require_current(may_apply)
        result = self.execution_host.execute(
            plan["step"]["command"],
            workdir=Path(plan["target_path"]),
            task_id=payload["task_id"],
            on_started=started,
        )
        current = self.store.operation(operation_id)
        if current is None:
            raise WisdomConflict("setup operation journal disappeared")
        if result.get("state") in _TERMINAL_STATES - {"abandoned"}:
            return self._finish(current, _operation_payload(current), result)
        return {
            "state": "unknown" if result.get("state") != "running" else "running",
            "operation_id": operation_id,
            "receipt": receipt,
        }

    def _bind_receipt(self, receipt: str, operation_id: str) -> None:
        metadata = self.store.get_metadata("setup_receipt:" + receipt)
        if not isinstance(metadata, dict) or metadata.get("operation_id") is None:
            self.receipts.start(receipt, operation_id)
        elif metadata["operation_id"] != operation_id:
            raise WisdomConflict("setup receipt is bound to another operation")

    def _finish(self, row: dict, payload: dict, result: dict) -> dict:
        state = str(result.get("state") or "unknown")
        if state not in _TERMINAL_STATES:
            return {"state": "unknown", "operation_id": row["id"]}
        stored = {
            "state": state,
            "operation_id": row["id"],
            "receipt": payload["receipt"],
        }
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            stored["exit_code"] = exit_code
        if isinstance(result.get("output"), str):
            stored["output"] = result["output"]
            stored["output_truncated"] = bool(result.get("output_truncated"))
        return self.receipts.finalize(payload["receipt"], row, payload, stored, state)

    def _resume(
        self, row: dict, payload: dict, *, may_apply: Callable[[], bool] | None = None
    ) -> dict:
        if row["state"] == "done":
            result = payload.get("result")
            return result if isinstance(result, dict) else {"state": row["phase"]}
        if row["phase"] == "starting" or not isinstance(payload.get("handle"), dict):
            if row["phase"] != "unknown":
                self._require_current(may_apply)
                _transition(self.store, row, "unknown", payload)
            return {
                "state": "unknown", "operation_id": row["id"],
                "receipt": payload.get("receipt"),
            }
        result = self.execution_host.status(
            payload["handle"], command=payload["step"]["command"],
            workdir=Path(payload["target_path"]), task_id=payload["task_id"],
        )
        if result.get("state") in _TERMINAL_STATES - {"abandoned"}:
            current = self.store.operation(row["id"])
            if current is None:
                raise WisdomConflict("setup operation journal disappeared")
            self._require_current(may_apply)
            return self._finish(current, _operation_payload(current), result)
        if result.get("state") != "running" and row["phase"] != "unknown":
            self._require_current(may_apply)
            _transition(self.store, row, "unknown", payload)
        return {
            "state": "running" if result.get("state") == "running" else "unknown",
            "operation_id": row["id"], "receipt": payload.get("receipt"),
        }

    def recover(
        self,
        skill_id: str,
        operation_id: str,
        *,
        acknowledged_effects_checked_and_stopped: bool = False,
        expected_recovery: dict | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict:
        """Abandon UNKNOWN only after an explicit effects/stopped acknowledgement."""

        with managed_operation_guard(Path(self.store.root), skill_id):
            row = self.store.operation(operation_id)
            if row is None or row["kind"] != KIND or row["entity_id"] != skill_id:
                raise PackagePolicyError("setup recovery operation was not found")
            payload = _operation_payload(row)
            if payload.get("owner") != self._owner():
                raise WisdomAuthError("setup recovery belongs to another enrolment")
            if expected_recovery is not None:
                self._require_current(may_apply)
                self._assert_expected_recovery(
                    expected_recovery, self._recovery_snapshot(skill_id, row)
                )
            if row["state"] == "done":
                return payload.get("result") or {"state": row["phase"]}
            self._require_current(may_apply)
            current = self._resume(row, payload, may_apply=may_apply)
            raw_step = payload.get("step")
            step = raw_step if isinstance(raw_step, dict) else {}
            current = {
                **current,
                "kind": KIND,
                "skill_id": skill_id,
                "version": payload.get("version"),
                "content_hash": payload.get("content_hash"),
                "phase": step.get("phase"),
                "index": step.get("index"),
                "command": step.get("command"),
            }
            if current["state"] == "running":
                return current
            if current["state"] != "unknown":
                return current
            if not acknowledged_effects_checked_and_stopped:
                return {
                    **current,
                    "recovery_review": True,
                    "recovery_consequence": (
                        "Abandoning records the outcome as abandoned only after effects "
                        "were checked and the recorded process was stopped; it never reruns."
                    ),
                }
            self._require_current(may_apply)
            latest = self.store.operation(operation_id)
            if latest is None:
                raise WisdomConflict("setup operation journal disappeared")
            latest_status = self._recovery_snapshot(skill_id, latest)
            if expected_recovery is not None:
                self._assert_recovery_identity(expected_recovery, latest_status)
            latest_payload = _operation_payload(latest)
            latest_payload["recovery"] = {
                "effects_checked_and_stopped": True,
                "at": utc_now(),
            }
            return self._finish(latest, latest_payload, {"state": "abandoned"})

    def _pending(self, skill_id: str) -> list[dict]:
        return [
            row for row in _rows(self.store, skill_id)
            if row["state"] == "pending"
        ]
