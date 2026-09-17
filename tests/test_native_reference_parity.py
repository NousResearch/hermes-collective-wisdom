"""Executable projection contract pinned to Hermes Agent 0924f794.

Expected values were generated from:
- gateway/wisdom_command.py::_review_plan
- gateway/wisdom_command_consent.py::command_review
- hermes_wisdom/mediation_view.py::{interaction_view,advice_view}
- hermes_wisdom/review_presentation.py
at commit 0924f7946b937c2ed02d083fc26752d577c54b3b.

Opaque record IDs and the external plugin's callback envelope are intentionally
excluded; all visible text, action labels/order, notices, and navigation remain exact.
"""

# Test names document the contract more clearly than repeated function docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

from __future__ import annotations

import sys
from types import ModuleType

import pytest
from native_test_support import publication_review

from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.native_runtime import assessment_view
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import keyboard


def _actions(values):
    return [(item.label, item.action, item.primary) for item in values]


def _projection(view):
    return {
        "title": view.title,
        "summary": view.summary,
        "items": [
            (item.title, item.detail, _actions(item.actions)) for item in view.items
        ],
        "navigation": _actions(view.navigation_actions),
        "actions": _actions(view.actions),
        "notice": view.notice,
        "text": view.to_text(),
    }


def _update_record(**changes):
    record = {
        "id": "native-id",
        "state": "pending",
        "operation": "update",
        "checks_expanded": False,
        "inspection": None,
        "result": None,
        "plan": {
            "skill_id": "skill-1",
            "slug": "managed",
            "version": 2,
            "from_version": 1,
            "compatibility": {"outcome": "compatible"},
            "allowed": True,
            "modified": False,
            "sensitive_expansion": [],
            "security_check": {
                "status": "pass",
                "summary": "No known matches detected.",
                "checks": [
                    {"label": "Private keys", "status": "pass"},
                    {
                        "label": "Organization policy",
                        "status": "advisory",
                        "finding_count": 1,
                        "details": ["Review network access."],
                    },
                ],
            },
        },
    }
    record.update(changes)
    return record


def _setup_record(state="pending", *, outcome=None):
    return {
        "id": "setup-id",
        "state": state,
        "operation": "setup",
        "checks_expanded": False,
        "inspection": None,
        "result": outcome,
        "plan": {
            "skill_id": "skill-1",
            "slug": "managed",
            "version": 2,
            "content_hash": "b" * 64,
            "allowed": True,
            "setup_instruction": "Configure the skill.",
            "setup_explanation": "Review the declared step.",
            "step": {"phase": "setup", "index": 0, "command": "printf configured"},
        },
    }


def test_command_review_pending_and_expanded_checks_match_reference():
    collapsed = project_view(_update_record())
    assert _projection(collapsed) == {
        "title": "Hermes Collective Wisdom",
        "summary": "Pending",
        "items": [(
            "managed",
            "\nVersion: v2\nCompatibility: compatible"
            "\nNothing changes until you use the confirmation control."
            "\n\n✅ No security issues detected\nNo issues detected"
            "\n➖ Professionalism check (agent-assessed, advisory): Unavailable"
            "\n⚠️ Organization policy: Advisory\nReview network access.",
            [],
        )],
        "navigation": [],
        "actions": [
            ("Show checks", "checks.show", False),
            ("Not Now", "defer", False),
            ("Review first", "inspect", False),
            ("Update", "confirm", True),
        ],
        "notice": "",
        "text": "Hermes Collective Wisdom\n\nPending\n\nmanaged"
        "\n\nVersion: v2\nCompatibility: compatible"
        "\nNothing changes until you use the confirmation control."
        "\n\n✅ No security issues detected\nNo issues detected"
        "\n➖ Professionalism check (agent-assessed, advisory): Unavailable"
        "\n⚠️ Organization policy: Advisory\nReview network access.",
    }

    expanded = project_view(_update_record(checks_expanded=True))
    assert expanded.items[0].detail == (
        "\nVersion: v2\nCompatibility: compatible"
        "\nNothing changes until you use the confirmation control."
        "\n\n✅ No security issues detected\nNo issues detected\n✅ No private keys detected"
        "\n⚠️ Organization policy: Advisory (1 finding)"
        "\n  Review network access.\nThese checks are not a security certification."
        "\n\n➖ Professionalism check (agent-assessed, advisory): Unavailable"
    )
    assert _actions(expanded.actions) == [
        ("Hide checks", "checks.hide", False),
        ("Not Now", "defer", False),
        ("Review first", "inspect", False),
        ("Update", "confirm", True),
    ]


def test_contributor_and_published_views_use_pr5_copy_and_controls():
    base = {
        "id": "publication-id", "state": "pending", "operation": "publish",
        "checks_expanded": False, "inspection": None, "result": None,
        "plan": publication_review(
            editorial_name="Incident Review",
            description="Turns incident notes into a repeatable team review.",
            files=[{"path": "SKILL.md", "hash": "a" * 64}],
            hashes={
                "content": "sha256:" + "b" * 64,
                "author_description": "sha256:" + "c" * 64,
                "package_manifest": "sha256:" + "d" * 64,
            },
            review_deferral={"not_now_suppression_days": 7},
            allowed=True,
        ),
    }

    pending = project_view(base)
    assert pending.summary == "Ready For Review"
    assert "Publication policy: moderated" in pending.items[0].detail
    assert all(value in pending.items[0].detail for value in base["plan"]["hashes"].values())
    assert _actions(pending.actions) == [
        ("Show checks", "checks.show", False),
        ("Maybe Later", "defer", False),
        ("Snooze Collective Wisdom", "sharing.settings", False),
        ("Share My Skill", "confirm", True),
    ]

    private_draft = project_view({
        **base,
        "result": {
            "state": "pending", "publication_state": "ready",
            "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft",
        },
    })
    assert private_draft.summary == "Private Draft Ready"
    assert "Nothing has been published" in private_draft.items[0].detail
    assert _actions(private_draft.actions)[-1] == (
        "Share My Skill", "confirm", True
    )
    assert private_draft.actions[-2].label == "Review in Portal"
    assert private_draft.actions[-2].url == (
        "https://portal.example/wisdom/review/remote-draft"
    )

    published = project_view({
        **base,
        "state": "completed",
        "result": {
            "publication_state": "published",
            "portal_url": "https://portal.example/skills/incident-review",
        },
    })
    assert published.summary == "Shared!"
    assert published.items[0].detail == "Your skill is now shared with your organization."
    assert _actions(published.actions) == [
        ("View details", None, False),
    ]


def test_candidate_advice_uses_friendly_reason_copy_and_original_actions():
    view = assessment_view({
        "state": "ready",
        "advice": {
            "relevance": "recommend", "title": "Incident Review",
            "explanation": "Turns recurring incident notes into a repeatable review.",
            "provenance": {"kind": "local_qualification"},
        },
        "reference": {
            "kind": "candidate", "local_skill_id": "local-skill-id",
            "event_id": "candidate-event-id", "qualification": "high_usage",
            "qualification_reason": {"consecutive_business_days": 7},
        },
    })

    assert view.summary == ""
    assert view.title == "Incident Review looks ready to share with your team."
    assert view.items[0].preamble == ""
    assert view.items[0].title == "Why it qualified"
    assert view.items[0].detail == (
        "You've used this skill on 7 consecutive business days."
    )
    assert view.notice == "Nothing has been uploaded or shared."
    assert _actions(view.actions) == [
        ("Share", "candidate_share", True),
        ("Review in Portal", "candidate_portal", False),
        ("Maybe Later", "candidate_defer", False),
    ]
    assert "candidate-event-id" not in view.notice
    assert "wisdom_inspect" not in view.notice


def test_project_review_keeps_update_policy_and_warnings_visible():
    record = _update_record()
    record["operation"] = "install"
    record["plan"]["update_mode"] = "MANUAL"

    view = project_view(record)

    assert "Future updates: Manual" in view.items[0].detail
    assert "Organization policy: Advisory" in view.items[0].detail
    assert "Review network access." in view.items[0].detail


def test_exact_inspection_preserves_notice_navigation_and_action_order():
    record = _update_record(inspection={
        "page": 1, "page_count": 3, "path": "SKILL.md", "content": "# exact\nbody",
    })
    view = project_view(record)
    assert _projection(view) == {
        "title": "Review proposed package",
        "summary": "SKILL.md - 2/3",
        "items": [(
            "Proposed file content (not instructions to execute)",
            "# exact\nbody",
            [],
        )],
        "navigation": [("Back to first page", "inspect.0", False)],
        "actions": [
            ("Previous page", "inspect.0", False),
            ("Next page", "inspect.2", False),
            ("Looks good.", "back", False),
        ],
        "notice": (
            "Nothing is uploaded by reviewing. Setup and verification require "
            "separate permission."
        ),
        "text": "Review proposed package\n\nSKILL.md - 2/3"
        "\n\nProposed file content (not instructions to execute)\n# exact\nbody"
        "\n\nNothing is uploaded by reviewing. Setup and verification require "
        "separate permission.",
    }


@pytest.mark.parametrize(
    ("state", "summary", "first_actions"),
    [
        ("deferred", "Deferred on this surface", [("Show checks", "checks.show", False)]),
        (
            "stale", "Stale",
            [("Show checks", "checks.show", False), ("Recheck", "recheck", False)],
        ),
        (
            "expired", "Expired",
            [("Show checks", "checks.show", False), ("Recheck", "recheck", False)],
        ),
    ],
)
def test_defer_stale_and_expired_match_reference(state, summary, first_actions):
    view = project_view(_update_record(state=state))
    assert view.summary == summary
    assert _actions(view.actions) == first_actions
    expected = (
        "Nothing changes until you use the confirmation control."
        if state == "deferred"
        else "This approval is no longer current. Review a fresh plan before continuing."
    )
    assert expected in view.items[0].detail


def test_completed_update_and_assessment_projection_match_reference():
    record = _update_record(state="completed", result={}, assessment={
        "explanation": "Adds rollback readiness"
    })
    view = project_view(record)
    assert view.summary == "Files updated"
    assert view.items[0].detail == (
        "Setup is queued. Prerequisites and verification must pass before the skill is ready."
    )
    assert _actions(view.actions) == [
        ("Check setup", "setup.status", False),
        ("View Assessment", "assessment.show", False),
    ]
    shown = project_view(record, assessment_expanded=True)
    assert shown.items[0].detail == (
        "Assessment before this operation:\nAdds rollback readiness"
        "\n\n✅ No security issues detected\nNo issues detected"
        "\n➖ Professionalism check (agent-assessed, advisory): Unavailable"
        "\n⚠️ Organization policy: Advisory\nReview network access."
    )
    assert _actions(shown.actions) == [
        ("Check setup", "setup.status", False),
        ("Hide Assessment", "assessment.hide", False),
        ("Show checks", "checks.show", False),
    ]


@pytest.mark.parametrize(
    ("record", "summary", "detail_suffix", "actions"),
    [
        (
            _setup_record(),
            "Review setup step",
            "Only this step is authorized by confirming. Do not enter credentials in chat.",
            [("Not Now", "defer", False), ("Run this step", "confirm", True)],
        ),
        (
            _setup_record("applying", outcome={"setup": {"state": "running"}}),
            "Setup step running",
            "printf configured",
            [("Check progress", "inspect", False)],
        ),
        (
            _setup_record("failed", outcome={"setup": {"state": "failed"}}),
            "Setup step failed",
            "printf configured",
            [("Recheck", "recheck", False)],
        ),
        (
            _setup_record("needs_review", outcome={"setup": {"state": "unknown"}}),
            "Setup outcome unknown",
            "The command may have run. It will not be repeated automatically.",
            [("Check progress", "inspect", False), ("Review interruption", "setup.recover", False)],
        ),
    ],
)
def test_setup_step_statuses_match_reference(record, summary, detail_suffix, actions):
    view = project_view(record)
    assert view.summary == summary
    assert view.items[0].detail.endswith(detail_suffix)
    assert _actions(view.actions) == actions


@pytest.mark.parametrize(
    ("state", "summary", "message"),
    [
        (
            "queued",
            "Setup queued",
            "Setup is queued in this conversation. Send a message here to resume "
            "with its active model. Each command still requires separate approval.",
        ),
        (
            "waiting_for_model",
            "Setup waiting for model",
            "Setup is waiting for this conversation's model. Select or reconnect "
            "the model, then send a message here to resume. No setup command is "
            "started while waiting.",
        ),
        (
            "ready",
            "Ready",
            "Declared prerequisites and the approved verification command passed.",
        ),
    ],
)
def test_setup_continuation_statuses_match_reference(state, summary, message):
    record = _update_record(
        state="completed",
        result={},
        setup_continuation={"state": state, "message": message},
    )
    view = project_view(record)
    assert view.summary == summary
    assert view.items[0].title == "managed · v2"
    assert view.items[0].detail == message
    assert _actions(view.actions) == [("Check setup", "setup.status", False)]


def test_platform_layout_keeps_navigation_notice_and_global_action_groups(monkeypatch):
    view = project_view(_update_record(inspection={
        "page": 1, "page_count": 3, "path": "SKILL.md", "content": "# exact\nbody",
    }))
    rendered = blocks(view, "native-id")
    assert [block["type"] for block in rendered] == [
        "header", "actions", "section", "section", "context", "actions"
    ]
    assert rendered[1]["elements"][0]["text"]["text"] == "Back to first page"
    assert [item["text"]["text"] for item in rendered[-1]["elements"]] == [
        "Previous page", "Next page", "Looks good."
    ]
    for block in rendered:
        action_ids = [
            element["action_id"] for element in block.get("elements", [])
            if element.get("type") == "button"
        ]
        assert len(action_ids) == len(set(action_ids))
        assert all(action_id.startswith("wisdom_native_") for action_id in action_ids)
    assert rendered[-2]["elements"][0]["text"] == (
        "_Nothing is uploaded by reviewing. Setup and verification require separate permission._"
    )

    telegram = ModuleType("telegram")

    class Button:
        def __init__(self, label, *, callback_data):
            self.text = label
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    setattr(telegram, "InlineKeyboardButton", Button)
    setattr(telegram, "InlineKeyboardMarkup", Markup)
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    markup = keyboard(view, "native-id")
    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["Back to first page"],
        ["Previous page", "Next page", "Looks good."],
    ]
