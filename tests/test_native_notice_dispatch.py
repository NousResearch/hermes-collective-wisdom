"""Native notice dispatch crosses from sync lifecycle workers to adapter loops."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access
# pylint: disable=too-few-public-methods,broad-exception-caught,invalid-name

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time

import pytest

from installed_test_support import JsonState
from native_test_support import owner
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime


class AdapterLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.tasks = []

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self):
        self.thread.start()
        self.call(lambda: None)
        return self

    def call(self, callback):
        result = Future()

        def invoke():
            try:
                result.set_result(callback())
            except BaseException as exc:  # pragma: no cover - propagated to test thread
                result.set_exception(exc)

        self.loop.call_soon_threadsafe(invoke)
        return result.result(timeout=2)

    def spawn(self, coroutine, *, name=None):
        task = asyncio.get_running_loop().create_task(coroutine, name=name)
        self.tasks.append(task)
        return task

    def drain(self):
        async def wait():
            pending = [task for task in self.tasks if not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(wait(), self.loop).result(timeout=2)

    def stop(self):
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)
        self.loop.close()


class Adapter:
    _owner_profile = "default"


class SlackNative:
    def __init__(self):
        self.client = self
        self.posts = []
        self.sent = threading.Event()

    @staticmethod
    def action(_pattern):
        return lambda callback: callback

    @staticmethod
    def command(_pattern):
        return lambda callback: callback

    async def chat_postMessage(self, **payload):
        self.posts.append((asyncio.get_running_loop(), payload))
        self.sent.set()
        return {"ok": True, "channel": payload["channel"], "ts": "notice-1"}


class TelegramNative:
    def __init__(self):
        self.sent = threading.Event()
        self.posts = []
        self.bot = self

    @staticmethod
    def add_handler(*_args, **_kwargs):
        return None

    async def do_api_request(self, endpoint, *, api_kwargs):
        self.posts.append((asyncio.get_running_loop(), endpoint, api_kwargs))
        self.sent.set()
        return {
            "ok": True,
            "result": {"message_id": 77, "chat": {"id": api_kwargs["chat_id"]}},
        }


class NoticeService:
    def __init__(self):
        self.owner = owner()
        self.eligible = True
        self.polls = 0
        self.closed = 0

    def native_owner(self):
        return dict(self.owner)

    def end_turn_notices(self, renderer):
        self.polls += 1
        rows = []
        if self.eligible:
            self.eligible = False
            rows = [{
                "category": "new_skill", "event_id": "event-1",
                "skill_id": "skill-1", "version": 1,
            }]
            renderer(rows)
        return {
            "supported": True, "offered": len(rows), "polled": True, "more": False,
        }

    def install_plan(self, reference, *, update_mode=None):
        del update_mode
        return {
            "operation": "install", "owner": self.native_owner(), "skill_id": reference,
            "version": 1, "receipt": "receipt-1", "allowed": True,
        }

    def close(self):
        self.closed += 1


def _wire(runtime, platform, host, adapter):
    native = SlackNative() if platform == "slack" else TelegramNative()
    if platform == "telegram":
        adapter._bot = getattr(native, "bot")
    host.call(lambda: getattr(runtime, f"wire_{platform}")(native, adapter))
    return native


def _grant(runtime, platform, adapter, *, task_id="task-1", session_id="session-1"):
    invocation = NativeInvocation(
        platform=platform, profile="default", chat_type="private", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="", scope_id="W1",
        message_id="request-1", session_key=f"{platform}:D1:U1", adapter=adapter,
        host_session_id=session_id, task_id=task_id, turn_id="turn-1",
    )
    route = (platform, id(adapter))
    runtime._turns[(task_id, session_id)] = (
        time.monotonic() + 60, invocation, owner(), runtime._leases[route],
    )


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_sync_end_turn_hands_notice_to_the_owning_adapter_loop(tmp_path, platform):
    host = AdapterLoop().start()
    service = NoticeService()
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, host.spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    try:
        native = _wire(runtime, platform, host, adapter)
        _grant(runtime, platform, adapter)

        result = runtime.end_turn(task_id="task-1", session_id="session-1")

        assert result["scheduled"] is True
        assert native.sent.wait(2)
        host.drain()
        assert service.polls == 1
        assert native.posts[0][0] is host.loop
        assert runtime.reviews.pending(owner())[0]["message_id"] in {"notice-1", "77"}
    finally:
        runtime.close()
        host.stop()


def test_scheduler_failure_keeps_notice_for_next_ordinary_turn(tmp_path, monkeypatch):
    host = AdapterLoop().start()
    service = NoticeService()

    def broken_spawn(coroutine, **_kwargs):
        coroutine.close()
        raise RuntimeError("scheduler unavailable")

    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, broken_spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    sent = threading.Event()

    async def transport(*_args, **_kwargs):
        sent.set()

    monkeypatch.setattr(runtime, "_send_notice", transport)
    try:
        _wire(runtime, "slack", host, adapter)
        _grant(runtime, "slack", adapter)
        assert runtime.end_turn(task_id="task-1", session_id="session-1")["scheduled"] is True
        host.call(lambda: None)
        assert service.polls == 0
        assert service.eligible is True

        runtime.spawn_task = host.spawn
        _grant(runtime, "slack", adapter, task_id="task-2", session_id="session-2")
        assert runtime.end_turn(task_id="task-2", session_id="session-2")["scheduled"] is True
        assert sent.wait(2)
        host.drain()
        assert service.polls == 1
        assert service.eligible is False
    finally:
        runtime.close()
        host.stop()


def test_notice_transport_does_not_require_a_second_scheduler_handoff(tmp_path):
    host = AdapterLoop().start()
    service = NoticeService()
    spawn_calls = []

    def one_spawn(coroutine, **kwargs):
        spawn_calls.append(kwargs.get("name"))
        if len(spawn_calls) > 1:
            coroutine.close()
            raise RuntimeError("second scheduling handoff refused")
        return host.spawn(coroutine, **kwargs)

    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, one_spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    try:
        native = _wire(runtime, "slack", host, adapter)
        _grant(runtime, "slack", adapter)

        assert runtime.end_turn(task_id="task-1", session_id="session-1")["scheduled"] is True
        assert native.sent.wait(2)
        host.drain()
        assert spawn_calls == ["wisdom-notice:slack:D1:U1"]
    finally:
        runtime.close()
        host.stop()


def test_ambiguous_transport_outcome_is_not_replayed(tmp_path):
    host = AdapterLoop().start()
    service = NoticeService()
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, host.spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    attempted = threading.Event()
    attempts = []

    try:
        native = _wire(runtime, "slack", host, adapter)

        async def uncertain_send(**payload):
            attempts.append(payload)
            attempted.set()
            raise TimeoutError("transport acknowledgement lost")

        setattr(native, "chat_postMessage", uncertain_send)
        _grant(runtime, "slack", adapter)
        runtime.end_turn(task_id="task-1", session_id="session-1")
        assert attempted.wait(2)
        host.drain()

        _grant(runtime, "slack", adapter, task_id="task-2", session_id="session-2")
        runtime.end_turn(task_id="task-2", session_id="session-2")
        host.drain()

        assert len(attempts) == 1
        assert service.polls == 2
    finally:
        runtime.close()
        host.stop()


@pytest.mark.parametrize("closed", [False, True])
def test_inactive_route_loop_is_rejected_before_notice_poll(tmp_path, closed):
    first = AdapterLoop().start()
    service = NoticeService()
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, first.spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    try:
        _wire(runtime, "slack", first, adapter)
        _grant(runtime, "slack", adapter)
        first.loop.call_soon_threadsafe(first.loop.stop)
        first.thread.join(timeout=2)
        assert not first.loop.is_running()
        if closed:
            first.loop.close()

        result = runtime.end_turn(task_id="task-1", session_id="session-1")

        assert result["scheduled"] is False
        assert service.polls == 0
        assert service.eligible is True
    finally:
        runtime.close()
        first.stop()


@pytest.mark.parametrize("change", ["owner", "profile", "route", "unload"])
def test_stale_native_route_is_rejected_before_consuming_notice(
    tmp_path, change,
):
    host = AdapterLoop().start()
    gate = threading.Event()
    entered = threading.Event()
    service = NoticeService()
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, host.spawn,
        installed_state=JsonState(tmp_path),
    )
    adapter = Adapter()
    try:
        _wire(runtime, "slack", host, adapter)
        _grant(runtime, "slack", adapter)

        def block_loop():
            entered.set()
            gate.wait(2)

        host.loop.call_soon_threadsafe(block_loop)
        assert entered.wait(2)
        assert runtime.end_turn(task_id="task-1", session_id="session-1")["scheduled"] is True
        if change == "owner":
            service.owner["account_id"] = "other-account"
        elif change == "profile":
            adapter._owner_profile = "other-profile"
        elif change == "route":
            with runtime._lock:
                runtime._leases[("slack", id(adapter))] = object()
        else:
            runtime.close()
        gate.set()
        host.call(lambda: None)
        host.drain()

        assert service.polls == 0
        assert service.eligible is True
    finally:
        gate.set()
        runtime.close()
        host.stop()


class ContextService(NoticeService):
    def __init__(self):
        super().__init__()
        self.eligible = False
        self.feed_reads = []
        self.plans = []
        self.preferences = {"muted": False, "suppressed": {}}

    def notice_policy(self):
        return {"new_skill": True, "recommendation_mode": "useful_only"}

    def notice_feed(self, cursor):
        self.feed_reads.append(cursor)
        return SimpleNamespace(events=[
            {"category": "new_skill", "kind": "new", "event_id": "event-incident",
             "skill_id": "incident", "version": 2}
        ])

    def notice_version(self, skill_id, version):
        return {"skill": {"id": skill_id, "state": "active", "slug": "incident-response",
                          "organization_id": self.owner["organization_id"]},
                "version": {"version": version, "state": "published",
                            "author_description": "Triage incidents"}}

    def notice_preferences(self, _references):
        return self.preferences

    def install_plan(self, reference, *, update_mode=None):
        self.plans.append(reference)
        plan = super().install_plan(reference, update_mode=update_mode)
        plan["skill_id"] = "incident"
        plan["version"] = 2
        return plan

    def end_turn_notices(self, renderer):
        if self.eligible:
            self.eligible = False
            self.polls += 1
            renderer([{"category": "new_skill", "event_id": "event-incident",
                       "skill_id": "incident", "version": 2}])
            return {"supported": True, "offered": 1, "polled": True, "more": False}
        return super().end_turn_notices(renderer)


class Decision:
    def __init__(self, index=0, unavailable=False):
        self.index, self.unavailable, self.calls, self.cancelled = index, unavailable, [], []

    def launch(self, request):
        self.calls.append(request)
        assert request.private_default_route is True and request.role == "leaf"
        assert request.parent_session_id == "session-1"
        assert request.model is None and request.allowed_toolsets is None
        if self.unavailable:
            raise RuntimeError("default route unavailable")
        return object()

    def wait(self, _handle, *, timeout_seconds):
        assert timeout_seconds <= 10
        return SimpleNamespace(state="SUCCEEDED", completed=True, timed_out=False)

    def result(self, _handle):
        return SimpleNamespace(ready=True, terminal_state="SUCCEEDED",
                               summary=str(self.index))

    def cancel(self, handle, *, reason):
        self.cancelled.append((handle, reason))


@pytest.mark.parametrize("case", [
    "relevant", "unrelated", "no_context", "unavailable", "stale_owner", "wrong_turn",
    "old_host", "muted", "suppressed", "duplicate", "credential_label",
    "credential_bare", "credential_bearer",
])
def test_context_relevance_end_turn_and_exact_version(tmp_path, case):
    host = AdapterLoop().start()
    service = ContextService()
    decision = Decision(index=-1 if case == "unrelated" else 0,
                        unavailable=case == "unavailable")
    if case == "old_host":
        decision = SimpleNamespace(
            complete_structured=lambda **_kwargs: pytest.fail("raw LLM called")
        )
    runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: service, None, host.spawn,
                                   installed_state=JsonState(tmp_path),
                                   subagent_lifecycle=decision)
    adapter = Adapter()
    try:
        native = _wire(runtime, "slack", host, adapter)
        _grant(runtime, "slack", adapter)
        if case == "duplicate":
            service.eligible = True
        if case == "muted":
            service.preferences["muted"] = True
        if case == "suppressed":
            service.preferences["suppressed"]["feed:event-incident"] = True
        if case != "no_context":
            message = {
                "credential_label": "Help triage an incident; token: secret-value",
                "credential_bare": "Help triage an incident " + "sk-" + "a" * 32,
                "credential_bearer": "Help triage an incident; Authorization: Bearer " + "a" * 32,
            }.get(case, "Help triage an incident")
            runtime.capture_context(task_id="task-1", session_id="session-1",
                                    turn_id="other" if case == "wrong_turn" else "turn-1",
                                    user_message=message,
                                    conversation_history=[{"role": "user", "content": "old secret"},
                                                          {"role": "tool", "content": "irrelevant"},
                                                          {"role": "user", "content": message}])
        if case == "stale_owner":
            service.owner["account_id"] = "other-account"
        runtime.end_turn(task_id="task-1", session_id="session-1")
        host.drain()
        assert (len(native.posts) == 1) == (case in {"relevant", "duplicate"})
        assert (len(getattr(decision, "calls", [])) == 1) == (
            case in {"relevant", "unrelated", "unavailable", "duplicate"}
        )
        if case == "relevant":
            assert service.plans == ["incident@v2"]
            assert service.feed_reads == [None]
            assert "old secret" not in str(decision.calls)
            assert runtime.reviews.pending(owner())[0]["message_id"] == "notice-1"
    finally:
        runtime.close()
        host.stop()


def test_context_handoff_preserves_original_turn_scope_and_consumes_once(tmp_path):
    scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "active_parent", default=None,
    )
    host = AdapterLoop().start()
    service = ContextService()
    decision = Decision()
    original_launch = decision.launch

    def launch(request):
        assert scope.get() == "original-parent"
        return original_launch(request)

    decision.launch = launch
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, host.spawn,
        installed_state=JsonState(tmp_path), subagent_lifecycle=decision,
    )
    adapter = Adapter()
    try:
        native = _wire(runtime, "slack", host, adapter)
        _grant(runtime, "slack", adapter)
        runtime.capture_context(
            task_id="task-1", session_id="session-1", turn_id="turn-1",
            user_message="Incident triage",
            conversation_history=[{"role": "user", "content": "Incident triage"}],
        )
        token = scope.set("original-parent")
        try:
            assert runtime.end_turn(task_id="task-1", session_id="session-1")["scheduled"]
            assert not runtime.end_turn(task_id="task-1", session_id="session-1")["supported"]
        finally:
            scope.reset(token)
        host.drain()
        assert len(decision.calls) == 1
        assert len(native.posts) == 1
    finally:
        runtime.close()
        host.stop()


def test_context_capture_requires_admitted_private_human_turn(tmp_path):
    from test_native_model_routing import _event, _gateway  # pylint: disable=import-outside-toplevel

    host = AdapterLoop().start()
    service = ContextService()
    decision = Decision()
    runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: service, None, host.spawn,
                                   installed_state=JsonState(tmp_path),
                                   subagent_lifecycle=decision)
    adapter = Adapter()
    try:
        native = _wire(runtime, "slack", host, adapter)

        async def admitted():
            runtime.hook(event=_event(adapter), gateway=_gateway(adapter))
            runtime.lifecycle_hook(session_id="session-1", task_id="task-1", turn_id="turn-1",
                                   sender_id="U_NATIVE", platform="slack", parent_session_id="")

        asyncio.run_coroutine_threadsafe(admitted(), host.loop).result(timeout=2)
        runtime.capture_context(task_id="task-1", session_id="session-1", turn_id="turn-1",
                                user_message="Help triage an incident",
                                conversation_history=[{"role": "user",
                                                       "content": "Help triage an incident"}])
        runtime.end_turn(task_id="task-1", session_id="session-1")
        host.drain()
        assert len(native.posts) == 1
        assert service.plans == ["incident@v2"]
        assert len(decision.calls) == 1
        assert not runtime._context
    finally:
        runtime.close()
        host.stop()
