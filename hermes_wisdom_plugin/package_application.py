"""Small direct filesystem kernel shared by install and update."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Iterable

from .errors import WisdomValidationError
from .package import PackagePolicyError


def tree_hashes(root: Path) -> dict[str, str] | None:
    """Return the exact regular-file tree, including executable intent."""
    if root.is_symlink() or _is_junction(root) or not root.is_dir():
        return None
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or _is_junction(path) or not (
            path.is_file() or path.is_dir()
        ):
            return None
        if path.is_file():
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            actual[path.relative_to(root).as_posix()] = (
                f"exec:{digest}" if path.stat().st_mode & 0o111 else digest
            )
    return actual


def require_tree(root: Path, expected: dict[str, str]) -> None:
    """Reject a tree that does not contain the exact expected files."""
    if tree_hashes(root) != expected:
        raise WisdomValidationError("managed package bytes do not match the recovery journal")


def durable_replace(source: Path, destination: Path) -> None:
    """Rename and durably record both affected directories."""
    source_parent = source.parent
    os.replace(source, destination)
    fsync_directory(destination.parent)
    if source_parent != destination.parent:
        fsync_directory(source_parent)


def stage_package(
    staging: Path,
    files: Iterable[tuple[str, str, bytes]],
    expected: dict[str, str],
) -> None:
    """Materialize exact reviewed bytes, replacing only incomplete staging."""
    if tree_hashes(staging) == expected:
        return
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, mode=0o700)
    try:
        for raw_path, mode, body in files:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise PackagePolicyError("verified package contains an unsafe path")
            if mode not in {"file", "exec"} or not isinstance(body, bytes):
                raise PackagePolicyError("verified package contains an unsupported entry")
            destination = staging.joinpath(*path.parts)
            within(destination, staging, "package file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with destination.open("xb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o700 if mode == "exec" else 0o600)
                    os.fsync(stream.fileno())
            except FileExistsError as exc:
                # Only a case-folding filesystem maps two distinct verified
                # paths onto one file.
                raise PackagePolicyError(
                    f"package path {raw_path!r} is refused: it collides with another "
                    "package file on this filesystem, which folds letter case"
                ) from exc
        require_tree(staging, expected)
        fsync_tree(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_package(
    staging: Path,
    target: Path,
    incoming: dict[str, str],
    check_staged: Callable[[Path], None],
) -> None:
    """Promote one exact staged tree or accept its interrupted rename."""
    if staging.is_dir():
        require_tree(staging, incoming)
        same_filesystem(staging, target.parent)
        if target.exists() or target.is_symlink():
            raise PackagePolicyError("managed target collision during install")
        check_staged(staging)
        durable_replace(staging, target)
    elif tree_hashes(target) != incoming:
        raise PackagePolicyError("staged install bytes are unavailable")
    require_tree(target, incoming)


def replace_package(
    paths: tuple[Path, Path, Path],
    trees: tuple[dict[str, str], dict[str, str]],
    check_staged: Callable[[Path], None],
    before_preserve: Callable[[], None] = lambda: None,
) -> None:
    """Preserve the old tree and promote exact staged bytes, restart safely."""
    staging, target, recovery = paths
    previous, incoming = trees
    target_state, recovery_state = tree_hashes(target), tree_hashes(recovery)
    if target_state not in (previous, incoming, None) or recovery_state not in (
        previous,
        None,
    ):
        raise PackagePolicyError("update recovery found unrecognized managed bytes")
    if recovery_state is None:
        if target_state != previous:
            raise PackagePolicyError("reviewed managed bytes are unavailable")
        require_tree(staging, incoming)
        check_staged(staging)
        recovery.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        same_filesystem(target, recovery.parent)
        before_preserve()
        durable_replace(target, recovery)
        target_state = None
    if target_state is None:
        require_tree(staging, incoming)
        same_filesystem(staging, target.parent)
        check_staged(staging)
        durable_replace(staging, target)
    require_tree(target, incoming)
    require_tree(recovery, previous)


def fsync_directory(path: Path) -> None:
    """Persist directory metadata where the platform supports it."""
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    """Persist all regular files and directories below a staged tree."""
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
        fsync_directory(directory)


def same_filesystem(source: Path, destination_parent: Path) -> None:
    """Require an atomic rename boundary between source and destination."""
    destination_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.stat().st_dev != destination_parent.stat().st_dev:
        raise PackagePolicyError("atomic rename requires staging and target on one filesystem")


def within(path: Path, root: Path, label: str) -> None:
    """Require a lexical path to remain below its designated root."""
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PackagePolicyError(f"{label} escaped its root") from exc


def reject_symlink_chain(root: Path, path: Path) -> None:
    """Reject symlinks or junctions between root and path."""
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink() or _is_junction(current):
            raise PackagePolicyError("managed install path contains a symlink")


def reject_existing_links(path: Path) -> None:
    """Reject symlinks or junctions in the existing absolute path."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or _is_junction(current):
            raise PackagePolicyError("managed install path contains a symlink")


def _is_junction(path: Path) -> bool:
    return bool(getattr(os.path, "isjunction", lambda _path: False)(path))
