"""Pure content-addressed object builders for Collective Wisdom packages.

Extracted from Hermes Agent's Skill Sync wire model. This module intentionally
contains no HTTP client, authentication, organization, merge, or sync routines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import stat as _stat
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("tools.skills_sync_client")
DEFAULT_MAX_OBJECT_BYTES = 26214400  # 25 MiB, mirrors capabilities default
KIND_BLOB, KIND_TREE, KIND_COMMIT = "blob", "tree", "commit"
MODE_FILE, MODE_EXEC, MODE_DIR = "file", "exec", "dir"
ARTIFACT_TYPE_SKILL = "skill"
_EXEC_BITS = _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH


# Content addressing uses the full 64-hex SHA-256, not a truncated local hash.
def wire_address(data: bytes) -> str:
    """Return the full wire-format SHA-256 address for exact bytes."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: Dict[str, Any]) -> bytes:
    """Canonical JSON for hashing: sorted compact UTF-8 with no newline."""

    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class ObjectSet:
    """Objects to push, ``hash -> (kind, bytes)``, deduped by address."""

    def __init__(self) -> None:
        self.objects: Dict[str, Tuple[str, bytes]] = {}

    def add(self, kind: str, data: bytes) -> str:
        """Add exact object bytes once and return their content address."""

        addr = wire_address(data)
        self.objects.setdefault(addr, (kind, data))
        return addr

    def __len__(self) -> int:
        return len(self.objects)


def _entry(name: str, kind: str, hash_: str, mode: str) -> Dict[str, str]:
    return {"name": name, "kind": kind, "hash": hash_, "mode": mode}


def _add_tree(entries: List[Dict[str, str]], objects: ObjectSet) -> str:
    """Canonicalize entries in UTF-8 byte order into a tree object."""

    entries.sort(key=lambda e: e["name"].encode("utf-8"))
    return objects.add(
        KIND_TREE, canonical_json_bytes({"type": KIND_TREE, "entries": entries})
    )


def _file_mode(path: Path) -> str:
    """Return ``exec`` for executable files and ``file`` otherwise."""

    with suppress(OSError):
        if path.stat().st_mode & _EXEC_BITS:
            return MODE_EXEC
    return MODE_FILE


def build_tree(dir_path: Path, objects: ObjectSet, *, max_object_bytes: int) -> str:
    """Recursively build a bounded tree, skipping links and special files."""

    entries: List[Dict[str, str]] = []
    for child in sorted(dir_path.iterdir(), key=lambda p: p.name):
        if child.is_symlink():
            logger.debug("skills_sync_client: skipping symlink %s", child)
        elif child.is_dir():
            entries.append(
                _entry(
                    child.name,
                    KIND_TREE,
                    build_tree(child, objects, max_object_bytes=max_object_bytes),
                    MODE_DIR,
                )
            )
        elif child.is_file():
            data = child.read_bytes()
            if len(data) > max_object_bytes:
                raise ValueError(
                    f"file {child} is {len(data)} bytes > "
                    f"max_object_bytes {max_object_bytes}"
                )
            entries.append(
                _entry(
                    child.name,
                    KIND_BLOB,
                    objects.add(KIND_BLOB, data),
                    _file_mode(child),
                )
            )
    return _add_tree(entries, objects)


def build_tree_from_snapshot(
    files: List[Tuple[str, str, bytes]], objects: ObjectSet, *, max_object_bytes: int
) -> str:
    """Build the wire tree from one already-validated immutable byte snapshot."""

    root: Dict[str, Any] = {}
    for relative, mode, body in files:
        parts = PurePosixPath(relative).parts
        if not parts or relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"invalid snapshot path: {relative}")
        if mode not in {MODE_FILE, MODE_EXEC}:
            raise ValueError(f"invalid snapshot mode: {mode}")
        if len(body) > max_object_bytes:
            raise ValueError(
                f"file {relative} is {len(body)} bytes > max_object_bytes {max_object_bytes}"
            )
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"snapshot path conflicts with file: {relative}")
            node = child
        if parts[-1] in node:
            raise ValueError(f"duplicate snapshot path: {relative}")
        node[parts[-1]] = (mode, body)

    def add_directory(node: Dict[str, Any]) -> str:
        entries: List[Dict[str, str]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(_entry(name, KIND_TREE, add_directory(value), MODE_DIR))
            else:
                mode, body = value
                entries.append(_entry(name, KIND_BLOB, objects.add(KIND_BLOB, body), mode))
        return _add_tree(entries, objects)

    return add_directory(root)


def build_commit(  # pylint: disable=too-many-arguments
    tree_hash: str,
    parents: List[str],
    *,
    owner: str,
    device: str,
    message: str,
    objects: ObjectSet,
    ts: Optional[str] = None,
) -> str:
    """Build a canonical commit object and return its content address."""

    return objects.add(
        KIND_COMMIT,
        canonical_json_bytes(
            {
                "type": KIND_COMMIT,
                "tree": tree_hash,
                "parents": list(parents),
                "author": {"owner": owner, "device": device},
                "ts": ts
                or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "message": message,
                "artifact_type": ARTIFACT_TYPE_SKILL,
            }
        ),
    )
