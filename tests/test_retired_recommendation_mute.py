"""Retired recommendation mutes cannot suppress notices or authorize effects."""
# pylint: disable=missing-function-docstring,protected-access
import argparse
import asyncio

import pytest
from fastapi import FastAPI

from restoration_support import SlackHarness, TelegramHarness
from test_restored_native_flow import Service
from test_desktop_api import _request
from test_notices import Page, Service as NoticeService, event, notices
from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.commands import build_parser
from hermes_wisdom_plugin.desktop_api import build_router
from hermes_wisdom_plugin.runtime import InvocationService


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("action", ["settings.mute.1w", "settings.mute.off"])
def test_legacy_mute_callback_cannot_act_on_saved_settings(tmp_path, monkeypatch, harness, action):
    async def scenario():
        service = Service()
        legacy = {"revision": 7, "muted": True, "duration": "forever"}
        monkeypatch.setattr(service, "notification_settings", lambda **_kw: {
            "delivery_mode": "agent", "mode_writable": True, "mute_control": legacy,
        }, raising=False)
        writes = []
        monkeypatch.setattr(service, "choose_notification_mute",
                            lambda *_a, **_kw: writes.append(True), raising=False)
        ui = harness(tmp_path, service)
        identity = ui.runtime.begin_settings(ui.caller)["review_id"]
        await ui.flush()
        await ui.click(identity, action)
        assert not writes
        assert ui.record(identity)["state"] == "pending"
        assert ui.record(identity)["plan"]["mute_control"] == legacy
        assert not any("mute" in a.action for a in ui.view(identity).actions)
    asyncio.run(scenario())


def test_legacy_mute_does_not_suppress_feed_or_rewrite_preferences(tmp_path):
    preferences = {"muted": True, "suppressed": {}}
    service = NoticeService([Page([event()], "after", False)], preferences=preferences)
    shown = []
    assert notices(tmp_path, service, [1000]).run(renderer=shown.extend)["offered"] == 1
    assert preferences == {"muted": True, "suppressed": {}}


def test_retired_mute_route_cannot_dispatch():
    app = FastAPI()
    app.include_router(build_router(lambda: pytest.fail("retired route dispatched")))
    assert _request(app, "PATCH", "/settings/mute", json={
        "view_id": "valid-view-id", "control_id": "7", "duration": "1_week",
    }).status_code == 404


def test_retired_mute_command_and_client_are_unavailable():
    parser = argparse.ArgumentParser()
    build_parser(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["mute", "1w"])
    for name in ("recommendation_mute", "set_recommendation_mute"):
        assert not hasattr(WisdomClient, name)
    for name in ("mute", "choose_notification_mute", "notification_mute_status"):
        assert not hasattr(InvocationService, name)
