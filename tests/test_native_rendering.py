"""Supported native rendering remains deterministic and bounded."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods

import asyncio

from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeView, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import keyboard, send


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
