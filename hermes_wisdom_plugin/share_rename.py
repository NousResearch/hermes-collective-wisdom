"""Rename a local skill so it can be shared under a name no teammate owns.

Used by the share name-collision flow ("A teammate already shares a skill
called X. Rename yours to share it."). Renames the skill directory in place and
rewrites the SKILL.md frontmatter ``name:`` line; nothing else changes.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .package import PackagePolicyError
from .package_application import durable_replace

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")


def valid_skill_name(value: str) -> bool:
    """Lowercase letters, digits and dashes; 3-64 characters (a publishable slug)."""
    return bool(_NAME.fullmatch(value or ""))


def _rewrite_frontmatter_name(skill_md: Path, new_name: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    end = text.find("\n---", 3)
    if end < 0:
        return
    head, rest = text[:end], text[end:]
    updated, count = re.subn(r"(?m)^name:\s*.*$", f"name: {new_name}", head, count=1)
    if not count:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".rename-", dir=skill_md.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated + rest)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, skill_md.stat().st_mode & 0o777)
        os.replace(temporary, skill_md)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def rename_local_skill(source: Path, new_name: str) -> Path:
    """Rename ``source`` to a sibling directory ``new_name``; return the new path."""

    if not valid_skill_name(new_name):
        raise PackagePolicyError(
            "Use 3-64 lowercase letters, digits or dashes for the new skill name"
        )
    source = Path(source)
    if source.is_symlink() or not (source / "SKILL.md").is_file():
        raise PackagePolicyError("local skill is unavailable")
    destination = source.parent / new_name
    if destination.exists() or destination.is_symlink():
        raise PackagePolicyError(f"You already have a skill called {new_name}")
    durable_replace(source, destination)
    _rewrite_frontmatter_name(destination / "SKILL.md", new_name)
    return destination


__all__ = ["rename_local_skill", "valid_skill_name"]
