"""Offline SDK harness invoking the registered Slack callback."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
# pylint: disable=protected-access
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from installed_test_support import JsonState
from native_test_support import native_invocation, owner
from hermes_wisdom_plugin.native_review import callback_value
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime


class SlackHarness:  # pylint: disable=too-many-instance-attributes
    def __init__(self, root, service):
        self.posts = []
        self.edits = []
        self.deletes = []
        self.tasks = []
        self.handler = None
        self.command_handler: Any = None
        self.command_pattern = None
        self.post_response: Any = None
        self.client = self
        self.adapter = SimpleNamespace(
            _owner_profile="default",
            _is_interactive_user_authorized=lambda actor, **_kw: actor == "U1",
        )
        self.caller = native_invocation(self.adapter, thread_id="")
        self.runtime = NativeSurfaceRuntime(
            root, "default", lambda: service, None,
            lambda coro, **_kw: self.tasks.append(coro), installed_state=JsonState(root),
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            async def wire():
                self.runtime.wire_slack(self, self.adapter)

            asyncio.run(wire())
        else:
            self.runtime.wire_slack(self, self.adapter)

    def action(self, _pattern):
        def register(handler):
            self.handler = handler
            return handler
        return register

    def command(self, pattern):
        def register(handler):
            self.command_pattern = pattern
            self.command_handler = handler
            return handler
        return register

    async def chat_postMessage(self, **payload):  # pylint: disable=invalid-name
        self.posts.append(payload)
        if callable(self.post_response):
            return self.post_response(payload)
        if self.post_response is not None:
            return self.post_response
        return {"channel": "D1", "ts": "card-1"}

    async def chat_update(self, **payload):
        self.edits.append(payload)
        return {"ok": True}

    async def chat_delete(self, **payload):
        self.deletes.append(payload)
        return {"ok": True}

    async def flush(self):
        while self.tasks:
            await self.tasks.pop(0)

    async def click(self, record_id, action, **changes):
        message_id = self.record(record_id).get("message_id") or "card-1"
        body = {
            "user": {"id": "U1"}, "channel": {"id": "D1"},
            "team": {"id": "W1"}, "message": {"ts": message_id},
        }
        body.update(changes)

        async def ack():
            return None

        await self.handler(ack, body, {"value": callback_value(record_id, action)})
        return self.runtime.reviews.load(record_id)

    async def next_page(self, record_id):
        following = next((a for a in self.view(record_id).actions if a.label == "Next"), None)
        if following is None:
            return False
        await self.click(record_id, following.action)
        return True

    def record(self, record_id):
        return self.runtime.reviews.load(record_id)

    def view(self, record_id):
        return self.runtime.reviews.view(self.record(record_id))

    def clicked(self):
        return replace(self.caller, message_id="card-1")


class TelegramHarness(SlackHarness):
    """Exercise the actual registered Telegram handler and plain SDK fallback."""

    def __init__(self, root, service):
        super().__init__(root, service)
        self.adapter._bot = self
        self.adapter._is_callback_user_authorized = lambda actor, **_kw: actor == "11"
        self.caller = replace(
            self.caller, platform="telegram", chat_type="private", chat_id="11",
            actor_id="11", scope_id="", session_key="telegram:11",
        )
        self.runtime.wire_telegram(self, self.adapter)

    def add_handler(self, handler, **_kwargs):
        self.handler = handler.callback

    async def send_message(self, **payload):
        self.posts.append(payload)
        return SimpleNamespace(message_id=123, chat_id=11)

    async def delete_message(self, **payload):
        self.deletes.append(payload)
        return True

    async def click(self, record_id, action, **changes):
        from telegram.ext import ApplicationHandlerStop  # pylint: disable=import-outside-toplevel
        actor = changes.get("actor_id", 11)
        query = SimpleNamespace(
            data=callback_value(record_id, action),
            from_user=SimpleNamespace(id=actor, username="fixture"),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private"), chat_id=11, message_id=123,
                message_thread_id=None, get_bot=lambda: self,
            ),
            answer=self._answer, edit_message_text=self.chat_update,
        )
        with pytest.raises(ApplicationHandlerStop):
            await self.handler(SimpleNamespace(callback_query=query), None)
        return self.record(record_id)

    async def _answer(self):
        return None


def switch_owner_after_post(monkeypatch, ui, current_owner):
    """Switch fixture ownership only after the provider has accepted a card."""
    method_name = "chat_postMessage" if ui.caller.platform == "slack" else "send_message"
    posted = getattr(ui, method_name)

    async def switch(**payload):
        response = await posted(**payload)
        current_owner["value"] = owner(account_id="other")
        return response

    monkeypatch.setattr(ui, method_name, switch)


async def assert_cancelled_review_is_inert(ui, service, identity, restore_owner):
    """Prove a stale visible card stays unusable after ownership returns."""
    restore_owner()
    await ui.click(identity, "review")
    assert not service.applied
    assert ui.record(identity)["state"] == "cancelled"


async def assert_owner_switch_after_post_is_cleaned(
    monkeypatch, harness_type, root, service, expected_delete,
):
    """Exercise the shared post-await ownership race for one provider harness."""
    current_owner = {"value": owner()}
    monkeypatch.setattr(service, "native_owner", lambda: current_owner["value"])
    ui = harness_type(root, service)
    switch_owner_after_post(monkeypatch, ui, current_owner)
    identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
    await ui.flush()

    assert ui.deletes == [expected_delete]
    assert ui.record(identity)["state"] == "cancelled"
    assert ui.record(identity)["message_id"] == ""
    assert ui.record(identity)["display_ready"] is False
    await assert_cancelled_review_is_inert(
        ui, service, identity, lambda: current_owner.update(value=owner())
    )
