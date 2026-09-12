"""Reference terminal plan/apply/removal semantics and inert slash handoffs."""

# pylint: disable=missing-function-docstring

import argparse
import json
import sys
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import build_parser, run_cli, run_slash


class Service:
    """Minimal effect-recording command boundary."""

    def __init__(self):
        self.calls = []

    def install_plan(self, reference, *, update_mode=None):
        self.calls.append(("plan", reference, update_mode))
        return {"receipt": "wip_" + "a" * 32, "version": 2}

    def install_apply(self, receipt, *, accept_partial=False):
        self.calls.append(("apply", receipt, accept_partial))
        return {"installed": True}

    def list_installations(self):
        self.calls.append(("installed",))
        return [{"skill_id": "example", "version": 2}]

    def uninstall(self, skill_id):
        self.calls.append(("uninstall", skill_id))
        return {"uninstalled": True}


def _args(*args):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return parser.parse_args(args)


@pytest.mark.parametrize("flags", [("--plan",), ("--json",), ("--plan", "--json")])
def test_plan_and_json_never_apply(flags, capsys):
    service = Service()
    assert run_cli(_args("install", "skill@v2", *flags), service) == 0
    assert service.calls == [("plan", "skill@v2", None)]
    assert json.loads(capsys.readouterr().out)["version"] == 2


def test_explicit_receipt_preserves_partial_acceptance(capsys):
    service = Service()
    assert run_cli(_args("install", "--apply-receipt", "receipt", "--accept-partial",
                         "--json"), service) == 0
    assert service.calls == [("apply", "receipt", True)]
    assert json.loads(capsys.readouterr().out) == {"installed": True}


@pytest.mark.parametrize("extra", [("other",), ("--plan",), ("--update-mode", "REQUIRED")])
def test_apply_cannot_change_the_plan(extra, capsys):
    service = Service()
    assert run_cli(_args("install", "--apply-receipt", "receipt", *extra, "--json"),
                   service) == 6
    assert not service.calls
    assert json.loads(capsys.readouterr().out)["category"] == 6


@pytest.mark.parametrize("answer,code,applied", [("yes", 0, True), ("n", 7, False), ("", 7, False)])
def test_interactive_install_confirms_once(monkeypatch, answer, code, applied):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    assert run_cli(_args("install", "skill"), service) == code
    assert len(prompts) == 1
    assert (len(service.calls) == 2) is applied


def test_noninteractive_install_and_uninstall_refuse_implicit_approval(monkeypatch):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert run_cli(_args("install", "skill"), service) == 6
    assert run_cli(_args("uninstall", "skill"), service) == 6
    assert service.calls == [("plan", "skill", None)]


def test_installed_and_confirmed_uninstall(capsys):
    service = Service()
    assert run_cli(_args("installed", "--json"), service) == 0
    assert json.loads(capsys.readouterr().out)["installations"][0]["version"] == 2
    assert run_cli(_args("uninstall", "skill", "--yes", "--json"), service) == 0
    assert service.calls[-1] == ("uninstall", "skill")


@pytest.mark.parametrize("raw", [
    "install skill", "install --apply-receipt SECRET", "uninstall skill --yes",
    "installed --json", "INSTALL secret --accept-partial",
])
def test_slash_handoffs_do_not_read_or_mutate_any_state(raw):
    service = Service()
    output = run_slash(raw, service)
    assert "terminal" in output
    assert "Nothing has been changed" in output
    assert "SECRET" not in output
    assert not service.calls
