"""Offline cross-repository artifact smoke harness tests."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

# Test names carry the behavior contract.
# pylint: disable=missing-function-docstring

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "test_wisdom_offline_acceptance.py"
WRAPPER = PROJECT_ROOT / "scripts" / "test-wisdom-cross-repo.sh"


@pytest.mark.parametrize("driver", ["delivery", "outbox", "publication"])
def test_configured_gateway_driver_imports_external_package_without_alias(driver):
    gateway = os.environ.get("WISDOM_GATEWAY_DIR")
    if not gateway:
        pytest.skip("WISDOM_GATEWAY_DIR is required for actual Gateway driver imports")
    # Execute only the drivers' real Wisdom imports, never their service-backed main().
    # This checks exported names as well as package provenance without starting services.
    program = """
import ast
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
path = pathlib.Path(sys.argv[2])
source = path.read_text(encoding='utf-8')
imports = [node for node in ast.walk(ast.parse(source))
           if isinstance(node, ast.ImportFrom)
           and (node.module or '').startswith('hermes_wisdom')]
if imports:
    exec(compile(ast.Module(body=imports, type_ignores=[]), str(path), 'exec'), {})
else:
    assert path.name == 'wisdom_publication_driver.py'
    assert 'load_external_plugin(home)' in source and 'PluginManager' in source
assert not any(name == 'hermes_wisdom' or name.startswith('hermes_wisdom.')
               for name in sys.modules), 'retired package alias was imported'
for name, module in list(sys.modules.items()):
    if name == 'hermes_wisdom_plugin' or name.startswith('hermes_wisdom_plugin.'):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
print('External package imports verified without running the service driver')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(PROJECT_ROOT),
         str(Path(gateway) / "test" / "e2e" / f"wisdom_{driver}_driver.py")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_publication_driver_calls_existing_client_methods():
    """Import-only bootstrap must not hide dead calls in the service-backed lane."""
    from hermes_wisdom_plugin.client import WisdomClient  # pylint: disable=import-outside-toplevel

    gateway = os.environ.get("WISDOM_GATEWAY_DIR")
    if not gateway:
        pytest.skip("WISDOM_GATEWAY_DIR is required for publication driver contracts")
    source = Path(gateway) / "test/e2e/wisdom_publication_driver.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    methods = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"publisher", "manager_client"}
    }
    assert methods, "Publication driver client calls were not inspected"
    missing = sorted(name for name in methods if not callable(getattr(WisdomClient, name, None)))
    assert not missing, f"Publication driver calls absent WisdomClient methods: {missing}"


def _load_harness():
    spec = importlib.util.spec_from_file_location("wisdom_offline_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_portal_revision_must_be_a_full_commit_id() -> None:
    harness = _load_harness()

    assert harness.validated_revision("a" * 40, label="Portal") == "a" * 40
    with pytest.raises(harness.AcceptanceError, match="40-character"):
        harness.validated_revision("abc123", label="Portal")


def test_checkout_revision_fails_closed_on_head_drift(tmp_path: Path) -> None:
    harness = _load_harness()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "drift"], check=True)

    with pytest.raises(harness.AcceptanceError, match="HEAD drift"):
        harness.verify_checkout(repo, expected, require_clean=True)


def test_portal_revision_requires_exact_clean_head(tmp_path: Path) -> None:
    harness = _load_harness()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    (repo / "value.txt").write_text("pin\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "pin"], check=True)
    pin = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "value.txt").write_text("head\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "other"], check=True)

    with pytest.raises(harness.AcceptanceError, match="HEAD drift"):
        harness.verify_checkout(repo, pin, require_clean=True)


def test_checkout_manifest_separates_immutable_pin_from_dirty_worktree(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    tracked = repo / "value.txt"
    tracked.write_text("pin\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "pin"], check=True)
    pin = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("new head\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "new head"], check=True)
    tracked.write_text("dirty bytes\n", encoding="utf-8")

    identity = harness.verify_checkout(
        repo, pin, require_clean=False, require_exact_head=False
    )

    assert identity["immutable_revision"] == {
        "commit": pin,
        "available": True,
    }
    assert identity["working_tree"]["head"] != pin
    assert identity["working_tree"]["head_exact"] is False
    assert identity["working_tree"]["dirty"] is True


def test_required_contract_paths_must_remain_clean_when_other_paths_are_dirty(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "src/contract.ts").write_text("export const version = 1\n")
    (repo / "scripts/smoke.sh").write_text("true\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    (repo / "scripts/smoke.sh").write_text("set -e\ntrue\n")

    assert harness.verify_clean_paths(repo, ("src",)) == ["src"]

    (repo / "src/contract.ts").write_text("export const version = 2\n")
    with pytest.raises(harness.AcceptanceError, match="pinned contract paths"):
        harness.verify_clean_paths(repo, ("src",))


def test_portal_pin_is_read_from_immutable_revision_not_dirty_worktree(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    repo = tmp_path / "portal"
    pin_path = (
        repo
        / "apps/nous-account-service/src/server/sync/generated/"
        "wisdom-plane-contract-pin.json"
    )
    pin_path.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    expected = {
        "gateway_contract": "a" * 40,
        "openapi_sha256": "b" * 64,
        "manifest_schema_sha256": "c" * 64,
        "canonical_vectors_sha256": "d" * 64,
    }
    pin_path.write_text(
        json.dumps(
            {
                "gatewayCommit": expected["gateway_contract"],
                "openapiSha256": expected["openapi_sha256"],
                "manifestSchemaSha256": expected["manifest_schema_sha256"],
                "canonicalVectorsSha256": expected["canonical_vectors_sha256"],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "portal pin"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pin_path.write_text("{}\n", encoding="utf-8")

    evidence = harness._verify_portal_pin(  # pylint: disable=protected-access
        repo, revision, expected
    )

    assert evidence["source_revision"] == revision
    assert evidence["gatewayCommit"] == expected["gateway_contract"]


def test_source_snapshot_handles_tracked_deletion_and_hashes_built_bytes(tmp_path: Path) -> None:
    harness = _load_harness()
    repo = tmp_path / "plugin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("extra\n", encoding="utf-8")
    (repo / "tracked.txt").unlink()
    snapshot = tmp_path / "snapshot"

    identity = harness.create_source_snapshot(repo, snapshot)

    assert identity["tracked_deletions"] == ["tracked.txt"]
    assert identity["snapshot_sha256"] == harness.snapshot_sha256(snapshot)
    assert not (snapshot / "tracked.txt").exists()
    assert (snapshot / "untracked.txt").read_text(encoding="utf-8") == "extra\n"


def test_source_snapshot_refuses_symlink_outside_snapshot(tmp_path: Path) -> None:
    harness = _load_harness()
    repo = tmp_path / "plugin"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    outside = tmp_path / "secret"
    outside.write_text("not source\n", encoding="utf-8")
    (repo / "linked.txt").symlink_to(outside)

    with pytest.raises(harness.AcceptanceError, match="symlink escapes snapshot"):
        harness.create_source_snapshot(repo, tmp_path / "snapshot")


def test_contract_read_does_not_mutate_source_snapshot(tmp_path: Path) -> None:
    harness = _load_harness()
    package = tmp_path / "hermes_wisdom_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "contract.py").write_text(
        "from types import SimpleNamespace\n"
        "CONTRACT_PIN = SimpleNamespace(gateway_commit='g', openapi_sha256='o', "
        "manifest_schema_sha256='m', canonical_vectors_sha256='c')\n",
        encoding="utf-8",
    )

    harness._contract_values(tmp_path)  # pylint: disable=protected-access

    assert not (package / "__pycache__").exists()


def test_run_command_uses_exact_argv_and_cwd(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()
    calls = []

    def recording_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", recording_run)
    result = harness.run_command(["tool", "argument with spaces", "--check"], cwd=tmp_path)

    assert result["argv"] == ["tool", "argument with spaces", "--check"]
    assert result["stdout"] == "ok"
    assert calls == [
        (
            ["tool", "argument with spaces", "--check"],
            {
                "cwd": tmp_path,
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 180,
            },
        )
    ]


def test_run_command_bounds_verbose_evidence(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()
    verbose = "a" * 12_000
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=verbose, stderr=""
        ),
    )

    result = harness.run_command(["verbose-tool"], cwd=tmp_path)

    assert len(result["stdout"]) < len(verbose)
    assert "omitted" in result["stdout"]


def test_run_command_converts_timeout_to_acceptance_failure(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()

    def time_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="partial", stderr="late")

    monkeypatch.setattr(harness.subprocess, "run", time_out)

    with pytest.raises(harness.AcceptanceError, match="timed out after 7 seconds"):
        harness.run_command(["slow-tool"], cwd=tmp_path, timeout=7)


def test_configured_executable_preserves_venv_symlink(tmp_path: Path) -> None:
    harness = _load_harness()
    target = tmp_path / "python-real"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    venv_python = tmp_path / "venv-python"
    venv_python.symlink_to(target)

    assert harness.configured_executable(venv_python) == venv_python.absolute()


def test_build_command_uses_isolated_environment_without_mutating_project_venv(
    tmp_path: Path,
) -> None:
    harness = _load_harness()

    assert harness.build_command(Path("/venv/python"), tmp_path) == [
        "/venv/python",
        "-m",
        "build",
        "--outdir",
        str(tmp_path),
    ]


def test_output_directory_must_be_fresh(tmp_path: Path) -> None:
    harness = _load_harness()
    existing = tmp_path / "evidence"
    existing.mkdir()
    marker = existing / "earlier-artifact.whl"
    marker.write_bytes(b"keep")

    with pytest.raises(harness.AcceptanceError, match="already exists"):
        harness.create_output_directory(existing)

    assert marker.read_bytes() == b"keep"


def test_timeout_failure_is_written_to_fresh_report(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()
    output = tmp_path / "evidence"

    def fail_checks(*_args):
        raise harness.AcceptanceError("offline command timed out after 7 seconds")

    monkeypatch.setattr(harness, "_run_checks", fail_checks)

    report = harness.execute(argparse.Namespace(output_dir=output))

    written = json.loads((output / "offline-acceptance-report.json").read_text())
    assert report["status"] == written["status"] == "failed"
    assert "timed out" in written["error"]


def test_portal_head_mismatch_fails_before_any_check(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()
    paths = {name: tmp_path / name for name in ("plugin", "gateway", "portal", "agent")}
    for path in paths.values():
        path.mkdir()
    args = argparse.Namespace(
        plugin_dir=paths["plugin"], gateway_dir=paths["gateway"],
        portal_dir=paths["portal"], portal_revision="a" * 40,
        agent_dir=paths["agent"], plugin_python=Path("python"),
        agent_python=Path("host-python"), pnpm=Path("pnpm"),
    )
    expected = {
        "agent": "b" * 40, "gateway_contract": "c" * 40,
        "portal_feature": "d" * 40, "gateway_feature": "e" * 40,
        "openapi_sha256": "o", "manifest_schema_sha256": "m",
        "canonical_vectors_sha256": "v",
    }
    monkeypatch.setattr(harness, "configured_executable", lambda path: path)
    monkeypatch.setattr(
        harness, "create_source_snapshot",
        lambda _repo, destination: (
            destination.mkdir(), {"snapshot_sha256": "s", "dirty": False}
        )[1],
    )
    monkeypatch.setattr(harness, "_sources_values", lambda _path: expected)
    monkeypatch.setattr(
        harness, "_contract_values",
        lambda _path: {key: expected[key] for key in (
            "gateway_contract", "openapi_sha256", "manifest_schema_sha256",
            "canonical_vectors_sha256",
        )},
    )

    def verify(repo, *_args, **_kwargs):
        if repo == paths["portal"]:
            raise harness.AcceptanceError("HEAD drift")
        return {}

    invoked = []
    monkeypatch.setattr(harness, "verify_checkout", verify)
    monkeypatch.setattr(harness, "run_command", lambda *_args, **_kwargs: invoked.append(True))

    with pytest.raises(harness.AcceptanceError, match="HEAD drift"):
        harness._run_checks(  # pylint: disable=protected-access
            args, harness.new_report(), tmp_path / "output"
        )

    assert not invoked


def test_native_load_is_isolated_and_records_sdist_module_path(tmp_path: Path) -> None:
    harness = _load_harness()
    command = harness._host_artifact_discovery_command(  # pylint: disable=protected-access
        Path("/host/python"), Path("/agent/source"), tmp_path
    )

    assert command[:3] == ["/host/python", "-I", "-B"]
    assert "module_file" in command[4]
    assert "PluginManager" in command[4]
    assert "hermes_wisdom" in command[4]
    compile(command[4], "<host-artifact-discovery>", "exec")
    assert command[-2:] == ["/agent/source", str(tmp_path)]


def test_native_artifact_layout_requires_loader_manifest_and_package(tmp_path: Path) -> None:
    harness = _load_harness()
    root = tmp_path / "artifact"
    package = root / "hermes_wisdom_plugin"
    package.mkdir(parents=True)
    (root / "__init__.py").write_text("from .hermes_wisdom_plugin import register\n")
    (package / "__init__.py").write_text("def register(ctx): del ctx\n")

    with pytest.raises(harness.AcceptanceError, match="plugin.yaml"):
        harness.verify_native_artifact_layout(root)

    (root / "plugin.yaml").write_text("name: hermes-collective-wisdom\n")
    with pytest.raises(harness.AcceptanceError, match="desktop/plugin.js"):
        harness.verify_native_artifact_layout(root)
    (root / "desktop").mkdir()
    (root / "desktop" / "plugin.js").write_text("export default {};\n")
    evidence = harness.verify_native_artifact_layout(root)
    assert evidence["desktop"] == "desktop/plugin.js"
    assert evidence["native_loader"] == "__init__.py"
    assert evidence["manifest"] == "plugin.yaml"
    assert evidence["package"] == "hermes_wisdom_plugin"


def test_gateway_driver_bootstrap_commands_pin_artifact_and_isolate_python(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    gateway = tmp_path / "gateway"
    artifact = tmp_path / "artifact"
    agent = tmp_path / "agent"

    commands, environment = harness.gateway_driver_bootstrap_commands(
        Path("/host/python"), agent, gateway, artifact
    )

    assert [command[-2:] for command in commands] == [
        [str(gateway / "test/e2e/wisdom_delivery_driver.py"), "--offline-bootstrap"],
        [str(gateway / "test/e2e/wisdom_outbox_driver.py"), "--offline-bootstrap"],
        [str(gateway / "test/e2e/wisdom_publication_driver.py"), "--offline-bootstrap"],
    ]
    assert all(command[:3] == ["/host/python", "-I", "-B"] for command in commands)
    assert environment["HERMES_AGENT_DIR"] == str(agent.resolve())
    assert environment["HERMES_WISDOM_ARTIFACT_ROOT"] == str(artifact.resolve())
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_built_wheel_and_sdist_package_bytes_match_snapshot(tmp_path: Path) -> None:
    harness = _load_harness()
    source = tmp_path / "source"
    package = source / "hermes_wisdom_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"VALUE = 1\n")
    sdist = tmp_path / "example.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(package, arcname="example-1.0/hermes_wisdom_plugin")
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hermes_wisdom_plugin/__init__.py", b"VALUE = 1\n")

    evidence = harness.verify_artifact_package_parity(source, sdist, wheel)

    assert evidence["package_file_count"] == 1
    assert len(evidence["package_bytes_sha256"]) == 64


def test_portal_check_command_targets_only_offline_contract_check(tmp_path: Path) -> None:
    harness = _load_harness()
    portal = tmp_path / "portal"
    app = portal / "apps" / "nous-account-service"
    app.mkdir(parents=True)

    command = harness.portal_contract_command(portal, Path("/tooling/pnpm"))

    assert command == (["/tooling/pnpm", "wisdom:contract:check"], app)


def test_contract_import_timeout_is_an_acceptance_failure(monkeypatch, tmp_path: Path) -> None:
    harness = _load_harness()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("contract import", 30)

    monkeypatch.setattr(harness.subprocess, "run", timeout)
    with pytest.raises(harness.AcceptanceError, match="timed out"):
        harness._contract_values(tmp_path)  # pylint: disable=protected-access


def test_contract_inspection_preserves_imported_package_modules() -> None:
    from hermes_wisdom_plugin import package  # pylint: disable=import-outside-toplevel

    harness = _load_harness()
    before = dict(sys.modules)
    harness._contract_values(Path(__file__).resolve().parents[1])  # pylint: disable=protected-access
    assert sys.modules.get("hermes_wisdom_plugin.package") is package
    assert all(sys.modules.get(name) is module for name, module in before.items())


def test_report_never_claims_four_repo_or_live_acceptance() -> None:
    harness = _load_harness()
    report = harness.new_report()

    assert report["scope"] == "offline_preflight_and_artifact_smoke"
    assert report["four_repo_acceptance"] is False
    assert report["live_acceptance"] is False
    assert json.dumps(report)


def test_offline_wrapper_fails_closed_when_checkout_paths_are_missing() -> None:
    completed = subprocess.run(
        ["bash", str(WRAPPER)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert completed.returncode != 0
    assert "WISDOM_GATEWAY_DIR" in completed.stderr
