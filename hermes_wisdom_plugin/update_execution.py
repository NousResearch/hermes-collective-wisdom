"""Direct, restart-safe execution of exact reviewed Wisdom updates."""

# The public constructor is shared with install execution and the direct resume
# method deliberately keeps one auditable sequence of filesystem boundaries.
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-statements,too-many-boolean-expressions

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .errors import WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError
from .install_execution import (
    _accepted_scan_warning,
    _field,
    _install_record,
    _operation_payload,
    _owner_snapshot,
    _require_current,
    _required_text,
    _require_scan,
)
from .package import PackagePolicyError
from .package_application import (
    durable_replace as _durable_replace,
    fsync_tree as _fsync_tree,
    reject_symlink_chain as _reject_symlink_chain,
    replace_package as _replace_package,
    require_tree as _require_tree,
    same_filesystem as _same_filesystem,
    stage_package as _write_staging,
    tree_hashes as _tree_hashes,
    within as _within,
)
from .setup_lock import managed_operation_guard
from .update_review import UpdateAuthorityRejected, validate_update_target

_TERMINAL_CODES = frozenset({
    "skill_not_active", "skill_taken_down", "skill_archived", "skill_not_found",
    "version_not_found", "takedown_generation_changed",
})


class UpdateExecutor:  # pylint: disable=too-many-instance-attributes
    """Replace one managed tree with a small synchronous recovery journal."""

    _require_current = staticmethod(_require_current)
    _payload = staticmethod(_operation_payload)
    _install_record = staticmethod(_install_record)

    def __init__(self, client, store, receipts, reviewer, host, owner) -> None:
        self.client, self.store, self.receipts = client, store, receipts
        self.reviewer, self.host, self.owner = reviewer, host, owner

    def apply(self, receipt: str, *, may_apply=None, expected_recovery=None) -> dict[str, Any]:
        """Apply or explicitly resume one human-reviewed update."""
        plan = self.receipts.load(receipt, allow_started=True)
        if plan.get("authorization", {}).get("kind") == "automatic_policy":
            raise PackagePolicyError(
                "automatic policy receipts cannot be applied as manual confirmation"
            )
        return self._locked_apply(receipt, plan, may_apply, expected_recovery)

    def apply_automatic(self, receipt: str) -> dict[str, Any]:
        """Apply or resume only an automatic-policy update."""
        plan = self.receipts.load(receipt, allow_started=True)
        if (plan.get("authorization", {}).get("kind") != "automatic_policy"
                or plan.get("user_confirmation") is not False):
            raise PackagePolicyError("automatic execution requires automatic policy authority")
        return self._locked_apply(receipt, plan, None, None)

    def _locked_apply(self, receipt, plan, may_apply, expected_recovery):
        skill_id = _required_text(plan, "skill_id")
        with managed_operation_guard(Path(self.store.root), skill_id):
            if expected_recovery is not None:
                operation_id = expected_recovery.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    raise WisdomConflict("reviewed update recovery identity is invalid")
                if self.recovery_status(skill_id, operation_id) != expected_recovery:
                    raise WisdomConflict("update recovery changed after it was reviewed")
            return self._execute(receipt, plan, may_apply)

    def recovery_status(self, skill_id: str, operation_id: str) -> dict[str, Any]:
        """Return the exact owner-bound operation identity without resuming it."""
        row = self.store.operation(operation_id)
        if row is None or row.get("kind") != "update" or row.get("skill_id") != skill_id:
            raise PackagePolicyError("update recovery operation was not found")
        payload = self._payload(row)
        if payload.get("owner") != self._owner():
            raise WisdomAuthError("update recovery belongs to another enrolment")
        self._validate_identity(payload, skill_id)
        self._matches_plan(payload, self.receipts.load(payload["receipt"], allow_started=True))
        return {
            "kind": "update", "skill_id": skill_id, "operation_id": operation_id,
            "receipt": payload["receipt"], "phase": row["stage"], "state": row["status"],
            "from_version": payload["from_version"], "version": payload["version"],
            "content_hash": payload["content_hash"], "can_resume": row["status"] != "done",
        }

    def _execute(self, receipt: str, plan: dict, may_apply) -> dict[str, Any]:
        owner = self._owner()
        if plan.get("owner") != owner:
            raise WisdomAuthError("Update receipt belongs to another enrolment")
        self._assert_owner(owner)
        skill_id = _required_text(plan, "skill_id")
        pending = self.store.pending_operations(skill_id)
        if pending:
            row = pending[0]
            payload = self._payload(row)
            if row.get("kind") != "update" or payload.get("receipt") != receipt:
                raise PackagePolicyError("another managed operation is pending for this skill")
            self._matches_plan(payload, plan)
            self._require_current(may_apply)
            self._ready_operation(row, payload, receipt)
            return self._resume(row, payload, may_apply)
        completed = self.receipts.outcome(receipt)
        if completed is not None:
            return completed
        verified = self.reviewer.verify(plan)
        if not verified.plan.get("allowed"):
            raise PackagePolicyError("reviewed update is not allowed")
        preservation = verified.plan.get("preservation")
        if not isinstance(preservation, dict) or preservation.get("action") not in {
            "none", "preserve_unmanaged_copy",
        }:
            raise PackagePolicyError("reviewed edited-copy action does not authorize replacement")
        self._require_current(may_apply)
        payload = {**dict(verified.plan), "owner": owner, "receipt": receipt}
        operation_id = self.store.begin("update", skill_id, payload, stage="prepared")
        operation = self.store.operation(operation_id)
        if not isinstance(operation, dict):
            raise PackagePolicyError("update recovery journal is missing")
        self._ready_operation(operation, payload, receipt)
        return self._resume(operation, payload, may_apply, verified)

    def _resume(self, row, payload, may_apply, verified=None):  # pylint: disable=too-many-branches
        operation_id = str(row["id"])
        owner = self._owner()
        if payload.get("owner") != owner:
            raise WisdomAuthError("update recovery belongs to another enrolment")
        if row["stage"] == "outcome_recorded":
            return self._finish(operation_id, payload)

        paths = self._paths(operation_id, payload)
        target = Path(payload["target_path"])
        staging, recovery = paths["staging_path"], paths["recovery_path"]
        old = payload["local_snapshot"]
        new = payload["baseline"]
        target_hashes, recovery_hashes = _tree_hashes(target), _tree_hashes(recovery)
        if target_hashes not in (old, new, None) or recovery_hashes not in (old, None):
            raise PackagePolicyError("update recovery found unrecognized managed bytes")

        recorded_stage = str(row["stage"])
        if recorded_stage in {"local_committed", "outcome_recorded"}:
            stage = recorded_stage
        elif recovery_hashes == old and target_hashes is None:
            stage = "old_preserved"
        elif recovery_hashes == old and target_hashes == new:
            stage = "files_committed"
        else:
            stage = recorded_stage
        if verified is None:
            try:
                verified = self.reviewer.verify(payload, recovery_phase=stage)
            except UpdateAuthorityRejected:
                return self._reject(operation_id, payload, owner)
        self._require_current(may_apply)
        if target_hashes != new:
            self._prepare_staging(staging, verified, payload)

        def before_preserve() -> None:
            self._assert_owner(owner)
            self._require_current(may_apply)

        def approve_staged(path: Path) -> None:
            _require_scan(
                self.host.scan(path), payload.get("local_scan"),
                _accepted_scan_warning(payload, operation="update"),
            )
            before_preserve()

        _replace_package(
            (staging, target, recovery), (old, new), approve_staged, before_preserve
        )
        self._preserve_copy(payload, recovery, operation_id, owner)

        installation = self.store.installation(payload["skill_id"], owner=owner)
        if not isinstance(installation, dict) or installation.get("version") != payload["version"]:
            self._assert_owner(owner)
            self.store.record_install(self._install_record(payload, owner))
        self.store.checkpoint(operation_id, stage="local_committed")
        return self._reconcile(operation_id, payload, owner, target, recovery, may_apply)

    def _prepare_staging(self, staging: Path, verified, payload: dict) -> None:
        if verified is None or not verified.files:
            if _tree_hashes(staging) != payload["baseline"]:
                raise PackagePolicyError("reviewed update staging bytes are unavailable")
            return
        _write_staging(staging, verified.files, payload["baseline"])

    def _reconcile(self, operation_id, payload, owner, target, recovery, may_apply):
        remote = self._remote(payload["skill_id"], owner)
        remote_version = remote.get("installed_version") if remote else None
        mode = remote.get("update_mode") if remote_version == payload["version"] else None
        manual = payload.get("authorization", {}).get("kind") == "manual_receipt"
        if remote_version == payload["from_version"] or (manual and remote is None):
            try:
                self._require_current(may_apply)
                server = self.client.record_install(
                    skill_id=payload["skill_id"], installation_id=owner["installation_id"],
                    version=int(payload["version"]),
                    takedown_generation=int(payload["takedown_generation"]),
                    update_mode=payload["update_mode"],
                )
                mode = _field(server, "effective_update_mode")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if _terminal_rejection(exc):
                    self._reject(operation_id, payload, owner)
                    raise
                return self._pending(payload, operation_id,
                                     "Gateway reconciliation is temporarily unavailable")
        elif remote_version != payload["version"]:
            return self._pending(payload, operation_id,
                                 "Gateway installation state changed; inspect before retrying")
        if mode not in {"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"}:
            return self._pending(payload, operation_id, "Gateway result is unverifiable")
        self._assert_owner(owner)
        self._require_current(may_apply)
        self.store.record_install(self._install_record(payload, owner, mode=mode))
        result = {
            "updated": True, "skill_id": payload["skill_id"], "slug": payload["slug"],
            "from_version": (
                int(payload["from_version"]) if payload.get("from_version") is not None else None
            ),
            "version": int(payload["version"]),
            "path": str(target), "content_hash": payload["content_hash"],
            "preserved_copy": payload["preservation"].get("destination"),
            "recovery_path": str(recovery), "effective_update_mode": mode,
            "setup_execution_authorized": False, "ready_to_use": None,
        }
        return self._record_outcome(operation_id, payload, result)

    def _reject(self, operation_id: str, payload: dict, owner: dict) -> dict[str, Any]:
        target = Path(payload["target_path"])
        paths = self._paths(operation_id, payload)
        staging, recovery, quarantine = (
            paths["staging_path"], paths["recovery_path"], paths["quarantine_path"]
        )
        quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_hashes = _tree_hashes(target)
        if target_hashes == payload["local_snapshot"] and not recovery.exists():
            recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._assert_owner(owner)
            _durable_replace(target, recovery)
        elif target_hashes == payload["baseline"] and not quarantine.exists():
            self._assert_owner(owner)
            _durable_replace(target, quarantine)
        elif target_hashes not in (None, payload["local_snapshot"], payload["baseline"]):
            raise PackagePolicyError("update recovery found unrecognized managed bytes")
        if _tree_hashes(staging) == payload["baseline"] and not quarantine.exists():
            _durable_replace(staging, quarantine)
        elif staging.exists() and _tree_hashes(staging) != payload["baseline"]:
            shutil.rmtree(staging, ignore_errors=True)
        _require_tree(recovery, payload["local_snapshot"])
        if quarantine.exists():
            _require_tree(quarantine, payload["baseline"])
        self._preserve_copy(payload, recovery, operation_id, owner)
        if payload.get("local_baseline") == "unknown":
            self._restore_local_source(payload, owner, paths)
        self.store.deactivate_install(payload["skill_id"])
        result = {
            "updated": False, "rejected": True, "skill_id": payload["skill_id"],
            "slug": payload["slug"],
            "version": int(payload["version"]), "path": None,
            "preserved_copy": payload["preservation"].get("destination"),
            "recovery_path": str(recovery),
            "quarantine_path": str(quarantine) if quarantine.exists() else None,
            "setup_execution_authorized": False, "ready_to_use": None,
        }
        return self._record_outcome(operation_id, payload, result)

    def _restore_local_source(self, payload, owner, paths):
        """A rejected adoption must not remove the user's original local skill."""
        target = Path(payload["target_path"])
        if target.exists():
            _require_tree(target, payload["local_snapshot"])
            return
        staging, recovery = paths["staging_path"], paths["recovery_path"]
        self._assert_owner(owner)
        if not staging.exists():
            shutil.copytree(recovery, staging)
        _require_tree(staging, payload["local_snapshot"])
        self._assert_owner(owner)
        _durable_replace(staging, target)

    def _record_outcome(self, operation_id, payload, result):
        payload["result"] = result
        self.store.checkpoint(operation_id, payload, stage="outcome_recorded")
        return self._finish(operation_id, payload)

    def _finish(self, operation_id, payload):
        result = payload.get("result")
        if not isinstance(result, dict):
            raise PackagePolicyError("completed update outcome is missing")
        self.receipts.complete(payload["receipt"], result)
        self.store.finish(operation_id, result, stage="complete")
        return result

    def _preserve_copy(self, payload, recovery, operation_id, owner):
        preservation = payload["preservation"]
        if preservation.get("action") == "none":
            if preservation.get("destination") is not None:
                raise PackagePolicyError("unreviewed edited-copy destination")
            return
        if preservation.get("action") != "preserve_unmanaged_copy":
            raise PackagePolicyError("edited managed bytes cannot be replaced without preservation")
        destination = Path(str(preservation.get("destination") or ""))
        root = Path(self.host.skills_root).expanduser().absolute()
        if destination.parent != root or destination.name.startswith((".", "_")):
            raise PackagePolicyError("edited-copy preservation destination is invalid")
        _within(destination, root, "edited-copy preservation destination")
        if destination.exists():
            _require_tree(destination, payload["local_snapshot"])
            return
        pending = root / ".hub" / "collective-wisdom-preservation" / operation_id
        _within(pending, root / ".hub" / "collective-wisdom-preservation",
                "edited-copy staging destination")
        if pending.exists() and _tree_hashes(pending) != payload["local_snapshot"]:
            shutil.rmtree(pending, ignore_errors=True)
        if not pending.exists():
            pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(recovery, pending, copy_function=shutil.copy2)
            _require_tree(pending, payload["local_snapshot"])
            _fsync_tree(pending)
        _same_filesystem(pending, destination.parent)
        self._assert_owner(owner)
        _durable_replace(pending, destination)
        _require_tree(destination, payload["local_snapshot"])

    def _paths(self, operation_id: str, payload: dict) -> dict[str, Path]:
        if Path(operation_id).name != operation_id:
            raise PackagePolicyError("update operation identifier is invalid")
        root = Path(self.host.skills_root).expanduser().absolute()
        staging = root / ".hub" / "collective-wisdom" / operation_id
        archive = root / ".archive" / "collective-wisdom-updates" / operation_id
        validate_update_target(
            self.host, payload, payload["owner"], require_tree=False,
        )
        for path in (staging, archive):
            _reject_symlink_chain(root, path)
        _within(staging, root / ".hub" / "collective-wisdom", "update staging path")
        _within(archive, root / ".archive" / "collective-wisdom-updates", "update recovery path")
        return {
            "staging_path": staging,
            "recovery_path": archive / "previous-managed",
            "quarantine_path": archive / "gateway-rejected",
        }

    def _ready_operation(self, row, payload, receipt):
        """Validate derived private paths and bind review before any effect."""
        operation_id = str(row["id"])
        self._paths(operation_id, payload)
        self.receipts.start(receipt, operation_id)

    @staticmethod
    def _matches_plan(payload, plan):
        if any(payload.get(key) != value for key, value in plan.items()):
            raise WisdomConflict("update recovery no longer matches its reviewed receipt")

    @staticmethod
    def _validate_identity(payload, skill_id):
        provenance = payload.get("target_provenance")
        local_source = isinstance(provenance, dict) and provenance.get("kind") == "local_source"
        valid_from = (
            isinstance(payload.get("from_version"), int)
            and not isinstance(payload.get("from_version"), bool)
        ) or (local_source and payload.get("from_version") is None)
        if (payload.get("skill_id") != skill_id or not isinstance(payload.get("receipt"), str)
                or not isinstance(payload.get("version"), int)
                or isinstance(payload.get("version"), bool)
                or not valid_from
                or not isinstance(payload.get("content_hash"), str)):
            raise PackagePolicyError("update recovery identity is invalid")

    def _remote(self, skill_id, owner):
        self._assert_owner(owner)
        values = self.client.installations(owner["installation_id"])
        self._assert_owner(owner)
        if not isinstance(values, list):
            return None
        matches = [row for row in values
                   if isinstance(row, dict) and row.get("skill_id") == skill_id]
        return dict(matches[0]) if len(matches) == 1 else None

    def _owner(self):
        try:
            return _owner_snapshot(self.owner)
        except PackagePolicyError as exc:
            raise WisdomAuthError("Update owner is unavailable") from exc

    def _assert_owner(self, expected):
        if self._owner() != expected:
            raise WisdomAuthError("Update owner changed during operation")


    @staticmethod
    def _pending(payload, operation_id, message):
        return {
            "pending": True, "operation_id": operation_id, "receipt": payload["receipt"],
            "skill_id": payload["skill_id"], "phase": "local_committed",
            "retry": "apply_automatic"
            if payload.get("authorization", {}).get("kind") == "automatic_policy"
            else "update_apply", "files_installed": True, "local_state": "active",
            "error": message,
        }


def _terminal_rejection(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return (isinstance(exc, (WisdomAuthError, WisdomNotFound)) or code in _TERMINAL_CODES
            or (isinstance(exc, WisdomValidationError) and code in _TERMINAL_CODES))
