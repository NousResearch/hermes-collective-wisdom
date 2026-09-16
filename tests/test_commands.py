"""CLI/slash tests adapted from the source Wisdom parser and gateway controller."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import argparse
import inspect
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import (
    MAX_PREPARED_PAYLOAD_BYTES,
    PAGE_SIZE,
    build_parser,
    run_cli,
    run_slash,
)
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomNotFound


@pytest.mark.parametrize("choice", ["status", "agent", "fixed"])
def test_cli_notification_settings_are_explicit_and_status_does_not_write(choice, capsys):
    service = SimpleNamespace(mode="agent", calls=[])
    service.require_setup = lambda: service.calls.append(("require_setup",))

    def set_mode(mode):
        service.calls.append(("set_notification_mode", mode))
        service.mode = mode

    service.notification_mode = lambda: service.mode
    service.notification_mode_writable = lambda: True
    service.set_notification_mode = set_mode
    args = _parser().parse_args(["settings", choice, "--json"])
    assert run_cli(args, service) == 0
    assert json.loads(capsys.readouterr().out) == {
        "delivery_mode": "agent" if choice == "status" else choice,
        "writable": True,
    }
    assert service.calls == (
        [("require_setup",)] if choice == "status"
        else [("require_setup",), ("set_notification_mode", choice)]
    )


@pytest.mark.parametrize("command", ["settings fixed", "mute forever", "sync retry"])
def test_shared_slash_controls_only_offer_terminal_handoff(command):
    service = _Service()
    output = run_slash(command, service)
    assert f"hermes wisdom {command.split()[0]} status" in output
    assert f"/wisdom {command.split()[0]}" in run_slash("help", service)
    assert not service.calls


def _parser():
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    return parser


class _Service:  # pylint: disable=too-many-public-methods
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

    def status(self, *, include_operations=False):  # pylint: disable=unused-argument
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

    def check_update(self, skill_id):
        self.calls.append(("check_update", skill_id))
        return {"skill_id": skill_id, "state": "update_available"}

    def suggest(self, skill_name, *, allow_model_review=False):
        self.calls.append(("suggest", skill_name, allow_model_review))
        return {
            "state": "prepared",
            "skill_name": skill_name,
            "professionalism_check": {
                "status": "pass" if allow_model_review else "unavailable"
            },
            "network_submission": False,
        }

    def review_week(self, *, dry_run=False):
        self.calls.append(("review_week", dry_run))
        return {"queued": not dry_run, "dry_run": dry_run, "network_submission": False}

    def prepared_inspect(self, draft_id):
        self.calls.append(("prepared_inspect", draft_id))
        return {"draft_id": draft_id, "files": [], "hashes": {}}

    def prepared_save(self, draft_id, *, author_description, files, expected_hashes):
        self.calls.append((
            "prepared_save", draft_id, author_description, files, expected_hashes
        ))
        return {"draft_id": draft_id, "drafted_description": author_description}

    def prepared_review(self, draft_id, *, allow_model_review=False):
        self.calls.append(("prepared_review", draft_id, allow_model_review))
        return {"draft_id": draft_id, "professionalism_check": {
            "status": "pass" if allow_model_review else "unavailable"
        }}

    def publication_review(self, draft_id):
        self.calls.append(("publication_review", draft_id))
        return {"draft_id": draft_id, "receipt": "wpub_review"}

    def publication_submit(self, receipt):
        self.calls.append(("publication_submit", receipt))
        return {"operation_id": "op-1", "publication_state": "published"}

    def publication_status(self, operation_id):
        self.calls.append(("publication_status", operation_id))
        return {"operation_id": operation_id, "state": "done"}

    def publication_reopen(self, operation_id):
        self.calls.append(("publication_reopen", operation_id))
        return {"operation_id": operation_id, "publication_state": "pending_moderation"}

    def publication_decline(self, operation_id):
        self.calls.append(("publication_decline", operation_id))
        return {"operation_id": operation_id, "publication_state": "declined"}

    def update_plan(self, skill_id, **values):
        self.calls.append(("update_plan", skill_id, values))
        return {"skill_id": skill_id, "receipt": "wip_update", **values}

    def update_apply(self, receipt):
        self.calls.append(("update_apply", receipt))
        return {"updated": True, "receipt": receipt}

    def installed_setup_inspect(self, skill_id, *, version=None):
        self.calls.append(("installed_setup_inspect", skill_id, version))
        return {"skill_id": skill_id, "state": "setup_required"}

    def installed_setup_plan(self, skill_id, *, version, phase, index, command):
        self.calls.append(("installed_setup_plan", skill_id, version, phase, index, command))
        return {"receipt": "wip_setup", "step": {"command": command}}

    def installed_setup_apply(self, receipt):
        self.calls.append(("installed_setup_apply", receipt))
        return {"state": "passed"}

    def installed_setup_status(self, skill_id, *, version=None):
        self.calls.append(("installed_setup_status", skill_id, version))
        return {"skill_id": skill_id, "state": "verification_required"}

    def installed_setup_recover(self, skill_id, operation_id, *, acknowledged=False):
        self.calls.append(("installed_setup_recover", skill_id, operation_id, acknowledged))
        return {"state": "abandoned"}

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
        "suggest": ["local-skill", "--json"],
        "review-week": ["--dry-run", "--json"],
        "help": ["--json"],
    }
    for command, trailing in cases.items():
        args = parser.parse_args([command, *trailing])
        assert args.wisdom_command == command
        assert args.json is True
    assert "install" in parser.format_help()
    assert "check" in parser.format_help()
    assert "update" in parser.format_help()


def test_cli_review_week_uses_public_owner_authorized_service_path_without_model_flag(capsys):
    parser = _parser()
    service = _Service()
    args = parser.parse_args(["review-week", "--dry-run", "--json"])
    assert not hasattr(args, "allow_model_review")
    assert run_cli(args, service) == 0
    assert json.loads(capsys.readouterr().out) == {
        "queued": False, "dry_run": True, "network_submission": False
    }
    assert service.calls == [("review_week", True)]


def test_cli_suggest_requires_explicit_model_disclosure_authorization(capsys):
    parser = _parser()
    service = _Service()

    assert run_cli(
        parser.parse_args(["suggest", "local-skill", "--json"]), service
    ) == 0
    local = json.loads(capsys.readouterr().out)
    assert local["professionalism_check"]["status"] == "unavailable"
    assert ("suggest", "local-skill", False) in service.calls

    assert run_cli(
        parser.parse_args([
            "suggest", "local-skill", "--allow-model-review", "--json"
        ]),
        service,
    ) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["professionalism_check"]["status"] == "pass"
    assert ("suggest", "local-skill", True) in service.calls


def test_cli_prepared_inspect_save_and_review_use_explicit_payload(tmp_path, capsys):
    parser = _parser()
    service = _Service()
    payload = {
        "draft_id": "local:draft-1",
        "author_description": "Owner copy.",
        "files": [{"path": "SKILL.md", "content_utf8": "# Edited\n"}],
        "expected_hashes": {
            "content": "sha256:old-content",
            "author_description": "sha256:old-description",
            "package_manifest": "sha256:old-manifest",
        },
    }
    payload_path = tmp_path / "draft.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    assert run_cli(
        parser.parse_args(["prepared", "inspect", "local:draft-1", "--json"]), service
    ) == 0
    assert json.loads(capsys.readouterr().out)["draft_id"] == "local:draft-1"
    assert run_cli(
        parser.parse_args(["prepared", "save", str(payload_path), "--json"]), service
    ) == 0
    assert json.loads(capsys.readouterr().out)["drafted_description"] == "Owner copy."
    assert run_cli(parser.parse_args([
        "prepared", "review", "local:draft-1", "--allow-model-review", "--json"
    ]), service) == 0
    assert json.loads(capsys.readouterr().out)["professionalism_check"]["status"] == "pass"
    assert ("prepared_save", "local:draft-1", "Owner copy.", payload["files"],
            payload["expected_hashes"]) in service.calls


@pytest.mark.parametrize("duplicate", [
    '"draft_id":"local:first","draft_id":"local:last"',
    '"expected_hashes":{"content":"first","content":"last"}',
])
def test_cli_prepared_save_rejects_duplicate_json_keys(tmp_path, capsys, duplicate):
    parser = _parser()
    service = _Service()
    fields = {
        "draft_id": '"draft_id":"local:draft-1"',
        "author_description": '"author_description":"Owner copy."',
        "files": '"files":[]',
        "expected_hashes": '"expected_hashes":{}',
    }
    if duplicate.startswith('"draft_id"'):
        fields["draft_id"] = duplicate
    else:
        fields["expected_hashes"] = duplicate
    payload_path = tmp_path / "duplicate.json"
    payload_path.write_text("{" + ",".join(fields.values()) + "}", encoding="utf-8")

    assert run_cli(
        parser.parse_args(["prepared", "save", str(payload_path), "--json"]), service
    ) == 6
    assert "duplicate JSON key" in json.loads(capsys.readouterr().out)["error"]
    assert not any(call[0] == "prepared_save" for call in service.calls)


def test_cli_prepared_save_rejects_payload_above_derived_bound(tmp_path, capsys):
    parser = _parser()
    service = _Service()
    payload_path = tmp_path / "oversized.json"
    payload_path.write_bytes(b" " * (MAX_PREPARED_PAYLOAD_BYTES + 1))

    assert run_cli(
        parser.parse_args(["prepared", "save", str(payload_path), "--json"]), service
    ) == 6
    error = json.loads(capsys.readouterr().out)["error"]
    assert str(MAX_PREPARED_PAYLOAD_BYTES) in error
    assert not any(call[0] == "prepared_save" for call in service.calls)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_cli_prepared_save_rejects_non_regular_payload_without_opening(
    tmp_path: Path, capsys, kind: str
):
    parser = _parser()
    service = _Service()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "unsafe.json"
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    assert run_cli(
        parser.parse_args(["prepared", "save", str(path), "--json"]), service
    ) == 6
    assert "regular" in json.loads(capsys.readouterr().out)["error"]
    assert not any(call[0] == "prepared_save" for call in service.calls)


def test_cli_check_and_update_dispatch_exact_receipt_bound_actions(capsys):
    parser = _parser()
    service = _Service()

    assert run_cli(parser.parse_args(["check", "skill-1", "--json"]), service) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "update_available"
    assert run_cli(parser.parse_args([
        "update", "skill-1", "--plan", "--preserve-modified",
        "--accept-sensitive", "--accept-partial", "--json",
    ]), service) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["preserve_modified"] is True
    assert ("update_plan", "skill-1", {
        "preserve_modified": True,
        "accept_sensitive": True,
        "accept_partial": True,
    }) in service.calls
    assert run_cli(parser.parse_args([
        "update", "--apply-receipt", "wip_update", "--json"
    ]), service) == 0
    assert json.loads(capsys.readouterr().out)["updated"] is True
    assert ("update_apply", "wip_update") in service.calls


def test_update_apply_cannot_smuggle_new_plan_acknowledgements(capsys):
    parser = _parser()
    service = _Service()
    result = run_cli(parser.parse_args([
        "update", "--apply-receipt", "wip_update", "--accept-sensitive", "--json"
    ]), service)
    assert result == 6
    assert not service.calls
    assert "cannot change" in json.loads(capsys.readouterr().out)["error"]


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


def test_installed_setup_cli_dispatches_exact_review_and_recovery(capsys):
    parser = _parser()
    service = _Service()
    command = "printf 'exact '  "
    cases = [
        (["installed-setup", "inspect", "skill-1", "--version", "1", "--json"],
         ("installed_setup_inspect", "skill-1", 1)),
        (["installed-setup", "plan", "skill-1", "--version", "1", "--phase", "setup",
          "--index", "0", "--command", command, "--json"],
         ("installed_setup_plan", "skill-1", 1, "setup", 0, command)),
        (["installed-setup", "apply", "wip_setup", "--json"],
         ("installed_setup_apply", "wip_setup")),
        (["installed-setup", "status", "skill-1", "--json"],
         ("installed_setup_status", "skill-1", None)),
        (["installed-setup", "recover", "skill-1", "operation-1",
          "--acknowledge-effects-checked-and-stopped", "--json"],
         ("installed_setup_recover", "skill-1", "operation-1", True)),
    ]
    for argv, expected in cases:
        assert run_cli(parser.parse_args(argv), service) == 0
        json.loads(capsys.readouterr().out)
        assert expected in service.calls


def test_cli_json_is_sorted_and_help_has_machine_shape(capsys):
    service = _Service()
    service.status = lambda **_kwargs: {"z": 1, "a": 2}

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == 0
    output = capsys.readouterr().out
    assert output.index('"a"') < output.index('"z"')

    assert run_cli(_parser().parse_args(["help", "--json"]), service) == 0
    assert json.loads(capsys.readouterr().out)["commands"][-1] == "help"


def test_cli_sanitizes_human_scalar_output_without_changing_json_data(
    capsys, monkeypatch
):
    service = _Service()
    raw = "résumé 🦝\x1b[2J"
    monkeypatch.setattr(service, "status", lambda **_kwargs: raw)

    assert run_cli(_parser().parse_args(["status"]), service) == 0
    human_output = capsys.readouterr().out
    assert "résumé 🦝" in human_output
    assert "\x1b" not in human_output

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == 0
    assert json.loads(capsys.readouterr().out) == raw


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
    service.status = lambda **_kwargs: (_ for _ in ()).throw(error)

    assert run_cli(_parser().parse_args(["status", "--json"]), service) == error.exit_code
    assert json.loads(capsys.readouterr().out)["category"] == error.exit_code


def test_publication_terminal_commands_are_explicit_and_inspectable(capsys):
    parser = _parser()
    service = _Service()
    cases = [
        (["publication", "review", "local:draft", "--json"], ("publication_review", "local:draft")),
        (["publication", "submit", "wpub_review", "--json"], ("publication_submit", "wpub_review")),
        (["publication", "status", "op-1", "--json"], ("publication_status", "op-1")),
        (["publication", "reopen", "op-1", "--json"], ("publication_reopen", "op-1")),
        (["publication", "decline", "op-1", "--json"], ("publication_decline", "op-1")),
    ]
    for argv, expected in cases:
        assert run_cli(parser.parse_args(argv), service) == 0
        assert json.loads(capsys.readouterr().out)
        assert service.calls[-1] == expected


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


def test_slash_help_distinguishes_reads_private_reviews_and_terminal_instructions():
    service = _Service()
    output = run_slash("help", service)

    for command in ("browse", "show", "versions", "status", "setup", "help"):
        assert f"/wisdom {command}" in output
    for command in ("share", "review"):
        assert f"/wisdom {command}" not in output
    for command in ("candidate", "suggest", "prepared", "publish", "publication"):
        assert f"/wisdom {command} " in output
    for command in ("install", "installed", "check", "update", "uninstall"):
        assert f"/wisdom {command} (terminal instructions)" in output
    assert not service.calls


@pytest.mark.parametrize("command", ["check skill-1", "update skill-1 --plan"])
def test_slash_update_state_is_private_terminal_handoff(command):
    service = _Service()
    output = run_slash(command, service)
    assert "Use the terminal" in output
    assert not service.calls


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


def test_slash_browse_sanitizes_untrusted_text_and_navigation_controls():
    service = _Service()
    service.rows[0]["slug"] = "résumé-helper"
    service.rows[0]["author_description"] = "Keep Unicode 🦝\x1b[2J\x07"

    output = run_slash("browse 'café\x1b[31m'", service)

    assert "résumé-helper" in output
    assert "Keep Unicode 🦝" in output
    assert "café" in output
    assert "Next: /wisdom browse 'café [31m' --page 2" in output
    assert "\x1b" not in output
    assert "\x07" not in output


def test_slash_navigation_preserves_full_query():
    query = "café  " + "long-query" * 60
    service = _Service()
    output = run_slash(shlex.join(("browse", query)), service)
    next_command = next(line.removeprefix("Next: ") for line in output.splitlines()
                        if line.startswith("Next: "))
    assert shlex.split(next_command) == ["/wisdom", "browse", query, "--page", "2"]


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


def test_slash_drops_portal_links_with_pre_normalization_controls():
    service = _Service()
    detail = service.resolve_skill("skill-1")
    detail["portal_url"] = "https://example.org/\nspoof"
    service.resolve_skill = lambda reference: detail

    show = run_slash("show skill-1", service)
    versions = run_slash("versions skill-1", service)

    assert "View in Portal" not in show
    assert "View in Portal" not in versions


@pytest.mark.parametrize("command", ["share local"])
def test_slash_rejects_mutation_commands_without_service_calls(command):
    service = _Service()

    output = run_slash(command, service)

    assert output.startswith("Unsupported")
    assert not service.calls
    assert "/wisdom install (terminal instructions)" in output


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
