"""Native notice dispatch crosses from sync lifecycle workers to adapter loops."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access
# pylint: disable=too-few-public-methods,broad-exception-caught,invalid-name

from __future__ import annotations

import asyncio
from concurrent.futures import Future
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
