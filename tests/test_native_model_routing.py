"""Real lifecycle admission routes registered model tools to native reviews."""
# pylint: disable=missing-function-docstring,protected-access,missing-class-docstring
# pylint: disable=too-few-public-methods,invalid-name

import asyncio
import json
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState
from hermes_wisdom_plugin.native_review import callback_value
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.native_tools import register_native_tools


OWNER = {
    "profile_name": "default", "profile_home": "/profiles/default",
    "account_id": "portal-account-1", "organization_id": "org-1",
    "base_url": "https://gateway.invalid", "installation_id": "install-1",
}


class Service:
    def __init__(self):
        self.applied = []
        self.owner = dict(OWNER)
        self.inbox_reads = 0

    def native_owner(self):
        return dict(self.owner)

    def close(self):
        return None

    def install_plan(self, reference, *, update_mode=None):
        del update_mode
        skill_id = reference.split("@v", 1)[0]
        return {
            "operation": "install", "owner": dict(self.owner), "skill_id": skill_id,
            "slug": "incident-triage", "version": 2,
            "receipt": "receipt-1", "allowed": True,
        }

    def native_install_inspect(self, receipt):
        assert receipt == "receipt-1"
        return [{"path": "SKILL.md", "content": "offline fixture"}]

    def installed_setup_plan(self, skill_id, *, version, phase, index, command):
        return {
            "operation": "setup", "owner": dict(self.owner), "skill_id": skill_id,
            "version": version, "step": {
                "phase": phase, "index": index, "command": command,
            },
            "receipt": "setup-receipt", "allowed": True,
        }

    def inbox(self, *, invocation):
        del invocation
        self.inbox_reads += 1
        return {"private": True}

    def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
        del accept_partial
        assert may_apply is not None and may_apply() is True
        self.applied.append(receipt)
        return {"installed": True, "skill_id": "skill-1", "version": 2}

    def end_turn_notices(self, _render):
        return {"supported": True, "offered": 0, "polled": False}


class Adapter:
    _owner_profile = "default"

    @staticmethod
    def _is_interactive_user_authorized(user_id, **_kwargs):
        return user_id == "U_NATIVE"


class SlackApp:
    def __init__(self):
        self.posts = []
        self.handler = None
        self.client = self

    def action(self, _pattern):
        def register(handler):
            self.handler = handler
            return handler
        return register

    def command(self, _pattern):
        return lambda handler: handler

    async def chat_postMessage(self, **payload):
        self.posts.append(payload)
        return {"ok": True, "channel": payload["channel"], "ts": "card-1"}

    async def chat_update(self, **_payload):
        return {"ok": True}


def _event(adapter):
    del adapter
    source = SimpleNamespace(
        platform=SimpleNamespace(value="slack"), profile="default",
        chat_type="private", chat_id="D1", user_id="U_NATIVE",
        user_name="Shannon", thread_id="", scope_id="W1",
    )
    return SimpleNamespace(
        internal=False, source=source, message_id="request-1",
        get_command=lambda: "", get_command_args=lambda: "",
    )


def _gateway(adapter):
    return SimpleNamespace(
        _session_key_for_source=lambda _source: "slack:W1:D1:U_NATIVE",
        _delivery_adapter_for=lambda _source: adapter,
        _is_user_authorized_for_source=lambda source: source.user_id == "U_NATIVE",
    )


def _registered(runtime):
    definitions = {}
    context = SimpleNamespace(register_tool=lambda **definition: (
        definitions.setdefault(definition["name"], definition)
        or SimpleNamespace(dispose=lambda: None)
    ))
    register_native_tools(context, runtime)
    return definitions


def _admit(runtime, adapter, **changes):
    runtime.native.hook(event=_event(adapter), gateway=_gateway(adapter))
    facts = {
        "model": "fixture-model", "session_id": "session-1", "task_id": "task-1",
        "turn_id": "turn-1", "platform": "slack", "sender_id": "U_NATIVE",
        "parent_session_id": "",
    }
    facts.update(changes)
    runtime.native.lifecycle_hook(**facts)
    return facts


def test_lifecycle_admission_reaches_registered_consent_and_real_native_review(tmp_path):
    async def scenario():
        service = Service()
        pending = []

        def spawn(coroutine, **_kwargs):
            task = asyncio.create_task(coroutine)
            pending.append(task)
            return task

        native = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, None, spawn,
            installed_state=JsonState(tmp_path),
        )
        runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        definitions = _registered(runtime)
        _admit(runtime, adapter)

        result = json.loads(definitions["present_wisdom_consent"]["handler"](
            {
                "kind": "skill", "identity": "skill-1", "version": 2,
                "step": None, "title": "Review", "explanation": "Exact package review",
            },
            task_id="task-1", session_id="session-1",
        ))
        await asyncio.gather(*pending)

        assert result["status"] == "queued"
        assert result["executed"] is False
        assert len(app.posts) == 1
        assert "Exact package review" in app.posts[0]["text"]
        assert "Agent explanation (not authorization)" in app.posts[0]["text"]
        assert not service.applied
        review_id = result["delivery"]["review_id"]
        assert native.reviews.load(review_id)["route"]["actor_id"] == "U_NATIVE"
        assert native.reviews.load(review_id)["owner"]["account_id"] == "portal-account-1"

        async def ack():
            return None

        await app.handler(ack, {
            "user": {"id": "U_NATIVE"}, "channel": {"id": "D1"},
            "team": {"id": "W1"}, "message": {"ts": "card-1"},
        }, {"value": callback_value(review_id, "install")})
        assert service.applied == ["receipt-1"]

    asyncio.run(scenario())


def test_lifecycle_admission_keeps_installed_setup_consent_on_setup_path(tmp_path):
    async def scenario():
        service = Service()
        pending = []

        def spawn(coroutine, **_kwargs):
            task = asyncio.create_task(coroutine)
            pending.append(task)
            return task

        native = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, None, spawn,
            installed_state=JsonState(tmp_path),
        )
        runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        definitions = _registered(runtime)
        _admit(runtime, adapter)

        result = json.loads(definitions["present_wisdom_consent"]["handler"](
            {
                "kind": "setup", "identity": "skill-1", "version": 3,
                "step": {"phase": "setup", "index": 0, "command": "python configure.py"},
                "title": "Review setup", "explanation": "Exact installed setup step",
            },
            task_id="task-1", session_id="session-1",
        ))
        await asyncio.gather(*pending)

        review = native.reviews.load(result["delivery"]["review_id"])
        assert review["operation"] == "setup"
        assert review["plan"]["receipt"] == "setup-receipt"
        assert not service.applied

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"parent_session_id": "parent"},
    {"platform": "telegram"},
    {"sender_id": "OTHER"},
])
def test_lifecycle_rejects_subagent_or_changed_native_route(tmp_path, changes):
    async def scenario():
        native = NativeSurfaceRuntime(
            tmp_path, "default", Service, None, lambda *_args, **_kwargs: None,
            installed_state=JsonState(tmp_path),
        )
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        _admit(SimpleNamespace(native=native), adapter, **changes)
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

    asyncio.run(scenario())


def test_lifecycle_rejects_group_route(tmp_path):
    async def scenario():
        native = NativeSurfaceRuntime(
            tmp_path, "default", Service, None, lambda *_args, **_kwargs: None,
            installed_state=JsonState(tmp_path),
        )
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        event = _event(adapter)
        event.source.chat_type = "channel"
        native.hook(event=event, gateway=_gateway(adapter))
        native.lifecycle_hook(
            session_id="session-1", task_id="task-1", turn_id="turn-1",
            platform="slack", sender_id="U_NATIVE", parent_session_id="",
        )
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

    asyncio.run(scenario())


def test_owner_change_denies_private_read_before_service_disclosure(tmp_path):
    async def scenario():
        service = Service()
        native = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, None, lambda *_args, **_kwargs: None,
            installed_state=JsonState(tmp_path),
        )
        runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        definitions = _registered(runtime)
        _admit(runtime, adapter)
        service.owner["account_id"] = "portal-account-2"

        with pytest.raises(PermissionError, match="ownership changed"):
            definitions["wisdom_inbox"]["handler"](
                {}, task_id="task-1", session_id="session-1",
            )

        assert service.inbox_reads == 0

    asyncio.run(scenario())


def test_native_turn_grant_is_fenced_by_key_end_turn_lease_expiry_and_unload(
    tmp_path, monkeypatch,
):
    async def scenario():
        now = [100.0]
        monkeypatch.setattr("hermes_wisdom_plugin.native_runtime.time.monotonic", lambda: now[0])
        native = NativeSurfaceRuntime(
            tmp_path, "default", Service, None, lambda *_args, **_kwargs: None,
            installed_state=JsonState(tmp_path),
        )
        adapter, app = Adapter(), SlackApp()
        native.wire_slack(app, adapter)
        _admit(SimpleNamespace(native=native), adapter)
        assert native.tool_invocation(task_id="wrong", session_id="session-1") is None
        assert native.tool_invocation(task_id="task-1", session_id="wrong") is None
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is not None

        native.wire_slack(app, adapter)
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

        _admit(SimpleNamespace(native=native), adapter)
        now[0] = 701.0
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

        now[0] = 800.0
        _admit(SimpleNamespace(native=native), adapter)
        native.end_turn(task_id="task-1", session_id="session-1")
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

        _admit(SimpleNamespace(native=native), adapter)
        native.close()
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

    asyncio.run(scenario())


def test_local_owner_recheck_rejects_an_expired_grant(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("hermes_wisdom_plugin.native_runtime.time.monotonic", lambda: now[0])
    native = NativeSurfaceRuntime(
        tmp_path, "default", Service, None, lambda *_args, **_kwargs: None,
        installed_state=JsonState(tmp_path),
    )
    native.admit_local_tool(
        owner=OWNER, surface="cli", session_id="session-1", task_id="task-1",
        turn_id="turn-1", route={"session_id": "session-1"},
    )
    invocation = native.tool_invocation(task_id="task-1", session_id="session-1")
    assert invocation is not None
    now[0] = 701.0

    with pytest.raises(PermissionError, match="ownership changed"):
        native.validate_local_tool_owner(invocation, Service())


def test_unwired_native_renderer_cannot_admit_model_tools(tmp_path):
    async def scenario():
        native = NativeSurfaceRuntime(
            tmp_path, "default", Service, None, lambda *_args, **_kwargs: None,
            installed_state=JsonState(tmp_path),
        )
        adapter = Adapter()
        _admit(SimpleNamespace(native=native), adapter)
        assert native.tool_invocation(task_id="task-1", session_id="session-1") is None

    asyncio.run(scenario())
