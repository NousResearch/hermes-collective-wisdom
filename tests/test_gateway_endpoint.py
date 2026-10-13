"""The plugin owns the Gateway base URL that Hermes core used to resolve for it."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.gateway_endpoint import (
    DEFAULT_GATEWAY_BASE_URL,
    host_config_loader,
    resolve_gateway_base_url,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_wisdom_plugin"
# Removed from Hermes core in 64ad33e32d (Skill Sync); the plugin must never import them.
REMOVED_CORE_MODULES = (
    "tools.skills_sync_client",
    "tools.skills_sync_client_org",
    "tools.skills_sync_client_wire",
    "hermes_cli.subcommands.sync",
)


def _config(value):
    """A config loader returning one sync.base_url value."""
    return lambda: {"sync": {"base_url": value}}


def test_env_wins_over_config_and_drops_the_trailing_slash():
    """HERMES_SYNC_BASE_URL wins over config.yaml, as in core."""
    url = resolve_gateway_base_url(
        load_config=_config("https://config.example"),
        environ={"HERMES_SYNC_BASE_URL": " https://env.example/ "},
    )
    assert url == "https://env.example"


def test_config_is_used_when_env_is_blank():
    """A blank env falls through to sync.base_url."""
    url = resolve_gateway_base_url(
        load_config=_config("https://config.example/"), environ={"HERMES_SYNC_BASE_URL": "  "}
    )
    assert url == "https://config.example"


@pytest.mark.parametrize(
    "loader",
    [None, lambda: None, lambda: {}, lambda: {"sync": None}, lambda: {"sync": {"base_url": 7}},
     _config("   ")],
)
def test_default_is_the_production_gateway(loader):
    """No usable env or config value means the production Gateway."""
    assert resolve_gateway_base_url(load_config=loader, environ={}) == DEFAULT_GATEWAY_BASE_URL
    assert DEFAULT_GATEWAY_BASE_URL == "https://gateway-gateway.nousresearch.com"


def test_a_failing_config_read_falls_back_to_the_default():
    """A config read error never blocks Wisdom; the default applies."""
    def broken():
        raise OSError("config.yaml unreadable")

    assert resolve_gateway_base_url(load_config=broken, environ={}) == DEFAULT_GATEWAY_BASE_URL


def _readonly():
    """Stand-in for load_config_readonly."""
    return {}


def _writing():
    """Stand-in for load_config."""
    return {}


def test_the_host_reader_prefers_the_side_effect_free_loader():
    """The read-only loader is used when the host has one."""
    readonly, writing = _readonly, _writing
    both = SimpleNamespace(load_config_readonly=readonly, load_config=writing)
    assert host_config_loader(both) is readonly
    assert host_config_loader(SimpleNamespace(load_config=writing)) is writing
    assert host_config_loader(SimpleNamespace()) is None


def test_the_plugin_never_imports_removed_skill_sync_core_modules():
    """Hermes main no longer ships Skill Sync; importing it breaks every Wisdom command."""
    offenders = []
    for path in sorted(PLUGIN_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for module in REMOVED_CORE_MODULES:
            pattern = (rf"import_module\(\s*[\"']{re.escape(module)}[\"']"
                       rf"|^\s*import {re.escape(module)}\b|^\s*from {re.escape(module)} import")
            if re.search(pattern, text, flags=re.M):
                offenders.append(f"{path.name}: {module}")
    assert not offenders, offenders
