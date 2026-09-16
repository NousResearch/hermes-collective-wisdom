"""Small shared plumbing for isolated, immutable-host integration probes."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator, Sequence

import pytest


def _git(host_path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=host_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    return result.stdout.strip()


def _assert_clean(host_path: Path) -> None:
    assert _git(host_path, "status", "--porcelain") == "", (
        f"host checkout became modified: {host_path}"
    )


@contextmanager
def immutable_host_checkout(
    expected_revision: str, *, required_path: str | None = None
) -> Iterator[Path]:
    """Yield the optional pinned stock host and verify it stays unchanged."""

    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to the unmodified Agent checkout")
    host_path = Path(agent_dir).resolve()
    if required_path is not None:
        assert (host_path / required_path).is_file()
    assert _git(host_path, "rev-parse", "HEAD") == expected_revision
    _assert_clean(host_path)
    try:
        yield host_path
    finally:
        _assert_clean(host_path)


def isolated_host_environment(
    *, home: Path, hermes_home: Path, host_path: Path, python_paths: Sequence[Path]
) -> dict[str, str]:
    """Build the common allowlisted environment for a disposable host probe."""

    paths = [str(path) for path in python_paths]
    if dependencies := os.environ.get("WISDOM_HOST_DEPENDENCIES"):
        paths.append(dependencies)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "WISDOM_AGENT_DIR": str(host_path),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONNOUSERSITE": "1",
    }


def run_host_python(
    arguments: Sequence[str], *, cwd: Path, environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run the selected host interpreter and fail with its captured diagnostics."""

    result = subprocess.run(
        [os.environ.get("WISDOM_HOST_PYTHON", sys.executable), *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    return result


def write_no_auth_refresh_sitecustomize(parent: Path) -> Path:
    """Create a Python startup guard that rejects host credential refreshes."""

    guard = parent / "refresh-guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "from hermes_cli import auth\n"
        "def forbidden_refresh(*_args, **_kwargs):\n"
        "    raise AssertionError('Wisdom commands must not refresh credentials')\n"
        "auth.resolve_nous_runtime_credentials = forbidden_refresh\n",
        encoding="utf-8",
    )
    return guard
