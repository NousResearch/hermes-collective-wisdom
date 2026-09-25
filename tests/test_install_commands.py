"""Reference terminal plan/apply/removal semantics and inert slash handoffs."""

# pylint: disable=missing-function-docstring

import argparse
import json
import sys
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_cli, run_slash


class Service:
    """Minimal effect-recording command boundary."""

    def __init__(self):
        self.calls = []

    def install_plan(self, reference, *, update_mode=None, accept_warnings=False):
        self.calls.append(("plan", reference, update_mode, accept_warnings))
        return {
            "receipt": "wip_" + "a" * 32,
            "skill_id": "skill-1", "slug": "example", "version": 2,
            "owner": {"organization_id": "team-7", "account_id": "member-42"},
            "files": [{
                "path": "scripts/setup.sh", "mode": "exec", "size": 20,
                "hash": "sha256:file",
            }],
            "compatibility": {
                "outcome": "compatible_after_setup", "satisfied": ["macOS"],
                "setup_actions": ["Install uv"], "limitations": ["No Windows"],
                "blocked": [],
            },
            "server_checks": {"security_check": {"status": "pass"}},
            "local_scan": {
                "allowed": False,
                "findings": [{
                    "severity": "warning", "category": "instructions",
                    "pattern_id": "shell-network", "file": "scripts/setup.sh",
                    "line": 2, "description": "Uses the network.",
                    "match": "RAW_SECRET",
                }],
            },
            "policy": {"warning_acceptance": {"accepted": accept_warnings}},
            "allowed": accept_warnings,
        }

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


@pytest.mark.parametrize("flags", [("--plan",), ("--plan", "--json")])
def test_plan_never_applies(flags, capsys):
    service = Service()
    assert run_cli(_args("install", "skill@v2", *flags), service) == 0
    assert service.calls == [("plan", "skill@v2", None, False)]
    output = capsys.readouterr().out
    if "--json" in flags:
        assert json.loads(output)["version"] == 2
    else:
        assert "Installation" in output and "Version: 2" in output


def test_json_install_never_prompts_on_tty(capsys, monkeypatch):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))

    assert run_cli(_args("install", "skill@v2", "--json", "--accept-warnings"), service) == 0

    assert json.loads(capsys.readouterr().out)["installed"] is True
    assert service.calls == [
        ("plan", "skill@v2", None, True),
        ("apply", "wip_" + "a" * 32, False),
    ]


def test_installed_files_with_failed_setup_return_nonzero(capsys):
    class FailedSetup(Service):
        """The package write succeeded, but setup did not."""

        def install_apply(self, receipt, *, accept_partial=False):
            super().install_apply(receipt, accept_partial=accept_partial)
            return {"installed": True, "setup": {"state": "failed", "step": 1}}

    service = FailedSetup()
    assert run_cli(_args("install", "skill", "--accept-warnings", "--json"), service) != 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"installed": True, "setup": {"state": "failed", "step": 1}}


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


@pytest.mark.parametrize("answer,applied", [("yes", True), ("n", False), ("", False)])
def test_interactive_install_only_acknowledges_specific_warning(monkeypatch, answer, applied):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    assert run_cli(_args("install", "skill"), service) == (0 if applied else 7)
    assert len(prompts) == 1
    assert "warnings" in prompts[0]
    expected: list[tuple] = [("plan", "skill", None, False)]
    if applied:
        expected.extend([
            ("plan", "skill", None, True),
            ("apply", "wip_" + "a" * 32, False),
        ])
    assert service.calls == expected


def test_interactive_install_displays_bounded_safe_warning_before_apply(
    capsys, monkeypatch
):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert run_cli(_args("install", "skill"), service) == 0

    output = capsys.readouterr().out
    for expected in (
        "Skill: example", "Version: 2", "Files: 1",
        "Install uv", "No Windows", "shell-network", "Uses the network.",
        "Warning acknowledgement: accepted",
    ):
        assert expected in output
    assert "RAW_SECRET" not in output
    assert "scripts/setup.sh [executable]" not in output
    assert "--apply-receipt" not in output
    assert service.calls[-1][0] == "apply"


@pytest.mark.parametrize("warning", [False, True])
@pytest.mark.parametrize("run_setup", [False, True])
def test_interactive_install_asks_separately_for_setup(
    warning, run_setup, capsys, monkeypatch,
):
    """Install writes files; setup needs its own explicit, command-specific choice."""
    class SetupService(Service):
        """Plan and record a disposable declared command."""
        def install_plan(self, reference, *, update_mode=None, accept_warnings=False):
            plan = super().install_plan(reference, update_mode=update_mode,
                                        accept_warnings=accept_warnings)
            plan["setup_commands"] = ["python scripts/setup.py --local"]
            if not warning:
                plan["local_scan"] = {"allowed": True, "findings": []}
                plan["allowed"] = True
            return plan

        def install_setup_apply(self, receipt):
            self.calls.append(("setup", receipt))
            return {"installed": True, "setup": {"state": "passed"},
                    "ready_to_use": None}

    service = SetupService()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return "yes" if "warnings" in prompt.lower() or run_setup else "n"
    monkeypatch.setattr("builtins.input", answer)
    assert run_cli(_args("install", "skill"), service) == 0
    output = capsys.readouterr().out
    assert 'Setup command: "python scripts/setup.py --local"' in output
    assert "Files installed; setup is still needed" in output
    assert len(prompts) == (2 if warning else 1)
    assert "Run setup now?" in prompts[-1]
    if warning:
        assert "warnings" in prompts[0].lower()
        assert "setup" not in prompts[0].lower()
    assert service.calls[-1][0] == ("setup" if run_setup else "apply")


def test_piped_warning_install_requires_explicit_warning_flag_without_applying(
    capsys, monkeypatch
):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))

    assert run_cli(_args("install", "skill"), service) == 6

    output = capsys.readouterr()
    assert not output.out
    assert "scanner warnings" in output.err
    assert service.calls == [("plan", "skill", None, False)]


def test_noninteractive_warning_install_and_uninstall_refuse_implicit_approval(monkeypatch):
    service = Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert run_cli(_args("install", "skill"), service) == 6
    assert run_cli(_args("uninstall", "skill"), service) == 6
    assert service.calls == [("plan", "skill", None, False)]


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
    output = run_slash(parse_slash(raw), service)
    assert "terminal" in output
    assert "Nothing has been changed" in output
    assert "SECRET" not in output
    assert not service.calls
