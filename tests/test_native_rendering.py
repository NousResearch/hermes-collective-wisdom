"""Supported native rendering remains deterministic and bounded."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods

import asyncio
import html
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import _human_render
from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeView, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import edit, keyboard, plain_html, send


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
        "receipt-exact", "sha256:content", "sha256:manifest",
        'compatible_after_setup', '"accept_partial": true', "ask",
        "Review shell capability", "shell", "scripts/check.sh", "exec", "sha256:file",
    ):
        assert exact_fact in text


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

    for fact in (
        '"version": 7', "Organization: org-1", "Owner account: owner-1",
        "Profile: work", "Review expires: 12345.5", "Approval permission: allowed",
    ):
        assert fact in fallback
        assert fact in str(slack)
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
    assert "Organization: record-org" in rendered
    assert "Owner account: record-owner" in rendered
    assert "Profile: work" in rendered
    assert "plan-org" not in rendered
    assert "plan-owner" not in rendered
