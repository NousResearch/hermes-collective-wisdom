"""Supported native rendering remains deterministic and bounded."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods

import asyncio
import html
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import _human_render
from hermes_wisdom_plugin.local_security import safe_scan_findings
from hermes_wisdom_plugin.native_inspection import inspection_pages
from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeView, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import edit, keyboard, plain_html, rich_html, send

def test_recorded_candidate_plan_is_a_human_list_on_real_transports():
    # Shape captured from a real candidate plan; authority tokens removed from fixture.
    path = Path(__file__).parent / "fixtures" / "recorded_candidate_plan.json"
    record = {"operation": "candidate", "plan": json.loads(path.read_text(encoding="utf-8")),
              "state": "pending", "screen": "details", "page": 0,
              "team_name": "Wisdom Staging Test"}
    original = deepcopy(record)
    pages = []
    while True:
        record["page"] = len(pages)
        view = project_view(record)
        pages.append(view.items[0].detail)
        assert view.items[0].literal is False
        assert html.escape(view.items[0].detail) in next(
            block["text"]["text"] for block in blocks(view, "record-1")
            if block.get("type") == "section" and "Review details" in block["text"]["text"]
        )
        assert view.items[0].detail.replace("&", "&amp;") in plain_html(view)
        expected = html.escape(view.items[0].detail).replace("\n", "<br/>")
        assert expected in rich_html(view, "record-1")
        if not any(action.label == "Next" for action in view.actions):
            break
    text = "\n".join(pages)
    for fact in ("Skill name: Shift Handover Summary", "What it does:",
                 "Why teammates benefit:", "Why suggested: Frequent use",
                 "Draft preparation: Complete", "Nothing has been uploaded or shared",
                 "✅ Private keys: No issues detected",
                 "⏳ Organization policy: Checked by Gateway after authorized submission"):
        assert fact in text
    for internal in ("high_usage", "Schema version", "local_preflight", "ascii_credential",
                     "author_description_hash", "Editorial failure: Unavailable"):
        assert internal not in text
    assert original["plan"] == record["plan"]

def test_candidate_details_keep_warning_and_unavailable_without_fake_pass():
    path = Path(__file__).parent / "fixtures" / "recorded_candidate_plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["security_check"]["checks"][0].update(
        status="warning_required", finding_count=1, details=["A key may be exposed"])
    plan["professionalism_check"]["checks"][0]["status"] = "unavailable"
    plan["facts"]["editorial_status"] = "incomplete"
    plan["facts"]["editorial_failure"] = "model_unavailable"
    record = {"operation": "candidate", "plan": plan, "state": "pending", "screen": "details"}
    text = "\n".join(inspection_pages(record))
    assert "⚠️ Private keys: warning required" in text
    assert "1 finding(s) reported" in text and "A key may be exposed" in text
    assert "⚠️ Profanity or abusive language: unavailable" in text
    assert "⚠️ Draft preparation: Incomplete" in text
    assert "Preparation needs attention: model unavailable" in text
    assert "✅ Private keys" not in text


@pytest.mark.parametrize("operation,screen", [
    ("candidate", "details"), ("publish", "details"), ("publish", "approval"),
])
def test_structured_scanner_findings_are_readable_on_native_surfaces(operation, screen):
    scan = {"available": True, "allowed": None, "findings": [{
        "severity": "high", "kind": "instruction_override", "path": "SKILL.md",
        "line": 7, "description": "Review the instruction override.",
        "matched_text": "private matched content must not be displayed",
    }]}
    plan = {
        "candidate_flow": operation == "publish", "allowed": False,
        "local_scan": scan, "security_check": {"status": "warning_required", "checks": [{
            "key": "skills_guard", "status": "warning_required", "finding_count": 1,
            "findings": safe_scan_findings(scan),
        }]},
    }
    record = {"operation": operation, "screen": screen, "state": "pending", "plan": plan}
    original = deepcopy(plan)
    texts = []
    while True:
        record["page"] = len(texts)
        view = project_view(record)
        text = view.items[0].detail
        texts.append(text)
        slack_text = "\n".join(block.get("text", {}).get("text", "")
                               for block in blocks(view, "record-1"))
        assert html.escape(text) in slack_text or text in slack_text
        assert html.escape(text).replace("\n", "<br/>") in rich_html(view, "record-1")
        assert html.escape(text) in plain_html(view)
        assert not any(action.primary for action in view.actions)
        if not any(action.label == "Next" for action in view.actions):
            break
    rendered = "\n".join(texts)
    for expected in ("Severity: high", "File: SKILL.md", "Line: 7",
                     "Review the instruction override."):
        assert expected in rendered
    assert "{'severity'" not in rendered
    assert "private matched content" not in rendered
    assert plan == original


@pytest.mark.parametrize("status,icon", [
    ("pass", "✅"), ("blocked", "❌"), ("warning_required", "⚠️"),
    ("unavailable", "⚠️"), ("pending", "⏳"),
])
def test_candidate_status_emoji_survive_native_renderers(status, icon):
    record = {
        "operation": "candidate", "screen": "details", "state": "pending",
        "plan": {"security_check": {"checks": [
            {"key": "private_keys", "status": status, "finding_count": 0},
        ]}},
    }
    view = project_view(record)
    expected = f"{icon} Private keys:"
    assert expected in view.to_text()
    assert expected in str(blocks(view, "record-1"))
    assert expected in rich_html(view, "record-1")
    assert expected in plain_html(view)
    assert "ℹ️ Skill name:" in view.to_text()


def test_explicit_review_controls_point_to_exact_record():
    view = NativeView(
        "Collective Wisdom review", "Install skill",
        [NativeItem("Skill", "Version: 2\nHash: sha256:abc")],
        actions=[
            NativeAction("Confirm", "confirm", primary=True), NativeAction("Cancel", "cancel"),
        ],
    )

    slack = blocks(view, "record-1")
    telegram = keyboard(view, "record-1")

    assert any(
        button.get("value") == "wi:agent:confirm:record-1"
        for block in slack for button in block.get("elements", [])
    )
    assert "wi:agent:confirm:record-1" in str(telegram)


def test_completed_review_has_no_effect_controls():
    view = NativeView(
        "Collective Wisdom review", "Install complete",
        [NativeItem("Skill", "Installed")], notice="State: completed", dismissed=True,
    )
    assert all(block.get("type") != "actions" for block in blocks(view, "record-1"))
    assert keyboard(view, "record-1") is None


@pytest.mark.parametrize("operation", ["publish", "suggest"])
def test_published_but_unmanaged_recovery_is_visible_without_new_effect_controls(operation):
    detail = (
        "Published successfully, but the original source is not linked to this version locally. "
        "Recover with: hermes wisdom publication enroll shared-skill"
    )
    result = {
        "operation_id": "publication-op", "state": "done", "publication_state": "published",
        "managed_enrollment": {"state": "not_linked", "detail": detail}, "detail": detail,
    }
    view = project_view({
        "state": "completed", "operation": operation,
        "plan": {"draft_id": "draft-1", "slug": "shared-skill"}, "result": result,
    })
    assert detail in _human_render("publication", result)
    assert detail in view.to_text()
    assert detail in html.unescape(plain_html(view))
    assert detail in str(blocks(view, "record-1"))
    assert view.dismissed and not view.actions
    assert keyboard(view, "record-1") is None


@pytest.mark.parametrize("flag", ["pending", "rejected"])
def test_contradictory_success_is_not_presented_as_installed(flag):
    result = {"installed": True, flag: True, "slug": "incident-triage"}
    view = project_view({
        "state": "completed", "operation": "install",
        "plan": {"slug": "incident-triage"}, "result": result,
    })
    for rendered in (view.to_text(), _human_render("install", result)):
        assert rendered == f"incident-triage: {flag}"
    assert flag in html.unescape(plain_html(view))
    assert flag in str(blocks(view, "record-1"))
    assert not view.actions


def test_effect_review_retains_exact_security_compatibility_and_file_facts():
    plan = {
        "operation": "install", "slug": "incident-triage", "version": 2,
        "receipt": "receipt-exact", "content_hash": "sha256:content",
        "manifest_hash": "sha256:manifest", "allowed": True,
        "accept_partial": True,
        "compatibility": {"outcome": "compatible_after_setup"},
        "local_scan": {
            "available": True, "allowed": None, "verdict": "ask",
            "summary": "Review shell capability", "findings": [
                {"kind": "capability", "capability": "shell", "path": "SKILL.md"}
            ],
        },
        "files": [{
            "path": "scripts/check.sh", "mode": "exec", "hash": "sha256:file",
        }],
    }

    text = project_view({
        "state": "pending", "operation": "install", "plan": plan, "screen": "details",
    }).to_text()

    for exact_fact in (
        'compatible_after_setup', "Accept partial: Yes", "ask",
        "Review shell capability", "shell", "scripts/check.sh", "exec", "sha256:file",
    ):
        assert exact_fact in text
    assert "receipt-exact" not in text
    approval = project_view({
        "state": "pending", "operation": "install", "plan": plan, "screen": "approval",
    }).to_text()
    assert "Sharing is" not in approval
    assert "Destination team: Team name unavailable" not in approval


def test_telegram_plain_fallback_removes_approval_when_escape_expansion_truncates():
    """A format fallback cannot approve facts omitted by HTML expansion."""

    class Bot:
        def __init__(self):
            self.payload = None

        async def send_message(self, **payload):
            self.payload = payload
            return type("Message", (), {"message_id": 77})()

    view = NativeView(
        "Collective Wisdom review", "Install skill",
        [NativeItem("Exact package", "&" * 3000)],
        actions=[
            NativeAction("Confirm", "confirm", primary=True),
            NativeAction("Cancel", "cancel", destructive=True),
        ],
    )
    bot = Bot()

    asyncio.run(send(bot, view, "record-1", chat_id="123"))

    assert bot.payload is not None
    assert "confirm" not in str(bot.payload["reply_markup"])


@pytest.mark.parametrize("operation", ["send", "edit"])
@pytest.mark.parametrize("overflow", ["x" * 33_000, "&" * 2_000], ids=["large", "escaped"])
def test_telegram_rich_transport_drops_approval_when_authority_facts_are_lost(
    operation, overflow,
):
    """Rich send and edit fall back without approval when owner/expiry are omitted."""

    class Bot:
        def __init__(self):
            self.rich = []
            self.sent = []

        async def do_api_request(self, endpoint, *, api_kwargs):
            self.rich.append((endpoint, api_kwargs))
            return {"ok": True, "result": {"message_id": 77}}

        async def send_message(self, **payload):
            self.sent.append(payload)
            return SimpleNamespace(message_id=77)

    view = NativeView(
        "Collective Wisdom review", "Install exact package",
        [NativeItem(
            "Exact package",
            overflow + "\nOwner account: owner-1\nReview expires: 12345.5",
        )],
        actions=[
            NativeAction("Confirm", "confirm", primary=True),
            NativeAction("Cancel", "cancel", destructive=True),
        ],
    )
    bot = Bot()

    if operation == "send":
        asyncio.run(send(bot, view, "record-1", chat_id="123"))
        assert len(bot.sent) == 1
        payload = bot.sent[0]
    else:
        edited = []

        async def edit_message_text(**payload):
            edited.append(payload)

        query = SimpleNamespace(
            message=SimpleNamespace(
                chat_id=123, message_id=77, get_bot=lambda: bot,
            ),
            edit_message_text=edit_message_text,
        )
        asyncio.run(edit(query, view, "record-1"))
        assert len(edited) == 1
        payload = edited[0]

    assert not bot.rich
    assert "Owner account: owner-1" not in html.unescape(payload["text"])
    assert "Review expires: 12345.5" not in html.unescape(payload["text"])
    assert "wi:agent:confirm:record-1" not in str(payload["reply_markup"])


def test_native_approval_fallback_preserves_version_owner_expiry_and_permission():
    """Approval exists only when every exact authority fact survives fallback."""

    record = {
        "state": "pending", "operation": "install", "expires_at": 12345.5,
        "owner": {
            "organization_id": "org-1", "account_id": "owner-1",
            "profile_name": "work",
        },
        "plan": {
            "operation": "install", "slug": "fixture", "version": 7,
            "receipt": "receipt-exact", "allowed": True, "files": [],
        },
        "screen": "approval", "page": 0, "inspected_through": 0,
    }

    view = project_view(record)
    fallback = html.unescape(plain_html(view))
    slack = blocks(view, "record-1")

    for fact in ("Version: 7", "Profile: work",
                 "Review expires: 01 Jan 1970", "Approval is available only after"):
        assert fact in fallback
        assert fact in str(slack)
    assert "org-1" not in fallback and "owner-1" not in fallback
    assert "wi:agent:confirm:record-1" in str(keyboard(view, "record-1"))


def test_native_install_offer_is_one_click_with_read_only_details():
    record = {
        "state": "pending", "operation": "install", "expires_at": 12345.5,
        "owner": {
            "organization_id": "org-1", "account_id": "owner-1",
            "profile_name": "work",
        },
        "plan": {
            "operation": "install", "slug": "incident-triage", "version": 7,
            "receipt": "receipt-exact", "allowed": True,
            "compatibility": {"outcome": "compatible"},
            "local_scan": {"allowed": True, "findings": []},
        },
    }

    view = project_view(record)
    rendered = view.to_text()

    assert "incident-triage" in rendered
    assert "receipt-exact" not in rendered
    assert '"package_files"' not in rendered
    assert any(action.action == "details" for action in view.actions)
    assert any(action.action == "install" and action.primary for action in view.actions)
    assert all(action.action != "confirm" for action in view.actions)


def test_native_completed_install_uses_plan_name_when_result_only_has_id():
    view = project_view({
        "state": "completed", "operation": "install",
        "plan": {"slug": "incident-triage", "skill_id": "uuid-value", "version": 2},
        "result": {"installed": True, "skill_id": "uuid-value", "version": 2},
    })

    assert view.title == "✅ incident-triage installed"
    assert not view.summary
    assert "uuid-value" not in view.to_text()
    assert view.dismissed and not view.actions

@pytest.mark.parametrize("operation, verb", [("install", "installed"), ("update", "updated")])
def test_completed_setup_receipt_keeps_full_slack_confirmation(operation, verb):
    name = "wisdom-setup-slack-smoke-20260926"
    view = project_view({
        "state": "completed", "operation": operation,
        "plan": {"slug": name, "version": 2},
        "result": {
            verb: True, "setup_required": True, "setup_execution_authorized": True,
            "ready_to_use": None, "setup": {"state": "passed"},
        },
    })
    rendered = blocks(view, "record-1")
    assert view.title == f"✅ {name} {verb}"
    assert rendered[0]["text"]["text"] == view.title
    assert rendered[1]["text"]["text"] == (
        "Setup ran successfully. Have your agent confirm the skill is ready "
        "before using it."
    )
    assert rendered[1]["text"]["text"] in view.to_text()
    assert view.dismissed and not view.actions


def test_entity_heavy_owner_loss_removes_slack_and_telegram_approval():
    record = {
        "state": "pending", "operation": "install", "expires_at": 12345.5,
        "owner": {
            "organization_id": "org-1", "account_id": "&" * 1000,
            "profile_name": "work",
        },
        "plan": {
            "operation": "install", "slug": "fixture", "version": 7,
            "receipt": "receipt-exact", "allowed": True,
        },
    }

    view = project_view(record)
    fallback = html.unescape(plain_html(view))
    slack = blocks(view, "record-1")

    assert "Review expires: 12345.5" not in fallback
    assert "wi:agent:confirm:record-1" not in str(slack)
    assert "wi:agent:confirm:record-1" not in str(keyboard(view, "record-1"))


def test_native_review_renders_persisted_owner_when_plan_owner_diverges():
    record_owner = {
        "organization_id": "record-org", "account_id": "record-owner",
        "profile_name": "work",
    }
    view = project_view({
        "state": "pending", "operation": "install", "expires_at": 12345.5,
        "owner": record_owner,
        "plan": {
            "operation": "install", "slug": "fixture", "version": 7,
            "receipt": "receipt-exact", "allowed": True,
            "owner": {
                "organization_id": "plan-org", "account_id": "plan-owner",
                "profile_name": "other",
            },
        },
        "screen": "approval", "page": 0, "inspected_through": 0,
    })

    rendered = view.to_text()
    assert f"Profile: {record_owner['profile_name']}" in rendered
    assert "Profile: other" not in rendered
    assert "record-org" not in rendered and "record-owner" not in rendered
    assert "plan-org" not in rendered
    assert "plan-owner" not in rendered
