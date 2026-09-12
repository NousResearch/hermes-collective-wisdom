"""Narrow read-only Hermes host adapter tests."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_wisdom_plugin.install_host as install_host_module
from hermes_wisdom_plugin.contract import (
    HermesRequirement,
    ModelRequirement,
    RuntimeRequirement,
    SystemSpecification,
)
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.package import PackagePolicyError


def _spec(**changes):
    values = {"hermes": HermesRequirement(minimum_version="0.1.0")}
    values.update(changes)
    return SystemSpecification(**values)


def _modules(profile_home, *, allowed=True, scan_error=None):
    calls = []

    @dataclass
    class Finding:
        pattern_id: str = "test"
        severity: str = "low"
        category: str = "test"
        file: str = "SKILL.md"
        line: int = 1
        match: str = "example"
        description: str = "example finding"

    def scan_skill(path, source):
        calls.append(("scan", path, source))
        if scan_error:
            raise scan_error
        return SimpleNamespace(verdict="caution", findings=[Finding()])

    def resolve_target(relative, slug):
        calls.append(("resolve", relative, slug))
        return profile_home / "skills" / relative

    modules = {
        "hermes_constants": SimpleNamespace(get_hermes_home=lambda: profile_home),
        "hermes_cli": SimpleNamespace(__version__="1.0.0"),
        "hermes_cli.config": SimpleNamespace(
            load_config_readonly=lambda: {
                "model": {"provider": "local", "default": "model"},
                "platform_toolsets": {"cli": ["terminal"]},
                "terminal": {"backend": "docker", "docker_network": False},
            },
            get_env_value=lambda name: "secret" if name == "TOKEN" else None,
        ),
        "hermes_cli.tools_config": SimpleNamespace(
            _platform_toolset_summary=lambda _config, _platforms: {
                "cli": {"terminal"}
            }
        ),
        "toolsets": SimpleNamespace(
            resolve_toolset=lambda name, include_registry: [name]
        ),
        "agent.models_dev": SimpleNamespace(
            get_model_capabilities=lambda provider, model, allow_network: SimpleNamespace(
                supports_tools=True,
                supports_vision=False,
                supports_reasoning=False,
                context_window=128000,
            )
        ),
        "tools.skills_guard": SimpleNamespace(
            scan_skill=scan_skill,
            should_allow_install=lambda result, force: (
                calls.append(("policy", result.verdict, force))
                or (allowed, "review finding")
            ),
        ),
        "tools.skills_hub_models": SimpleNamespace(
            _validate_skill_name=lambda name: name
        ),
        "tools.skills_hub_install": SimpleNamespace(
            _resolve_lock_install_path=resolve_target,
            _check_install_target=lambda path: calls.append(("target", path)),
        ),
    }
    return modules, calls


def _bind(monkeypatch, modules):
    monkeypatch.setattr(
        install_host_module.importlib,
        "import_module",
        lambda name: modules[name],
    )


def test_capabilities_are_static_scoped_and_do_not_retain_secrets(
    tmp_path: Path, monkeypatch
):
    modules, _calls = _modules(tmp_path)
    _bind(monkeypatch, modules)
    specification = _spec(
        model=ModelRequirement(
            capabilities=["tools"], minimum_context_window=32000
        ),
        credentials=["TOKEN"],
        runtime=RuntimeRequirement(shell=True, code=True),
    )

    result = InstallationHost(tmp_path).capabilities(specification)

    assert result.hermes_version == "1.0.0"
    assert result.model_capabilities == frozenset({"tools"})
    assert result.context_window == 128000
    assert result.credentials == frozenset({"TOKEN"})
    assert result.network_access is False
    assert "secret" not in repr(result)


@pytest.mark.parametrize("allowed", [True, None, False])
def test_scan_uses_community_policy_without_force(tmp_path: Path, monkeypatch, allowed):
    modules, calls = _modules(tmp_path, allowed=allowed)
    _bind(monkeypatch, modules)
    directory = tmp_path / "review"
    directory.mkdir()

    result = InstallationHost(tmp_path).scan(directory)

    assert result["allowed"] is allowed
    assert result["findings"][0]["file"] == "SKILL.md"
    assert calls == [
        ("scan", directory, "community"),
        ("policy", "caution", False),
    ]


def test_nested_target_uses_nondestructive_host_guards(tmp_path: Path, monkeypatch):
    modules, calls = _modules(tmp_path)
    _bind(monkeypatch, modules)
    host = InstallationHost(tmp_path)
    target = host.skills_root / "_wisdom" / "org-1" / "managed-skill"

    host.validate_target(target)

    assert calls == [
        ("resolve", "_wisdom/org-1/managed-skill", "managed-skill"),
        ("target", target),
    ]
    with pytest.raises(PackagePolicyError, match="contained"):
        host.validate_target(tmp_path / "outside")
    assert not hasattr(host, "install_from_quarantine")


def test_unavailable_scan_and_profile_change_fail_closed(tmp_path: Path, monkeypatch):
    modules, _calls = _modules(
        tmp_path, scan_error=RuntimeError("private host details")
    )
    _bind(monkeypatch, modules)
    with pytest.raises(PackagePolicyError, match="scan is unavailable") as caught:
        InstallationHost(tmp_path).scan(tmp_path)
    assert "private host details" not in str(caught.value)

    modules, _calls = _modules(tmp_path)
    current = {"home": tmp_path}
    modules["hermes_constants"].get_hermes_home = lambda: current["home"]
    modules["tools.skills_guard"].scan_skill = lambda path, source: (
        current.update(home=tmp_path / "other")
        or SimpleNamespace(verdict="safe", findings=[])
    )
    _bind(monkeypatch, modules)
    with pytest.raises(PackagePolicyError, match="profile changed"):
        InstallationHost(tmp_path).scan(tmp_path)


def test_import_is_pure_under_a_synthetic_package_root():
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.util, sys
root = sys.argv[1]
name = '_manual_review_native.hermes_collective_wisdom'
spec = importlib.util.spec_from_file_location(name, root + '/__init__.py', submodule_search_locations=[root])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
__import__(name + '.hermes_wisdom_plugin.install_host', fromlist=['InstallationHost'])
for forbidden in ('agent', 'hermes_cli', 'tools', 'tools.skills_hub_install'):
    assert forbidden not in sys.modules, forbidden
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert result.returncode == 0, result.stderr
