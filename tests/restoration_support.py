"""Offline SDK harness invoking the registered Slack callback."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
# pylint: disable=protected-access
from dataclasses import replace
from types import SimpleNamespace

from installed_test_support import JsonState
from native_test_support import native_invocation
from hermes_wisdom_plugin.native_review import callback_value
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime


class SlackHarness:  # pylint: disable=too-many-instance-attributes
    def __init__(self, root, service):
        self.posts = []
        self.edits = []
        self.tasks = []
        self.handler = None
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
        self.runtime.wire_slack(self, self.adapter)

    def action(self, _pattern):
        def register(handler):
            self.handler = handler
            return handler
        return register

    async def chat_postMessage(self, **payload):  # pylint: disable=invalid-name
        self.posts.append(payload)
        return {"channel": "D1", "ts": "card-1"}

    async def chat_update(self, **payload):
        self.edits.append(payload)
        return {"ok": True}

    async def flush(self):
        while self.tasks:
            await self.tasks.pop(0)

    async def click(self, record_id, action, **changes):
        body = {
            "user": {"id": "U1"}, "channel": {"id": "D1"},
            "team": {"id": "W1"}, "message": {"ts": "card-1"},
        }
        body.update(changes)

        async def ack():
            return None

        await self.handler(ack, body, {"value": callback_value(record_id, action)})
        return self.runtime.reviews.load(record_id)

    def record(self, record_id):
        return self.runtime.reviews.load(record_id)

    def view(self, record_id):
        return self.runtime.reviews.view(self.record(record_id))

    def clicked(self):
        return replace(self.caller, message_id="card-1")
