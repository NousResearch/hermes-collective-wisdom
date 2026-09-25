"""Offline Telegram SDK callbacks cover restored inspection and quiet dismissal."""
# pylint: disable=missing-function-docstring
import asyncio
import html
import json

import pytest

from restoration_support import (
    TelegramHarness, assert_cancelled_review_is_inert,
    assert_owner_switch_after_post_is_cleaned,
)
from native_test_support import owner
from test_restored_native_flow import Service
from hermes_wisdom_plugin.errors import WisdomUnavailable


@pytest.mark.parametrize("command,target", [
    ("browse", "incident"), ("show", "skill-1"), ("versions", "skill-1"),
])
def test_telegram_failed_catalog_read_offers_manual_retry_without_card(
    tmp_path, monkeypatch, command, target,
):
    async def scenario():
        service = Service()
        calls = []

        def unavailable(*args, **kwargs):
            calls.append((args, kwargs))
            raise WisdomUnavailable()

        method = {"browse": "search_skills", "show": "show", "versions": "versions"}[command]
        monkeypatch.setattr(service, method, unavailable, raising=False)
        ui = TelegramHarness(tmp_path, service)
        result = ui.runtime.begin_catalog(ui.caller, command, target)
        assert result == (
            "Collective Wisdom is temporarily unavailable. "
            f"Resend /wisdom {command} {target} to try again."
        )
        assert len(calls) == 1
        assert not ui.posts and not ui.edits and not service.applied
        assert ui.runtime.reviews.pending(owner()) == []

    asyncio.run(scenario())


def test_telegram_failed_read_card_alert_does_not_edit_or_replay(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = TelegramHarness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()
        before = ui.record(identity)
        answers = []

        async def answer(**kwargs):
            answers.append(kwargs)

        monkeypatch.setattr(ui, "_answer", answer)
        calls = []

        def unavailable(receipt):
            calls.append(receipt)
            raise WisdomUnavailable()

        monkeypatch.setattr(service, "native_install_inspect", unavailable)
        await ui.click(identity, "details")
        assert calls == ["exact-receipt"]
        assert answers == [{
            "text": (
                "Collective Wisdom is temporarily unavailable. "
                "Try this read-only control again later."
            ),
            "show_alert": True,
        }]
        assert ui.record(identity) == before
        assert not ui.edits and not service.applied
        await ui.click(identity, "details", actor_id=22)
        assert calls == ["exact-receipt"]
        assert answers[-1] == {}

    asyncio.run(scenario())


def test_telegram_read_failure_after_owner_change_has_no_retry_alert(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        current_owner = {"value": owner()}
        monkeypatch.setattr(service, "native_owner", lambda: current_owner["value"])
        ui = TelegramHarness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()
        answers = []

        async def answer(**kwargs):
            answers.append(kwargs)

        monkeypatch.setattr(ui, "_answer", answer)

        def unavailable(_receipt):
            current_owner["value"] = owner(account_id="other")
            raise WisdomUnavailable()

        monkeypatch.setattr(service, "native_install_inspect", unavailable)
        await ui.click(identity, "details")
        assert answers == [{}]
        assert not ui.edits and not service.applied

    asyncio.run(scenario())


def test_telegram_complete_inspection_then_quiet_dismissal(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        plan = service.publication_review("draft")
        plan["files"][0]["content_utf8"] = "first\n" + "<&" * 1500 + "\nlast"
        monkeypatch.setattr(service, "publication_review", lambda _draft: plan)
        ui = TelegramHarness(tmp_path, service)
        response = ui.runtime.begin_publication(ui.caller, "draft")
        await ui.flush()
        identity = response["review_id"]
        assert "Share My Skill" in str(ui.posts[-1]["reply_markup"])
        await ui.click(identity, "details", actor_id=22)
        assert not ui.edits
        await ui.click(identity, "details")
        chunks = []
        while True:
            payload = ui.edits[-1]
            assert len(payload["text"]) < 4000
            decoded = html.unescape(payload["text"])
            chunks.append(decoded.split("Exact review data (JSON)\n", 1)[1])
            assert "confirm:" not in str(payload["reply_markup"])
            if not await ui.next_page(identity):
                break
        assert json.loads("".join(chunks)) == {
            key: value for key, value in plan.items() if key not in {"draft_id", "receipt"}
        }
        await ui.click(identity, "back")
        before = ui.edits[-1]["text"]
        assert not service.applied
        await ui.click(identity, "cancel")
        assert ui.edits[-1]["text"] == before
        assert ui.edits[-1]["reply_markup"] is None
        assert len(ui.posts) == 1
        await ui.click(identity, "confirm")
        assert not service.applied
    asyncio.run(scenario())


def test_telegram_posted_card_is_cancelled_and_deleted_after_account_switch(
    tmp_path, monkeypatch,
):
    asyncio.run(assert_owner_switch_after_post_is_cleaned(
        monkeypatch, TelegramHarness, tmp_path, Service(),
        {"chat_id": 11, "message_id": 123},
    ))


def test_telegram_unavailable_owner_and_delete_method_leave_card_cancelled(
    tmp_path, monkeypatch,
):
    async def scenario():
        service = Service()
        current_owner: dict[str, object] = {"value": owner()}
        monkeypatch.setattr(service, "native_owner", lambda: current_owner["value"])
        ui = TelegramHarness(tmp_path, service)
        posted = ui.send_message

        async def lose_owner_lookup_after_post(**payload):
            response = await posted(**payload)
            current_owner["value"] = None
            return response

        monkeypatch.setattr(ui, "send_message", lose_owner_lookup_after_post)
        monkeypatch.setattr(ui, "delete_message", None)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()

        assert not ui.deletes
        assert ui.record(identity)["state"] == "cancelled"
        await assert_cancelled_review_is_inert(
            ui, service, identity, lambda: current_owner.update(value=owner())
        )

    asyncio.run(scenario())
