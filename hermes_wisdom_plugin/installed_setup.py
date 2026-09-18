"""Read-only inspection of exact, owner-bound managed installation bytes."""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from .compatibility import evaluate
from .contract import parse_manifest_bytes
from .package import (
    MAX_METADATA_BYTES,
    MAX_TREE_BYTES,
    PackagePolicyError,
    _validate_package_path,
    verify_content_files,
)
from .setup_document import SETUP_PATH, parse_setup_document


def _installed_files(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    installation: dict, host: Any
) -> dict[str, bytes]:
    target = Path(installation["target_path"])
    host.validate_target(target)
    for candidate in (target, target.parent, target.parent.parent):
        try:
            if candidate.is_symlink():
                raise PackagePolicyError("managed setup target contains a symbolic link")
        except OSError as exc:
            raise PackagePolicyError("managed setup target is unavailable") from exc
    if not target.is_dir():
        raise PackagePolicyError("managed setup target is missing")
    baseline = installation.get("baseline")
    if not isinstance(baseline, dict):
        raise PackagePolicyError("managed setup package baseline is invalid")
    directories: set[str] = set()
    for name, digest in baseline.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise PackagePolicyError("managed setup baseline is invalid")
        _validate_package_path(name, source="installed")
        directories.update(
            str(parent) for parent in PurePosixPath(name).parents if str(parent) != "."
        )
    files: list[tuple[str, str, bytes]] = []
    total = 0
    metadata_total = 0
    pending = [target]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError as exc:
            raise PackagePolicyError("managed setup package is unavailable") from exc
        with entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(target).as_posix()
                metadata_total += len(name.encode("utf-8")) + 128
                if metadata_total > MAX_METADATA_BYTES:
                    raise PackagePolicyError("managed setup package exceeds metadata limits")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PackagePolicyError(
                        "managed setup package changed during inspection"
                    ) from exc
                if stat.S_ISDIR(metadata.st_mode) and (
                    name in directories or name == PurePosixPath(SETUP_PATH).parent.as_posix()
                ):
                    pending.append(path)
                    continue
                if (
                    name not in baseline
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise PackagePolicyError(
                        "managed setup package contains an unexpected or unsafe entry"
                    )
                remaining = MAX_TREE_BYTES - total
                if metadata.st_size > remaining:
                    raise PackagePolicyError("managed setup package exceeds size limits")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                try:
                    descriptor = os.open(path, flags)
                    with os.fdopen(descriptor, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (
                            not os.path.samestat(metadata, opened)
                            or not stat.S_ISREG(opened.st_mode)
                            or opened.st_nlink != 1
                        ):
                            raise PackagePolicyError(
                                "managed setup package changed during inspection"
                            )
                        body = stream.read(remaining + 1)
                except OSError as exc:
                    raise PackagePolicyError(
                        "managed setup package changed during inspection"
                    ) from exc
                total += len(body)
                if total > MAX_TREE_BYTES:
                    raise PackagePolicyError("managed setup package exceeds size limits")
                mode = (
                    "exec" if metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    else "file"
                )
                files.append((name, mode, body))
    records, content_hash = verify_content_files(files)
    if (
        content_hash != installation.get("content_hash")
        or {
            record.path: (
                f"exec:{record.hash}" if record.mode == "exec" else record.hash
            )
            for record in records
        } != baseline
    ):
        raise PackagePolicyError(
            "installed package changed; review its current bytes before setup"
        )
    return {name: body for name, _mode, body in files}


def _require_committed(
    installed: Any, owner: dict, skill_id: str, version: int | None
) -> dict:
    installation = installed.installation(skill_id, owner=owner)
    if not installation or installation.get("state") != "active":
        raise PackagePolicyError("no active managed installation for this enrolment")
    if installation.get("owner") != owner:
        raise PackagePolicyError("managed installation belongs to another enrolment")
    if version is not None and version != installation.get("version"):
        raise PackagePolicyError(
            "installed version changed; inspect the current version before setup"
        )
    for item in installed.pending_operations(skill_id):
        if item["kind"] in {"install", "update", "uninstall"}:
            raise PackagePolicyError(
                "installation has an unfinished operation; recover it before setup"
            )
    return installation


def inspect_installed_setup(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments
    installed: Any,
    progress: Any,
    host: Any,
    owner: dict,
    skill_id: str,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    """Inspect installed bytes and prerequisite presence without execution."""

    installation = _require_committed(installed, owner, skill_id, version)
    try:
        files = _installed_files(installation, host)
        manifest = parse_manifest_bytes(files["skill.manifest.json"])
    except (KeyError, OSError, ValueError) as exc:
        raise PackagePolicyError(
            "installed setup package is unavailable or invalid"
        ) from exc
    if manifest.name != installation["slug"]:
        raise PackagePolicyError("installed manifest does not match the managed skill")
    compatibility = asdict(
        evaluate(manifest.requirements, host.capabilities(manifest.requirements))
    )
    result: dict[str, Any] = {
        "kind": "installed_setup",
        "skill_id": skill_id,
        "slug": installation["slug"],
        "version": installation["version"],
        "content_hash": installation["content_hash"],
        "target_path": installation["target_path"],
        "files_installed": True,
        "execution_authorized": False,
        "ready_to_use": None,
        "system_specification": manifest.requirements.model_dump(mode="json"),
        "compatibility": compatibility,
        "guidance": None,
        "prerequisites": [],
        "setup_progress": [],
        "verification": {"state": "not_recorded"},
        "state": "guidance_unavailable",
        "message": "Files installed. Setup and verification have not been confirmed.",
    }
    if SETUP_PATH in files:
        try:
            document = parse_setup_document(files[SETUP_PATH])
        except (UnicodeError, ValueError):
            result["guidance_error"] = (
                "The installed setup document is invalid or unsupported."
            )
        else:
            result["guidance"] = document.model_dump(mode="json")
            for item in document.requirements:
                if item.kind == "command":
                    status = "present" if shutil.which(item.name) else "missing"
                elif item.kind == "env_var":
                    status = "present" if host.environment_present(item.name) else "missing"
                else:
                    status = "acknowledgement_required"
                result["prerequisites"].append(
                    {**item.model_dump(mode="json"), "status": status}
                )
            needs_setup = bool(
                compatibility["setup_actions"]
                or compatibility["blocked"]
                or document.setup_instructions
                or any(row["status"] != "present" for row in result["prerequisites"])
            )
            result["state"] = "setup_required" if needs_setup else "verification_required"
    result["setup_progress"] = setup_progress(progress, owner, result)
    _derive_readiness(result)
    if _require_committed(installed, owner, skill_id, version) != installation:
        raise PackagePolicyError(
            "installation changed during setup inspection; inspect again"
        )
    if len(json.dumps(result).encode("utf-8")) > 64_000:
        raise PackagePolicyError("setup guidance exceeds the complete inspection limit")
    return result


def _derive_readiness(result: dict[str, Any]) -> None:
    progress = result["setup_progress"]
    if not progress:
        return
    result["message"] = "Files installed. Review recorded setup progress before continuing."
    latest = {(row["phase"], row["index"]): row for row in progress}
    verification = [row for row in progress if row["phase"] == "verify"]
    if verification:
        result["verification"] = verification[-1]
    if any(row["state"] in {"starting", "running", "unknown"} for row in progress):
        result["state"] = "setup_in_progress"
        return
    guidance = result.get("guidance")
    if not guidance or not verification:
        return
    prerequisites_ready = all(
        item["status"] == "present"
        or (item["status"] == "acknowledgement_required"
            and latest.get(("prerequisite", index), {}).get("state") == "passed")
        for index, item in enumerate(result["prerequisites"])
    )
    setup_ready = all(
        latest.get(("setup", index), {}).get("state") == "passed"
        for index in range(len(guidance["setup_instructions"]))
    )
    verify_ready = verification[-1]["state"] == "passed" and verification[-1] == progress[-1]
    if (
        prerequisites_ready
        and setup_ready
        and verify_ready
        and result["compatibility"]["outcome"] == "compatible"
    ):
        result["ready_to_use"] = True
        result["state"] = "verified"
        result["message"] = (
            "Files installed. Declared prerequisites and the approved verification "
            "command passed."
        )


def setup_progress(store: Any, owner: dict, inspection: dict) -> list[dict]:
    """Project owner/content-matched durable progress without launching commands."""

    direct = getattr(store, "setup_progress", None)
    if not callable(direct):
        raise TypeError("installed setup requires direct setup state")
    return direct(owner, inspection)
