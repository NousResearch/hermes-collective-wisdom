"""Golden behavioral coverage for pure terminal review rendering."""

import pytest

from hermes_wisdom_plugin.terminal_ui import render_action_review, render_file_preview


def test_publication_review_preserves_complete_ordered_safe_output():
    """Publication review keeps all consent facts ordered and terminal-safe."""

    value = {
        "owner": {
            "organization_id": "org\x1b[31m",
            "account_id": "acct",
            "profile_name": "work",
        },
        "slug": "raccoon",
        "skill_id": "skill-1",
        "version": 3,
        "publication_mode": "moderated",
        "state": "review",
        "publication_state": "private",
        "operation_id": "op-1",
        "draft_id": "draft-1",
        "drafted_description": "Useful\n skill",
        "compatibility": {
            "outcome": "compatible_after_setup",
            "satisfied": ["macOS"],
            "setup_actions": [{"description": "Install uv"}],
            "limitations": ["No Windows"],
            "blocked": ["Need tool"],
        },
        "manifest": {
            "requirements": {
                "hermes": {"minimum_version": "1.2"},
                "platforms": ["darwin"],
                "architectures": ["arm64"],
                "runtime": {"python": True, "node": False},
                "tools": ["uv"],
                "known_limitations": ["offline"],
            }
        },
        "files": [{
            "path": "SKILL\x1b[2J.md",
            "mode": "exec",
            "size": 12,
            "hash": "sha256:abc",
        }],
        "server_checks": {
            "security": {"status": "pass"},
            "editorial": {"state": "unavailable"},
            "ignored": "not a structured check",
        },
        "local_scan": {
            "allowed": False,
            "findings": [{
                "severity": "warning",
                "category": "instructions",
                "pattern_id": "shell",
                "file": "SKILL.md",
                "line": 2,
                "description": "Review this",
                "match": "SECRET",
            }],
        },
        "policy": {"warning_acceptance": {"accepted": True}},
        "modified": True,
        "preservation": {"action": "preserve"},
        "sensitive_expansion": ["network"],
        "allowed": False,
        "network_submission": False,
        "receipt": "receipt with ' quote",
    }

    assert render_action_review("publication", value) == """Sharing review
Organization: org [31m
Owner account: acct
Profile: work
Skill: raccoon
Skill ID: skill-1
Version: 3
Handling: moderated
State: review
Publication state: private
Operation: op-1
Draft: draft-1
Description: Useful skill
Compatibility: compatible_after_setup
Satisfied checks:
  - macOS
Required setup actions:
  - Install uv
Known limitations:
  - No Windows
Blocking requirements:
  - Need tool
Requirements: Hermes 1.2+; OS: darwin; CPU: arm64; runtime: python; 1 tool requirement(s); 1 limitation requirement(s)
Files (1)
  1. SKILL [2J.md [executable] — 12 bytes, sha256:abc
Server checks:
  - security: pass
  - editorial: unavailable
Scanner findings (1)
  WARNING / instructions
    - shell at SKILL.md:2: Review this
Warning acknowledgement: accepted
Local edits: detected; action: preserve
New sensitive capabilities:
  - network
Approval permission: blocked
Approval status: blocked until the listed review actions are completed.
Consequence: submits to organization moderation before publication.
Nothing has been uploaded yet.
Next: hermes wisdom publication submit 'receipt with '\"'\"' quote'"""


def test_setup_review_preserves_literal_command_and_eligibility_output():
    """Setup review escapes commands and retains prerequisite and approval facts."""

    value = {
        "owner": {"organization_id": "org"},
        "slug": "raccoon",
        "version": 3,
        "files": [],
        "step": {"phase": "setup", "index": 1, "command": 'printf "ok"\n\x1b'},
        "prerequisites": [
            {"name": "uv", "status": "passed"},
            {"kind": "tool"},
        ],
        "verification": {"state": "pending"},
        "allowed": True,
        "receipt": "setup receipt",
    }

    assert render_action_review("installed-setup", value) == """Installed setup review
Organization: org
Skill: raccoon
Version: 3
Files (0)
Scanner warnings: none reported.
Setup phase: setup
Setup step: 2
Command (escaped literal; not executed by review): "printf \\"ok\\"\\n\\u001b"
Prerequisites:
  - uv: passed
  - tool: unknown
Verification: pending
Approval permission: allowed
Approval status: eligible for exact final confirmation.
Next: hermes wisdom installed-setup apply 'setup receipt'"""


@pytest.mark.parametrize(
    ("kind", "title", "next_command"),
    [
        ("install", "Installation review", "hermes wisdom install --apply-receipt receipt"),
        ("update", "Update review", "hermes wisdom update --apply-receipt receipt"),
    ],
)
def test_package_reviews_preserve_kind_specific_actions(kind, title, next_command):
    """Install and update reviews retain distinct titles and resumable actions."""

    assert render_action_review(kind, {"files": [], "receipt": "receipt"}) == (
        f"{title}\n"
        "Files (0)\n"
        "Scanner warnings: none reported.\n"
        "Consequence: writes the reviewed package files; setup commands are not run.\n"
        f"Next: {next_command}"
    )


def test_unknown_review_kind_retains_generic_denial_without_receipt_action():
    """Unsupported review kinds show denial but never invent an apply action."""

    assert render_action_review(
        "future-kind", {"state": "denied", "files": [], "allowed": False, "receipt": "ignored"}
    ) == """Collective Wisdom review
State: denied
Files (0)
Scanner warnings: none reported.
Approval permission: blocked
Approval status: blocked until the listed review actions are completed."""


def test_file_preview_preserves_newlines_and_tabs_but_removes_active_controls():
    """Inert previews keep source layout without emitting terminal controls."""

    rendered = render_file_preview({
        "path": "references/example.md",
        "content_utf8": "first line\n\tindented\nlast\x1b[2J line",
    })

    assert "first line\n\tindented\nlast [2J line" in rendered
    assert "\x1b" not in rendered
