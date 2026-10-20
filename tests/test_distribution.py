"""Distribution-content smoke tests."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONTRACT_DIGESTS = {
    "canonical-hash-vectors.v1.json": (
        "eff9b596d1bd9c46eda287b7231b11c35e5c0ae3b193e645d5b0a421b7570974"
    ),
    "skill-manifest.schema.v1.json": (
        "64d0010eada1d79fa16309e9fd715faf77b6186360ea0b095182b2bdaeec5714"
    ),
}


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
        roots = {Path(name).parts[0] for name in sdist_paths}
        assert len(roots) == 1, "Source distribution must have one package root"
        sdist_root = roots.pop()
        assert f"{sdist_root}/plugin.yaml" in sdist_paths
        assert f"{sdist_root}/dashboard/manifest.json" in sdist_paths
        assert f"{sdist_root}/dashboard/plugin_api.py" in sdist_paths
        assert f"{sdist_root}/desktop/plugin.js" in sdist_paths
        assert f"{sdist_root}/__init__.py" in sdist_paths
        assert f"{sdist_root}/after-install.md" in sdist_paths
        for asset in (
            "pyproject.toml", ".pre-commit-config.yaml", "scripts/check_complexity.py",
            "scripts/complexity-baseline.json", "scripts/install_complexity_hook.py",
            "scripts/run_receipt_mutations.py",
        ):
            assert f"{sdist_root}/{asset}" in sdist_paths
        assert f"{sdist_root}/hermes_wisdom_plugin/__init__.py" in sdist_paths

        for filename, expected_digest in RUNTIME_CONTRACT_DIGESTS.items():
            path = f"{sdist_root}/hermes_wisdom_plugin/contracts/{filename}"
            assert path in sdist_paths
            member = archive.extractfile(path)
            assert member is not None
            assert hashlib.sha256(member.read()).hexdigest() == expected_digest

    with zipfile.ZipFile(wheel) as archive:
        wheel_paths = set(archive.namelist())
        assert "hermes_wisdom_plugin/__init__.py" in wheel_paths
        assert not any(".data/data/__init__.py" in path for path in wheel_paths)
        for filename, expected_digest in RUNTIME_CONTRACT_DIGESTS.items():
            path = f"hermes_wisdom_plugin/contracts/{filename}"
            assert path in wheel_paths
            assert hashlib.sha256(archive.read(path)).hexdigest() == expected_digest
