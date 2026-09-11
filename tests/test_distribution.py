"""Distribution-content smoke tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_built_distributions_contain_native_and_python_surfaces(tmp_path: Path) -> None:
    """The sdist supports native installs and the wheel contains the package."""
    source_copy = tmp_path / "source"
    shutil.copytree(
        PROJECT_ROOT,
        source_copy,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", "*.egg-info", "build", "dist"
        ),
    )
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
        cwd=source_copy,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    sdist = next(dist_dir.glob("*.tar.gz"))
    wheel = next(dist_dir.glob("*.whl"))

    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_paths = {Path(name).as_posix() for name in archive.getnames()}
    sdist_root = "hermes_collective_wisdom-0.1.0.dev0"
    assert f"{sdist_root}/plugin.yaml" in sdist_paths
    assert f"{sdist_root}/__init__.py" in sdist_paths
    assert f"{sdist_root}/hermes_wisdom_plugin/__init__.py" in sdist_paths
    assert f"{sdist_root}/docs/FEATURES.md" in sdist_paths

    with zipfile.ZipFile(wheel) as archive:
        wheel_paths = set(archive.namelist())
    assert "hermes_wisdom_plugin/__init__.py" in wheel_paths
    assert not any(".data/data/__init__.py" in path for path in wheel_paths)
