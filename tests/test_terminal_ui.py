"""Golden behavioral coverage for pure terminal review rendering."""

import pytest

from hermes_wisdom_plugin.native_inspection import sharing_checks
from hermes_wisdom_plugin.terminal_ui import (
    render_action_review,
    render_file_preview,
    render_operation_outcome,
)


@pytest.mark.parametrize(
    ("kind", "result", "name", "expected"),
    [
        ("install", {"installed": True}, "Incident Triage", "✅ Incident Triage installed"),
        ("update", {"updated": True}, "Incident Triage", "✅ Incident Triage updated"),
        ("uninstall", {"uninstalled": True}, "Incident Triage", "✅ Incident Triage removed"),
    ],
)
def test_completed_outcomes_use_human_name_and_one_line(kind, result, name, expected):
    """Completed mutations render only the human-facing success line."""
    assert render_operation_outcome(kind, result, skill_name=name) == expected


@pytest.mark.parametrize(
    "result",
    [
        {"pending": True, "skill_id": "skill-id"},
        {"installed": False, "rejected": True, "skill_id": "skill-id"},
        {"state": "uncertain", "skill_id": "skill-id"},
        {"installed": True, "pending": True},
        {"installed": True, "rejected": True},
        {"installed": True, "state": "uncertain"},
    ],
)
def test_incomplete_install_outcomes_are_never_success(result):
    """Pending, rejected, and uncertain outcomes never claim installation."""
    output = render_operation_outcome("install", result, skill_name="Incident Triage")
    assert not output.startswith("✅")
    assert "installed" not in output.casefold()


def test_success_without_a_human_name_is_not_announced():
    """An opaque identifier is not a skill name for success copy."""
    assert not render_operation_outcome(
        "install", {"installed": True, "skill_id": "opaque-id"}
    ).startswith("✅")


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

    rendered = render_action_review("publication", value)
    for fact in ("Skill: raccoon", "Version: 3", "Install uv", "No Windows",
                 "Need tool", "Files (1)", "shell at SKILL.md:2: Review this",
                 "Warning acknowledgement: accepted", "Local edits: detected",
                 "Approval permission: blocked", "Nothing has been uploaded yet"):
        assert fact in rendered
    for private in ("org [31m", "skill-1", "op-1", "draft-1", "sha256:abc",
                    "receipt with", "SECRET"):
        assert private not in rendered


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

    rendered = render_action_review("installed-setup", value)
    for fact in ("Skill: raccoon", "Version: 3", "Files (0)", "Setup step: 2",
                 '"printf \\"ok\\"\\n\\u001b"', "uv: passed", "Verification: pending",
                 "Approval permission: allowed"):
        assert fact in rendered
    assert "setup receipt" not in rendered


def test_publication_gateway_advisory_uses_shared_server_check_rendering():
    """Gateway evidence remains distinct from the local scanner in human output."""
    rendered = render_action_review("publication", {
        "files": [],
        "server_checks": {"security_check": {
            "status": "advisory", "findings": [{
                "severity": "advisory", "category": "secret_assignment",
                "path": "references/example.md", "detail": "Possible secret assignment",
            }],
        }},
        "local_scan": {"allowed": True, "findings": []},
        "warning_acceptance": {"accepted": True},
    })

    assert "Server checks:\n  - security_check: advisory" in rendered
    assert "references/example.md" in rendered
    assert "Possible secret assignment" in rendered
    assert "Local scanner warnings: none reported." in rendered
    assert "Warning acknowledgement: accepted" in rendered


def test_native_sharing_checks_show_gateway_advisory_separately():
    """Native checks cannot claim all-safe while Gateway evidence is advisory."""
    safe, rendered = sharing_checks({
        "server_checks": {"security_check": {"status": "advisory"}},
    })

    assert safe is False
    assert "Gateway security: advisory" in rendered


def test_unverified_local_update_discloses_unknown_baseline_and_preservation():
    """Unknown local history is not mislabeled as detected edits."""

    output = render_action_review("update", {
        "slug": "raccoon", "version": 2, "files": [],
        "modified": True, "local_baseline": "unknown",
        "preservation": {"action": "preserve_unmanaged_copy"},
        "allowed": True,
    })

    assert "Local published baseline: unknown" in output
    assert "preservation required; action: preserve_unmanaged_copy" in output
    assert "Local edits: detected" not in output


@pytest.mark.parametrize("kind", ["install", "update"])
def test_package_review_stays_bounded_for_large_packages(kind):
    """Consent shows severity and a few safe examples, not hundreds of files."""
    findings = [{"severity": "medium", "pattern_id": "generic", "file": "file.md"}]
    findings += [{
        "severity": "critical", "pattern_id": "system_passwd_access",
        "file": "references/remote.md", "line": 84,
        "description": "references system password files",
    } for _ in range(199)]
    result = {
        "slug": "hermes-agent-dev", "version": 1,
        "files": [{"path": f"references/{n}.md", "hash": "sha256:secret"}
                  for n in range(452)],
        "server_checks": {"security_check": {"status": "advisory"}},
        "local_scan": {"allowed": False, "findings": findings},
        "allowed": False, "receipt": "wip_secret",
    }
    output = render_action_review(kind, result)
    assert "Skill: hermes-agent-dev" in output
    assert "Files: 452" in output
    assert "Scanner warnings: 200" in output
    assert "critical: 199" in output
    assert "system_passwd_access" in output
    assert "security_check: advisory" in output
    assert "Approval permission: blocked" in output
    assert "references/451.md" not in output
    assert "sha256:secret" not in output
    assert "wip_secret" not in output
    assert "Next:" not in output
    assert "setup commands are not run" not in output
    assert len(output.splitlines()) < 20


def test_install_review_displays_exact_auto_setup_before_approval():
    """A single Install approval covers only visible hash-bound commands."""
    result = {"slug": "raccoon", "files": [], "allowed": True,
              "setup_commands": ["python scripts/setup.py --local"]}
    output = render_action_review("install", result)
    assert 'Setup command: "python scripts/setup.py --local"' in output
    assert output.index("Setup command:") < output.index("Approval permission: allowed")


@pytest.mark.parametrize("state", ["failed", "unknown", "blocked"])
def test_installed_files_do_not_claim_complete_setup_when_command_stops(state):
    """The install result names partial success without claiming readiness."""
    result = {"installed": True, "setup": {"state": state, "step": 1}}
    output = render_operation_outcome("install", result, skill_name="raccoon")
    assert "files installed" in output
    assert state in output
    assert output != "✅ raccoon installed"


def test_partial_install_outcome_explains_setup_block():
    """After package write, a refused setup retains the actionable reason."""
    result = {"installed": True, "setup": {
        "state": "blocked", "detail": "setup prerequisites unavailable",
    }}
    output = render_operation_outcome("install", result, skill_name="raccoon")
    assert "files installed" in output
    assert "setup prerequisites unavailable" in output


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("open", "Open: confirmation may publish directly."),
        ("moderated", "Moderated: confirmation submits for team moderation."),
        ("managed", "Managed: confirmation follows organization-managed Gateway handling."),
        (
            "future-policy",
            "Gateway publication policy is unavailable or unrecognized; "
            "no publication route is claimed.",
        ),
    ],
)
def test_publication_consequence_uses_authoritative_policy_modes(mode, expected):
    """Only recognized authoritative modes claim a concrete publication route."""

    output = render_action_review(
        "publication", {"files": [], "publication_mode": mode}
    )

    assert "Consequence: " + expected in output


def test_local_prepared_preview_does_not_invent_unchecked_moderation():
    """Local preparation precedes the authoritative Gateway policy read."""

    output = render_action_review(
        "publication", {"files": [], "network_submission": False}
    )

    assert "Gateway publication policy has not been checked yet" in output
    assert "moderation" not in output.casefold()
    assert "publish directly" not in output.casefold()
    assert "Nothing has been uploaded yet." in output


@pytest.mark.parametrize(
    ("kind", "title"),
    [
        ("install", "Installation review"),
        ("update", "Update review"),
    ],
)
def test_package_reviews_hide_internal_receipts(kind, title):
    """Approval still works without exposing the internal apply command."""

    assert render_action_review(kind, {"files": [], "receipt": "receipt"}) == (
        f"{title}\n"
        "Files: 0\n"
        "Scanner warnings: none reported."
    )


def test_unknown_review_kind_retains_generic_denial_without_receipt_action():
    """Unsupported review kinds show denial but never invent an apply action."""

    assert render_action_review(
        "future-kind", {"state": "denied", "files": [], "allowed": False, "receipt": "ignored"}
    ) == """Collective Wisdom review
State: denied
Files (0)
Local scanner warnings: none reported.
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
