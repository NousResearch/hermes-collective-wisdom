#!/usr/bin/env python3
"""Offline-only Collective Wisdom source, contract, and artifact smoke harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Sequence


class AcceptanceError(RuntimeError):
    """An offline acceptance prerequisite or check failed closed."""


MAX_COMMAND_OUTPUT = 8_000


def _bounded_output(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= MAX_COMMAND_OUTPUT:
        return stripped
    half = MAX_COMMAND_OUTPUT // 2
    omitted = len(stripped) - (half * 2)
    return f"{stripped[:half]}\n... {omitted} characters omitted ...\n{stripped[-half:]}"


def run_command(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    timeout: int = 180,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one argv-only offline check and return inspectable evidence."""
    command = [os.fspath(item) for item in argv]
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if env is not None:
        kwargs["env"] = env
    try:
        completed = subprocess.run(command, **kwargs)  # pylint: disable=subprocess-run-check
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes) else exc.stdout or ""
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes) else exc.stderr or ""
        )
        raise AcceptanceError(
            f"offline command timed out after {timeout} seconds: {command!r}\n"
            f"stdout: {_bounded_output(stdout)}\nstderr: {_bounded_output(stderr)}"
        ) from exc
    result = {
        "argv": command,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout": _bounded_output(completed.stdout),
        "stderr": _bounded_output(completed.stderr),
    }
    if completed.returncode != 0:
        raise AcceptanceError(
            f"offline command failed ({completed.returncode}): {command!r}\n"
            f"stdout: {result['stdout']}\nstderr: {result['stderr']}"
        )
    return result


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and completed.returncode != 0:
        raise AcceptanceError(
            f"git {' '.join(arguments)} failed for {repo}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def verify_checkout(
    repo: Path,
    expected_revision: str,
    *,
    require_clean: bool,
    require_exact_head: bool = True,
) -> dict[str, Any]:
    """Inspect a local Git source without conflating a pin with worktree bytes."""
    if not repo.is_dir():
        raise AcceptanceError(f"checkout is missing: {repo}")
    head = _git(repo, "rev-parse", "HEAD")
    object_type = _git(repo, "cat-file", "-t", expected_revision)
    if object_type != "commit":
        raise AcceptanceError(f"pinned object is not a commit in {repo}: {expected_revision}")
    if require_exact_head and head != expected_revision:
        raise AcceptanceError(
            f"HEAD drift in {repo}: expected {expected_revision}, got {head}"
        )
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if require_clean and status:
        raise AcceptanceError(f"checkout has uncommitted content: {repo}")
    working_tree = {
        "path": str(repo),
        "head": head,
        "head_exact": head == expected_revision,
        "dirty": bool(status),
        "clean": not bool(status),
    }
    return {
        "immutable_revision": {
            "commit": expected_revision,
            "available": True,
        },
        "working_tree": working_tree,
        # Flat aliases retain compatibility with earlier evidence readers.
        "path": str(repo),
        "head": head,
        "required_revision": expected_revision,
        "required_revision_available": True,
        "head_exact": head == expected_revision,
        "clean": not bool(status),
    }


def _git_blob(repo: Path, revision: str, relative: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{relative}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AcceptanceError(
            f"required immutable Git file is unavailable: {revision}:{relative} "
            f"in {repo}: {os.fsdecode(completed.stderr).strip()}"
        )
    return completed.stdout


def verify_clean_paths(repo: Path, paths: tuple[str, ...]) -> list[str]:
    """Require selected contract/runtime paths to match the checked-out commit."""
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *paths,
    )
    if status:
        raise AcceptanceError(
            f"pinned contract paths have uncommitted content in {repo}: {status}"
        )
    return list(paths)


def validated_revision(value: str, *, label: str) -> str:
    """Require an explicit lowercase, full-length Git commit identifier."""
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise AcceptanceError(f"{label} revision must be a 40-character lowercase commit id")
    return value


def _git_names(repo: Path, *arguments: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", *arguments],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AcceptanceError(
            f"git ls-files {' '.join(arguments)} failed for {repo}: "
            f"{os.fsdecode(completed.stderr).strip()}"
        )
    return [os.fsdecode(item) for item in completed.stdout.split(b"\0") if item]


def _digest_entries(entries: Sequence[tuple[str, bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative_text, kind, body in entries:
        relative = os.fsencode(relative_text)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(kind)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _excluded_non_source(relative: str) -> bool:
    parts = Path(relative).parts
    return any(
        part in {".venv", ".pytest_cache", "__pycache__", "build", "dist"}
        or part.endswith(".egg-info")
        or part.endswith(".pyc")
        for part in parts
    )


def _capture_source(  # pylint: disable=too-many-locals
    repo: Path,
) -> tuple[list[tuple[str, bytes, bytes]], list[str], list[str]]:
    tracked = set(_git_names(repo, "--cached"))
    selected = tracked | set(_git_names(repo, "--others", "--exclude-standard"))
    excluded = sorted(name for name in selected if _excluded_non_source(name))
    names = sorted(selected - set(excluded))
    entries: list[tuple[str, bytes, bytes]] = []
    deletions: list[str] = []
    for relative in names:
        path = repo / relative
        try:
            before = path.lstat()
        except FileNotFoundError as exc:
            if relative in tracked:
                deletions.append(relative)
                continue
            raise AcceptanceError(f"untracked source disappeared during snapshot: {path}") from exc
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(path)
            body = os.fsencode(target)
            kind = b"symlink\0"
        elif stat.S_ISREG(before.st_mode):
            body = path.read_bytes()
            kind = b"file\0"
        else:
            raise AcceptanceError(f"unsupported source entry in snapshot: {path}")
        try:
            after = path.lstat()
        except FileNotFoundError as exc:
            raise AcceptanceError(f"source changed during snapshot: {path}") from exc
        stable = (
            before.st_mode,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_mode,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable:
            raise AcceptanceError(f"source changed during snapshot: {path}")
        entries.append((Path(relative).as_posix(), kind, body))
    return entries, deletions, excluded


def _filesystem_entries(root: Path) -> list[tuple[str, bytes, bytes]]:
    entries: list[tuple[str, bytes, bytes]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        symlink_dirs = [name for name in dirnames if (directory_path / name).is_symlink()]
        dirnames[:] = [name for name in dirnames if name not in symlink_dirs]
        for name in sorted(filenames + symlink_dirs):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, b"symlink\0", os.fsencode(os.readlink(path))))
            elif path.is_file():
                entries.append((relative, b"file\0", path.read_bytes()))
            else:
                raise AcceptanceError(f"unsupported snapshot entry: {path}")
    return sorted(entries, key=lambda item: item[0])


def snapshot_sha256(root: Path) -> str:
    """Hash the exact files and symlink targets present in a source snapshot."""
    return _digest_entries(_filesystem_entries(root))


def create_source_snapshot(repo: Path, destination: Path) -> dict[str, Any]:
    """Capture Git-selected source bytes once, then build only that immutable snapshot."""
    entries, deletions, excluded = _capture_source(repo)
    destination.mkdir(parents=True, exist_ok=False)
    for relative, kind, body in entries:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == b"symlink\0":
            link_target = os.fsdecode(body)
            if Path(link_target).is_absolute():
                raise AcceptanceError(
                    f"source symlink escapes snapshot: {relative} -> {link_target}"
                )
            target.symlink_to(link_target)
        else:
            target.write_bytes(body)
    snapshot_root = destination.resolve()
    for path in destination.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=False).is_relative_to(snapshot_root):
            raise AcceptanceError(
                "source symlink escapes snapshot: "
                f"{path.relative_to(destination)} -> {os.readlink(path)}"
            )
    digest = _digest_entries(entries)
    if snapshot_sha256(destination) != digest:
        raise AcceptanceError("written source snapshot differs from captured bytes")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "path": str(repo),
        "head": _git(repo, "rev-parse", "HEAD"),
        "dirty": bool(status),
        "snapshot_sha256": digest,
        "snapshot_file_count": len(entries),
        "tracked_deletions": deletions,
        "excluded_non_source_paths": excluded,
    }


def portal_contract_command(portal_dir: Path, pnpm: Path) -> tuple[list[str], Path]:
    """Return the existing Portal offline contract check, never a build or service command."""
    return [str(pnpm), "wisdom:contract:check"], portal_dir / "apps" / "nous-account-service"


def new_report() -> dict[str, Any]:
    """Create a report whose claim boundary cannot imply release/live acceptance."""
    return {
        "scope": "offline_preflight_and_artifact_smoke",
        "four_repo_acceptance": False,
        "live_acceptance": False,
        "status": "running",
        "checks": [],
        "artifacts": [],
        "remaining_blockers": [
            "Gateway's four-repository journey requires disposable Postgres/S3 services; "
            "this lane starts no Docker or services.",
            "Live CLI/TUI/Slack/Telegram acceptance and deployed Portal-login acceptance "
            "were not authorized or attempted.",
        ],
    }


def _required_path(path: Path, *, executable: bool = False) -> Path:
    if executable:
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AcceptanceError(f"required executable is unavailable: {path}")
    elif not path.exists():
        raise AcceptanceError(f"required path is unavailable: {path}")
    return path


def configured_executable(path: Path) -> Path:
    """Validate an executable while preserving venv/shim symlink semantics."""
    absolute = path.absolute()
    return _required_path(absolute, executable=True)


def create_output_directory(path: Path) -> None:
    """Create a new evidence directory without overwriting or mixing prior output."""
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise AcceptanceError(f"acceptance output directory already exists: {path}") from exc


def _sources_values(plugin_dir: Path) -> dict[str, str]:
    text = (plugin_dir / "docs" / "SOURCES.md").read_text(encoding="utf-8")
    labels = {
        "agent": "bf51fee548ceaa28d2c299a41c0f65066c04c061",
        "gateway_feature": "1a1d0d7a71edb617d6d4aae453a2609080561874",
        "portal_feature": "dc1cdb3449e89331244cc9cc132f842da44ab353",
        "gateway_contract": "16bd05e81b134d1ab8bb230aade6f6de9010cae3",
        "openapi_sha256": (
            "b46f0db55befbb682a392fc0c1015ed0a9a81e02d5ad9337c29347802972937d"
        ),
        "manifest_schema_sha256": (
            "64d0010eada1d79fa16309e9fd715faf77b6186360ea0b095182b2bdaeec5714"
        ),
        "canonical_vectors_sha256": (
            "eff9b596d1bd9c46eda287b7231b11c35e5c0ae3b193e645d5b0a421b7570974"
        ),
    }
    missing = [value for value in labels.values() if value not in text]
    if missing:
        raise AcceptanceError(f"docs/SOURCES.md is missing pinned values: {missing}")
    return labels


def _contract_values(plugin_dir: Path) -> dict[str, str]:
    # A fresh interpreter reads the snapshot, never an already-imported checkout.
    code = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from hermes_wisdom_plugin.contract import CONTRACT_PIN as pin
print(json.dumps({
    'gateway_contract': pin.gateway_commit,
    'openapi_sha256': pin.openapi_sha256,
    'manifest_schema_sha256': pin.manifest_schema_sha256,
    'canonical_vectors_sha256': pin.canonical_vectors_sha256,
}))
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(plugin_dir)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcceptanceError("snapshot contract import timed out") from exc
    if completed.returncode:
        raise AcceptanceError(f"snapshot contract import failed: {completed.stderr}")
    return json.loads(completed.stdout)


def _verify_contract_bytes(plugin_dir: Path, expected: dict[str, str]) -> dict[str, str]:
    contract_dir = plugin_dir / "hermes_wisdom_plugin" / "contracts"
    paths = {
        "manifest_schema_sha256": contract_dir / "skill-manifest.schema.v1.json",
        "canonical_vectors_sha256": contract_dir / "canonical-hash-vectors.v1.json",
    }
    actual = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }
    for name, digest in actual.items():
        if digest != expected[name]:
            raise AcceptanceError(
                f"runtime contract drift for {name}: expected {expected[name]}, got {digest}"
            )
    return actual


def _verify_portal_pin(
    portal_dir: Path, portal_revision: str, expected: dict[str, str]
) -> dict[str, str]:
    relative = (
        "apps/nous-account-service/src/server/sync/generated/"
        "wisdom-plane-contract-pin.json"
    )
    pin = json.loads(_git_blob(portal_dir, portal_revision, relative))
    comparisons = {
        "gatewayCommit": expected["gateway_contract"],
        "openapiSha256": expected["openapi_sha256"],
        "manifestSchemaSha256": expected["manifest_schema_sha256"],
        "canonicalVectorsSha256": expected["canonical_vectors_sha256"],
    }
    for key, value in comparisons.items():
        if pin.get(key) != value:
            raise AcceptanceError(
                f"Portal contract pin drift for {key}: expected {value}, got {pin.get(key)}"
            )
    return {
        "source_revision": portal_revision,
        "source_path": relative,
        **{key: str(pin[key]) for key in comparisons},
    }


def build_command(plugin_python: Path, artifacts_dir: Path) -> list[str]:
    """Build in an ephemeral isolated environment, leaving the project venv unchanged."""
    return [str(plugin_python), "-m", "build", "--outdir", str(artifacts_dir)]


def gateway_driver_bootstrap_commands(
    agent_python: Path,
    agent_dir: Path,
    gateway_dir: Path,
    artifact_root: Path,
) -> tuple[list[list[str]], dict[str, str]]:
    """Return isolated offline driver probes pinned to one extracted artifact."""

    driver_root = gateway_dir / "test" / "e2e"
    commands = [
        [str(agent_python), "-I", "-B", str(driver_root / name), "--offline-bootstrap"]
        for name in (
            "wisdom_delivery_driver.py",
            "wisdom_outbox_driver.py",
            "wisdom_publication_driver.py",
        )
    ]
    environment = dict(os.environ)
    environment.update(
        {
            "HERMES_AGENT_DIR": str(agent_dir.resolve()),
            "HERMES_WISDOM_ARTIFACT_ROOT": str(artifact_root.resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return commands, environment


def verify_native_artifact_layout(extracted_root: Path) -> dict[str, str]:
    """Require the native directory-loader files from the extracted sdist."""
    required = {
        "native_loader": extracted_root / "__init__.py",
        "manifest": extracted_root / "plugin.yaml",
        "package": extracted_root / "hermes_wisdom_plugin",
        "desktop": extracted_root / "desktop" / "plugin.js",
    }
    for label, path in required.items():
        if label == "package":
            valid = path.is_dir() and (path / "__init__.py").is_file()
        else:
            valid = path.is_file()
        if not valid:
            raise AcceptanceError(
                f"extracted native artifact is missing {path.relative_to(extracted_root)} ({label})"
            )
    return {
        "native_loader": "__init__.py",
        "manifest": "plugin.yaml",
        "package": "hermes_wisdom_plugin",
        "desktop": "desktop/plugin.js",
    }


def _host_artifact_discovery_command(
    agent_python: Path, agent_dir: Path, extracted_root: Path
) -> list[str]:
    """Load the extracted external artifact through the real host plugin manager."""
    code = """
import json
import os
import pathlib
import shutil
import sys
import tempfile
assert sys.flags.isolated == 1
agent = pathlib.Path(sys.argv[1]).resolve()
artifact = pathlib.Path(sys.argv[2]).resolve()
sys.path.insert(0, str(agent))
with tempfile.TemporaryDirectory(prefix='wisdom-host-discovery-') as temporary:
    home = pathlib.Path(temporary)
    installed = home / 'plugins' / 'hermes-collective-wisdom'
    shutil.copytree(artifact, installed, symlinks=True)
    (home / 'config.yaml').write_text(
        'plugins:\\n  enabled:\\n    - hermes-collective-wisdom\\n', encoding='utf-8')
    os.environ['HERMES_HOME'] = str(home)
    from hermes_cli.plugins import PluginContext, PluginManager
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        matches = [row for row in manager.list_plugins()
                   if row['name'] == 'hermes-collective-wisdom']
        assert len(matches) == 1, matches
        row = matches[0]
        assert row['enabled'] is True and row['error'] is None, row
        loaded = next(item for item in manager._plugins.values()
                      if item.manifest.name == 'hermes-collective-wisdom')
        module_file = pathlib.Path(loaded.module.__file__).resolve()
        assert module_file.is_relative_to(installed.resolve())
        assert 'hermes_wisdom' not in sys.modules
        print(json.dumps({
            'artifact_import': 'passed',
            'host_discovery': 'passed',
            'module_file': str(module_file),
            'plugin': row,
            'host_presentation_api': hasattr(PluginContext, 'register_presentation_handler'),
        }))
    finally:
        manager.unload()
""".strip()
    return [
        str(agent_python), "-I", "-B", "-c", code,
        str(agent_dir), str(extracted_root),
    ]


def verify_artifact_package_parity(  # pylint: disable=too-many-locals
    source_root: Path, sdist: Path, wheel: Path
) -> dict[str, Any]:
    """Prove package paths and bytes match across the snapshot, sdist, and wheel."""
    prefix = "hermes_wisdom_plugin/"
    source_entries = _filesystem_entries(source_root / "hermes_wisdom_plugin")
    if any(kind != b"file\0" for _, kind, _ in source_entries):
        raise AcceptanceError("package source contains symlinks unsupported by artifact parity")
    source_files = {relative: body for relative, _, body in source_entries}
    with tarfile.open(sdist, "r:gz") as archive:
        package_members = [
            member for member in archive.getmembers()
            if member.isfile() and f"/{prefix}" in member.name
        ]
        sdist_files = {}
        for member in package_members:
            relative = member.name.split(f"/{prefix}", 1)[1]
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AcceptanceError(f"cannot read sdist package member: {member.name}")
            sdist_files[relative] = extracted.read()
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = {
            name[len(prefix):]: archive.read(name)
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        }
    if source_files != sdist_files or source_files != wheel_files:
        sdist_only = sorted(sdist_files.keys() - source_files.keys())
        wheel_only = sorted(wheel_files.keys() - source_files.keys())
        source_only = sorted(
            source_files.keys() - sdist_files.keys() | source_files.keys() - wheel_files.keys()
        )
        differing = sorted(
            relative for relative, body in source_files.items()
            if sdist_files.get(relative) not in (None, body)
            or wheel_files.get(relative) not in (None, body)
        )
        raise AcceptanceError(
            "package paths or bytes differ across source snapshot, sdist, and wheel: "
            f"source_only={source_only}, sdist_only={sdist_only}, "
            f"wheel_only={wheel_only}, differing={differing}"
        )
    digest_entries = [
        (relative, b"file\0", body) for relative, body in sorted(source_files.items())
    ]
    return {
        "package_file_count": len(source_files),
        "package_bytes_sha256": _digest_entries(digest_entries),
    }


def _build_and_load(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    source_snapshot: Path,
    plugin_python: Path,
    agent_python: Path,
    agent_dir: Path,
    gateway_dir: Path,
    output_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, Any]] = []
    environment = os.environ.copy()
    environment["WISDOM_GATEWAY_DIR"] = str(gateway_dir)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    commands.append(
        run_command(
            [
                str(plugin_python), "-m", "pytest", "-q",
                "-p", "no:cacheprovider",
                "tests/test_contract.py", "tests/test_contract_openapi.py",
                "tests/test_native_plugin.py",
            ],
            cwd=source_snapshot,
            timeout=300,
            env=environment,
        )
    )
    commands.append(
        run_command(
            build_command(plugin_python, artifacts_dir),
            cwd=source_snapshot,
            timeout=180,
        )
    )
    sdists = sorted(artifacts_dir.glob("*.tar.gz"))
    wheels = sorted(artifacts_dir.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise AcceptanceError("build did not produce exactly one sdist and one wheel")
    package_parity = verify_artifact_package_parity(
        source_snapshot, sdists[0], wheels[0]
    )
    with tempfile.TemporaryDirectory(prefix="wisdom-offline-extract-") as temporary:
        temporary_dir = Path(temporary)
        extract_dir = temporary_dir / "extracted"
        with tarfile.open(sdists[0], "r:gz") as archive:
            archive.extractall(extract_dir, filter="data")
        roots = [path for path in extract_dir.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise AcceptanceError("sdist extraction did not produce exactly one root")
        native_layout = verify_native_artifact_layout(roots[0])
        commands.append(
            run_command(
                _host_artifact_discovery_command(agent_python, agent_dir, roots[0]),
                cwd=extract_dir,
            )
        )
        driver_commands, driver_environment = gateway_driver_bootstrap_commands(
            agent_python, agent_dir, gateway_dir, roots[0]
        )
        for command in driver_commands:
            commands.append(
                run_command(
                    command,
                    cwd=gateway_dir,
                    timeout=120,
                    env=driver_environment,
                )
            )
    artifacts = [
        {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": str(path.stat().st_size),
        }
        for path in sorted(artifacts_dir.iterdir())
        if path.is_file()
    ]
    return artifacts, commands, package_parity, native_layout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path)
    parser.add_argument("--gateway-dir", type=Path)
    parser.add_argument("--portal-dir", type=Path)
    parser.add_argument("--portal-revision")
    parser.add_argument("--agent-dir", type=Path)
    parser.add_argument("--plugin-python", type=Path)
    parser.add_argument("--agent-python", type=Path)
    parser.add_argument("--pnpm", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def _require_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    missing = [
        name.replace("_", "-")
        for name in (
            "plugin_dir", "gateway_dir", "portal_dir", "portal_revision", "agent_dir",
            "plugin_python", "agent_python", "pnpm", "output_dir",
        )
        if getattr(args, name) is None
    ]
    if missing:
        parser.error(
            "missing required offline arguments: "
            + ", ".join(f"--{name}" for name in missing)
        )


def _run_checks(  # pylint: disable=too-many-locals
    args: argparse.Namespace, report: dict[str, Any], output_dir: Path
) -> None:
    plugin_dir = _required_path(args.plugin_dir.resolve())
    gateway_dir = _required_path(args.gateway_dir.resolve())
    portal_dir = _required_path(args.portal_dir.resolve())
    agent_dir = _required_path(args.agent_dir.resolve())
    plugin_python = configured_executable(args.plugin_python)
    agent_python = configured_executable(args.agent_python)
    pnpm = configured_executable(args.pnpm)
    portal_revision = validated_revision(args.portal_revision, label="Portal")

    with tempfile.TemporaryDirectory(prefix="wisdom-offline-snapshot-") as temporary:
        source_snapshot = Path(temporary) / "source"
        source_identity = create_source_snapshot(plugin_dir, source_snapshot)
        expected = _sources_values(source_snapshot)
        contract_values = _contract_values(source_snapshot)
        if contract_values != {key: expected[key] for key in contract_values}:
            raise AcceptanceError(
                f"plugin contract constants drift from docs/SOURCES.md: {contract_values}"
            )
        report["pins"] = expected
        report["plugin_source"] = source_identity
        if source_identity["dirty"]:
            report["remaining_blockers"].append(
                "Plugin source is uncommitted; the snapshot hash identifies tested bytes, "
                "not an immutable release commit."
            )
        portal_identity = verify_checkout(
            portal_dir,
            portal_revision,
            require_clean=False,
            require_exact_head=False,
        )
        gateway_identity = verify_checkout(
            gateway_dir, expected["gateway_contract"], require_clean=False
        )
        gateway_identity["verified_clean_paths"] = verify_clean_paths(
            gateway_dir,
            ("src", "prisma", "openapi.json", "package-lock.json", "tsconfig.json"),
        )
        if not gateway_identity["clean"]:
            report["remaining_blockers"].append(
                "Gateway has uncommitted content outside its pinned runtime-contract paths; "
                "the worktree is recorded separately from the immutable contract commit."
            )
        report["sources"] = {
            "gateway": gateway_identity,
            "agent": verify_checkout(agent_dir, expected["agent"], require_clean=True),
            "portal": portal_identity,
        }
        report["runtime_contracts"] = _verify_contract_bytes(source_snapshot, expected)
        report["portal_contract_pin"] = _verify_portal_pin(
            portal_dir, portal_revision, expected
        )

        if portal_identity["head_exact"] and portal_identity["clean"]:
            portal_argv, portal_cwd = portal_contract_command(portal_dir, pnpm)
            report["checks"].append(run_command(portal_argv, cwd=portal_cwd, timeout=180))
        else:
            report["checks"].append({
                "name": "portal_contract_check",
                "status": "blocked",
                "reason": (
                    "the located Portal worktree is not a clean checkout of the immutable "
                    f"release pin {portal_revision}"
                ),
            })
            report["remaining_blockers"].append(
                "Portal's generated TypeScript contract check requires a clean checkout at "
                f"{portal_revision}; immutable pin bytes were verified directly from Git."
            )
        artifacts, build_checks, package_parity, native_layout = _build_and_load(
            source_snapshot, plugin_python, agent_python, agent_dir, gateway_dir, output_dir
        )
        report["checks"].extend(build_checks)
        report["artifacts"] = artifacts
        report["artifact_package_parity"] = package_parity
        report["native_artifact_layout"] = native_layout
        report["status"] = "passed"


def execute(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the bounded offline path and return its evidence report."""
    report = new_report()
    output_dir = args.output_dir.resolve()
    report_path = output_dir / "offline-acceptance-report.json"
    try:
        create_output_directory(output_dir)
    except AcceptanceError as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["report_path"] = None
        return report
    try:
        _run_checks(args, report, output_dir)
    except (AcceptanceError, OSError, ValueError, json.JSONDecodeError) as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, execute the offline lane, and emit its JSON report."""
    parser = _parser()
    args = parser.parse_args(argv)
    _require_args(parser, args)
    report = execute(args)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
