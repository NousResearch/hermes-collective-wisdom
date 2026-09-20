"""Supported native rendering remains deterministic and bounded."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods

import asyncio
import html
from types import SimpleNamespace

import pytest

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
        "state": "pending", "operation": "install", "plan": plan,
    }).to_text()

    for exact_fact in (
        "receipt-exact", "sha256:content", "sha256:manifest",
        "compatible_after_setup", "Accept partial: True", "ask",
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
    }

    view = project_view(record)
    fallback = html.unescape(plain_html(view))
    slack = blocks(view, "record-1")

    for fact in (
        "Version: 7", "Organization: org-1", "Owner account: owner-1",
        "Profile: work", "Review expires: 12345.5", "Approval permission: allowed",
    ):
        assert fact in fallback
        assert fact in str(slack)
    assert "wi:agent:confirm:record-1" in str(keyboard(view, "record-1"))


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
    })

    rendered = view.to_text()
    assert "Organization: record-org" in rendered
    assert "Owner account: record-owner" in rendered
    assert "Profile: work" in rendered
    assert "plan-org" not in rendered
    assert "plan-owner" not in rendered
