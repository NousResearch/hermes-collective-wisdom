"""Result-based native suggestion delivery through the real runtime and SDK seams."""
# pylint: disable=missing-function-docstring,protected-access
import asyncio

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_auto_suggest_cards import _build, _decisions, _rows, _suggest
from test_auto_suggest_card import _suggestion
from test_restored_native_flow import Service


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("by_card", [False, True])
def test_native_suggestion_settles_only_after_supervised_send(
    tmp_path, monkeypatch, surface, delivered, by_card,
):
    async def scenario():
        ui = surface(tmp_path, Service())
        route, _ = _suggestion(ui)
        method = "chat_postMessage" if surface is SlackHarness else "send_message"
        original = getattr(ui, method)

        async def send(**payload):
            if not delivered:
                raise OSError("synthetic transport failure")
            return await original(**payload)

        monkeypatch.setattr(ui, method, send)
        host, cards, controller = _build(
            tmp_path, surface=route, present=ui.runtime.present_auto_suggest, api=by_card,
        )
        _suggest(controller, platform=ui.caller.platform)
        if not by_card:
            controller.tool({"skill": "incident"}, session_id="s1")
        assert len(ui.tasks) == 1
        assert not cards.posted, "queueing must not write 'card posted' to history"
        assert not any(event == "shown" for event, _, _ in _decisions(host)), (
            "queueing is not proof the card was shown")
        assert _rows(host)[0]["state"] == "delivering"
        await ui.flush()
        snapshot = host.store.snapshot()
        shown = [row for row in _decisions(host) if row[0] == "shown"]
        if delivered:
            assert len(cards.posted) == int(by_card) and len(ui.posts) == 1
            assert _rows(host)[0]["state"] == "presented"
            assert shown == [("shown", ui.caller.platform, "card" if by_card else None)]
            assert snapshot["day"]["shown"] == 1
            await ui.flush()
            assert len(cards.posted) == int(by_card) and len(shown) == 1
            controller._native_delivered(_rows(host)[0], "delivered", by_card=by_card)
            assert len(cards.posted) == int(by_card)
            assert len([row for row in _decisions(host) if row[0] == "shown"]) == 1
        else:
            assert not cards.posted and not ui.posts
            assert not shown
            assert snapshot["day"]["shown"] == 0, "failed send returns the daily allowance"
            assert not host.store.seen("incident", "sha-i")
        controller.close()
        ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("boundary", ["route_lost", "spawn_failed", "cancelled", "closed"])
def test_native_delivery_without_success_never_records_history(
    tmp_path, monkeypatch, surface, boundary,
):
    async def scenario():
        ui = surface(tmp_path, Service())
        route, _ = _suggestion(ui)
        host, cards, controller = _build(
            tmp_path, surface=route, present=ui.runtime.present_auto_suggest,
        )
        if boundary == "spawn_failed":
            def fail_spawn(*_args, **_kwargs):
                raise RuntimeError("synthetic dispatch failure")
            ui.runtime.spawn_task = fail_spawn
        _suggest(controller, platform=ui.caller.platform)
        if boundary == "route_lost":
            ui.runtime.close()
        if boundary == "closed":
            controller.close()
        if boundary == "cancelled":
            async def cancelled(**_payload):
                raise asyncio.CancelledError
            method = "chat_postMessage" if surface is SlackHarness else "send_message"
            monkeypatch.setattr(ui, method, cancelled)
            with pytest.raises(asyncio.CancelledError):
                await ui.flush()
        else:
            await ui.flush()
        assert not cards.posted and not host.injected
        shown = [row for row in _decisions(host) if row[0] == "shown"]
        assert bool(shown) is (boundary == "closed")
        if boundary == "cancelled":
            assert _rows(host)[0]["state"] == "delivering"
            assert host.store.snapshot()["day"]["shown"] == 1
        elif boundary != "closed":
            assert host.store.snapshot()["day"]["shown"] == 0
        controller.close()
        ui.runtime.close()
    asyncio.run(scenario())
