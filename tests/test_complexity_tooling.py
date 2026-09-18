"""Regression tests for the complexity ratchet and its local Git hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

CHECKER = Path(__file__).parents[1] / "scripts" / "check_complexity.py"
INSTALLER = Path(__file__).parents[1] / "scripts" / "install_complexity_hook.py"
PRE_COMMIT_CONFIG = Path(__file__).parents[1] / ".pre-commit-config.yaml"


def _function(branches: int) -> str:
    conditions = "\n".join(
        f"    if value == {index}:\n        return {index}" for index in range(branches)
    )
    return f"def legacy(value):\n{conditions}\n    return -1\n"


def _baseline(
    path: Path,
    complexity: int,
    identity: str = "runtime.py::legacy",
) -> Path:
    baseline = path / "complexity-baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema": 1,
                "engine": {"name": "mccabe", "version": "0.7.0"},
                "default_max": 10,
                "functions": {identity: complexity},
            }
        ),
        encoding="utf-8",
    )
    return baseline


def _run_checker(repo: Path, baseline: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(repo),
            "--baseline",
            str(baseline),
            "--source",
            "runtime.py",
            *extra,
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def test_unchanged_legacy_hotspot_passes_but_regression_fails(tmp_path: Path) -> None:
    """Preserve legacy debt while rejecting a higher measured value."""
    source = tmp_path / "runtime.py"
    source.write_text(
        _function(10) + "\n\ndef simple():\n    return 1\n", encoding="utf-8"
    )
    baseline = _baseline(tmp_path, 11)

    unchanged = _run_checker(tmp_path, baseline)
    assert unchanged.returncode == 0, unchanged.stderr
    assert "2 functions checked" in unchanged.stdout
    assert "new function" not in unchanged.stdout

    source.write_text(_function(11), encoding="utf-8")
    regressed = _run_checker(tmp_path, baseline)
    assert regressed.returncode == 1
    assert "runtime.py::legacy complexity 12 exceeds allowed 11" in regressed.stderr


def test_new_hotspot_and_syntax_error_fail_closed(tmp_path: Path) -> None:
    """Reject a new hotspot and source that McCabe cannot parse."""
    source = tmp_path / "runtime.py"
    source.write_text(_function(10), encoding="utf-8")
    baseline = _baseline(tmp_path, 11)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n"
        + _function(11).replace("legacy", "added"),
        encoding="utf-8",
    )

    added = _run_checker(tmp_path, baseline)
    assert added.returncode == 1
    assert "runtime.py::added complexity 12 exceeds allowed 10 (new function)" in added.stderr

    source.write_text("def broken(:\n", encoding="utf-8")
    broken = _run_checker(tmp_path, baseline)
    assert broken.returncode != 0
    assert "syntax error" in broken.stderr


def test_duplicate_function_identity_fails_instead_of_overwriting_result(tmp_path: Path) -> None:
    """Reject identities that McCabe would otherwise overwrite silently."""
    source = tmp_path / "runtime.py"
    source.write_text("def repeated():\n    pass\n\ndef repeated():\n    pass\n", encoding="utf-8")
    baseline = _baseline(tmp_path, 1, "runtime.py::repeated")

    result = _run_checker(tmp_path, baseline)

    assert result.returncode != 0
    assert "duplicate function identity: runtime.py::repeated" in result.stderr


def test_print_baseline_emits_reviewable_json_without_writing(tmp_path: Path) -> None:
    """Generate a candidate baseline only on stdout."""
    source = tmp_path / "runtime.py"
    source.write_text(
        _function(10) + "\n\ndef simple():\n    return 1\n", encoding="utf-8"
    )
    missing = tmp_path / "does-not-exist.json"

    result = _run_checker(tmp_path, missing, "--print-baseline")

    assert result.returncode == 0, result.stderr
    generated = json.loads(result.stdout)
    assert generated["engine"] == {"name": "mccabe", "version": "0.7.0"}
    assert generated["functions"] == {"runtime.py::legacy": 11}
    assert not missing.exists()


def test_invalid_caps_and_exception_values_fail_closed(tmp_path: Path) -> None:
    """Reject booleans, nonpositive caps, and non-exception baseline values."""
    (tmp_path / "runtime.py").write_text(_function(10), encoding="utf-8")
    baseline = _baseline(tmp_path, 11)

    for default_max, recorded, expected in (
        (True, 11, "default_max must be a positive integer"),
        (0, 11, "default_max must be a positive integer"),
        (10, True, "complexities must be integers above default_max"),
        (10, 10, "complexities must be integers above default_max"),
    ):
        baseline.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "engine": {"name": "mccabe", "version": "0.7.0"},
                    "default_max": default_max,
                    "functions": {"runtime.py::legacy": recorded},
                }
            ),
            encoding="utf-8",
        )
        result = _run_checker(tmp_path, baseline)
        assert result.returncode != 0
        assert expected in result.stderr


def test_staged_mode_reads_index_not_worktree(tmp_path: Path) -> None:
    """Use index blobs despite stale or partially staged working files."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    _git(tmp_path, "config", "user.name", "Fixture")
    source = tmp_path / "runtime.py"
    source.write_text(_function(10), encoding="utf-8")
    baseline = _baseline(tmp_path, 11)
    _git(tmp_path, "add", "runtime.py", baseline.name)
    _git(tmp_path, "commit", "-qm", "base")

    source.write_text(_function(11), encoding="utf-8")
    _git(tmp_path, "add", "runtime.py")
    source.write_text(_function(10), encoding="utf-8")
    staged_regression = _run_checker(tmp_path, baseline, "--staged")
    assert staged_regression.returncode == 1
    assert "complexity 12 exceeds allowed 11" in staged_regression.stderr

    _git(tmp_path, "add", "runtime.py")
    source.write_text(_function(11), encoding="utf-8")
    staged_clean = _run_checker(tmp_path, baseline, "--staged")
    assert staged_clean.returncode == 0, staged_clean.stderr


def test_staged_rename_and_path_with_spaces_are_analyzed(tmp_path: Path) -> None:
    """Analyze renamed staged paths without splitting spaces."""
    _git(tmp_path, "init", "-q")
    source = tmp_path / "runtime.py"
    source.write_text(_function(10), encoding="utf-8")
    baseline = _baseline(tmp_path, 11)
    _git(tmp_path, "add", "runtime.py", baseline.name)

    spaced = tmp_path / "path with spaces.py"
    source.rename(spaced)
    _git(tmp_path, "add", "-A")
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--source",
            "path with spaces.py",
            "--staged",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (
        "path with spaces.py::legacy complexity 11 exceeds allowed 10 (new function)"
        in result.stderr
    )
    assert "removed baseline exception: runtime.py::legacy" in result.stdout


def test_staged_source_only_deletion_passes_with_exception_diagnostic(
    tmp_path: Path,
) -> None:
    """Allow deleting source while reporting the removed legacy exception."""
    _git(tmp_path, "init", "-q")
    source = tmp_path / "runtime.py"
    source.write_text(_function(10), encoding="utf-8")
    baseline = _baseline(tmp_path, 11)
    _git(tmp_path, "add", "runtime.py", baseline.name)
    source.unlink()
    _git(tmp_path, "add", "-u")

    result = _run_checker(tmp_path, baseline, "--staged")

    assert result.returncode == 0, result.stderr
    assert "removed baseline exception: runtime.py::legacy" in result.stdout
    assert "new function" not in result.stdout


def _copy_hook_fixture(repo: Path) -> None:
    (repo / "scripts").mkdir()
    shutil.copy2(CHECKER, repo / "scripts" / CHECKER.name)
    shutil.copy2(INSTALLER, repo / "scripts" / INSTALLER.name)
    shutil.copy2(PRE_COMMIT_CONFIG, repo / PRE_COMMIT_CONFIG.name)
    (repo / "hermes_wisdom_plugin").mkdir()
    source = repo / "hermes_wisdom_plugin" / "runtime.py"
    source.write_text(_function(10), encoding="utf-8")
    _baseline(repo / "scripts", 11, "hermes_wisdom_plugin/runtime.py::legacy")


def _run_installer(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / INSTALLER.name), *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "dangling-symlink"])
def test_installer_refuses_nonregular_hook_without_touching_it(
    tmp_path: Path, kind: str,
) -> None:
    """Reject special paths before reading, following, or replacing a hook."""
    _git(tmp_path, "init", "-q")
    _copy_hook_fixture(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    target = tmp_path / "external-hook"
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixture requires mkfifo")
        os.mkfifo(hook)
    elif kind == "directory":
        hook.mkdir()
    else:
        if kind == "symlink":
            target.write_text("# File generated by pre-commit\n# sentinel\n", encoding="utf-8")
        hook.symlink_to(target)
    before = hook.lstat()
    target_before = target.read_bytes() if target.exists() else None

    refused = _run_installer(tmp_path, "--replace-existing")

    assert refused.returncode == 2
    assert "refusing non-regular pre-commit hook" in refused.stderr
    after = hook.lstat()
    assert (after.st_ino, after.st_mode, after.st_ctime_ns) == (
        before.st_ino, before.st_mode, before.st_ctime_ns,
    )
    assert (target.read_bytes() if target.exists() else None) == target_before
    assert not (hook.parent / "pre-commit.pre-wisdom-backup").exists()


def test_installer_refuses_configured_hooks_path(tmp_path: Path) -> None:
    """Reject hooksPath before inspecting or transiently renaming its hook."""
    _git(tmp_path, "init", "-q")
    _copy_hook_fixture(tmp_path)
    _git(tmp_path, "config", "core.hooksPath", "custom-hooks")
    hooks = tmp_path / "custom-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    before = hook.stat()

    refused = _run_installer(tmp_path, "--replace-existing")

    assert refused.returncode != 0
    assert "core.hooksPath is configured; refusing before hook inspection" in refused.stderr
    after = hook.stat()
    assert (after.st_ino, after.st_ctime_ns) == (before.st_ino, before.st_ctime_ns)
    assert hook.read_text(encoding="utf-8").endswith("exit 23\n")
    assert not (hooks / "pre-commit.pre-wisdom-backup").exists()


def test_installer_refuses_conflict_unless_explicitly_replaced(tmp_path: Path) -> None:
    """Preserve conflicting hooks unless replacement is explicit."""
    _git(tmp_path, "init", "-q")
    _copy_hook_fixture(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")

    refused = _run_installer(tmp_path)
    assert refused.returncode != 0
    assert "existing pre-commit hook" in refused.stderr
    assert "exit 19" in hook.read_text(encoding="utf-8")

    replaced = _run_installer(tmp_path, "--replace-existing")
    assert replaced.returncode == 0, replaced.stderr
    assert "File generated by pre-commit" in hook.read_text(encoding="utf-8")
    backup = hook.parent / "pre-commit.pre-wisdom-backup"
    assert backup.read_text(encoding="utf-8").endswith("exit 19\n")


def test_installed_hook_isolated_environment_and_deletion_only_gate(
    tmp_path: Path,
) -> None:
    """Use the hook environment and run even for a baseline-only deletion."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    _git(tmp_path, "config", "user.name", "Fixture")
    _copy_hook_fixture(tmp_path)
    installed = _run_installer(tmp_path)
    assert installed.returncode == 0, installed.stderr

    commit_env = os.environ.copy()
    commit_env["PATH"] = "/usr/bin:/bin"
    commit_env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-cache")
    _git(tmp_path, "add", ".")
    accepted = subprocess.run(
        ["git", "commit", "-qm", "legacy baseline"],
        cwd=tmp_path,
        env=commit_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr

    baseline = tmp_path / "scripts" / "complexity-baseline.json"
    baseline.unlink()
    _git(tmp_path, "add", "-u")
    rejected = subprocess.run(
        ["git", "commit", "-qm", "delete baseline"],
        cwd=tmp_path,
        env=commit_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    combined = rejected.stdout + rejected.stderr
    assert "no files to check" not in combined
    assert "complexity-baseline.json" in combined


def test_installed_hook_rejects_staged_regression_not_unstaged_copy(
    tmp_path: Path,
) -> None:
    """Exercise staged regression rejection without reading unstaged source."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    _git(tmp_path, "config", "user.name", "Fixture")
    _copy_hook_fixture(tmp_path)
    installed = _run_installer(tmp_path)
    assert installed.returncode == 0, installed.stderr
    hook_env = os.environ.copy()
    hook_env["PATH"] = "/usr/bin:/bin"
    hook_env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-cache")
    _git(tmp_path, "add", ".")
    baseline_commit = subprocess.run(
        ["git", "commit", "-qm", "legacy baseline"],
        cwd=tmp_path,
        env=hook_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert baseline_commit.returncode == 0, baseline_commit.stderr

    runtime = tmp_path / "hermes_wisdom_plugin" / "runtime.py"
    runtime.write_text(_function(11), encoding="utf-8")
    _git(tmp_path, "add", "hermes_wisdom_plugin/runtime.py")
    runtime.write_text(_function(10), encoding="utf-8")
    rejected = subprocess.run(
        ["git", "commit", "-qm", "regression"],
        cwd=tmp_path,
        env=hook_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "complexity 12 exceeds allowed 11" in rejected.stdout + rejected.stderr

    _git(tmp_path, "restore", "--staged", "hermes_wisdom_plugin/runtime.py")
    runtime.unlink()
    _git(tmp_path, "add", "-u")
    deleted = subprocess.run(
        [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            "--hook-stage",
            "pre-commit",
            "--verbose",
        ],
        cwd=tmp_path,
        env=hook_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert "removed baseline exception: hermes_wisdom_plugin/runtime.py::legacy" in (
        deleted.stdout + deleted.stderr
    )
