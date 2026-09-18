"""Atomic, durable execution of reviewed manual Wisdom installations."""

# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-branches,too-many-statements

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .contract import org_directory_name
from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomNotFound,
    WisdomValidationError,
)
from .package import PackagePolicyError
from .package_application import (
    durable_replace as _durable_replace,
    install_package as _install_package,
    reject_existing_links as _reject_existing_links,
    reject_symlink_chain as _reject_symlink_chain,
    require_tree as _require_tree,
    same_filesystem as _same_filesystem,
    stage_package as _write_staging,
    tree_hashes as _tree_hashes,
    within as _within,
)
from .setup_lock import managed_operation_guard


def _require_current(may_apply: Callable[[], bool] | None) -> None:
    if may_apply is not None and may_apply() is not True:
        raise WisdomAuthError("Collective Wisdom review is no longer current")


def _operation_payload(operation: dict) -> dict:
    payload = operation.get("payload")
    if not isinstance(payload, dict):
        raise PackagePolicyError("operation recovery payload is invalid")
    return dict(payload)


def _install_record(
    payload: dict, owner: dict, state: str = "active", *, mode: str | None = None
) -> dict:
    return {
        "skill_id": payload["skill_id"], "org_id": owner["organization_id"],
        "slug": payload["slug"], "version": int(payload["version"]),
        "content_hash": payload["content_hash"], "baseline": dict(payload["baseline"]),
        "target_path": payload["target_path"],
        "update_mode": mode or payload.get("update_mode") or "MANUAL",
        "state": state, "owner": owner,
    }


class InstallExecutor:
    """Apply reviewed installs through one guarded synchronous operation."""

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

    _require_current = staticmethod(_require_current)
    _payload = staticmethod(_operation_payload)
    _install_record = staticmethod(_install_record)

    def apply(
        self,
        receipt: str,
        accept_partial: bool = False,
        *,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict:
        """Apply or reconcile one exact reviewed installation."""
        plan = self.receipts.load(receipt, allow_started=True)
        self._require_current(may_apply)
        skill_id = _required_text(plan, "skill_id")
        with managed_operation_guard(Path(self.store.root), skill_id):
            return self._apply(receipt, accept_partial, may_apply)

    def _apply(
        self,
        receipt: str,
        accept_partial: bool,
        may_apply: Callable[[], bool] | None,
    ) -> dict:
        owner = self._owner()
        self._require_current(may_apply)
        plan = self.receipts.load(receipt, allow_started=True)
        completed = self.receipts.outcome(receipt)
        if completed is not None:
            return completed
        skill_id = _required_text(plan, "skill_id")
        self._assert_owner(owner)
        pending = self.store.pending_operations(skill_id)
        if pending:
            return self._resume_pending_install(
                pending[0], receipt, owner, may_apply
            )
        return self._begin_install(plan, receipt, owner, accept_partial, may_apply)

    def _resume_pending_install(self, operation, receipt, owner, may_apply):
        payload = self._payload(operation)
        if operation.get("kind") == "wisdom_setup":
            raise PackagePolicyError("a setup operation is pending for this skill")
        if operation.get("kind") != "install":
            raise PackagePolicyError("another managed operation is pending for this skill")
        self._require_payload_owner(payload, owner)
        if payload.get("receipt") != receipt:
            raise PackagePolicyError(
                "another install receipt is already pending for this skill"
            )
        self.receipts.start(receipt, str(operation["id"]))
        return self._resume_install(operation, payload, may_apply, call_gateway=False)

    def _begin_install(self, plan, receipt, owner, accept_partial, may_apply):
        skill_id = _required_text(plan, "skill_id")
        existing = self.store.installation(skill_id)
        if existing:
            self._require_record_owner(existing, owner)
            if existing.get("state") == "active":
                raise PackagePolicyError("managed installation already exists")
            target = self._validated_record_target(existing, owner)
            if target.exists() or target.is_symlink():
                raise PackagePolicyError("inactive managed target still exists")
        _require_compatibility(plan, accept_partial)

        verified = self.reviewer.verify(plan)
        self._require_current(may_apply)
        self._assert_owner(owner)
        if verified.plan.get("receipt") != receipt or verified.plan.get("skill_id") != skill_id:
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
            "baseline": dict(verified.baseline),
            "accepted_partial": bool(accept_partial),
        }
        operation_id = self.store.begin("install", skill_id, payload, stage="prepared")
        self.receipts.start(receipt, operation_id)
        operation = self.store.operation(operation_id)
        if operation is None:
            raise PackagePolicyError("install operation is unavailable")
        staging = self._staging_path(operation_id)
        _write_staging(staging, verified.files, verified.baseline)
        return self._resume_install(
            operation, payload, may_apply, call_gateway=True, verified=verified
        )

    def uninstall(
        self,
        skill_id: str,
        *,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict:
        """Move a managed tree to recoverable storage and reconcile removal."""
        self._require_current(may_apply)
        with managed_operation_guard(Path(self.store.root), skill_id):
            return self._uninstall(skill_id, may_apply)

    def _uninstall(
        self,
        skill_id: str,
        may_apply: Callable[[], bool] | None,
    ) -> dict:
        owner = self._owner()
        self._require_current(may_apply)
        self._assert_owner(owner)
        pending = self.store.pending_operations(skill_id)
        if pending:
            operation = pending[0]
            if operation.get("kind") == "wisdom_setup":
                raise PackagePolicyError("a setup operation is pending for this skill")
            if operation.get("kind") != "uninstall":
                raise PackagePolicyError("another managed operation is pending for this skill")
            payload = self._payload(operation)
            self._require_payload_owner(payload, owner)
            return self._resume_uninstall(operation, payload, may_apply, call_gateway=False)
        installation = self.store.installation(skill_id)
        if not installation:
            raise PackagePolicyError("managed installation not found")
        self._require_record_owner(installation, owner)
        if installation.get("state") != "active":
            result = installation.get("uninstall_result")
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
            "baseline": dict(installation.get("baseline") or {}),
            "version": int(installation.get("version") or 0),
        }
        operation_id = self.store.begin("uninstall", skill_id, payload, stage="prepared")
        operation = self.store.operation(operation_id)
        if operation is None:
            raise PackagePolicyError("uninstall operation is unavailable")
        return self._resume_uninstall(operation, payload, may_apply, call_gateway=True)

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
            item["skill_id"]: item
            for item in pending
            if self._payload(item).get("owner") == owner
        }
        result = []
        for item in local:
            if item.get("owner") != owner:
                continue
            operation = pending_by_skill.pop(item["skill_id"], None)
            if item.get("state") == "active" or operation:
                result.append(self._installed_item(item, by_skill, operation))
        for skill_id, operation in pending_by_skill.items():
            payload = self._payload(operation)
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
                {"kind": operation["kind"], "phase": operation["stage"]}
                if operation
                else None
            ),
            "setup_execution_authorized": False,
            "ready_to_use": None,
        }

    def _resume_install(
        self, operation, payload, may_apply, *, call_gateway, verified=None
    ) -> dict:
        operation_id = str(operation["id"])
        owner = self._owner()
        self._require_current(may_apply)
        self._require_payload_owner(payload, owner)
        stage = str(operation["stage"])
        staging = self._staging_path(operation_id)
        target = Path(payload["target_path"])
        self._validate_operation_paths(
            staging, target, owner, operation_id, payload["slug"]
        )
        if stage == "outcome_recorded":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise PackagePolicyError("install recovery outcome is missing")
            self._require_current(may_apply)
            self.receipts.complete(payload["receipt"], result)
            self.store.finish(operation_id, result, stage="complete")
            return result
        if stage != "prepared":
            raise PackagePolicyError(f"unsupported install recovery stage: {stage}")

        def authorize(scan_path: Path | None = None):
            current = verified or self.reviewer.verify(
                self.receipts.load(payload["receipt"], allow_started=True)
            )
            _require_verified_match(payload, current)
            _require_compatibility(
                current.plan, bool(payload.get("accepted_partial")), changed=True
            )
            if scan_path is not None:
                _require_scan(
                    self.host.scan(scan_path), current.plan.get("local_scan"),
                    _accepted_scan_warning(current.plan),
                )
            self._assert_owner(owner)
            self._require_current(may_apply)
            return current

        if _tree_hashes(target) != payload["baseline"]:
            verified = authorize()
            if not staging.exists():
                _write_staging(staging, verified.files, payload["baseline"])
            _install_package(staging, target, payload["baseline"], authorize)
        self._assert_owner(owner)
        self._require_current(may_apply)
        self.store.record_install(self._install_record(payload, owner))

        if not call_gateway:
            known, remote = self._inspect_remote(payload["skill_id"], owner)
            if not known:
                return self._pending_result(payload, operation_id, RuntimeError())
            if remote is not None:
                mode = remote.get("update_mode")
                if remote.get("installed_version") == payload["version"] and mode in {
                    "MANUAL", "AUTO_WITH_NOTICE", "REQUIRED",
                }:
                    return self._complete_install(
                        operation_id, payload, owner, target, mode, may_apply
                    )
                return self._pending_result(payload, operation_id, RuntimeError())
            authorize(target)

        self._assert_owner(owner)
        self._require_current(may_apply)
        try:
            server = self.client.record_install(
                skill_id=payload["skill_id"], installation_id=owner["installation_id"],
                version=int(payload["version"]),
                takedown_generation=int(payload["takedown_generation"]),
                update_mode=payload.get("update_mode"),
            )
        except WisdomAuthError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if _permanent_gateway_error(exc):
                self._quarantine_rejected(operation_id, payload, target, owner)
                raise
            return self._pending_result(payload, operation_id, exc)
        return self._complete_install(
            operation_id, payload, owner, target,
            _field(server, "effective_update_mode"), may_apply,
        )

    def _complete_install(self, operation_id, payload, owner, target, mode, may_apply):
        result = self._success_result(payload, target, mode)
        payload["result"] = result
        self.store.record_install({
            **self._install_record(payload, owner, "active"), "update_mode": mode,
        })
        self.store.checkpoint(operation_id, payload=payload, stage="outcome_recorded")
        self._require_current(may_apply)
        self.receipts.complete(payload["receipt"], result)
        self.store.finish(operation_id, result, stage="complete")
        return result

    def _resume_uninstall(
        self, operation, payload, may_apply, *, call_gateway
    ) -> dict:
        operation_id = str(operation["id"])
        owner = self._owner()
        self._require_current(may_apply)
        self._require_payload_owner(payload, owner)
        stage = str(operation["stage"])
        target = Path(payload["target_path"])
        trash = self._trash_path(operation_id, payload["slug"])
        self._validate_uninstall_paths(target, trash, owner, operation_id, payload["slug"])
        if stage != "prepared":
            return self._finish_uninstall_recovery(
                stage, operation_id, payload, may_apply
            )

        self._archive_uninstall(payload, owner, target, trash, may_apply)

        if not call_gateway:
            known, remote = self._inspect_remote(payload["skill_id"], owner)
            if not known or (
                remote is not None and remote.get("installed_version") != payload["version"]
            ):
                return self._pending_result(
                    payload, operation_id, RuntimeError(), uninstall=True
                )
            if remote is None:
                return self._complete_uninstall(
                    operation_id, payload, trash, "inactive", may_apply
                )
        self._assert_owner(owner)
        self._require_current(may_apply)
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
        return self._complete_uninstall(
            operation_id, payload, trash, _field(server, "state"), may_apply
        )

    def _archive_uninstall(self, payload, owner, target, trash, may_apply) -> None:
        trash.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and not trash.exists():
            _same_filesystem(target, trash.parent)
            self._assert_owner(owner)
            self._require_current(may_apply)
            _durable_replace(target, trash)
        elif not trash.exists():
            raise PackagePolicyError("managed uninstall bytes are unavailable")
        self._assert_owner(owner)
        self._require_current(may_apply)
        self.store.deactivate_install(payload["skill_id"])

    def _finish_uninstall_recovery(self, stage, operation_id, payload, may_apply):
        if stage != "outcome_recorded":
            raise PackagePolicyError(f"unsupported uninstall recovery stage: {stage}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise PackagePolicyError("uninstall recovery outcome is missing")
        self._require_current(may_apply)
        self.store.finish(operation_id, result, stage="complete")
        return result

    def _inspect_remote(self, skill_id, owner) -> tuple[bool, dict | None]:
        try:
            return True, self._remote_installation(skill_id, owner)
        except WisdomAuthError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if _permanent_gateway_error(exc):
                raise
            return False, None

    def _complete_uninstall(self, operation_id, payload, trash, state, may_apply):
        result = {
            "uninstalled": True,
            "skill_id": payload["skill_id"],
            "state": state,
            "recoverable_path": str(trash),
        }
        payload["result"] = result
        self.store.deactivate_install(payload["skill_id"], result)
        self.store.checkpoint(operation_id, payload=payload, stage="outcome_recorded")
        self._require_current(may_apply)
        self.store.finish(operation_id, result, stage="complete")
        return result

    def _quarantine_rejected(
        self,
        operation_id: str,
        payload: dict,
        target: Path,
        owner: dict,
    ) -> None:
        trash = self._trash_path(operation_id, payload["slug"])
        trash.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() and not trash.exists():
            _same_filesystem(target, trash.parent)
            self._assert_owner(owner)
            _durable_replace(target, trash)
        self.store.deactivate_install(payload["skill_id"])
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
        self.store.checkpoint(operation_id, payload=payload, stage="outcome_recorded")
        self.receipts.complete(payload["receipt"], result)
        self.store.finish(operation_id, result, stage="complete")


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

    def _remote_installation(self, skill_id: str, owner: dict) -> dict | None:
        self._assert_owner(owner)
        rows = self.client.installations(owner["installation_id"])
        self._assert_owner(owner)
        if not isinstance(rows, list):
            raise RuntimeError("Gateway installation listing is invalid")
        matches = [
            row for row in rows
            if isinstance(row, dict) and row.get("skill_id") == skill_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError("Gateway installation listing is ambiguous")
        return dict(matches[0])


    def _owner(self) -> dict:
        return _owner_snapshot(self.owner)

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


    def _validate_uninstall_paths(
        self, target: Path, trash: Path, owner: dict, operation_id: str, slug: str
    ) -> None:
        expected_target = self._target(owner, _safe_segment(slug, "skill slug"))
        if target != expected_target or trash != self._trash_path(operation_id, slug):
            raise PackagePolicyError("uninstall recovery path is invalid")
        self.host.validate_target(target)


def _required_text(values: dict, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise PackagePolicyError(f"install plan has no valid {key}")
    return value


def _owner_snapshot(provider: Callable[[], dict]) -> dict[str, str]:
    value = provider()
    required = (
        "profile_name", "profile_home", "account_id", "organization_id",
        "base_url", "installation_id",
    )
    if not isinstance(value, dict) or set(value) != set(required) or any(
        not value.get(key) for key in required
    ):
        raise PackagePolicyError("current installation owner is unavailable")
    return {key: str(value[key]) for key in required}


def _safe_segment(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise PackagePolicyError(f"{label} is invalid")
    return value


def _accepted_scan_warning(plan: object, *, operation: str = "install") -> bool:
    if not isinstance(plan, dict):
        return False
    policy = plan.get("policy")
    acceptance = policy.get("warning_acceptance") if isinstance(policy, dict) else None
    return bool(
        isinstance(acceptance, dict)
        and acceptance.get("accepted") is True
        and acceptance.get("operation") == operation
        and acceptance.get("content_hash") == plan.get("content_hash")
    )


def _require_scan(current: object, reviewed: object, accepted_warning: bool) -> None:
    if not isinstance(current, dict) or not isinstance(reviewed, dict):
        raise PackagePolicyError("host scan changed after review; create a new review")
    allowed = current.get("allowed")
    if allowed is not True and allowed is not False and allowed is not None:
        raise PackagePolicyError("host scan blocked installation")
    reviewed_allowed = reviewed.get("allowed")
    if (
        reviewed_allowed is not True
        and reviewed_allowed is not False
        and reviewed_allowed is not None
    ):
        raise PackagePolicyError("host scan changed after review; create a new review")
    available = current.get("available", allowed is not None)
    reviewed_available = reviewed.get("available", reviewed_allowed is not None)
    if (
        not isinstance(available, bool)
        or not isinstance(reviewed_available, bool)
        or not available
        or not reviewed_available
    ):
        raise PackagePolicyError("host scan is unavailable; retry review")
    if current != reviewed:
        raise PackagePolicyError("host scan changed after review; create a new review")
    if allowed is True:
        return
    if not accepted_warning:
        raise PackagePolicyError(
            "reviewed scan warning requires explicit warning acknowledgement"
        )


def _require_verified_match(payload: dict, verified: object) -> None:
    plan = verified.plan
    immutable = (
        "receipt",
        "skill_id",
        "slug",
        "version",
        "content_hash",
        "takedown_generation",
        "update_mode",
        "local_scan",
        "security_check",
        "policy",
    )
    if any(plan.get(key) != payload.get(key) for key in immutable):
        raise WisdomValidationError("install authorization changed; create a new review")
    if dict(verified.baseline) != payload.get("baseline"):
        raise WisdomValidationError("reviewed install bytes changed; create a new review")


def _compatibility_outcome(plan: dict) -> str | None:
    compatibility = plan.get("compatibility")
    return compatibility.get("outcome") if isinstance(compatibility, dict) else None


def _require_compatibility(plan: dict, accept_partial: bool, *, changed: bool = False) -> None:
    outcome = _compatibility_outcome(plan)
    if outcome == "blocked_pending_action":
        message = "reviewed compatibility no longer authorizes activation" if changed else (
            "blocked compatibility requirements prevent activation"
        )
        raise PackagePolicyError(message)
    if outcome in {"partial", "compatible_after_setup"} and not accept_partial:
        message = "reviewed compatibility no longer authorizes activation" if changed else (
            "explicit --accept-partial is required"
        )
        raise PackagePolicyError(message)


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
