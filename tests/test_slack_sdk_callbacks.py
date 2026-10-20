"""Real slack-sdk response coverage for persisted native review callbacks."""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access
import asyncio
import logging

import pytest
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from native_test_support import owner
from restoration_support import SlackHarness


class Service:
    def __init__(self):
        self.applied = []

    def native_owner(self):
        return owner()

    def close(self):
        return None

    def install_plan(self, reference, **_kwargs):
        return {
            "operation": "install", "skill_id": reference.split("@v", 1)[0],
            "slug": "incident", "receipt": "exact-receipt", "allowed": True,
            "description": "Triage incidents", "version": 2,
        }

    def native_install_inspect(self, receipt):
        assert receipt == "exact-receipt"
        return [{"path": "SKILL.md", "content": "offline fixture"}]

    def install_apply(self, receipt, *, may_apply, **_kwargs):
        assert receipt == "exact-receipt"
        assert may_apply()
        self.applied.append(receipt)
        return {"installed": True}

    def prepare_candidate_offer(self, identity):
        return self.native_candidate_inspect(identity)

    def native_candidate_inspect(self, identity):
        return {"event_id": identity, "facts": {"name": "incident"}, "networked": False}

    def notification_settings(self, *, may_apply, include_mute_control):
        assert may_apply()
        assert include_mute_control is True
        return {
            "delivery_mode": "agent", "mode_writable": True,
            "mute_control": {"revision": 7},
        }


def _response(client, data):
    return AsyncSlackResponse(
        client=client, http_verb="POST", api_url="https://slack.invalid/api/chat.postMessage",
        req_args={}, data=data, headers={}, status_code=200,
    )


def _record_id(payload, label):
    buttons = [
        element
        for block in payload["blocks"]
        for element in block.get("elements", [])
        if element.get("type") == "button"
    ]
    value = next(button["value"] for button in buttons if button["text"]["text"] == label)
    return value.rsplit(":", 1)[-1]


@pytest.mark.usefixtures("full_release_scope")
def test_real_sdk_response_binds_exact_message_for_registered_callbacks(tmp_path):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        posted = {"count": 0}

        def real_response(_payload):
            posted["count"] += 1
            return _response(ui, {
                "ok": True, "channel": "D1", "ts": f"card-{posted['count']}",
            })

        ui.post_response = real_response
        await ui.runtime._send_notice(ui.caller, [{
            "category": "new_skill", "skill_id": "incident", "version": 2,
        }])
        install_id = _record_id(ui.posts[-1], "Install Skill")
        assert ui.record(install_id)["message_id"] == "card-1"
        assert ui.record(install_id)["display_ready"] is True

        await ui.click(install_id, "details")
        assert ui.edits[-1]["ts"] == "card-1"
        await ui.click(install_id, "back")
        await ui.click(install_id, "install")
        assert service.applied == ["exact-receipt"]

        await ui.runtime._send_notice(ui.caller, [{
            "category": "skill_ready_to_share", "event_id": "candidate-1",
        }])
        candidate_id = _record_id(ui.posts[-1], "View details")
        assert ui.record(candidate_id)["message_id"] == "card-2"
        assert ui.record(candidate_id)["display_ready"] is True

        await ui.click(candidate_id, "details")
        assert ui.record(candidate_id)["screen"] == "details"
        assert ui.edits[-1]["ts"] == "card-2"
        await ui.click(candidate_id, "back")
        assert ui.edits[-1]["ts"] == "card-2"
        await ui.click(candidate_id, "cancel")
        assert ui.record(candidate_id)["state"] == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    {"ok": True, "channel": "D-other", "ts": "card-1"},
    {"ok": True, "channel": "D1"},
])
def test_uncertain_sdk_ack_fails_closed_without_second_send(tmp_path, caplog, data):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        ui.post_response = lambda _payload: _response(ui, data)
        with caplog.at_level(logging.WARNING):
            await ui.runtime._send_notice(ui.caller, [
                {"category": "new_skill", "skill_id": "first", "version": 1},
            ])
        assert len(ui.posts) == 1
        identity = _record_id(ui.posts[0], "Install Skill")
        assert ui.record(identity)["message_id"] == ""
        assert ui.record(identity)["display_ready"] is False
        await ui.click(identity, "install")
        assert not service.applied and not ui.edits
        assert "WisdomValidationError" in caplog.text
        assert "D-other" not in caplog.text

    asyncio.run(scenario())


def test_callback_failure_logs_class_and_message_after_completed_effect(
    tmp_path, monkeypatch, caplog,
):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        ui.post_response = lambda _payload: _response(
            ui, {"ok": True, "channel": "D1", "ts": "card-1"},
        )
        await ui.runtime._send_notice(ui.caller, [{
            "category": "new_skill", "skill_id": "incident", "version": 2,
        }])
        identity = _record_id(ui.posts[-1], "Install Skill")

        original_edit = ui.chat_update
        async def failed_edit(**payload):
            if service.applied:
                raise ConnectionError("«redacted:xox…» callback body")
            return await original_edit(**payload)

        monkeypatch.setattr(ui, "chat_update", failed_edit)
        with caplog.at_level(logging.WARNING):
            await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        assert "ConnectionError: «redacted:xox…» callback body" in caplog.text
        assert "xoxb-super-secret" not in caplog.text

    asyncio.run(scenario())
