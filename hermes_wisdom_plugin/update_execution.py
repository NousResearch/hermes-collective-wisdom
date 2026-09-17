"""Crash-recoverable replacement for exact reviewed manual Wisdom updates."""

# Update-specific source/preservation facts stay here; bounded tree writing,
# hashing, durable renames, and Gateway error types reuse the hardened installer.
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements
# pylint: disable=too-few-public-methods,duplicate-code

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from .errors import WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError
from .install_execution import (
    _accepted_scan_warning,
    _durable_replace,
    _field,
    _fsync_directory,
    _reject_symlink_chain,
    _require_tree,
    _require_scan,
    _same_filesystem,
    _tree_hashes,
    _within,
    _write_staging,
)
from .package import PackagePolicyError
from .setup_lock import managed_operation_guard
from .update_review import UpdateAuthorityRejected, validate_update_target

_TERMINAL_CODES = frozenset(
    {
        "skill_not_active",
        "skill_taken_down",
        "skill_archived",
        "skill_not_found",
        "version_not_found",
        "takedown_generation_changed",
    }
)


class UpdateExecutor:
    """Replace one managed tree while retaining exact prior bytes and authority."""

    def __init__(
        self,
        client: Any,
        store: Any,
        receipts: Any,
        reviewer: Any,
        host: Any,
        owner: Callable[[], dict],
    ) -> None:
        self.client = client
        self.store = store
        self.receipts = receipts
        self.reviewer = reviewer
        self.host = host
        self.owner = owner

    @staticmethod
    def _require_current(may_apply: Callable[[], bool] | None) -> None:
        if may_apply is not None and may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")

    @staticmethod
    def _assert_payload_matches_plan(payload: dict, plan: dict) -> None:
        if any(payload.get(key) != value for key, value in plan.items()):
            raise WisdomConflict("update recovery no longer matches its reviewed receipt")

    def recovery_status(self, skill_id: str, operation_id: str) -> dict[str, Any]:
        """Inspect one exact update journal without verification, retry, or mutation."""

        row = self.store.operation(operation_id)
        if row is None or row.get("kind") != "update" or row.get("entity_id") != skill_id:
            raise PackagePolicyError("update recovery operation was not found")
        payload = _operation_payload(row)
        if payload.get("owner") != self._owner():
            raise WisdomAuthError("update recovery belongs to another enrolment")
        receipt = payload.get("receipt")
        version = payload.get("version")
        from_version = payload.get("from_version")
        content_hash = payload.get("content_hash")
        # Keep the complete fail-closed journal identity check visible together.
        if (
            # pylint: disable-next=too-many-boolean-expressions
            payload.get("skill_id") != skill_id
            or not isinstance(receipt, str)
            or not receipt
            or not isinstance(version, int)
            or isinstance(version, bool)
            or not isinstance(from_version, int)
            or isinstance(from_version, bool)
            or not isinstance(content_hash, str)
            or not content_hash
        ):
            raise PackagePolicyError("update recovery identity is invalid")
        plan = self.receipts.load(receipt, allow_started=True)
        self._assert_payload_matches_plan(payload, plan)
        return {
            "kind": "update", "skill_id": skill_id,
            "operation_id": operation_id, "receipt": receipt,
            "phase": str(row["phase"]), "state": str(row["state"]),
            "from_version": from_version, "version": version,
            "content_hash": content_hash, "can_resume": row["state"] != "done",
        }

    def apply(
        self,
        receipt: str,
        *,
        may_apply: Callable[[], bool] | None = None,
        expected_recovery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply or explicitly retry one receipt; status never enters this path."""

        plan = self.receipts.load(receipt, allow_started=True)
        if plan.get("authorization", {}).get("kind") == "automatic_policy":
            raise PackagePolicyError(
                "automatic policy receipts cannot be applied as manual confirmation"
            )
        skill_id = _required_text(plan, "skill_id")
        with managed_operation_guard(Path(self.store.root), skill_id):
            if expected_recovery is not None:
                operation_id = expected_recovery.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    raise WisdomConflict("reviewed update recovery identity is invalid")
                current = self.recovery_status(skill_id, operation_id)
                if current != expected_recovery:
                    raise WisdomConflict("update recovery changed after it was reviewed")
            return self._apply(receipt, plan, may_apply=may_apply)

    def apply_automatic(self, receipt: str) -> dict[str, Any]:
        """Apply or resume only a fresh policy-authorized automatic operation."""

        plan = self.receipts.load(receipt, allow_started=True)
        if (
            plan.get("authorization", {}).get("kind") != "automatic_policy"
            or plan.get("user_confirmation") is not False
        ):
            raise PackagePolicyError("automatic execution requires automatic policy authority")
        skill_id = _required_text(plan, "skill_id")
        with managed_operation_guard(Path(self.store.root), skill_id):
            return self._apply(receipt, plan, may_apply=None)

    def _apply(
        self, receipt: str, plan: dict[str, Any], *, may_apply: Callable[[], bool] | None
    ) -> dict[str, Any]:
        completed = self.receipts.outcome(receipt)
        owner = self._owner()
        if plan.get("owner") != owner:
            raise WisdomAuthError("Update receipt belongs to another enrolment")
        skill_id = _required_text(plan, "skill_id")
        lease = self.store.acquire_operation_lock(skill_id)
        if not lease:
            raise PackagePolicyError("another managed operation is already active for this skill")
        try:
            self._assert_owner(owner)
            pending = self._pending_for_skill(skill_id)
            updates = [row for row in pending if row.get("kind") == "update"]
            other = [row for row in pending if row.get("kind") != "update"]
            if other:
                kind = str(other[0].get("kind") or "managed")
                if kind == "wisdom_setup":
                    raise PackagePolicyError("a setup operation is pending for this skill")
                raise PackagePolicyError("another managed operation is pending for this skill")
            if updates:
                row = updates[0]
                payload = _operation_payload(row)
                if payload.get("receipt") != receipt or payload.get("owner") != owner:
                    raise PackagePolicyError("another update receipt is pending for this skill")
                validate_update_target(self.host, payload, owner)
                self._assert_payload_matches_plan(payload, plan)
                self._require_current(may_apply)
                self._repair_start(row, payload)
                phase = str(row["phase"])
                if phase in {"gateway_recorded", "gateway_rejected"}:
                    self._require_current(may_apply)
                    self.store.assert_operation_lock(skill_id, lease)
                    return self._resume(
                        str(row["id"]), payload, lease, may_apply=may_apply
                    )
                try:
                    verified = self.reviewer.verify(plan, recovery_phase=phase)
                except UpdateAuthorityRejected:
                    self._require_current(may_apply)
                    return self._reject_recovery(str(row["id"]), payload, owner, lease)
                self._require_current(may_apply)
                self.store.assert_operation_lock(skill_id, lease)
                return self._resume(
                    str(row["id"]), payload, lease,
                    verified=verified, may_apply=may_apply,
                )
            if completed is not None:
                return completed
            verified = self.reviewer.verify(plan)
            self._assert_owner(owner)
            self._require_current(may_apply)
            self.store.assert_operation_lock(skill_id, lease)
            return self._start(verified, receipt, owner, lease, may_apply=may_apply)
        finally:
            self.store.release_operation_lock(skill_id, lease)

    def _start(
        self, verified: Any, receipt: str, owner: dict, lease: str,
        *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        plan = verified.plan
        if not plan.get("allowed"):
            raise PackagePolicyError("reviewed update is not allowed")
        preservation = plan.get("preservation")
        if not isinstance(preservation, dict) or preservation.get("action") not in {
            "none", "preserve_unmanaged_copy"
        }:
            raise PackagePolicyError("reviewed edited-copy action does not authorize replacement")
        payload = {
            **dict(plan),
            "owner": owner,
            "receipt": receipt,
            "staging_path": None,
            "recovery_path": None,
            "quarantine_path": None,
        }
        self._require_current(may_apply)
        operation_id = self.store.journal(
            "update", _required_text(plan, "skill_id"), "reviewed", payload
        )
        payload.update(self._paths(operation_id, payload))
        self.store.replace_operation_payload(operation_id, payload)
        self._bind_receipt(receipt, operation_id)
        staging = Path(payload["staging_path"])
        self._require_current(may_apply)
        _write_staging(staging, verified.files)
        _require_tree(staging, verified.baseline)
        _require_scan(
            self.host.scan(staging),
            plan.get("local_scan"),
            _accepted_scan_warning(plan, operation="update"),
        )
        self._assert_owner(owner)
        self.store.assert_operation_lock(payload["skill_id"], lease)
        self.store.advance(operation_id, "staged")
        # Failures retain the deterministic journal/staging path for explicit retry.
        return self._resume(
            operation_id, payload, lease, verified=verified, may_apply=may_apply
        )

    def _resume(
        self, operation_id: str, payload: dict, lease: str, *,
        verified: Any | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        operation = self.store.operation(operation_id)
        if operation is None:
            raise PackagePolicyError("update recovery journal is missing")
        self._require_current(may_apply)
        owner = self._owner()
        if payload.get("owner") != owner:
            raise WisdomAuthError("update recovery belongs to another enrolment")
        skill_id = _required_text(payload, "skill_id")
        self.store.assert_operation_lock(skill_id, lease)
        paths = self._paths(operation_id, payload)
        if any(payload.get(key) != value for key, value in paths.items()):
            raise PackagePolicyError("update recovery paths do not match the reviewed operation")
        target = Path(payload["target_path"])
        staging = Path(payload["staging_path"])
        recovery = Path(payload["recovery_path"])
        phase = str(operation["phase"])

        if phase == "reviewed":
            if _tree_hashes(staging) != payload["baseline"]:
                if verified is None:
                    raise PackagePolicyError("reviewed update staging bytes are unavailable")
                if staging.exists() or staging.is_symlink():
                    self._require_current(may_apply)
                    shutil.rmtree(staging, ignore_errors=True)
                self._require_current(may_apply)
                _write_staging(staging, verified.files)
            _require_tree(staging, payload["baseline"])
            self.store.advance(operation_id, "staged")
            phase = "staged"
        if phase == "staged":
            self._require_current(may_apply)
            _require_scan(
                self.host.scan(staging),
                payload.get("local_scan"),
                _accepted_scan_warning(payload, operation="update"),
            )
            self._require_current(may_apply)
            recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and not recovery.exists():
                _require_tree(target, payload["local_snapshot"])
                _same_filesystem(target, recovery.parent)
                self._assert_owner(owner)
                self.store.assert_operation_lock(skill_id, lease)
                _durable_replace(target, recovery)
            elif target.exists() and recovery.exists():
                raise PackagePolicyError("update recovery found both source and preserved trees")
            _require_tree(recovery, payload["local_snapshot"])
            self.store.advance(operation_id, "old_preserved")
            phase = "old_preserved"
        if phase == "old_preserved":
            self._require_current(may_apply)
            self._preserve_copy(payload, recovery, operation_id, owner, lease)
            self.store.advance(operation_id, "edits_preserved")
            phase = "edits_preserved"
        if phase == "edits_preserved":
            already_promoted = target.exists() and _tree_hashes(target) == payload["baseline"]
            if staging.exists() and not target.exists():
                _require_tree(staging, payload["baseline"])
                _same_filesystem(staging, target.parent)
                self._assert_owner(owner)
                self._require_current(may_apply)
                self.store.assert_operation_lock(skill_id, lease)
                _durable_replace(staging, target)
            elif not already_promoted:
                raise PackagePolicyError("update staging or promoted bytes are unavailable")
            _require_tree(target, payload["baseline"])
            _require_tree(recovery, payload["local_snapshot"])
            self.store.advance(operation_id, "files_committed")
            phase = "files_committed"
        if phase == "files_committed":
            _require_tree(target, payload["baseline"])
            _require_tree(recovery, payload["local_snapshot"])
            self._assert_owner(owner)
            self._require_current(may_apply)
            self.store.assert_operation_lock(skill_id, lease)
            self.store.record_install(self._install_record(payload, owner, "active"))
            self.store.advance(operation_id, "local_ledger_committed")
            phase = "local_ledger_committed"
        if phase == "local_ledger_committed":
            return self._record_gateway(
                operation_id, payload, owner, lease, target, recovery,
                may_apply=may_apply,
            )
        if phase == "gateway_recorded":
            result = payload.get("result") or self.receipts.outcome(payload["receipt"])
            if not isinstance(result, dict):
                raise PackagePolicyError("completed update outcome is missing")
            self._require_current(may_apply)
            self.receipts.complete(payload["receipt"], result)
            self.store.advance(operation_id, "gateway_recorded", done=True)
            return result
        if phase == "gateway_rejected":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise PackagePolicyError("rejected update outcome is missing")
            self._require_current(may_apply)
            self.receipts.complete(payload["receipt"], result)
            self.store.advance(operation_id, "gateway_rejected", done=True)
            return result
        raise PackagePolicyError(f"unsupported update recovery phase: {phase}")

    def _record_gateway(
        self,
        operation_id: str,
        payload: dict,
        owner: dict,
        lease: str,
        target: Path,
        recovery: Path,
        *,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        remote = self._remote(payload["skill_id"], owner)
        remote_version = remote.get("installed_version") if remote else None
        if remote_version == payload["version"]:
            mode = remote.get("update_mode")
            if mode not in {"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"}:
                return self._pending_result(payload, operation_id, "Gateway result is unverifiable")
        elif remote_version == payload["from_version"]:
            try:
                self._require_current(may_apply)
                server = self.client.record_install(
                    skill_id=payload["skill_id"],
                    installation_id=owner["installation_id"],
                    version=int(payload["version"]),
                    takedown_generation=int(payload["takedown_generation"]),
                    update_mode=payload["update_mode"],
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if _terminal_rejection(exc):
                    self._quarantine(
                        operation_id, payload, owner, lease, target, recovery
                    )
                    raise
                return self._pending_result(
                    payload, operation_id,
                    "Gateway reconciliation is temporarily unavailable",
                )
            mode = _field(server, "effective_update_mode")
            if mode not in {"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"}:
                return self._pending_result(payload, operation_id, "Gateway result is unverifiable")
        else:
            return self._pending_result(
                payload, operation_id, "Gateway installation state changed; inspect before retrying"
            )
        self._assert_owner(owner)
        self._require_current(may_apply)
        self.store.assert_operation_lock(payload["skill_id"], lease)
        self.store.record_install(self._install_record(payload, owner, "active", mode=mode))
        result = {
            "updated": True,
            "skill_id": payload["skill_id"],
            "from_version": int(payload["from_version"]),
            "version": int(payload["version"]),
            "path": str(target),
            "content_hash": payload["content_hash"],
            "preserved_copy": payload["preservation"].get("destination"),
            "recovery_path": str(recovery),
            "effective_update_mode": mode,
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }
        payload["result"] = result
        self.store.replace_operation_payload(operation_id, payload)
        self.store.advance(operation_id, "gateway_recorded")
        self.receipts.complete(payload["receipt"], result)
        self.store.advance(operation_id, "gateway_recorded", done=True)
        return result

    def _quarantine(
        self,
        operation_id: str,
        payload: dict,
        owner: dict,
        lease: str,
        target: Path,
        recovery: Path,
    ) -> None:
        quarantine = Path(payload["quarantine_path"])
        quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and not quarantine.exists():
            _require_tree(target, payload["baseline"])
            _same_filesystem(target, quarantine.parent)
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lease)
            _durable_replace(target, quarantine)
        _require_tree(quarantine, payload["baseline"])
        _require_tree(recovery, payload["local_snapshot"])
        self.store.deactivate_install(payload["skill_id"])
        result = {
            "updated": False,
            "rejected": True,
            "skill_id": payload["skill_id"],
            "version": int(payload["version"]),
            "path": None,
            "preserved_copy": payload["preservation"].get("destination"),
            "recovery_path": str(recovery),
            "quarantine_path": str(quarantine),
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }
        payload["result"] = result
        self.store.replace_operation_payload(operation_id, payload)
        self.store.advance(operation_id, "gateway_rejected")
        self.receipts.complete(payload["receipt"], result)
        self.store.advance(operation_id, "gateway_rejected", done=True)

    def _reject_recovery(
        self, operation_id: str, payload: dict, owner: dict, lease: str
    ) -> dict[str, Any]:
        """Converge a resumed operation after definitive authority removal."""

        target = Path(payload["target_path"])
        staging = Path(payload["staging_path"])
        recovery = Path(payload["recovery_path"])
        quarantine = Path(payload["quarantine_path"])
        recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_hashes = _tree_hashes(target)
        if target_hashes == payload["local_snapshot"] and not recovery.exists():
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lease)
            _durable_replace(target, recovery)
        elif target_hashes == payload["baseline"] and not quarantine.exists():
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lease)
            _durable_replace(target, quarantine)
        elif target.exists() and target_hashes not in (
            payload["local_snapshot"], payload["baseline"]
        ):
            raise PackagePolicyError("update recovery found unrecognized managed bytes")
        if _tree_hashes(staging) == payload["baseline"] and not quarantine.exists():
            _durable_replace(staging, quarantine)
        elif staging.exists() and _tree_hashes(staging) != payload["baseline"]:
            shutil.rmtree(staging, ignore_errors=True)
        if not recovery.exists():
            raise PackagePolicyError(
                "prior managed bytes are unavailable after authority rejection"
            )
        _require_tree(recovery, payload["local_snapshot"])
        if quarantine.exists():
            _require_tree(quarantine, payload["baseline"])
        self._preserve_copy(payload, recovery, operation_id, owner, lease)
        self.store.deactivate_install(payload["skill_id"])
        result = {
            "updated": False,
            "rejected": True,
            "skill_id": payload["skill_id"],
            "version": int(payload["version"]),
            "path": None,
            "preserved_copy": payload["preservation"].get("destination"),
            "recovery_path": str(recovery),
            "quarantine_path": str(quarantine) if quarantine.exists() else None,
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }
        payload["result"] = result
        self.store.replace_operation_payload(operation_id, payload)
        self.store.advance(operation_id, "gateway_rejected")
        self.receipts.complete(payload["receipt"], result)
        self.store.advance(operation_id, "gateway_rejected", done=True)
        return result

    def _preserve_copy(
        self, payload: dict, recovery: Path, operation_id: str, owner: dict, lease: str
    ) -> None:
        preservation = payload["preservation"]
        if preservation.get("action") == "none":
            if preservation.get("destination") is not None:
                raise PackagePolicyError("unreviewed edited-copy destination")
            return
        if preservation.get("action") != "preserve_unmanaged_copy":
            raise PackagePolicyError("edited managed bytes cannot be replaced without preservation")
        destination = Path(str(preservation.get("destination") or ""))
        root = Path(self.host.skills_root).expanduser().absolute()
        if destination.parent != root or destination.name.startswith(('.', '_')):
            raise PackagePolicyError("edited-copy preservation destination is invalid")
        _within(destination, root, "edited-copy preservation destination")
        if destination.exists():
            _require_tree(destination, payload["local_snapshot"])
            return
        pending = root / ".hub" / "collective-wisdom-preservation" / operation_id
        _within(
            pending,
            root / ".hub" / "collective-wisdom-preservation",
            "edited-copy staging destination",
        )
        if pending.exists() or pending.is_symlink():
            if _tree_hashes(pending) != payload["local_snapshot"]:
                shutil.rmtree(pending, ignore_errors=True)
        if not pending.exists():
            pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(recovery, pending, copy_function=shutil.copy2)
            _require_tree(pending, payload["local_snapshot"])
            _fsync_tree(pending)
        _same_filesystem(pending, destination.parent)
        self._assert_owner(owner)
        self.store.assert_operation_lock(payload["skill_id"], lease)
        _durable_replace(pending, destination)
        _require_tree(destination, payload["local_snapshot"])

    def _paths(self, operation_id: str, payload: dict) -> dict[str, str]:
        if Path(operation_id).name != operation_id:
            raise PackagePolicyError("update operation identifier is invalid")
        root = Path(self.host.skills_root).expanduser().absolute()
        staging = root / ".hub" / "collective-wisdom" / operation_id
        archive = root / ".archive" / "collective-wisdom-updates" / operation_id
        validate_update_target(self.host, payload, payload["owner"])
        _reject_symlink_chain(root, staging)
        _reject_symlink_chain(root, archive)
        _within(staging, root / ".hub" / "collective-wisdom", "update staging path")
        _within(archive, root / ".archive" / "collective-wisdom-updates", "update recovery path")
        return {
            "staging_path": str(staging),
            "recovery_path": str(archive / "previous-managed"),
            "quarantine_path": str(archive / "gateway-rejected"),
        }

    def _repair_start(self, row: dict, payload: dict) -> None:
        changed = False
        for key, value in self._paths(str(row["id"]), payload).items():
            if payload.get(key) is None:
                payload[key] = value
                changed = True
            elif payload[key] != value:
                raise PackagePolicyError("update recovery path is invalid")
        if changed:
            self.store.replace_operation_payload(str(row["id"]), payload)
        self._bind_receipt(payload["receipt"], str(row["id"]))

    def _bind_receipt(self, receipt: str, operation_id: str) -> None:
        metadata = self.store.get_metadata("update_receipt:" + receipt)
        if not isinstance(metadata, dict) or metadata.get("operation_id") is None:
            self.receipts.start(receipt, operation_id)
        elif metadata["operation_id"] != operation_id:
            raise WisdomConflict("Update receipt is bound to another operation")

    def _remote(self, skill_id: str, owner: dict) -> dict | None:
        self._assert_owner(owner)
        values = self.client.installations(owner["installation_id"])
        self._assert_owner(owner)
        if not isinstance(values, list):
            return None
        matches = [
            row for row in values
            if isinstance(row, dict) and row.get("skill_id") == skill_id
        ]
        return dict(matches[0]) if len(matches) == 1 else None

    def _pending_for_skill(self, skill_id: str) -> list[dict]:
        return [
            row for row in self.store.pending_operations()
            if row.get("entity_id") == skill_id
        ]

    def _owner(self) -> dict:
        value = self.owner()
        required = {
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        }
        if not isinstance(value, dict) or set(value) != required or any(
            not value.get(key) for key in required
        ):
            raise WisdomAuthError("Update owner is unavailable")
        return {key: str(value[key]) for key in required}

    def _assert_owner(self, expected: dict) -> None:
        if self._owner() != expected:
            raise WisdomAuthError("Update owner changed during operation")

    @staticmethod
    def _install_record(
        payload: dict, owner: dict, state: str, *, mode: str | None = None
    ) -> dict:
        return {
            "skill_id": payload["skill_id"],
            "org_id": owner["organization_id"],
            "slug": payload["slug"],
            "version": int(payload["version"]),
            "content_hash": payload["content_hash"],
            "baseline": dict(payload["baseline"]),
            "target_path": payload["target_path"],
            "update_mode": mode or payload["update_mode"],
            "state": state,
            "owner": owner,
        }

    @staticmethod
    def _pending_result(payload: dict, operation_id: str, message: str) -> dict:
        return {
            "pending": True,
            "operation_id": operation_id,
            "receipt": payload["receipt"],
            "skill_id": payload["skill_id"],
            "phase": "local_ledger_committed",
            "retry": (
                "apply_automatic"
                if payload.get("authorization", {}).get("kind") == "automatic_policy"
                else "update_apply"
            ),
            "files_installed": True,
            "local_state": "active",
            "error": message,
        }


def _required_text(values: dict, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise PackagePolicyError(f"update plan has no valid {key}")
    return value


def _operation_payload(operation: dict) -> dict:
    payload = operation.get("payload")
    if payload is None and isinstance(operation.get("payload_json"), str):
        try:
            payload = json.loads(operation["payload_json"])
        except json.JSONDecodeError as exc:
            raise PackagePolicyError("update recovery payload is invalid") from exc
    if not isinstance(payload, dict):
        raise PackagePolicyError("update recovery payload is invalid")
    return dict(payload)


def _terminal_rejection(exc: Exception) -> bool:
    if isinstance(exc, WisdomAuthError):
        return True
    code = getattr(exc, "code", None)
    if code in _TERMINAL_CODES:
        return True
    return isinstance(exc, WisdomNotFound) or (
        isinstance(exc, WisdomValidationError) and code in _TERMINAL_CODES
    )


def _fsync_tree(root: Path) -> None:
    """Durably flush copied file contents and directories before promotion."""

    if os.name != "posix":
        return
    for path in (item for item in root.rglob("*") if item.is_file()):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = [root, *(item for item in root.rglob("*") if item.is_dir())]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
