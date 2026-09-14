"""Atomic, durable execution of reviewed manual Wisdom installations."""

# The frozen six-dependency interface and explicit recovery phases are intentionally
# kept together so every filesystem transition remains auditable.
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .contract import org_directory_name
from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomNotFound,
    WisdomValidationError,
)
from .package import PackagePolicyError
from .setup_lock import managed_operation_guard


class InstallExecutor:
    """Adapt Agent 0924 install/uninstall phases to owner-bound plugin APIs."""

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

    def apply(self, receipt: str, accept_partial: bool = False) -> dict:
        """Exclude setup starters while applying one reviewed installation."""
        plan = self.receipts.load(receipt, allow_started=True)
        skill_id = _required_text(plan, "skill_id")
        with managed_operation_guard(Path(self.store.root), skill_id):
            return self._apply(receipt, accept_partial=accept_partial)

    def _apply(self, receipt: str, accept_partial: bool = False) -> dict:
        """Apply one exact reviewed receipt, or resume its unfinished operation."""
        owner = self._owner()
        plan = self.receipts.load(receipt, allow_started=True)
        completed = self.receipts.outcome(receipt)
        if completed is not None:
            return completed
        skill_id = _required_text(plan, "skill_id")
        lock = self.store.acquire_operation_lock(skill_id)
        if not lock:
            raise PackagePolicyError("another managed operation is already active for this skill")
        try:
            self._assert_owner(owner)
            conflicting = self._pending("uninstall", skill_id)
            if conflicting or self._pending("update", skill_id):
                raise PackagePolicyError("another managed operation is pending for this skill")
            if self._pending("wisdom_setup", skill_id):
                raise PackagePolicyError("a setup operation is pending for this skill")
            pending = self._pending("install", skill_id)
            if pending:
                payload = _operation_payload(pending)
                self._require_payload_owner(payload, owner)
                if payload.get("receipt") != receipt:
                    raise PackagePolicyError(
                        "another install receipt is already pending for this skill"
                    )
                self._repair_install_start(str(pending["id"]), payload)
                self.store.assert_operation_lock(skill_id, lock)
                return self._resume_install(str(pending["id"]), payload, lock)
            return self._start_install(plan, receipt, owner, lock, accept_partial)
        finally:
            self.store.release_operation_lock(skill_id, lock)

    def uninstall(self, skill_id: str) -> dict:
        """Exclude setup starters while beginning recoverable removal."""
        with managed_operation_guard(Path(self.store.root), skill_id):
            return self._uninstall(skill_id)

    def _uninstall(self, skill_id: str) -> dict:
        """Remove one owner-managed skill into recoverable hidden storage."""
        owner = self._owner()
        lock = self.store.acquire_operation_lock(skill_id)
        if not lock:
            raise PackagePolicyError("another managed operation is already active for this skill")
        try:
            self._assert_owner(owner)
            conflicting = self._pending("install", skill_id)
            if conflicting or self._pending("update", skill_id):
                raise PackagePolicyError("another managed operation is pending for this skill")
            if self._pending("wisdom_setup", skill_id):
                raise PackagePolicyError("a setup operation is pending for this skill")
            pending = self._pending("uninstall", skill_id)
            if pending:
                payload = _operation_payload(pending)
                self._require_payload_owner(payload, owner)
                if payload.get("trash_path") is None:
                    payload["trash_path"] = str(
                        self._trash_path(str(pending["id"]), payload["slug"])
                    )
                    self.store.replace_operation_payload(str(pending["id"]), payload)
                return self._resume_uninstall(str(pending["id"]), payload, lock)
            installation = self.store.installation(skill_id)
            if not installation:
                raise PackagePolicyError("managed installation not found")
            self._require_record_owner(installation, owner)
            if installation.get("state") != "active":
                completed = self.store.get_metadata(_uninstall_key(skill_id))
                if completed and completed.get("owner") == owner:
                    result = completed.get("result")
                    if isinstance(result, dict):
                        return result
                raise PackagePolicyError("managed installation is already inactive")
            target = self._validated_record_target(installation, owner)
            payload = {
                "owner": owner,
                "receipt": None,
                "skill_id": skill_id,
                "slug": _safe_segment(installation.get("slug"), "skill slug"),
                "target_path": str(target),
                "staging_path": None,
                "trash_path": None,
                "baseline": dict(installation.get("baseline") or {}),
            }
            operation_id = self.store.journal("uninstall", skill_id, "staged", payload)
            trash = self._trash_path(operation_id, payload["slug"])
            payload["trash_path"] = str(trash)
            self.store.replace_operation_payload(operation_id, payload)
            return self._resume_uninstall(operation_id, payload, lock)
        finally:
            self.store.release_operation_lock(skill_id, lock)

    def installed(self) -> list[dict]:
        """Merge owner-local integrity and pending state with current Gateway facts."""
        owner = self._owner()
        local = self.store.installations(owner=owner)
        pending = self.store.pending_operations()
        self._assert_owner(owner)
        remote = self.client.installations(owner["installation_id"])
        self._assert_owner(owner)
        by_skill = {item["skill_id"]: item for item in remote}
        pending_by_skill = {
            item["entity_id"]: item
            for item in pending
            if _operation_payload(item).get("owner") == owner
        }
        result = []
        for item in local:
            if item.get("owner") != owner:
                continue
            operation = pending_by_skill.pop(item["skill_id"], None)
            if item.get("state") == "active" or operation:
                result.append(self._installed_item(item, by_skill, operation))
        for skill_id, operation in pending_by_skill.items():
            payload = _operation_payload(operation)
            result.append(self._installed_item({
                "skill_id": skill_id,
                "slug": payload.get("slug"),
                "version": payload.get("version"),
                "content_hash": payload.get("content_hash"),
                "baseline": payload.get("baseline", {}),
                "target_path": payload.get("target_path"),
                "update_mode": payload.get("update_mode") or "MANUAL",
                "state": "pending",
                "owner": owner,
            }, by_skill, operation))
        self._assert_owner(owner)
        return result

    @staticmethod
    def _installed_item(item: dict, remote: dict, operation: dict | None) -> dict:
        server = remote.get(item["skill_id"], {})
        target = item.get("target_path")
        return {
            **item,
            "latest_version": server.get("latest_version"),
            "skill_state": server.get("skill_state"),
            "effective_update_mode": server.get("update_mode"),
            "modified": not target
            or _tree_hashes(Path(target)) != item.get("baseline"),
            "pending_operation": (
                {"kind": operation["kind"], "phase": operation["phase"]}
                if operation
                else None
            ),
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }

    def _start_install(
        self, plan: dict, receipt: str, owner: dict, lock: str, accept_partial: bool
    ) -> dict:
        existing = self.store.installation(_required_text(plan, "skill_id"))
        if existing:
            self._require_record_owner(existing, owner)
            if existing.get("state") == "active":
                raise PackagePolicyError("managed installation already exists")
            inactive_target = self._validated_record_target(existing, owner)
            if inactive_target.exists() or inactive_target.is_symlink():
                raise PackagePolicyError("inactive managed target still exists")
        outcome = _compatibility_outcome(plan)
        if outcome == "blocked_pending_action":
            raise PackagePolicyError("blocked compatibility requirements prevent activation")
        if outcome in {"partial", "compatible_after_setup"} and not accept_partial:
            raise PackagePolicyError("explicit --accept-partial is required")
        verified = self.reviewer.verify(plan)
        self._assert_owner(owner)
        self.store.assert_operation_lock(_required_text(plan, "skill_id"), lock)
        skill_id = _required_text(verified.plan, "skill_id")
        if skill_id != plan["skill_id"] or verified.plan.get("receipt") != receipt:
            raise WisdomValidationError("verified install does not match its receipt")
        slug = _safe_segment(verified.plan.get("slug"), "skill slug")
        target = self._target(owner, slug)
        if target.exists() or target.is_symlink():
            raise PackagePolicyError("managed target already exists")
        self.host.validate_target(target)
        payload = {
            **dict(verified.plan),
            "owner": owner,
            "receipt": receipt,
            "skill_id": skill_id,
            "slug": slug,
            "target_path": str(target),
            "staging_path": None,
            "trash_path": None,
            "baseline": dict(verified.baseline),
            "accepted_partial": bool(accept_partial),
        }
        operation_id = self.store.journal("install", skill_id, "staged", payload)
        staging = self._staging_path(operation_id)
        payload["staging_path"] = str(staging)
        self.store.replace_operation_payload(operation_id, payload)
        self._bind_install_receipt(receipt, operation_id)
        try:
            _write_staging(staging, verified.files)
            scan = self.host.scan(staging)
            try:
                _require_scan(scan, verified.plan.get("local_scan"), accept_partial)
            except PackagePolicyError:
                shutil.rmtree(staging, ignore_errors=True)
                self.store.advance(operation_id, "scan_rejected", done=True)
                raise
            payload["local_scan"] = scan
            self.store.replace_operation_payload(operation_id, payload)
            self._assert_owner(owner)
            self.store.assert_operation_lock(skill_id, lock)
            return self._resume_install(operation_id, payload, lock)
        except BaseException:
            if staging.exists() and not self._pending("install", skill_id):
                shutil.rmtree(staging)
            raise

    def _resume_install(self, operation_id: str, payload: dict, lock: str) -> dict:
        operation = self.store.operation(operation_id)
        if not operation:
            raise PackagePolicyError("install recovery journal is missing")
        owner = self._owner()
        self._require_payload_owner(payload, owner)
        self.store.assert_operation_lock(payload["skill_id"], lock)
        phase = str(operation["phase"])
        staging = Path(payload["staging_path"])
        target = Path(payload["target_path"])
        self._validate_operation_paths(
            staging, target, owner, operation_id, payload["slug"]
        )
        if phase == "staged":
            verified, scan_path = self._revalidate_staged(payload, staging, target)
            if not staging.exists() and not target.exists():
                _write_staging(staging, verified.files)
                scan_path = staging
            scan = self.host.scan(scan_path)
            _require_scan(
                scan,
                verified.plan.get("local_scan"),
                bool(payload.get("accepted_partial")),
            )
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            if staging.is_dir():
                _require_tree(staging, payload["baseline"])
                _same_filesystem(staging, target.parent)
                if target.exists() or target.is_symlink():
                    raise PackagePolicyError("managed target collision during install")
                self._assert_owner(owner)
                self.store.assert_operation_lock(payload["skill_id"], lock)
                _durable_replace(staging, target)
            elif target.is_dir():
                _require_tree(target, payload["baseline"])
            else:
                raise PackagePolicyError("staged install bytes are unavailable")
            self.store.advance(operation_id, "files_committed")
            phase = "files_committed"
        if phase == "files_committed":
            _require_tree(target, payload["baseline"])
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            self.store.record_install(self._install_record(payload, owner, "active"))
            self.store.advance(operation_id, "local_ledger_committed")
            phase = "local_ledger_committed"
        if phase == "local_ledger_committed":
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            try:
                server = self.client.record_install(
                    skill_id=payload["skill_id"],
                    installation_id=owner["installation_id"],
                    version=int(payload["version"]),
                    takedown_generation=int(payload["takedown_generation"]),
                    update_mode=payload.get("update_mode"),
                )
            except WisdomAuthError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if _permanent_gateway_error(exc):
                    self._assert_owner(owner)
                    self.store.assert_operation_lock(payload["skill_id"], lock)
                    self._quarantine_rejected(
                        operation_id, payload, target, owner, lock
                    )
                    raise
                return self._pending_result(payload, operation_id, exc)
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            mode = _field(server, "effective_update_mode")
            self.store.record_install({
                **self._install_record(payload, owner, "active"),
                "update_mode": mode,
            })
            result = self._success_result(payload, target, mode)
            payload["result"] = result
            self.store.replace_operation_payload(operation_id, payload)
            self.store.advance(operation_id, "gateway_recorded")
            self.receipts.complete(payload["receipt"], result)
            self.store.advance(operation_id, "gateway_recorded", done=True)
            return result
        if phase == "gateway_recorded":
            result = payload.get("result") or self.receipts.outcome(payload["receipt"])
            if result:
                self.receipts.complete(payload["receipt"], result)
                self.store.advance(operation_id, "gateway_recorded", done=True)
                return result
        if phase == "gateway_rejected":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise PackagePolicyError("rejected install recovery outcome is missing")
            self.receipts.complete(payload["receipt"], result)
            self.store.advance(operation_id, "gateway_rejected", done=True)
            return result
        raise PackagePolicyError(f"unsupported install recovery phase: {phase}")

    def _repair_install_start(self, operation_id: str, payload: dict) -> None:
        if payload.get("staging_path") is None:
            payload["staging_path"] = str(self._staging_path(operation_id))
            self.store.replace_operation_payload(operation_id, payload)
        self._bind_install_receipt(payload["receipt"], operation_id)

    def _bind_install_receipt(self, receipt: str, operation_id: str) -> None:
        metadata = self.store.get_metadata("install_receipt:" + receipt)
        if not isinstance(metadata, dict):
            # The receipt boundary rejects missing or invalid authority records.
            self.receipts.start(receipt, operation_id)
            return
        bound = metadata.get("operation_id")
        if bound is None:
            self.receipts.start(receipt, operation_id)
        elif bound != operation_id:
            raise WisdomConflict("Install receipt is already bound to another operation")

    def _resume_uninstall(self, operation_id: str, payload: dict, lock: str) -> dict:
        operation = self.store.operation(operation_id)
        if not operation:
            raise PackagePolicyError("uninstall recovery journal is missing")
        owner = self._owner()
        self._require_payload_owner(payload, owner)
        self.store.assert_operation_lock(payload["skill_id"], lock)
        phase = str(operation["phase"])
        target = Path(payload["target_path"])
        trash = Path(payload["trash_path"])
        self._validate_uninstall_paths(target, trash, owner, operation_id, payload["slug"])
        if phase == "staged":
            trash.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists() and not trash.exists():
                _same_filesystem(target, trash.parent)
                self._assert_owner(owner)
                self.store.assert_operation_lock(payload["skill_id"], lock)
                _durable_replace(target, trash)
            self.store.advance(operation_id, "files_committed")
            phase = "files_committed"
        if phase == "files_committed":
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            self.store.deactivate_install(payload["skill_id"])
            self.store.advance(operation_id, "local_ledger_committed")
            phase = "local_ledger_committed"
        if phase == "local_ledger_committed":
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            try:
                server = self.client.deactivate_install(
                    owner["installation_id"], payload["skill_id"]
                )
            except WisdomAuthError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if getattr(exc, "status", None) == 404 or isinstance(exc, WisdomNotFound):
                    server = {"state": "inactive"}
                elif not _permanent_gateway_error(exc):
                    return self._pending_result(payload, operation_id, exc, uninstall=True)
                else:
                    raise
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            result = {
                "uninstalled": True,
                "skill_id": payload["skill_id"],
                "state": _field(server, "state"),
                "recoverable_path": str(trash),
            }
            payload["result"] = result
            self.store.replace_operation_payload(operation_id, payload)
            self.store.set_metadata(
                _uninstall_key(payload["skill_id"]),
                {"owner": owner, "result": result},
            )
            self.store.advance(operation_id, "gateway_recorded", done=True)
            return result
        raise PackagePolicyError(f"unsupported uninstall recovery phase: {phase}")

    def _quarantine_rejected(
        self, operation_id: str, payload: dict, target: Path, owner: dict, lock: str
    ) -> None:
        trash = self._trash_path(operation_id, payload["slug"])
        trash.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and not trash.exists():
            _same_filesystem(target, trash.parent)
            self._assert_owner(owner)
            self.store.assert_operation_lock(payload["skill_id"], lock)
            _durable_replace(target, trash)
        self.store.deactivate_install(payload["skill_id"])
        payload["trash_path"] = str(trash)
        result = {
            "installed": False,
            "rejected": True,
            "skill_id": payload["skill_id"],
            "version": int(payload["version"]),
            "path": None,
            "recoverable_path": str(trash),
            "content_hash": payload["content_hash"],
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }
        payload["result"] = result
        self.store.replace_operation_payload(operation_id, payload)
        self.store.advance(operation_id, "gateway_rejected")
        self.receipts.complete(payload["receipt"], result)
        self.store.advance(operation_id, "gateway_rejected", done=True)

    def _install_record(self, payload: dict, owner: dict, state: str) -> dict:
        return {
            "skill_id": payload["skill_id"],
            "org_id": owner["organization_id"],
            "slug": payload["slug"],
            "version": int(payload["version"]),
            "content_hash": payload["content_hash"],
            "baseline": dict(payload["baseline"]),
            "target_path": payload["target_path"],
            "update_mode": payload.get("update_mode") or "MANUAL",
            "state": state,
            "owner": owner,
        }

    @staticmethod
    def _success_result(payload: dict, target: Path, mode: str) -> dict:
        return {
            "installed": True,
            "skill_id": payload["skill_id"],
            "version": int(payload["version"]),
            "path": str(target),
            "content_hash": payload["content_hash"],
            "effective_update_mode": mode,
            "compatibility": payload.get("compatibility"),
            "local_scan": payload.get("local_scan"),
            "setup_required": _compatibility_outcome(payload)
            in {"partial", "compatible_after_setup"},
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }

    @staticmethod
    def _pending_result(
        payload: dict, operation_id: str, _exc: Exception, uninstall: bool = False
    ) -> dict:
        return {
            "pending": True,
            "operation_id": operation_id,
            "receipt": payload.get("receipt"),
            "skill_id": payload["skill_id"],
            "phase": "local_ledger_committed",
            "retry": "uninstall" if uninstall else "apply",
            "files_installed": not uninstall,
            "local_state": "inactive" if uninstall else "active",
            "error": "Gateway reconciliation is temporarily unavailable",
        }

    def _pending(self, kind: str, skill_id: str) -> dict | None:
        for operation in self.store.pending_operations():
            if operation.get("kind") == kind and operation.get("entity_id") == skill_id:
                return operation
        return None

    def _owner(self) -> dict:
        value = self.owner()
        required = (
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        )
        if (
            not isinstance(value, dict)
            or set(value) != set(required)
            or any(not value.get(key) for key in required)
        ):
            raise PackagePolicyError("current installation owner is unavailable")
        return {key: str(value[key]) for key in required}

    def _assert_owner(self, expected: dict) -> None:
        if self._owner() != expected:
            raise PackagePolicyError("installation owner changed during operation")

    @staticmethod
    def _require_payload_owner(payload: dict, owner: dict) -> None:
        if payload.get("owner") != owner:
            raise PackagePolicyError("operation belongs to another installation owner")

    @staticmethod
    def _require_record_owner(record: dict, owner: dict) -> None:
        if record.get("owner") != owner:
            raise PackagePolicyError("skill id belongs to another installation owner")

    def _target(self, owner: dict, slug: str) -> Path:
        root = self._skills_root()
        managed = root / "_wisdom" / org_directory_name(owner["organization_id"])
        _reject_symlink_chain(root, managed)
        managed.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = managed / slug
        _within(target, managed, "managed install target")
        return target

    def _staging_path(self, operation_id: str) -> Path:
        operation = _safe_segment(operation_id, "operation id")
        root = self._skills_root()
        path = root / ".hub" / "collective-wisdom" / operation
        _reject_symlink_chain(root, path.parent)
        _within(path, root / ".hub" / "collective-wisdom", "staging path")
        return path

    def _trash_path(self, operation_id: str, slug: str) -> Path:
        operation = _safe_segment(operation_id, "operation id")
        root = self._skills_root()
        path = root / ".archive" / "collective-wisdom" / operation / slug
        _reject_symlink_chain(root, path.parent)
        _within(path, root / ".archive" / "collective-wisdom", "archive path")
        return path

    def _skills_root(self) -> Path:
        root = Path(self.host.skills_root).expanduser().absolute()
        _reject_existing_links(root)
        return root

    def _validated_record_target(self, record: dict, owner: dict) -> Path:
        target = Path(str(record.get("target_path") or ""))
        expected = self._target(owner, _safe_segment(record.get("slug"), "skill slug"))
        if target != expected:
            raise PackagePolicyError("managed installation target is invalid")
        self.host.validate_target(target)
        return target

    def _validate_operation_paths(
        self,
        staging: Path,
        target: Path,
        owner: dict,
        operation_id: str,
        slug: str,
    ) -> None:
        if staging != self._staging_path(operation_id):
            raise PackagePolicyError("install recovery staging path is invalid")
        expected = self._target(owner, _safe_segment(slug, "skill slug"))
        if target != expected:
            raise PackagePolicyError("install recovery target path is invalid")
        self.host.validate_target(target)

    def _revalidate_staged(
        self, payload: dict, staging: Path, target: Path
    ) -> tuple[object, Path]:
        plan = self.receipts.load(payload["receipt"], allow_started=True)
        verified = self.reviewer.verify(plan)
        _require_verified_match(payload, verified)
        outcome = _compatibility_outcome(verified.plan)
        if outcome == "blocked_pending_action":
            raise PackagePolicyError("blocked compatibility requirements prevent activation")
        if (
            outcome in {"partial", "compatible_after_setup"}
            and not payload.get("accepted_partial")
        ):
            raise PackagePolicyError("explicit --accept-partial is required")
        return verified, staging if staging.exists() else target

    def _validate_uninstall_paths(
        self, target: Path, trash: Path, owner: dict, operation_id: str, slug: str
    ) -> None:
        expected_target = self._target(owner, _safe_segment(slug, "skill slug"))
        if target != expected_target or trash != self._trash_path(operation_id, slug):
            raise PackagePolicyError("uninstall recovery path is invalid")
        self.host.validate_target(target)


def _operation_payload(operation: dict) -> dict:
    payload = operation.get("payload")
    if payload is None and isinstance(operation.get("payload_json"), str):
        try:
            payload = json.loads(operation["payload_json"])
        except json.JSONDecodeError as exc:
            raise PackagePolicyError("operation recovery payload is invalid") from exc
    if not isinstance(payload, dict):
        raise PackagePolicyError("operation recovery payload is invalid")
    return dict(payload)


def _uninstall_key(skill_id: str) -> str:
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()
    return "install_uninstall_outcome:" + digest


def _required_text(values: dict, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise PackagePolicyError(f"install plan has no valid {key}")
    return value


def _safe_segment(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise PackagePolicyError(f"{label} is invalid")
    return value


def _within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PackagePolicyError(f"{label} escaped its root") from exc


def _reject_symlink_chain(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink() or _is_junction(current):
            raise PackagePolicyError("managed install path contains a symlink")


def _reject_existing_links(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or _is_junction(current):
            raise PackagePolicyError("managed install path contains a symlink")


def _write_staging(staging: Path, files: tuple | list) -> None:
    if staging.exists() or staging.is_symlink():
        raise PackagePolicyError("staging target already exists")
    staging.mkdir(parents=True, mode=0o700)
    try:
        for raw_path, mode, body in files:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise PackagePolicyError("verified package contains an unsafe path")
            if mode != "file" or not isinstance(body, bytes):
                raise PackagePolicyError("verified package contains an unsupported entry")
            destination = staging.joinpath(*path.parts)
            _within(destination, staging, "package file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise PackagePolicyError("verified package contains a target collision")
            with destination.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fchmod(stream.fileno(), 0o600)
                os.fsync(stream.fileno())
        for directory in sorted(
            (path for path in staging.rglob("*") if path.is_dir()),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _require_tree(root: Path, baseline: dict) -> None:
    if root.is_symlink() or not root.is_dir():
        raise PackagePolicyError("managed package tree is unavailable")
    actual = _tree_hashes(root)
    if actual != baseline:
        raise WisdomValidationError("managed package bytes do not match the recovery journal")


def _tree_hashes(root: Path) -> dict[str, str] | None:
    if root.is_symlink() or _is_junction(root) or not root.is_dir():
        return None
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or _is_junction(path) or (
            not path.is_file() and not path.is_dir()
        ):
            return None
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = "sha256:" + hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return actual


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", lambda _path: False)
    return bool(checker(path))


def _require_scan(current: object, reviewed: object, accepted_partial: bool) -> None:
    if not isinstance(current, dict) or current != reviewed:
        raise PackagePolicyError("host scan changed after review; create a new review")
    allowed = current.get("allowed")
    if allowed is False or allowed not in {True, None}:
        raise PackagePolicyError("host scan blocked installation")
    if allowed is None and not accepted_partial:
        raise PackagePolicyError("reviewed scan warning requires explicit --accept-partial")


def _require_verified_match(payload: dict, verified: object) -> None:
    plan = verified.plan
    immutable = (
        "receipt",
        "skill_id",
        "slug",
        "version",
        "content_hash",
        "takedown_generation",
    )
    if any(plan.get(key) != payload.get(key) for key in immutable):
        raise WisdomValidationError("install authorization changed; create a new review")
    if dict(verified.baseline) != payload.get("baseline"):
        raise WisdomValidationError("reviewed install bytes changed; create a new review")


def _same_filesystem(source: Path, destination_parent: Path) -> None:
    destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.stat().st_dev != destination_parent.stat().st_dev:
        raise PackagePolicyError("atomic rename requires staging and target on one filesystem")


def _durable_replace(source: Path, destination: Path) -> None:
    source_parent = source.parent
    destination_parent = destination.parent
    os.replace(source, destination)
    _fsync_directory(destination_parent)
    if source_parent != destination_parent:
        _fsync_directory(source_parent)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _compatibility_outcome(plan: dict) -> str | None:
    compatibility = plan.get("compatibility")
    return compatibility.get("outcome") if isinstance(compatibility, dict) else None


def _field(value: object, name: str) -> Any:
    if isinstance(value, dict):
        result = value.get(name)
    else:
        result = getattr(value, name, None)
    if not isinstance(result, str) or not result:
        raise WisdomValidationError(f"Gateway acknowledgement omitted {name}")
    return result


def _permanent_gateway_error(exc: Exception) -> bool:
    return isinstance(exc, (WisdomConflict, WisdomNotFound, WisdomValidationError)) or getattr(
        exc, "status", None
    ) in {404, 409, 422}
