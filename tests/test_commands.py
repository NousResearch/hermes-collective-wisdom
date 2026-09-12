"""CLI/slash tests adapted from the source Wisdom parser and gateway controller."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import argparse
import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import PAGE_SIZE, build_parser, run_cli, run_slash
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomNotFound


def _parser():
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    return parser


class _Service:
    def __init__(self):
        self.calls = []
        self.rows = [
            {
                "id": f"skill-{number}",
                "slug": f"incident-{number}",
                "latest_version": number + 1,
                "author_description": f"Description {number}",
                "created_by_user_id": "account-secret",
                "local_path": "/Users/private/skill",
                "local_installation": {"version": 1},
                "security_check": {
                    "status": "pass",
                    "summary": "PRIVATE_SUMMARY",
                    "checks": [{"details": ["PRIVATE_FINDING"]}],
                },
                "professionalism_check": {"status": "pending"},
            }
            for number in range(PAGE_SIZE + 1)
        ]

    def require_setup(self):
        self.calls.append(("require_setup",))

    def setup(self, *, accept_disclosure=False):
        self.calls.append(("setup", accept_disclosure))
        return {"configured": True}

    def status(self):
        self.calls.append(("status",))
        return {
            "configured": True,
            "state": "active",
            "gateway_available": True,
            "capability_advertised": True,
            "entitled": True,
            "verified_org_id": "account-secret",
            "installation_id": "private-installation",
            "profile_home": "/Users/private/profile",
            "error": "Authorization: Bearer secret-token",
        }

    def list_skills(self):
        self.calls.append(("list_skills",))
        return {"skills": self.rows[:1], "next_cursor": None}

    def search_skills(self, query=None):
        self.calls.append(("search_skills", query))
        return self.rows

    def show(self, skill_id):
        self.calls.append(("show", skill_id))
        return self.resolve_skill(skill_id)

    def versions(self, skill_id):
        self.calls.append(("versions", skill_id))
        return self.resolve_skill(skill_id)["versions"]

    def resolve_skill(self, reference):
        self.calls.append(("resolve_skill", reference))
        return {
            "skill": {
                "id": "skill-1",
                "slug": "incident-handoff",
                "author_description": "Fallback description",
                "created_by_user_id": "account-secret",
            },
            "versions": [
                {
                    "version": number,
                    "published_at": f"2026-09-{number:02d}",
                    "state": "published",
                    "system_spec": {
                        "hermes": {"minimum_version": "0.20.5"},
                        "platforms": ["macOS"],
                        "runtime": {"shell": True},
                    },
                    "security_check": {
                        "status": "blocked" if number == 6 else "pass",
                        "summary": "PRIVATE_SUMMARY",
                    },
                    "professionalism_check": {"status": "unavailable"},
                }
                for number in range(7, 0, -1)
            ]
            + [{"version": 99, "state": "draft"}, {"version": True}],
            "latest_version_detail": {
                "version": {
                    "version": 7,
                    "author_description": "Summarize an incident",
                    "system_spec": {
                        "hermes": {"minimum_version": "0.20.5"},
                        "platforms": ["macOS"],
                        "runtime": {"shell": True},
                        "tools": [{"name": "browser"}],
                        "plugins": [{"id": "safe-count-only"}],
                        "model": {
                            "capabilities": ["PRIVATE_MODEL_CAPABILITY"],
                            "minimum_context_window": 8192,
                        },
                        "credentials": ["PRIVATE_CREDENTIAL"],
                        "connections": ["PRIVATE_CONNECTION"],
                        "filesystem": {
                            "read": ["/Users/private/read"],
                            "write": ["/Users/private/write"],
                        },
                        "network": {"destinations": ["private.internal"]},
                        "hardware": ["PRIVATE_HARDWARE"],
                        "known_limitations": ["PRIVATE_LIMITATION"],
                    },
                    "security_check": {
                        "status": "pass",
                        "summary": "PRIVATE_SUMMARY",
                        "checks": [{"details": ["PRIVATE_FINDING"]}],
                    },
                    "professionalism_check": {"status": "advisory"},
                }
            },
            "portal_url": (
                "https://portal.example/wisdom/skills/skill-1?org_id=org-1"
            ),
            "local_compatibility": {"path": "/Users/private/compat"},
            "local_installation": {"version": 2},
        }


def test_parser_registers_only_bounded_commands_and_json_flags():
    parser = _parser()
    cases = {
        "setup": ["--accept-disclosure", "--json"],
        "status": ["--json"],
        "list": ["--json"],
        "browse": ["incident", "--json"],
        "show": ["skill-1", "--json"],
        "versions": ["skill-1", "--json"],
        "help": ["--json"],
    }
    for command, trailing in cases.items():
        args = parser.parse_args([command, *trailing])
        assert args.wisdom_command == command
        assert args.json is True
    assert "install" not in parser.format_help()


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_key"),
    [
        (["status", "--json"], ("status",), "configured"),
        (["list", "--json"], ("list_skills",), "skills"),
        (["browse", "incident", "--json"], ("search_skills", "incident"), "skills"),
        (["show", "skill-1", "--json"], ("show", "skill-1"), "skill"),
        (["versions", "skill-1", "--json"], ("versions", "skill-1"), "versions"),
    ],
)
def test_cli_dispatch_shapes_and_exit_parity(argv, expected_call, expected_key, capsys):
    service = _Service()
    assert run_cli(_parser().parse_args(argv), service) == 0

    result = json.loads(capsys.readouterr().out)
    assert expected_key in result
    assert expected_call in service.calls
    if argv[0] not in {"status"}:
        assert ("require_setup",) in service.calls


def test_cli_json_is_sorted_and_help_has_machine_shape(capsys):
    service = _Service()
    service.status = lambda: {"z": 1, "a": 2}

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == 0
    output = capsys.readouterr().out
    assert output.index('"a"') < output.index('"z"')

    assert run_cli(_parser().parse_args(["help", "--json"]), service) == 0
    assert json.loads(capsys.readouterr().out)["commands"][-1] == "help"


def test_cli_setup_noninteractive_requires_switch(monkeypatch, capsys):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    result = run_cli(_parser().parse_args(["setup", "--json"]), service)

    assert result == 6
    assert not service.calls
    assert json.loads(capsys.readouterr().out)["category"] == 6


def test_cli_interactive_disclosure_precedes_setup(monkeypatch, capsys):
    service = _Service()
    module = SimpleNamespace(WISDOM_DISCLOSURE="CANONICAL DISCLOSURE")
    monkeypatch.setitem(sys.modules, "hermes_wisdom_plugin.onboarding", module)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert run_cli(_parser().parse_args(["setup"]), service) == 0
    assert "CANONICAL DISCLOSURE" in capsys.readouterr().out
    assert service.calls == [("setup", True)]


def test_cli_declined_disclosure_returns_cancel_code(monkeypatch):
    service = _Service()
    monkeypatch.setitem(
        sys.modules,
        "hermes_wisdom_plugin.onboarding",
        SimpleNamespace(WISDOM_DISCLOSURE="DISCLOSURE"),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert run_cli(_parser().parse_args(["setup"]), service) == 7
    assert not service.calls


@pytest.mark.parametrize("error", [WisdomNotFound("missing"), WisdomAuthError("login")])
def test_cli_preserves_typed_error_exit_codes(error, capsys):
    service = _Service()
    service.status = lambda: (_ for _ in ()).throw(error)

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == error.exit_code
    assert json.loads(capsys.readouterr().out)["category"] == error.exit_code


def test_cli_redacts_unexpected_exceptions(capsys):
    service = _Service()
    service.status = lambda: (_ for _ in ()).throw(RuntimeError("secret-token /tmp/private"))

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == 8
    output = capsys.readouterr().out
    assert "temporarily unavailable" in output
    assert "secret-token" not in output
    assert "/tmp/private" not in output


def test_slash_is_synchronous_and_quoted_search_is_preserved():
    service = _Service()

    output = run_slash('browse "incident handoff"', service)

    assert inspect.iscoroutinefunction(run_slash) is False
    assert ("search_skills", "incident handoff") in service.calls
    assert "Results for \"incident handoff\"" in output


def test_slash_list_alias_paginates_five_items_and_back_navigation():
    service = _Service()
    first = run_slash("list", service)
    second = run_slash("browse --page 2", service)

    assert first.count("Show: /wisdom show") == PAGE_SIZE
    assert "Next: /wisdom browse --page 2" in first
    assert second.count("Show: /wisdom show") == 1
    assert "Back: /wisdom browse --page 1" in second


@pytest.mark.parametrize(
    "raw_args",
    [
        "browse --page",
        "browse --page nope",
        "browse --page 0",
        "browse --page 2 --page 3",
        "versions skill-1 --unknown 2",
        'browse "unterminated',
    ],
)
def test_malformed_navigation_is_safe_and_does_not_call_service(raw_args):
    service = _Service()

    output = run_slash(raw_args, service)

    assert not service.calls
    assert "secret" not in output
    assert output.startswith(("Usage:", "Could not parse"))


def test_slash_setup_with_any_arguments_is_instruction_only():
    service = _Service()

    output = run_slash("setup --accept-disclosure anything", service)

    assert "hermes wisdom setup" in output
    assert not service.calls


def test_slash_help_advertises_only_read_commands_and_terminal_setup():
    output = run_slash("help", _Service())

    for command in ("browse", "show", "versions", "status", "setup", "help"):
        assert f"/wisdom {command}" in output
    for command in ("install", "update", "uninstall", "share", "review", "publish"):
        assert f"/wisdom {command}" not in output


def test_slash_status_uses_an_explicit_shared_chat_whitelist():
    output = run_slash("status", _Service())

    assert "Setup: ready" in output
    assert "State: ready" in output
    for private in ("account-secret", "private-installation", "/Users/private", "secret-token"):
        assert private not in output


def test_slash_status_does_not_call_previous_owner_setup_ready():
    """An active record for another account is not current readiness."""
    service = _Service()
    service.status = lambda: {"configured": False, "state": "active", "reason": "owner_changed"}
    output = run_slash("status", service)
    assert "Setup: required" in output
    assert "State: not configured" in output


def test_slash_browse_omits_account_local_install_and_private_check_details():
    output = run_slash("browse incident", _Service())

    assert "incident-0" in output
    assert "Security check: Pass" in output
    assert "Professionalism check: Pending" in output
    for private in (
        "account-secret",
        "/Users/private",
        "local_installation",
        "PRIVATE_SUMMARY",
        "PRIVATE_FINDING",
        "Install",
    ):
        assert private not in output


def test_slash_show_renders_published_metadata_requirements_states_and_portal():
    output = run_slash("show incident-handoff", _Service())

    assert "Summarize an incident" in output
    assert "Latest: v7" in output
    assert "Hermes 0.20.5+; OS: macOS; runtime: shell" in output
    for summary in (
        "1 tool requirement(s)",
        "1 plugin requirement(s)",
        "model: 1 capability requirement(s), context minimum declared",
        "1 credential requirement(s)",
        "1 connection requirement(s)",
        "filesystem: 1 read, 1 write location(s)",
        "1 network destination requirement(s)",
        "1 hardware requirement(s)",
        "1 known limitation(s)",
    ):
        assert summary in output
    assert "Security check: Pass" in output
    assert "Professionalism check: Advisory" in output
    assert "https://portal.example/wisdom/skills/skill-1?org_id=org-1" in output
    assert "Back: /wisdom browse" in output
    for private in (
        "account-secret",
        "/Users/private",
        "PRIVATE_SUMMARY",
        "PRIVATE_FINDING",
        "PRIVATE_MODEL_CAPABILITY",
        "PRIVATE_CREDENTIAL",
        "PRIVATE_CONNECTION",
        "private.internal",
        "PRIVATE_HARDWARE",
        "PRIVATE_LIMITATION",
    ):
        assert private not in output


def test_slash_default_sandbox_flag_does_not_imply_a_special_requirement():
    service = _Service()
    detail = service.resolve_skill("skill-1")
    detail["latest_version_detail"]["version"]["system_spec"] = {
        "hermes": {"minimum_version": "0.20.5"},
        "runtime": {"sandbox": True},
    }
    service.resolve_skill = lambda _reference: detail

    output = run_slash("show skill-1", service)

    assert "Requirements: Hermes 0.20.5+" in output
    assert "runtime:" not in output
    assert "No special requirements declared" not in output


def test_slash_versions_filters_and_sorts_published_rows_with_links_and_back():
    output = run_slash("versions incident-handoff --page 2", _Service())

    assert output.index("Version 2") < output.index("Version 1")
    assert "Version 99" not in output
    assert "Version True" not in output
    assert "Requirements: Hermes 0.20.5+" in output
    assert "version=2" in output
    assert "Back: /wisdom show incident-handoff" in output
    assert "Previous: /wisdom versions incident-handoff --page 1" in output
    assert "PRIVATE_SUMMARY" not in output


def test_slash_versions_first_page_always_links_back_to_show():
    output = run_slash("versions incident-handoff", _Service())

    assert "Back: /wisdom show incident-handoff" in output
    assert "Previous:" not in output


def test_slash_navigation_preserves_browse_show_versions_parent_intent():
    service = _Service()

    browse = run_slash("browse incident", service)
    show = run_slash("show incident-handoff", service)
    versions = run_slash("versions incident-handoff", service)

    assert "Show: /wisdom show incident-0" in browse
    assert "Versions: /wisdom versions incident-handoff" in show
    assert "Back: /wisdom browse" in show
    assert "Back: /wisdom show incident-handoff" in versions


@pytest.mark.parametrize(
    "detail",
    [
        None,
        [],
        {"skill": [], "versions": "private-path", "portal_url": {"secret": True}},
        {
            "skill": {"slug": "safe-skill", "private": "/Users/private"},
            "versions": [None, "bad", {"version": False}, {"version": -1}],
            "latest_version_detail": {"version": []},
            "raw_exception": "secret-token",
        },
    ],
)
def test_slash_renderers_fail_closed_on_malformed_detail(detail):
    service = _Service()
    service.resolve_skill = lambda _reference: detail

    show = run_slash("show skill-1", service)
    versions = run_slash("versions skill-1", service)

    for output in (show, versions):
        assert "secret-token" not in output
        assert "/Users/private" not in output
        assert "private-path" not in output


def test_slash_drops_unsafe_portal_links():
    service = _Service()
    detail = service.resolve_skill("skill-1")
    detail["portal_url"] = "https://user:secret@portal.example/private"
    service.resolve_skill = lambda _reference: detail

    output = run_slash("show skill-1", service)

    assert "View in Portal" not in output
    assert "user:secret" not in output


@pytest.mark.parametrize("command", ["install x", "update --all", "share local"])
def test_slash_rejects_mutation_commands_without_service_calls(command):
    service = _Service()

    output = run_slash(command, service)

    assert output.startswith("Unsupported")
    assert not service.calls
    assert "/wisdom install" not in output


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Authorization: Bearer secret-token /Users/private"),
        ValueError("Usage: /wisdom secret-token /Users/private"),
        WisdomNotFound("secret upstream body"),
        WisdomAuthError("token=secret"),
    ],
)
def test_slash_exceptions_are_redacted(error):
    service = _Service()
    service.search_skills = lambda _query=None: (_ for _ in ()).throw(error)

    output = run_slash("browse", service)

    assert "secret" not in output
    assert "/Users/private" not in output
