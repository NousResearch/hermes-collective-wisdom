"""Undo for an uninstall: restore the archived managed copy and re-record it.

Uninstall moves the managed tree to
``<skills>/.archive/collective-wisdom/<operation>/<slug>`` (see
``InstallExecutor._archive_uninstall``) and keeps the inactive installation
record. Undo moves that exact archive back to the canonical target and records
the same version and update mode with the Gateway again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import WisdomConflict, WisdomNotFound
from .install_host import InstallationHost
from .managed_target import validate_managed_target
from .package import PackagePolicyError
from .package_application import (
    durable_replace, reject_symlink_chain, same_filesystem, tree_hashes, within,
)
from .setup_lock import managed_operation_guard


def _archive_path(root: Path, record: dict[str, Any]) -> Path:
    result = record.get("uninstall_result")
    path = Path(str((result or {}).get("recoverable_path") or ""))
    archive_root = root / ".archive" / "collective-wisdom"
    if not result or not path.name:
        raise WisdomNotFound("Nothing to undo for this skill")
    within(path, archive_root, "archive path")
    reject_symlink_chain(root, path.parent)
    if path.name != record.get("slug"):
        raise PackagePolicyError("archived copy does not match the removed skill")
    return path


def restore_uninstalled(service: Any, owner: dict[str, Any], skill_id: str) -> dict[str, Any]:
    """Move the archived copy back, mark it active and re-record it remotely."""

    store = service.installed
    host = InstallationHost(service.profile[1])
    root = Path(host.skills_root).expanduser().absolute()
    with managed_operation_guard(Path(store.root), skill_id):
        record = store.installation(skill_id, owner=owner)
        if not isinstance(record, dict):
            raise WisdomNotFound("Nothing to undo for this skill")
        if record.get("state") == "active":
            return {"restored": True, "skill_id": skill_id, "slug": record.get("slug"),
                    "already_active": True}
        if store.pending_operations(skill_id):
            raise WisdomConflict("Another operation is pending for this skill")
        archive = _archive_path(root, record)
        target = validate_managed_target(host, record, owner, require_tree=False)
        if target.exists() or target.is_symlink():
            raise WisdomConflict("A skill already exists where this one was installed")
        if tree_hashes(archive) is None:
            raise WisdomNotFound("The removed copy is no longer in the archive")
        host.validate_target(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        same_filesystem(archive, target.parent)
        durable_replace(archive, target)
        restored = {key: value for key, value in record.items() if key != "uninstall_result"}
        restored["state"] = "active"
        store.record_install(restored)
    detail = service.client.skill(skill_id)
    skill = detail.skill if isinstance(getattr(detail, "skill", None), dict) else {}
    generation = skill.get("takedown_generation")
    mode = record.get("update_mode") or "AUTO_WITH_NOTICE"
    remote_state = "pending"
    if skill.get("state") == "active" and isinstance(generation, int):
        try:
            server = service.client.record_install(
                skill_id=skill_id, installation_id=owner["installation_id"],
                version=int(record["version"]), takedown_generation=generation,
                update_mode=mode,
            )
            mode = server.effective_update_mode
            store.record_install({**restored, "update_mode": mode})
            remote_state = "recorded"
        except Exception:  # pylint: disable=broad-exception-caught
            remote_state = "pending"  # Files are back; the next update check reconciles.
    return {
        "restored": True, "skill_id": skill_id, "slug": record.get("slug"),
        "path": str(target), "version": record.get("version"),
        "effective_update_mode": mode, "gateway": remote_state,
    }


__all__ = ["restore_uninstalled"]
