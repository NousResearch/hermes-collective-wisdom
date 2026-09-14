"""Nonexpiring per-skill starter lock shared by managed mutations."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .package import PackagePolicyError


@contextmanager
def managed_operation_guard(root: Path, skill_id: str):
    """Hold a no-follow OS descriptor lock through approval/spawn or mutation."""

    if not isinstance(root, Path) or not isinstance(skill_id, str) or not skill_id:
        raise TypeError("invalid managed operation lock identity")
    try:
        import fcntl  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - current receipts are POSIX-only
        raise PackagePolicyError(
            "managed setup execution is unavailable on this platform"
        ) from exc
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = directory_fd = lock_fd = None
    try:
        root_fd = os.open(root, directory_flags)
        try:
            os.mkdir("operation-locks", mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open("operation-locks", directory_flags, dir_fd=root_fd)
        name = hashlib.sha256(skill_id.encode("utf-8")).hexdigest() + ".lock"
        lock_fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
            raise OSError("managed operation lock is not a private regular file")
    except OSError as exc:
        for descriptor in (lock_fd, directory_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise PackagePolicyError(
            "managed operation lock storage is unavailable or unsafe"
        ) from exc
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PackagePolicyError(
                "another managed operation is starting or running for this skill"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
        os.close(directory_fd)
        os.close(root_fd)
