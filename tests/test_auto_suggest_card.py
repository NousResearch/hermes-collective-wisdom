"""The three-choice suggestion card on rendered Slack and Telegram output."""
# pylint: disable=missing-function-docstring,protected-access
import asyncio

import pytest

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_native_share_card import WarningService
from test_restored_native_flow import Service
from hermes_wisdom_plugin.auto_suggest import SuggestionStore
from hermes_wisdom_plugin.auto_suggest_card import DISMISS, NEVER, SHARE

LABELS = ["Confirm share", "Don't share", "Never for this skill"]


def _suggestion(ui):
    caller = ui.caller
    route = {"kind": "messaging", "native": True, "platform": caller.platform,
             "chat_type": caller.chat_type, "chat_id": caller.chat_id,
             "user_id": caller.actor_id, "thread_id": caller.thread_id,
             "scope_id": caller.scope_id, "session_key": caller.session_key}
    return route, {"id": "s-1", "skill": "incident", "description": "Review incident notes.",
                   "reason": "Teams handle incidents often.", "content_hash": "sha-1"}


async def _card(ui):
    route, suggestion = _suggestion(ui)
    queued = ui.runtime.present_auto_suggest(route, "session-1", suggestion)
    assert queued["state"] == "queued"
    await ui.flush()
    return queued["review_id"]


def _rendered_labels(ui, payload):
    if isinstance(ui, TelegramHarness):
        return [button.text for row in payload["reply_markup"].inline_keyboard for button in row]
    return [button["text"]["text"] for block in payload["blocks"] if block["type"] == "actions"
            for button in block["elements"]]


def _store(tmp_path):
    return SuggestionStore(JsonState(tmp_path), tmp_path)


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_card_renders_exactly_three_choices(tmp_path, surface):
    async def scenario():
        ui = surface(tmp_path, Service())
        identity = await _card(ui)
        assert _rendered_labels(ui, ui.posts[-1]) == LABELS
        assert [action.action for action in ui.view(identity).actions] == [SHARE, DISMISS, NEVER]
        assert "incident" in str(ui.posts[-1])
        assert "Nothing is shared until you approve the share review." in str(ui.posts[-1])
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_confirm_share_opens_the_share_review_once(tmp_path, surface):
    async def scenario():
        service = WarningService()
        ui = surface(tmp_path, service)
        identity = await _card(ui)
        posts = len(ui.posts)
        await ui.click(identity, SHARE)
        record = ui.record(identity)
        assert record["operation"] == "suggest"
        assert record["auto_suggest"]["skill_name"] == "incident"
        assert ui.view(identity).title == "⚠️ Review warnings"
        assert not service.applied, "Confirm only opens the review; nothing is shared yet"
        await ui.click(identity, SHARE)  # The old control is no longer on the card.
        assert ui.record(identity)["operation"] == "suggest" and not service.applied
        # The normal review continues on the same card: acknowledge, then exact consent.
        await ui.click(identity, "suggest.ack")
        await ui.click(identity, "confirm")
        assert (service.applied, ui.record(identity)["state"]) == (
            ["publication-receipt"], "completed")
        assert len(ui.posts) == posts, "the review replaces the card in place"
        events = [row["event"] for row in _store(tmp_path).snapshot()["decisions"]]
        assert events.count("choice") == 1
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize(("action", "suppressed", "seen"), [
    (DISMISS, False, True), (NEVER, True, False),
])
def test_dont_share_and_never_persist_exactly_once(  # pylint: disable=too-many-arguments
    tmp_path, surface, action, suppressed, seen,
):
    async def scenario():
        ui = surface(tmp_path, Service())
        identity = await _card(ui)
        await ui.click(identity, action)
        store = _store(tmp_path)
        assert store.suppressed("incident") is suppressed
        assert store.seen("incident", "sha-1") is seen
        assert ui.record(identity)["state"] == "completed"
        assert not ui.view(identity).actions, "the card collapses to a one-line result"
        await ui.click(identity, DISMISS if action == NEVER else NEVER)
        choices = [row for row in store.snapshot()["decisions"] if row["event"] == "choice"]
        assert len(choices) == 1
        assert store.suppressed("incident") is suppressed
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_another_user_cannot_answer_the_card(tmp_path, surface):
    async def scenario():
        ui = surface(tmp_path, Service())
        identity = await _card(ui)
        stranger = ({"actor_id": 99} if surface is TelegramHarness
                    else {"user": {"id": "U2"}})
        await ui.click(identity, NEVER, **stranger)
        assert ui.record(identity)["state"] == "pending"
        assert not _store(tmp_path).suppressed("incident")
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_forged_action_is_refused(tmp_path, surface):
    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = await _card(ui)
        await ui.click(identity, "confirm")
        await ui.click(identity, "autosuggest.forged")
        assert ui.record(identity)["state"] == "pending"
        assert not service.applied and not _store(tmp_path).snapshot()["decisions"]
        ui.runtime.close()
    asyncio.run(scenario())


def test_group_chat_route_gets_no_card(tmp_path):
    async def scenario():
        ui = SlackHarness(tmp_path, Service())
        route, suggestion = _suggestion(ui)
        assert ui.runtime.present_auto_suggest(
            {**route, "chat_type": "channel"}, "session-1", suggestion)["state"] == "unavailable"
        await ui.flush()
        assert not ui.posts
        ui.runtime.close()
    asyncio.run(scenario())
