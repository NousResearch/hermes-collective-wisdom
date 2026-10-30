"""Setup lets Collective Wisdom start chat turns unless the operator already chose."""
# pylint: disable=missing-function-docstring

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from installed_test_support import JsonState
from test_setup_prompt import _args

from hermes_wisdom_plugin import gateway_injection
from hermes_wisdom_plugin.commands import run_cli
from hermes_wisdom_plugin.gateway_injection import (
    ensure_injection_default, injection_status, injection_text, write_command,
)
from hermes_wisdom_plugin.runtime import InvocationService

KEY = "plugins.entries.collective-wisdom.allow_gateway_injection"


class HostConfig:
    """A config.yaml stand-in written only by the fake ``hermes config set``."""

    def __init__(self, entry=None):
        self.config = {"plugins": {"entries": {}}}
        if entry is not None:
            self.config["plugins"]["entries"]["collective-wisdom"] = entry
        self.commands = []

    def load(self):
        return self.config

    def run(self, command, **kwargs):
        assert kwargs["check"] is False and kwargs["timeout"] > 0
        self.commands.append(command)
        key, value = command[-2:]
        assert key == KEY
        entry = self.config["plugins"]["entries"].setdefault("collective-wisdom", {})
        entry["allow_gateway_injection"] = value == "true"
        return SimpleNamespace(returncode=0)


def test_setup_turns_injection_on_when_unset():
    host = HostConfig()
    result = ensure_injection_default("collective-wisdom", "default",
                                      load_config=host.load, run=host.run)
    assert result["enabled"] is True and result["changed"] is True
    assert host.commands == [
        [sys.executable, "-m", "hermes_cli.main", "config", "set", KEY, "true"]]
    assert host.config["plugins"]["entries"]["collective-wisdom"] == {
        "allow_gateway_injection": True}


def test_setup_keeps_an_operator_false_and_is_idempotent():
    for entry in ({"allow_gateway_injection": False}, {"allow_gateway_injection": True}):
        host = HostConfig(dict(entry))
        for _ in range(2):
            result = ensure_injection_default("collective-wisdom", "default",
                                              load_config=host.load, run=host.run)
            assert result["changed"] is False and result["configured"] is True
            assert result["enabled"] is entry["allow_gateway_injection"]
        assert not host.commands
    host = HostConfig()
    for _ in range(3):
        ensure_injection_default("collective-wisdom", "default",
                                 load_config=host.load, run=host.run)
    assert len(host.commands) == 1


def test_setup_writes_the_named_profile_only():
    host = HostConfig({"enabled": True})
    ensure_injection_default("collective-wisdom", "work", load_config=host.load, run=host.run)
    assert host.commands[0][3:5] == ["-p", "work"]
    assert host.config["plugins"]["entries"]["collective-wisdom"] == {
        "enabled": True, "allow_gateway_injection": True}
    assert write_command("collective-wisdom", "default", False)[3:] == [
        "config", "set", KEY, "false"]


def test_failed_write_reports_off_and_never_claims_a_change():
    host = HostConfig()
    result = ensure_injection_default(
        "collective-wisdom", "default", load_config=host.load,
        run=lambda *_a, **_k: SimpleNamespace(returncode=1),
    )
    assert (result["enabled"], result["changed"], result["write_failed"]) == (False, False, True)
    assert "couldn't change" in injection_text(result)


def test_non_boolean_operator_value_counts_as_set_and_off():
    status = injection_status("collective-wisdom",
                              load_config=lambda: {"plugins": {"entries": {"collective-wisdom": {
                                  "allow_gateway_injection": "yes"}}}})
    assert (status["configured"], status["enabled"]) == (True, False)


def test_status_text_names_state_and_profile_scoped_command():
    on = injection_text({"key": KEY, "configured": True, "enabled": True, "changed": True})
    assert on.splitlines() == [
        "Chat suggestions: on (setup allowed Collective Wisdom to start turns in your chats).",
        f"To turn them off: hermes config set {KEY} false",
    ]
    off = injection_text({"key": KEY, "configured": True, "enabled": False}, "work")
    assert off.splitlines() == [
        "Chat suggestions: off (set by an operator).",
        f"To turn them on: hermes -p work config set {KEY} true",
    ]


class _Service:
    profile = ("work", Path("/profiles/work"))

    def __init__(self):
        self.calls = []

    def require_setup(self):
        return None

    def setup_pending(self):
        return False

    def setup(self, *, accept_disclosure):
        self.calls.append(("setup", accept_disclosure))
        return {"configured": True, "state": "ready"}

    def status(self, *, include_operations=False):
        del include_operations
        return {"configured": True, "state": "ready"}

    def gateway_injection(self, *, apply_default=False):
        self.calls.append(("gateway_injection", apply_default))
        return {"key": KEY, "configured": True, "enabled": True, "changed": apply_default}


def test_cli_setup_applies_the_default_and_prints_the_opt_out(capsys):
    service = _Service()
    assert run_cli(_args("setup", "--accept-disclosure"), service) == 0
    assert service.calls == [("setup", True), ("gateway_injection", True)]
    out = capsys.readouterr().out
    assert f"To turn them off: hermes -p work config set {KEY} false" in out
    assert run_cli(_args("status", "--json"), service) == 0
    assert service.calls[-1] == ("gateway_injection", False)
    assert json.loads(capsys.readouterr().out)["gateway_injection"]["enabled"] is True


def test_invocation_service_reads_its_own_plugin_entry(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(gateway_injection, "injection_status",
                        lambda plugin_id: seen.append(("read", plugin_id)) or {})
    monkeypatch.setattr(gateway_injection, "ensure_injection_default",
                        lambda plugin_id, profile: seen.append(("set", plugin_id, profile)) or {})
    service = InvocationService(tmp_path, ("work", tmp_path), state=JsonState(tmp_path),
                                plugin_id="wisdom-fork")
    service.gateway_injection()
    service.gateway_injection(apply_default=True)
    assert seen == [("read", "wisdom-fork"), ("set", "wisdom-fork", "work")]
