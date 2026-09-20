"""Semantic tests for the shared direct filesystem application kernel."""

from pathlib import Path

import pytest

from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.package_application import (
    durable_replace,
    install_package,
    replace_package,
    tree_hashes,
)


def test_replace_resumes_after_old_rename_and_rechecks_staged_tree(tmp_path: Path) -> None:
    """Interrupted replacement rescans the real staged directory before promotion."""
    target = tmp_path / "managed"
    staging = tmp_path / "staged"
    recovery = tmp_path / "recovery"
    target.mkdir()
    staging.mkdir()
    target.joinpath("SKILL.md").write_bytes(b"old")
    staging.joinpath("SKILL.md").write_bytes(b"new")
    old = tree_hashes(target)
    new = tree_hashes(staging)
    assert old is not None and new is not None
    recovery.parent.mkdir(parents=True, exist_ok=True)
    durable_replace(target, recovery)
    checks: list[Path] = []

    def check(path: Path) -> None:
        checks.append(path)

    replace_package((staging, target, recovery), (old, new), check)

    assert checks == [staging]
    assert tree_hashes(target) == new
    assert tree_hashes(recovery) == old


def test_install_collision_preserves_existing_and_staged_trees(tmp_path: Path) -> None:
    """I07/X07: a target collision must not overwrite or discard either tree."""
    target = tmp_path / "managed"
    staging = tmp_path / "staged"
    target.mkdir()
    staging.mkdir()
    target.joinpath("SKILL.md").write_bytes(b"existing")
    staging.joinpath("SKILL.md").write_bytes(b"reviewed")
    existing = tree_hashes(target)
    incoming = tree_hashes(staging)
    assert existing is not None and incoming is not None

    with pytest.raises(PackagePolicyError, match="target collision"):
        install_package(staging, target, incoming, lambda _path: None)

    assert tree_hashes(target) == existing
    assert tree_hashes(staging) == incoming
