"""CLI/slash tests adapted from the source Wisdom parser and gateway controller."""

# pylint: disable=missing-function-docstring,too-many-lines,too-many-locals
# pylint: disable=missing-class-docstring,too-few-public-methods,invalid-name
# pylint: disable=import-outside-toplevel

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from installed_test_support import JsonState
from native_test_support import direct_owner as _native_owner
from review_test_support import prepared_inspection

from hermes_wisdom_plugin.commands import (
    MAX_PREPARED_PAYLOAD_BYTES,
    PAGE_SIZE,
    _human_render,
    build_parser,
    parse_slash,
    run_cli,
    run_slash as render_slash,
)
from hermes_wisdom_plugin.errors import (
    WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomUnavailable,
    WisdomValidationError,
)
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeReviewController, NativeView, callback_value,
)
from hermes_wisdom_plugin.native_slack import blocks as slack_blocks
from hermes_wisdom_plugin.native_slack import callback_invocation as slack_callback
from hermes_wisdom_plugin.native_telegram import callback_invocation as telegram_callback
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.review_state import ReviewState
from hermes_wisdom_plugin.runtime import CommandRuntime


def run_slash(raw_args, service):
    return render_slash(parse_slash(raw_args), service)


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


@pytest.mark.parametrize("command", ["settings fixed", "mute forever"])
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


class _Service:  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    def __init__(self):
        self.calls = []
        self.publication_result: dict[str, object] = {
            "operation_id": "op-1", "state": "done",
            "publication_state": "published",
        }
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
            "draft_id": "local:draft-1",
            "files": [
                {
                    "path": "SKILL.md", "mode": "file", "size": 12,
                    "hash": "sha256:skill", "content_utf8": "# Safe skill\n",
                },
                {"path": "assets/bad\x1b[2J.png", "mode": "file", "size": 4},
            ],
            "hashes": {
                "content": "sha256:content",
                "author_description": "sha256:description",
                "package_manifest": "sha256:manifest",
            },
            "description": "A carefully reviewed local skill.",
            "local_scan": {
                "guard": {
                    "allowed": False,
                    "findings": [{
                        "severity": "warning", "category": "instructions",
                        "pattern_id": "shell-download", "file": "SKILL.md",
                        "line": 7, "description": "Downloads content before use.",
                        "match": "SECRET_RAW_MATCH",
                    }],
                }
            },
            "security_check": {
                "local_status": "warning_required",
                "summary": "Review local scanner findings.",
            },
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
        return prepared_inspection(draft_id, "Owner copy.")

    def prepared_save(self, draft_id, *, author_description, files, expected_hashes):
        self.calls.append((
            "prepared_save", draft_id, author_description, files, expected_hashes
        ))
        return {"draft_id": draft_id, "drafted_description": author_description}

    def prepared_review(self, draft_id, *, allow_model_review=False):
        self.calls.append(("prepared_review", draft_id, allow_model_review))
        result = self.suggest("reviewed-skill", allow_model_review=allow_model_review)
        self.calls.pop()
        result["draft_id"] = draft_id
        return result

    def publication_review(self, draft_id, *, accept_warnings=False):
        self.calls.append(("publication_review", draft_id, accept_warnings))
        result = self.suggest("reviewed-skill")
        self.calls.pop()
        return {
            **result,
            "draft_id": draft_id,
            "receipt": "wpub_review",
            "publication_mode": "moderated",
            "warning_acceptance": {"accepted": accept_warnings},
        }

    def binding(self):
        return {
            "profile_name": "default",
            "profile_home": "/private/profile",
            "account_id": "member-42",
            "organization_id": "team-7",
            "base_url": "https://gateway.invalid",
        }

    def publication_submit(self, receipt):
        self.calls.append(("publication_submit", receipt))
        return dict(self.publication_result)

    def publication_status(self, operation_id):
        self.calls.append(("publication_status", operation_id))
        return {"operation_id": operation_id, "state": "done"}

    def publication_reopen(self, operation_id):
        self.calls.append(("publication_reopen", operation_id))
        return {"operation_id": operation_id, "publication_state": "pending_moderation"}

    def publication_enroll(self, skill_name):
        self.calls.append(("publication_enroll", skill_name))
        return {
            "operation_id": "internal-publication-operation", "state": "done",
            "publication_state": "published",
            "managed_enrollment": {
                "state": "managed", "skill_id": "skill-1", "version": 2,
                "effective_update_mode": "MANUAL",
                "detail": (
                    "reviewed-skill is now Managed v2. "
                    "Update mode: MANUAL (manual/review required)."
                ),
            },
            "detail": (
                "reviewed-skill is now Managed v2. "
                "Update mode: MANUAL (manual/review required)."
            ),
        }

    def publication_decline(self, operation_id):
        self.calls.append(("publication_decline", operation_id))
        return {"operation_id": operation_id, "publication_state": "declined"}

    def update_plan(self, skill_id, **values):
        self.calls.append(("update_plan", skill_id, values))
        accepted = values.get("accept_warnings") is True
        return {
            "skill_id": skill_id, "slug": "incident-handoff",
            "from_version": 1, "version": 2, "receipt": "wip_update",
            "owner": {"organization_id": "team-7", "account_id": "member-42"},
            "files": [{
                "path": "SKILL.md", "mode": "file", "size": 12,
                "hash": "sha256:update",
            }],
            "local_scan": {"allowed": False, "findings": [{
                "severity": "warning", "category": "instructions",
                "pattern_id": "update-warning", "file": "SKILL.md", "line": 3,
                "description": "Review changed instructions.", "match": "RAW_UPDATE",
            }]},
            "policy": {"warning_acceptance": {"accepted": accepted}},
            "allowed": accepted,
            **values,
        }

    def update_apply(self, receipt):
        self.calls.append(("update_apply", receipt))
        return {"updated": True, "receipt": receipt}

    def install_plan(self, reference, **values):
        self.calls.append(("install_plan", reference, values))
        return {
            "operation": "install", "skill_id": "skill-1",
            "slug": "incident-handoff", "version": 2,
            "receipt": "wip_install", "allowed": True,
            "local_scan": {"allowed": True, "findings": []},
            "compatibility": {"outcome": "compatible"},
            "policy": {},
        }

    def install_apply(self, receipt, *, accept_partial=False):
        self.calls.append(("install_apply", receipt, accept_partial))
        return {
            "installed": True, "skill_id": "skill-1",
            "skill_name": "incident-handoff", "version": 2,
        }

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

        "help": ["--json"],
    }
    for command, trailing in cases.items():
        args = parser.parse_args([command, *trailing])
        assert args.wisdom_command == command
        assert args.json is True
    assert "install" in parser.format_help()
    assert "check" in parser.format_help()
    assert "update" in parser.format_help()



def test_cli_suggest_defaults_to_model_route_but_honors_explicit_refusal(capsys):
    parser = _parser()
    service = _Service()

    assert run_cli(
        parser.parse_args(["suggest", "local-skill", "--json"]), service
    ) == 0
    local = json.loads(capsys.readouterr().out)
    assert local["professionalism_check"]["status"] == "unavailable"
    assert ("suggest", "local-skill", None) in service.calls

    assert run_cli(
        parser.parse_args(["suggest", "local-skill", "--no-model-review", "--json"]), service
    ) == 0
    capsys.readouterr()
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


def test_human_suggest_nontty_keeps_draft_and_prints_resume_without_prompt(
    capsys, monkeypatch
):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0

    output = capsys.readouterr().out
    assert "Draft prepared" in output
    assert "local:draft-1" not in output
    assert "prepared review local-skill" in output
    assert "hermes wisdom publication review local-skill" in output
    assert "\x1b" not in output
    assert not any(call[0].startswith("publication_") for call in service.calls)


def test_json_suggest_never_prompts_even_on_tty(capsys, monkeypatch):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))

    assert run_cli(
        _parser().parse_args(["suggest", "local-skill", "--json"]), service
    ) == 0
    assert json.loads(capsys.readouterr().out)["draft_id"] == "local:draft-1"
    assert not any(call[0].startswith("publication_") for call in service.calls)


def test_interactive_suggest_warning_requires_exact_ack_and_confirmation(
    capsys, monkeypatch
):
    service = _Service()
    answers = iter(("3", "yes", "yes"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0

    output = capsys.readouterr().out
    assert "Organization: team-7" not in output
    assert "Owner account: member-42" not in output
    assert "moderated" in output
    assert "shell-download" in output
    assert "Downloads content before use." in output
    assert "SECRET_RAW_MATCH" not in output
    assert ("publication_review", "local:draft-1", True) in service.calls
    assert ("publication_submit", "wpub_review") in service.calls


def test_interactive_suggest_uncertain_submission_is_nonzero_with_safe_recovery(
    capsys, monkeypatch
):
    service = _Service()
    service.publication_result = {
        "operation_id": "op-uncertain",
        "state": "uncertain",
        "phase": "draft_submission_unknown",
        "detail": "No unique exact owner draft is visible; nothing was retried.",
        "retry_safe": False,
    }
    answers = iter(("3", "yes", "yes"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 8

    output = capsys.readouterr().out
    assert "State: uncertain" in output
    assert "Phase: draft_submission_unknown" in output
    assert "No unique exact owner draft is visible; nothing was retried." in output
    assert "hermes wisdom publication status <skill-name>" in output
    assert "op-uncertain" not in output
    outcome = output.split("Publication result", 1)[1]
    assert "Files (0)" not in outcome
    assert "Nothing has been uploaded" not in outcome


def test_interactive_suggest_rejected_submission_uses_validation_exit(
    capsys, monkeypatch
):
    service = _Service()
    service.publication_result = {
        "operation_id": "op-rejected", "state": "rejected", "phase": "rejected",
        "detail": "Gateway rejected the publication draft before creation (HTTP 422).",
        "code": "instruction_only_violation", "retry_safe": False,
    }
    answers = iter(("3", "yes", "yes"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 6
    output = capsys.readouterr().out
    assert "State: rejected" in output
    assert "instruction_only_violation" not in output
    assert "Edit and re-review the local draft" in output


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(("state", "expected_exit"), [("rejected", 6), ("uncertain", 8)])
def test_publication_submit_reports_unsuccessful_exit(capsys, as_json, state, expected_exit):
    """Explicit receipt submission must not disguise failed effects as success."""
    service = _Service()
    service.publication_result = {"operation_id": "op-test", "state": state}
    argv = ["publication", "submit", "wpub_review"] + (["--json"] if as_json else [])

    assert run_cli(_parser().parse_args(argv), service) == expected_exit
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["state"] == state
    else:
        assert f"State: {state}" in output
    assert service.calls == [("publication_submit", "wpub_review")]


def test_publication_enroll_dispatches_local_skill_name(capsys):
    service = _Service()
    args = _parser().parse_args(
        ["publication", "enroll", "reviewed-skill", "--json"]
    )

    assert args.skill_name == "reviewed-skill"
    assert not hasattr(args, "operation_id")
    assert run_cli(args, service) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["operation_id"] == "internal-publication-operation"
    assert result["managed_enrollment"]["skill_id"] == "skill-1"
    assert result["managed_enrollment"]["state"] == "managed"
    assert result["managed_enrollment"]["effective_update_mode"] == "MANUAL"
    assert service.calls == [("publication_enroll", "reviewed-skill")]


def test_publication_enroll_human_output_leads_with_managed_outcome(capsys):
    service = _Service()

    assert run_cli(
        _parser().parse_args(["publication", "enroll", "reviewed-skill"]), service
    ) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "Publication result",
        "reviewed-skill is now Managed v2. Update mode: MANUAL (manual/review required).",
    ]
    assert "Operation: internal-publication-operation" not in lines
    assert not any(line.startswith("Remote draft:") for line in lines)


@pytest.mark.parametrize(
    "result",
    [
        {
            "operation_id": "op-pending", "state": "done",
            "publication_state": "pending_moderation",
        },
        {
            "operation_id": "op-unlinked", "state": "done",
            "publication_state": "published",
            "detail": "Published successfully, but the original source is not linked locally.",
            "managed_enrollment": {"state": "not_linked"},
        },
        {"operation_id": "op-uncertain", "state": "uncertain"},
        {"operation_id": "op-rejected", "state": "rejected"},
    ],
    ids=["pending-moderation", "not-linked", "uncertain", "rejected"],
)
def test_publication_nonmanaged_outcomes_never_claim_managed_success(result):
    assert "Managed v" not in _human_render("publication", result)


def test_interactive_suggest_cancel_has_no_publication_effect(capsys, monkeypatch):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "4")

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0
    assert "Draft kept" in capsys.readouterr().out
    assert not any(call[0].startswith("publication_") for call in service.calls)


def test_human_dict_output_is_command_specific_not_json(capsys):
    service = _Service()
    assert run_cli(_parser().parse_args(["check", "skill-1"]), service) == 0
    output = capsys.readouterr().out
    assert output.startswith("Update check")
    assert not output.lstrip().startswith("{")


@pytest.mark.parametrize("as_json", [False, True])
def test_explicit_install_command_applies_clean_plan_without_prompt(
    as_json, capsys, monkeypatch
):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))
    argv = ["install", "skill-1@v2"] + (["--json"] if as_json else [])

    assert run_cli(_parser().parse_args(argv), service) == 0

    assert ("install_plan", "skill-1@v2", {
        "update_mode": None, "accept_warnings": False,
    }) in service.calls
    assert ("install_apply", "wip_install", False) in service.calls
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["installed"] is True
    else:
        assert output.strip() == "✅ incident-handoff installed"


def test_install_plan_stays_read_only_and_human_handoff_uses_direct_command(capsys):
    service = _Service()

    assert run_cli(_parser().parse_args([
        "install", "skill-1@v2", "--plan", "--json",
    ]), service) == 0
    assert json.loads(capsys.readouterr().out)["receipt"] == "wip_install"
    assert not any(call[0] == "install_apply" for call in service.calls)
    assert "hermes wisdom install <skill-name>" in run_slash("install", service)
    assert "--plan" not in run_slash("install", service)


def test_clean_install_does_not_silently_accept_scanner_warnings(capsys):
    service = _Service()
    original = service.install_plan

    def warned(reference, **values):
        plan = original(reference, **values)
        plan["allowed"] = False
        plan["local_scan"] = {"allowed": False, "findings": [{
            "severity": "warning", "category": "instructions",
            "pattern_id": "review-me", "file": "SKILL.md",
            "description": "Review this exact instruction.",
        }]}
        return plan

    service.install_plan = warned
    assert run_cli(_parser().parse_args(["install", "skill-1@v2", "--json"]), service) == 6
    assert not any(call[0] == "install_apply" for call in service.calls)
    assert "warning" in json.loads(capsys.readouterr().out)["error"].casefold()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["list"], "incident-0"),
        (["browse", "incident"], "Description 0"),
        (["show", "skill-1"], "Summarize an incident"),
        (["versions", "skill-1"], "Version 7"),
        (["installed"], "example"),
    ],
)
def test_human_read_commands_render_substantive_rows(argv, expected, capsys):
    service = _Service()
    setattr(service, "list_installations", lambda: [
        {"skill_id": "opaque-example", "slug": "example", "version": 2}
    ])

    assert run_cli(_parser().parse_args(argv), service) == 0

    output = capsys.readouterr().out
    assert expected in output
    assert output.count("\n") > 1
    assert not output.lstrip().startswith(("{", "["))


def test_standalone_catalog_status_is_visible_in_human_and_json_output(capsys):
    service = _Service()
    service.rows[0]["installation_status"] = {
        "state": "installed", "text": "Installed v1 · update to v2 available",
        "installed_version": 1, "latest_version": 2,
    }

    assert run_cli(_parser().parse_args(["list"]), service) == 0
    assert "Installed v1 · update to v2 available" in capsys.readouterr().out

    assert run_cli(_parser().parse_args(["list", "--json"]), service) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["skills"][0]["installation_status"]["state"] == "installed"


def test_suggest_warning_without_safe_evidence_cannot_be_acknowledged(capsys, monkeypatch):
    service = _Service()
    original = service.suggest

    def no_evidence(*args, **kwargs):
        result = original(*args, **kwargs)
        result["local_scan"] = {"guard": {"allowed": False, "findings": []}}
        return result

    service.suggest = no_evidence
    service.prepared_review = lambda *_args, **_kwargs: no_evidence("review")
    answers = iter(("3",))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0

    output = capsys.readouterr().out
    assert "warning details are unavailable" in output.lower()
    assert not any(call[0] == "publication_review" for call in service.calls)


def test_suggest_review_menu_previews_text_and_keeps_binary_inert(capsys, monkeypatch):
    service = _Service()
    result = service.suggest("fixture")
    result["files"][1]["content_base64"] = "AAE="
    service.suggest = lambda *_args, **_kwargs: result
    answers = iter(("1", "1", "2", "", "4"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0

    output = capsys.readouterr().out
    assert "# Safe skill" in output
    assert "Binary/opaque file" in output
    assert "active preview is disabled" in output
    assert "SECRET_RAW_MATCH" not in output


def test_suggest_changed_findings_require_new_ack_before_final_consent(
    capsys, monkeypatch
):
    service = _Service()
    original_review = service.publication_review

    def changed_review(*args, **kwargs):
        result = original_review(*args, **kwargs)
        result["local_scan"]["guard"]["findings"][0].update({
            "pattern_id": "changed-warning",
            "description": "A newly observed safe detail.",
        })
        return result

    service.publication_review = changed_review
    answers = iter(("3", "yes", "no"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["suggest", "local-skill"]), service) == 0

    output = capsys.readouterr().out
    assert "changed-warning" in output
    assert "updated warnings were not accepted" in output
    assert not any(call[0] == "publication_submit" for call in service.calls)


def test_prepared_edit_uses_editor_copy_and_saves_complete_snapshot(
    tmp_path, capsys, monkeypatch
):
    service = _Service()
    editor = tmp_path / "fixture-editor"
    editor.write_text(
        "#!/bin/sh\nprintf '# After\\n' > \"$1\"\n", encoding="utf-8"
    )
    editor.chmod(0o700)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    assert run_cli(
        _parser().parse_args(["prepared", "edit", "local:draft-1"]), service
    ) == 0

    save = next(call for call in service.calls if call[0] == "prepared_save")
    assert save[1] == "local:draft-1"
    assert save[3][0] == {"path": "SKILL.md", "content_utf8": "# After\n"}
    assert save[3][1] == {"path": "assets/logo.bin", "content_base64": "AAE="}
    assert save[4]["content"] == "sha256:old-content"
    assert "Draft saved locally" in capsys.readouterr().out


def test_prepared_edit_json_never_starts_editor_or_prompts(capsys, monkeypatch):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("must not edit"))

    assert run_cli(
        _parser().parse_args(["prepared", "edit", "local:draft-1", "--json"]), service
    ) == 6
    assert json.loads(capsys.readouterr().out)["category"] == 6


def test_prepared_edit_rejects_editor_symlink_replacement(tmp_path, capsys, monkeypatch):
    service = _Service()
    editor = tmp_path / "fixture-editor"
    editor.write_text(
        "#!/bin/sh\nrm \"$1\"\nln -s /etc/hosts \"$1\"\n", encoding="utf-8"
    )
    editor.chmod(0o700)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    assert run_cli(
        _parser().parse_args(["prepared", "edit", "local:draft-1"]), service
    ) == 6

    assert "unsafe" in capsys.readouterr().err
    assert not any(call[0] == "prepared_save" for call in service.calls)


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
        "accept_warnings": False,
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


def test_interactive_update_replans_warning_then_requires_final_confirmation(
    capsys, monkeypatch
):
    service = _Service()
    answers = iter(("yes", "yes"))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert run_cli(_parser().parse_args(["update", "skill-1"]), service) == 0

    output = capsys.readouterr().out
    assert "update-warning" in output
    assert "Review changed instructions." in output
    assert "RAW_UPDATE" not in output
    plans = [call for call in service.calls if call[0] == "update_plan"]
    assert plans[0][2]["accept_warnings"] is False
    assert plans[1][2]["accept_warnings"] is True
    assert service.calls[-1] == ("update_apply", "wip_update")


@pytest.mark.parametrize(
    "argv",
    [
        ["setup", "--json"],
        ["uninstall", "skill-1", "--json"],
    ],
)
def test_json_mutations_never_prompt_on_tty_without_explicit_switch(
    argv, capsys, monkeypatch
):
    service = _Service()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("must not prompt"))

    assert run_cli(_parser().parse_args(argv), service) == 6
    assert json.loads(capsys.readouterr().out)["category"] == 6


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


def test_human_installed_setup_plan_shows_exact_inert_command_and_next_action(capsys):
    service = _Service()
    command = "printf 'exact '  \n"

    assert run_cli(_parser().parse_args([
        "installed-setup", "plan", "skill-1", "--version", "1",
        "--phase", "setup", "--index", "0", "--command", command,
    ]), service) == 0

    output = capsys.readouterr().out
    assert "Command (escaped literal; not executed by review)" in output
    assert "printf 'exact '  \\n" in output
    assert "wip_setup" not in output
    assert service.calls == [
        ("installed_setup_plan", "skill-1", 1, "setup", 0, command)
    ]


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
    raw = "résumé 🦝\x1b[2J\u202espoof"
    monkeypatch.setattr(service, "status", lambda **_kwargs: raw)

    assert run_cli(_parser().parse_args(["status"]), service) == 0
    human_output = capsys.readouterr().out
    assert "résumé 🦝" in human_output
    assert "\x1b" not in human_output
    assert "\u202e" not in human_output

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

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert run_cli(_parser().parse_args(["setup"]), service) == 0
    assert "Candidate signals stay on this profile" in capsys.readouterr().out
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


@pytest.mark.parametrize("argv,method,retry_safe", [
    (["status"], "status", True),
    (["list"], "list_skills", True),
    (["publication", "status", "op-1"], "publication_status", True),
    (["publication", "submit", "wpub_review"], "publication_submit", False),
    (["install", "--apply-receipt", "wip_install"], "install_apply", False),
])
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_unavailable_retry_guidance(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    argv, method, retry_safe, as_json, capsys, monkeypatch
):
    service = _Service()
    attempts = []

    def unavailable(*_args, **_kwargs):
        attempts.append(1)
        raise WisdomUnavailable(status=503)

    monkeypatch.setattr(service, method, unavailable)
    assert run_cli(_parser().parse_args(argv + (["--json"] if as_json else [])), service) == 8
    output = capsys.readouterr()
    assert len(attempts) == 1
    if as_json:
        assert not output.err
        assert json.loads(output.out) == {
            "ok": False, "error": "Collective Wisdom is temporarily unavailable.",
            "category": 8,
        }
    else:
        assert not output.out
        assert "Collective Wisdom is temporarily unavailable." in output.err
        assert ("Retry this read-only command later." in output.err) == retry_safe
        assert ("Check the current status before retrying" in output.err) != retry_safe
        assert "sign in" not in output.err.lower()


@pytest.mark.parametrize("argv,retry_safe", [
    (["notifications"], True),
    (["notifications", "--mark-seen"], False),
    (["settings", "status"], True),
    (["settings", "fixed"], False),
    (["mute", "status"], True),
    (["mute", "forever"], False),
    (["prepared", "inspect", "local:draft"], True),
    (["prepared", "review", "local:draft"], False),
    (["installed-setup", "inspect", "skill-1"], True),
    (["installed-setup", "apply", "wip_setup"], False),
])
def test_cli_unavailable_nested_and_option_retry_safety(argv, retry_safe, capsys):
    class FailingService:
        def __getattr__(self, _name):
            def unavailable(*_args, **_kwargs):
                raise WisdomUnavailable()
            return unavailable

    assert run_cli(_parser().parse_args(argv), FailingService()) == 8
    output = capsys.readouterr()
    assert not output.out
    assert ("Retry this read-only command later." in output.err) is retry_safe
    assert ("Check the current status before retrying" in output.err) is not retry_safe


def test_publication_terminal_commands_are_explicit_and_inspectable(capsys):
    parser = _parser()
    service = _Service()
    cases = [
        (["publication", "review", "local:draft", "--json"],
         ("publication_review", "local:draft", False)),
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
        assert f"/wisdom {command}" in output
    assert "Terminal:" in output
    assert not service.calls


def test_slash_help_is_bounded_for_an_80_by_24_dialog():
    """Plugin copy leaves room for the host dialog border and close hint."""

    lines = run_slash("help", _Service()).splitlines()

    assert len(lines) <= 16
    assert max(map(len, lines)) <= 72


def test_prepared_human_output_shell_quotes_every_draft_continuation(capsys):
    """Both displayed commands remain one inert argv value for hostile draft IDs."""

    service = _Service()
    draft_id = "draft with spaces;echo nope"
    args = _parser().parse_args(["prepared", "review", draft_id])

    assert run_cli(args, service) == 0
    output = capsys.readouterr().out
    assert "prepared edit reviewed-skill" in output
    assert "prepared inspect reviewed-skill --json" in output
    assert draft_id not in output


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
    ):
        assert private not in output
    assert "Installation status unavailable" in output
    assert "Review install" not in output
    assert "Install:" not in output


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
    assert "Terminal: /wisdom install" in output


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


@pytest.mark.parametrize("raw, method", [
    ("status", "status"),
    ("list", "search_skills"),
    ('browse "incident handoff" --page 2', "search_skills"),
    ("show skill-1", "resolve_skill"),
    ("versions skill-1 --page 2", "resolve_skill"),
])
def test_slash_transient_read_offers_original_manual_retry(raw, method):
    service = _Service()
    parsed = parse_slash(raw)
    assert parsed is not None
    command, args = parsed
    setattr(service, method, lambda *_args, **_kwargs: (_ for _ in ()).throw(
        WisdomUnavailable(status=503)
    ))

    output = run_slash(raw, service)

    assert output == (
        "Collective Wisdom is temporarily unavailable.\n"
        f"Run /wisdom {shlex.join((command, *args))} again to retry."
    )
    assert "Sign in" not in output
    assert len([call for call in service.calls if call[0] == "require_setup"]) <= 1


def test_slash_auth_failure_does_not_offer_transient_retry():
    service = _Service()
    service.require_setup = lambda: (_ for _ in ()).throw(WisdomAuthError("private"))

    output = run_slash("browse incident", service)

    assert "Sign in again" in output
    assert "again to retry" not in output


def _native_invocation(platform="slack", *, adapter=None, message_id="command-1"):
    return NativeInvocation(
        platform=platform, profile="default", chat_type="private",
        chat_id="C1" if platform == "slack" else "123", actor_id="native-user",
        actor_name="Shannon", thread_id="", scope_id="W1" if platform == "slack" else "",
        message_id=message_id, session_key=f"{platform}:private", adapter=adapter or object(),
    )


class _NativeSuggestService(_Service):
    def native_owner(self):
        return _native_owner()

    def close(self):
        return None

    def publication_submit(self, receipt, *, may_apply=None):
        assert may_apply is not None and may_apply() is True
        return super().publication_submit(receipt)


def _act_displayed(controller, record_id, action, service, caller):
    """Simulate the provider acknowledgement between native card transitions."""
    result = controller.act(record_id, action, service, caller)
    with controller.displaying(result):
        pass
    return controller.load(record_id)


def test_admitted_native_suggest_dispatches_prepare_review_not_handoff(tmp_path, monkeypatch):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    invocation = _native_invocation()
    monkeypatch.setattr(runtime.native, "consume_request", lambda raw: invocation)
    seen = []
    monkeypatch.setattr(
        runtime.native, "begin_suggest",
        lambda caller, skill: seen.append((caller, skill)) or {"state": "queued"},
        raising=False,
    )

    try:
        assert runtime.slash("suggest local-skill") == ""
        assert seen == [(invocation, "local-skill")]
    finally:
        runtime.close()


def test_admitted_slash_install_directly_applies_and_sends_result(tmp_path, monkeypatch):
    async def scenario():
        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
        )
        caller = _native_invocation()
        calls = []
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: caller)
        monkeypatch.setattr(
            runtime.native, "install_direct",
            lambda invocation, name: calls.append((invocation, name))
            or NativeView("Collective Wisdom", "✅ incident installed", dismissed=True),
        )

        async def send(invocation, view):
            calls.append((invocation, view.summary))

        monkeypatch.setattr(runtime.native, "send_direct_result", send)
        try:
            response = runtime.slash("install incident")
            assert inspect.isawaitable(response)
            assert await response == ""
            assert calls == [(caller, "incident"), (caller, "✅ incident installed")]
        finally:
            runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["telegram", "slack"])
@pytest.mark.parametrize("command_state", ["passed", "pending"])
def test_repeat_native_install_distinguishes_setup_execution_from_readiness(
    tmp_path, monkeypatch, platform, command_state,
):
    class AlreadyInstalled:
        def __init__(self):
            self.calls = []

        def native_owner(self):
            return _native_owner()

        def install_plan(self, reference):
            self.calls.append(("plan", reference))
            raise PackagePolicyError("skill is already managed; use the update workflow")

        def list_installations(self):
            self.calls.append(("installed",))
            return [{"slug": "incident", "skill_id": "skill-1", "state": "active", "version": 1}]

        def installed_setup_inspect(self, skill_id, *, version=None):
            self.calls.append(("inspect", skill_id, version))
            return {
                "slug": "incident", "files_installed": True, "state": "setup_required",
                "ready_to_use": None, "version": 1,
                "setup_progress": ([{"phase": "setup", "index": 0, "state": "passed"}]
                                   if command_state == "passed" else []),
                "verification": {"state": "not_recorded"},
                "guidance": {
                    "setup_commands": ["printf 'setup ok'"],
                    "verification_step": "Check that the local service responds.",
                },
            }

        def close(self):
            pass

    async def scenario():
        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
        )
        caller = _native_invocation(platform)
        service = AlreadyInstalled()
        views = []
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: caller)
        monkeypatch.setattr(runtime.native, "service_factory", lambda: service)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            runtime.native, "_direct_route", lambda _caller: (object(), object(), loop)
        )
        monkeypatch.setattr(runtime.native, "_direct_authorized", lambda *_args: True)

        async def send(_caller, view):
            views.append(view)

        monkeypatch.setattr(runtime.native, "send_direct_result", send)
        try:
            response = runtime.slash("install incident")
            assert inspect.isawaitable(response)
            assert await response == ""
            assert len(views) == 1
            text = views[0].to_text()
            assert "incident" in text
            assert "already installed" in text
            if command_state == "passed":
                assert "Setup ran successfully" in text
                assert "readiness is not verified" in text
                assert "setup incomplete" not in text.casefold()
                assert "Setup commands passed; verification not recorded." in text
            else:
                assert "setup incomplete" in text.casefold()
                assert "Setup ran successfully" not in text
            assert "Check that the local service responds." in text
            assert "uncertain" not in text.casefold()
            assert service.calls == [
                ("plan", "incident"), ("installed",), ("inspect", "skill-1", 1),
            ]
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_admitted_install_with_setup_sends_only_the_bound_review(tmp_path, monkeypatch):
    """A queued setup review is the reply, not an extra success card."""
    async def scenario():
        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
        )
        caller = _native_invocation()
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: caller)
        monkeypatch.setattr(runtime.native, "install_direct", lambda *_args: {
            "state": "queued", "review_id": "internal-receipt",
        })

        async def unexpected_send(*_args):
            pytest.fail("do not send a second result before reviewed setup")

        monkeypatch.setattr(runtime.native, "send_direct_result", unexpected_send)
        try:
            response = runtime.slash("install incident")
            assert inspect.isawaitable(response)
            assert await response == ""
        finally:
            runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw, method", [("browse", "begin_catalog")])
def test_admitted_native_failure_does_not_fall_through_to_unknown_command(
    tmp_path, monkeypatch, raw, method,
):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def unavailable(*_args):
        raise RuntimeError("internal review failure")

    try:
        monkeypatch.setattr(runtime.native, method, unavailable)
        assert runtime.slash(raw) == "Couldn't open Collective Wisdom right now. Please try again."
        monkeypatch.setattr(runtime.native, method, lambda *_args: {"state": "unavailable"})
        assert runtime.slash(raw) == "Couldn't open Collective Wisdom right now. Please try again."
    finally:
        runtime.close()


def test_direct_install_failure_reports_uncertain_outcome_not_unknown_command(
    tmp_path, monkeypatch,
):
    async def scenario():
        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
        )
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

        def fail(*_args):
            raise RuntimeError("internal error")

        monkeypatch.setattr(runtime.native, "install_direct", fail)
        try:
            response = runtime.slash("install incident")
            assert inspect.isawaitable(response)
            assert await response == (
                "Couldn't confirm the installation. Check installed skills before trying again."
            )
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_direct_install_card_failure_reports_uncertain_outcome(tmp_path, monkeypatch):
    async def scenario():
        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
        )
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())
        calls = []

        def install(*_args):
            calls.append("install")
            return NativeView("Collective Wisdom", "✅ incident installed", dismissed=True)

        async def failed_send(*_args):
            raise RuntimeError("could have reached Slack")

        monkeypatch.setattr(runtime.native, "install_direct", install)
        monkeypatch.setattr(runtime.native, "send_direct_result", failed_send)
        try:
            response = runtime.slash("install incident")
            assert inspect.isawaitable(response)
            assert await response == (
                "Couldn't confirm the installation. Check installed skills before trying again."
            )
            assert calls == ["install"]
        finally:
            runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["suggest", "suggest one two"])
def test_admitted_native_suggest_rejects_missing_or_ambiguous_name(tmp_path, monkeypatch, raw):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())
    monkeypatch.setattr(
        runtime.native, "begin_suggest",
        lambda *_args: pytest.fail("invalid suggest must not prepare a draft"),
    )
    try:
        assert runtime.slash(raw) == "Usage: /wisdom suggest <local-skill>"
    finally:
        runtime.close()


def test_native_suggest_prepare_inspect_keep_and_separate_share_confirmation(tmp_path):
    service = _NativeSuggestService()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()

    prepared = controller.request_suggest(service, caller, "local-skill")
    assert service.calls == [("suggest", "local-skill", False)]
    assert prepared["operation"] == "suggest"
    view = controller.view(prepared)
    assert view.summary == "local-skill — prepared locally"
    assert "Nothing has been uploaded. The original skill is unchanged." in view.to_text()
    assert [action.label for action in view.actions] == [
        "Review files", "Review checks", "Re-review edited draft",
        "Continue to sharing review", "Keep draft", "Cancel",
    ]

    controller.bind_message(prepared["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    files = _act_displayed(controller, prepared["id"], "suggest.files", service, clicked)
    assert files["state"] == "pending"
    assert "SKILL.md" in controller.view(files).to_text()
    back = _act_displayed(controller, prepared["id"], "suggest.back", service, clicked)
    checks = _act_displayed(controller, prepared["id"], "suggest.checks", service, clicked)
    assert "shell-download" in controller.view(checks).to_text()
    _act_displayed(controller, prepared["id"], "suggest.back", service, clicked)
    warning_review = _act_displayed(
        controller, prepared["id"], "suggest.share", service, clicked
    )
    assert back["plan"]["native_stage"] == "prepared"
    assert warning_review["plan"]["native_stage"] == "warnings"
    assert "shell-download" in controller.view(warning_review).to_text()
    assert not any(call[0] == "publication_review" for call in service.calls)
    assert [action.label for action in controller.view(warning_review).actions] == [
        "Acknowledge displayed warnings and continue", "Back", "Cancel",
    ]
    exact = _act_displayed(controller, prepared["id"], "suggest.ack", service, clicked)
    assert exact["plan"]["native_stage"] == "sharing"
    assert ("prepared_review", "local:draft-1", False) in service.calls
    assert ("publication_review", "local:draft-1", True) in service.calls
    assert not any(call[0] == "publication_submit" for call in service.calls)
    assert [action.label for action in controller.view(exact).actions] == [
        "Share this exact reviewed package", "Back", "Cancel",
    ]
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(prepared["id"], "suggest.files", service, clicked)
    assert controller.load(prepared["id"])["plan"]["native_stage"] == "sharing"

    completed = controller.act(prepared["id"], "suggest.confirm", service, clicked)
    assert completed["state"] == "completed"
    assert service.calls[-1] == ("publication_submit", "wpub_review")
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(prepared["id"], "suggest.confirm", service, clicked)


def test_native_suggest_cancel_keep_wrong_route_expiry_and_changed_review_fail_closed(
    tmp_path, monkeypatch
):
    now = [10.0]
    service = _NativeSuggestService()
    controller = NativeReviewController(
        ReviewState(JsonState(tmp_path), tmp_path), clock=lambda: now[0], ttl_seconds=5,
    )
    caller = _native_invocation()
    first = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(first["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(first["id"], "suggest.share", service, replace(clicked, actor_id="other"))
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(first["id"], "suggest.share", service, replace(clicked, profile="other"))
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(first["id"], "suggest.share", service, replace(clicked, message_id="card-2"))
    kept = controller.act(first["id"], "suggest.keep", service, clicked)
    assert kept["state"] == "kept"
    assert not any(call[0] == "publication_submit" for call in service.calls)

    second = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(second["id"], caller, "card-2")
    cancelled = controller.act(
        second["id"], "cancel", service, replace(caller, message_id="card-2")
    )
    assert cancelled["state"] == "cancelled"
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(
            second["id"], "cancel", service, replace(caller, message_id="card-2")
        )

    third = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(third["id"], caller, "card-3")
    now[0] = 20.0
    with pytest.raises(WisdomAuthError, match="expired"):
        controller.act(
            third["id"], "suggest.share", service, replace(caller, message_id="card-3")
        )

    changed = _NativeSuggestService()
    original = changed.publication_review
    def changed_review(*args, **kwargs):
        result = original(*args, **kwargs)
        result["hashes"]["content"] = "sha256:changed"
        return result
    monkeypatch.setattr(changed, "publication_review", changed_review)
    fresh = NativeReviewController(ReviewState(
        JsonState(tmp_path / "changed"), tmp_path / "changed",
    ))
    record = fresh.request_suggest(changed, caller, "local-skill")
    fresh.bind_message(record["id"], caller, "card-4")
    with pytest.raises(WisdomConflict, match="changed"):
        _act_displayed(
            fresh, record["id"], "suggest.share", changed,
            replace(caller, message_id="card-4"),
        )
        fresh.act(record["id"], "suggest.ack", changed, replace(caller, message_id="card-4"))
    assert not any(call[0] == "publication_submit" for call in changed.calls)


def test_native_suggest_large_review_has_bounded_same_draft_continuation(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest
    def large(*args, **kwargs):
        result = original(*args, **kwargs)
        result["files"] = [
            {"path": f"references/{index:03d}.md", "mode": "file", "size": 5000,
             "hash": f"sha256:{index}", "content_utf8": "x" * 5000}
            for index in range(100)
        ]
        return result
    service.suggest = large
    service.prepared_review = lambda *_args, **_kwargs: large("review")
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "large-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    files = _act_displayed(controller, record["id"], "suggest.files", service, clicked)
    first_page = controller.view(files)
    assert len(first_page.to_text()) <= 3500
    assert any(action.action == "suggest.files.next" for action in first_page.navigation_actions)
    second_page = _act_displayed(controller, record["id"], "suggest.files.next", service, clicked)
    while second_page["plan"]["native_page"] == 0:
        second_page = _act_displayed(
            controller, record["id"], "suggest.files.next", service, clicked,
        )
    assert second_page["plan"]["native_page"] == 1
    first_again = _act_displayed(controller, record["id"], "suggest.files.prev", service, clicked)
    assert first_again["plan"]["native_page"] == 0

    _act_displayed(controller, record["id"], "suggest.back", service, clicked)
    _act_displayed(controller, record["id"], "suggest.share", service, clicked)
    sharing = _act_displayed(controller, record["id"], "suggest.ack", service, clicked)
    view = controller.view(sharing)
    assert not any(action.primary for action in view.actions)
    assert "large-skill" in view.to_text()
    assert "local:draft-1" not in view.to_text()
    assert "same prepared draft" in view.notice
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "suggest.confirm", service, clicked)
    assert not any(call[0] == "publication_submit" for call in service.calls)


def test_native_suggest_re_review_refreshes_exact_edited_draft(tmp_path):
    """A native card can refresh a terminal-edited draft before sharing review."""

    service = _NativeSuggestService()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    original = service.prepared_review

    def edited(*args, **kwargs):
        result = original(*args, **kwargs)
        result["hashes"]["content"] = "sha256:edited"
        result["files"][0]["content_utf8"] = "# Edited\n"
        return result

    service.prepared_review = edited

    refreshed = controller.act(
        record["id"], "suggest.refresh", service,
        replace(caller, message_id="card-1"),
    )

    assert refreshed["plan"]["hashes"]["content"] == "sha256:edited"
    assert refreshed["plan"]["files"][0]["content_utf8"] == "# Edited\n"
    assert refreshed["plan"]["native_stage"] == "prepared"
    assert not any(call[0].startswith("publication_") for call in service.calls)


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"draft_id": "local:other"},
        {"draft_id": "local:draft-1", "local_draft_id": "local:other"},
    ],
    ids=["missing", "swapped", "conflicting"],
)
def test_native_suggest_refresh_rejects_unbound_draft_identity(tmp_path, identity):
    """A malformed refresh cannot replace state or become submission authority."""

    service = _NativeSuggestService()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    before = controller.load(record["id"])
    original = service.prepared_review

    def wrong_identity(*args, **kwargs):
        result = original(*args, **kwargs)
        result.pop("draft_id", None)
        result.pop("local_draft_id", None)
        result.update(identity)
        return result

    service.prepared_review = wrong_identity
    clicked = replace(caller, message_id="card-1")

    with pytest.raises(WisdomConflict, match="draft changed"):
        controller.act(record["id"], "suggest.refresh", service, clicked)
    assert controller.load(record["id"]) == before

    with pytest.raises(WisdomConflict, match="draft changed"):
        controller.act(record["id"], "suggest.share", service, clicked)
    assert controller.load(record["id"]) == before
    assert not any(call[0].startswith("publication_") for call in service.calls)


def test_native_file_navigation_labels_parts_and_file_boundaries_truthfully(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest

    def two_files(*args, **kwargs):
        result = original(*args, **kwargs)
        result["files"] = [
            {"path": "first.md", "content_utf8": "a" * 600},
            {"path": "second.md", "content_utf8": "second"},
        ]
        return result

    service.suggest = two_files
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "two-files")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    current = _act_displayed(controller, record["id"], "suggest.files", service, clicked)
    assert [action.label for action in controller.view(current).navigation_actions] == [
        "Back", "Next part",
    ]
    current = _act_displayed(controller, record["id"], "suggest.files.next", service, clicked)
    assert [action.label for action in controller.view(current).navigation_actions] == [
        "Previous part", "Back", "Next file",
    ]
    current = _act_displayed(controller, record["id"], "suggest.files.next", service, clicked)
    assert [action.label for action in controller.view(current).navigation_actions] == [
        "Previous file", "Back",
    ]
    current = _act_displayed(controller, record["id"], "suggest.files.prev", service, clicked)
    assert [action.label for action in controller.view(current).navigation_actions] == [
        "Previous part", "Back", "Next file",
    ]


def test_native_file_preview_distinguishes_empty_opaque_and_sanitized_content(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest

    def unusual_files(*args, **kwargs):
        result = original(*args, **kwargs)
        result["files"] = [
            {"path": "empty.txt", "content_utf8": "", "size": 0},
            {"path": "opaque.bin", "content_base64": "AAE=", "size": 2},
            {"path": "controls.txt", "content_utf8": "before\x00\x1b[2Jafter"},
        ]
        return result

    service.suggest = unusual_files
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "unusual-files")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    current = _act_displayed(controller, record["id"], "suggest.files", service, clicked)
    assert "Empty UTF-8 file" in controller.view(current).to_text()

    current = _act_displayed(controller, record["id"], "suggest.files.next", service, clicked)
    assert "Binary/opaque file" in controller.view(current).to_text()

    current = _act_displayed(controller, record["id"], "suggest.files.next", service, clicked)
    rendered = controller.view(current).to_text()
    assert "Sanitized display" in rendered
    assert "control bytes were replaced" in rendered
    assert "exact bytes" in rendered
    assert "\x00" not in rendered
    assert "\x1b" not in rendered



def test_native_large_text_file_is_completely_reachable_without_terminal_handoff(tmp_path):
    """Native chunk navigation reaches the exact tail of an oversized text file."""

    service = _NativeSuggestService()
    original = service.suggest

    def large(*args, **kwargs):
        result = original(*args, **kwargs)
        result["files"] = [{
            "path": "references/large.md", "mode": "file", "size": 6000,
            "hash": "sha256:large", "content_utf8": "START\n" + "&" * 6000 + "\nTAIL",
        }]
        return result

    service.suggest = large
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "large-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    current = _act_displayed(controller, record["id"], "suggest.files", service, clicked)
    observed = controller.view(current).to_text()
    assert "…" not in str(slack_blocks(controller.view(current), record["id"]))

    while any(
        action.action == "suggest.files.next"
        for action in controller.view(current).navigation_actions
    ):
        current = _act_displayed(
            controller, record["id"], "suggest.files.next", service, clicked,
        )
        observed += "\n" + controller.view(current).to_text()
        assert "…" not in str(slack_blocks(controller.view(current), record["id"]))

    assert "START" in observed
    assert "TAIL" in observed
    assert "export full bytes" not in observed
    assert len(controller.view(current).to_text()) <= 3500


def test_slack_truncated_review_drops_primary_control():
    view = NativeView(
        "Collective Wisdom sharing review", "Review before sharing",
        [NativeItem("Exact package", "&" * 2890)],
        actions=[
            NativeAction("Share this exact reviewed package", "suggest.confirm", primary=True),
            NativeAction("Cancel", "cancel", destructive=True),
        ],
    )

    rendered = slack_blocks(view, "record-1")

    buttons = [
        element for block in rendered if block.get("type") == "actions"
        for element in block.get("elements", [])
    ]
    assert not any(button.get("style") == "primary" for button in buttons)
    assert not any("suggest.confirm" in button.get("value", "") for button in buttons)


def test_native_entity_heavy_warning_requires_terminal_review(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest

    def entity_heavy(*args, **kwargs):
        result = original(*args, **kwargs)
        result["local_scan"]["guard"]["findings"][0]["description"] = "&" * 1000
        result["local_scan"]["guard"]["findings"][0]["file"] = "&" * 500
        return result

    service.suggest = entity_heavy
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "entity-heavy")
    controller.bind_message(record["id"], caller, "card-1")

    warning = controller.act(
        record["id"], "suggest.share", service, replace(caller, message_id="card-1")
    )
    view = controller.view(warning)

    assert not any(action.primary for action in view.actions)
    assert "cannot be acknowledged here" in view.notice


def test_native_missing_warning_evidence_blocks_acknowledgement(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest

    def missing_details(*args, **kwargs):
        result = original(*args, **kwargs)
        result["local_scan"]["guard"]["findings"] = []
        return result

    service.suggest = missing_details
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "missing-evidence")
    controller.bind_message(record["id"], caller, "card-1")

    with pytest.raises(WisdomValidationError, match="warning details are unavailable"):
        controller.act(
            record["id"], "suggest.share", service,
            replace(caller, message_id="card-1"),
        )
    assert not any(call[0].startswith("publication_") for call in service.calls)


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_native_suggest_callback_adapters_preserve_actor_profile_and_message(
    tmp_path, platform
):
    service = _NativeSuggestService()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    bot = object()
    if platform == "slack":
        class Adapter:
            _owner_profile = "default"
            @staticmethod
            def _is_interactive_user_authorized(user_id, **_kwargs):
                return user_id == "native-user"
        adapter = Adapter()
        caller = _native_invocation("slack", adapter=adapter)
        record = controller.request_suggest(service, caller, "local-skill")
        controller.bind_message(record["id"], caller, "card-1")
        callback = slack_callback(adapter, {
            "user": {"id": "native-user", "name": "Shannon"},
            "channel": {"id": "C1"}, "team": {"id": "W1"},
            "message": {"ts": "card-1"}, "container": {"message_ts": "card-1"},
        }, record)
    else:
        class Adapter:
            _owner_profile = "default"
            _bot = bot
            @staticmethod
            def _is_callback_user_authorized(user_id, **_kwargs):
                return user_id == "native-user"
        adapter = Adapter()
        caller = _native_invocation("telegram", adapter=adapter)
        record = controller.request_suggest(service, caller, "local-skill")
        controller.bind_message(record["id"], caller, "77")
        query = SimpleNamespace(
            from_user=SimpleNamespace(id="native-user", username="Shannon", first_name=""),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private"), chat_id=123,
                message_thread_id=None, message_id=77, get_bot=lambda: bot,
            ),
        )
        callback = telegram_callback(adapter, query, record)
    assert callback.actor_id == "native-user"
    assert callback.profile == "default"
    assert callback.message_id in {"card-1", "77"}
    assert controller.act(record["id"], "suggest.files", service, callback)["state"] == "pending"


def test_slack_actual_dispatch_and_callbacks_require_warning_ack_then_confirmation(
    tmp_path, monkeypatch
):
    async def scenario():
        service = _NativeSuggestService()
        pending = []
        posts, edits = [], []

        class Adapter:
            _owner_profile = "default"
            @staticmethod
            def _is_interactive_user_authorized(user_id, **_kwargs):
                return user_id == "native-user"

        class Client:
            async def chat_postMessage(self, **payload):
                posts.append(payload)
                return {"ok": True, "channel": "C1", "ts": "card-1"}
            async def chat_update(self, **payload):
                edits.append(payload)
                return {"ok": True}

        class App:
            def __init__(self):
                self.client, self.handler = Client(), None
            def action(self, _pattern):
                def register(handler):
                    self.handler = handler
                    return handler
                return register

            def command(self, _pattern):
                return lambda handler: handler

        def spawn(coroutine, **_kwargs):
            pending.append(coroutine)

        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=spawn,
        )
        app, adapter = App(), Adapter()
        runtime.native.wire_slack(app, adapter)
        caller = _native_invocation("slack", adapter=adapter)
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: caller)
        monkeypatch.setattr(runtime.native, "service_factory", lambda: service)

        def value(payload, label):
            buttons = [element for block in payload["blocks"] if block["type"] == "actions"
                       for element in block["elements"]]
            return next(button["value"] for button in buttons if button["text"]["text"] == label)

        async def click(label, *, actor="native-user", message="card-1"):
            payload = edits[-1] if edits else posts[-1]
            acknowledgements = []
            async def ack():
                acknowledgements.append(True)
            body = {
                "user": {"id": actor, "name": "Shannon"},
                "channel": {"id": "C1"}, "team": {"id": "W1"},
                "message": {"ts": message}, "container": {"message_ts": message},
            }
            await app.handler(ack, body, {"value": value(payload, label)})
            assert acknowledgements == [True]

        try:
            result = runtime.slash("suggest local-skill")
            assert result == ""
            review_id = next(iter(runtime.native.reviews.state._load()["records"]))  # pylint: disable=protected-access
            await pending.pop(0)
            assert runtime.native.reviews.load(review_id)["message_id"] == "card-1"
            assert "Nothing has been uploaded" in posts[0]["text"]
            assert not any(call[0].startswith("publication_") for call in service.calls)

            await click("Continue to sharing review")
            assert "shell-download" in edits[-1]["text"]
            assert not any(call[0] == "publication_review" for call in service.calls)
            await click("Acknowledge displayed warnings and continue")
            assert "Sharing review" in edits[-1]["text"]
            assert not any(call[0] == "publication_submit" for call in service.calls)

            await click("Share this exact reviewed package")
            assert service.calls.count(("publication_submit", "wpub_review")) == 1
            assert runtime.native.reviews.load(review_id)["state"] == "completed"
            before = len(edits)
            await app.handler(
                lambda: asyncio.sleep(0),
                {"user": {"id": "native-user"}, "channel": {"id": "C1"},
                 "team": {"id": "W1"}, "message": {"ts": "card-1"}},
                {"value": f"wi:agent:suggest.confirm:{review_id}"},
            )
            assert len(edits) == before
            assert service.calls.count(("publication_submit", "wpub_review")) == 1
        finally:
            runtime.close()
            for coroutine in pending:
                coroutine.close()

    asyncio.run(scenario())


def test_telegram_actual_dispatch_and_callback_edits_bound_card_without_upload(
    tmp_path, monkeypatch
):
    async def scenario():
        from telegram.ext import ApplicationHandlerStop

        service = _NativeSuggestService()
        pending, requests = [], []

        class Bot:
            async def do_api_request(self, endpoint, *, api_kwargs):
                requests.append((endpoint, api_kwargs))
                if endpoint == "sendRichMessage":
                    return {"ok": True, "result": {
                        "message_id": 77, "chat": {"id": 123},
                    }}
                return {"ok": True}

        bot = Bot()

        class Adapter:
            _owner_profile = "default"
            _bot = bot
            @staticmethod
            def _is_callback_user_authorized(user_id, **_kwargs):
                return user_id == "native-user"

        class App:
            def __init__(self):
                self.handler = None
            def add_handler(self, handler, group=0):
                assert group == -10
                self.handler = handler

        def spawn(coroutine, **_kwargs):
            pending.append(coroutine)

        runtime = CommandRuntime(
            tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
            spawn_task=spawn,
        )
        app, adapter = App(), Adapter()
        runtime.native.wire_telegram(app, adapter)
        caller = _native_invocation("telegram", adapter=adapter)
        monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: caller)
        monkeypatch.setattr(runtime.native, "service_factory", lambda: service)

        answers = []
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"), chat_id=123,
            message_thread_id=None, message_id=77, get_bot=lambda: bot,
        )

        async def answer():
            answers.append(True)

        try:
            result = runtime.slash("suggest local-skill")
            assert result == ""
            review_id = next(iter(runtime.native.reviews.state._load()["records"]))  # pylint: disable=protected-access
            await pending.pop(0)
            assert requests[0][0] == "sendRichMessage"
            assert requests[0][1]["rich_message"]["html"].startswith("<h5>")
            assert "Continue to sharing review" in requests[0][1]["rich_message"]["html"]
            assert runtime.native.reviews.load(review_id)["message_id"] == "77"

            query = SimpleNamespace(
                data=callback_value(review_id, "suggest.share"),
                from_user=SimpleNamespace(id="native-user", username="Shannon", first_name=""),
                message=message, answer=answer,
                edit_message_text=lambda **_kwargs: pytest.fail("rich edit should be used"),
            )
            with pytest.raises(ApplicationHandlerStop):
                await app.handler.callback(SimpleNamespace(callback_query=query), None)
            assert answers == [True]
            assert requests[-1][0] == "editMessageText"
            assert requests[-1][1]["rich_message"]["html"].startswith("<h5>")
            assert "Acknowledge displayed warnings and continue" in (
                requests[-1][1]["rich_message"]["html"]
            )
            assert not any(call[0].startswith("publication_") for call in service.calls)
        finally:
            runtime.close()
            for coroutine in pending:
                coroutine.close()

    asyncio.run(scenario())
