"""Admission-bound native invocation capture tests."""

# Behavior-named tests use minimal doubles for pinned private host admission seams.
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=too-few-public-methods,protected-access,too-many-arguments

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.native_context import InvocationCapture


LIFECYCLE = {
    "profile_name": "default", "session_id": "durable-session-1",
    "task_id": "host-task-1", "turn_id": "turn-1", "sender_id": "U1",
    "platform": "slack", "parent_session_id": "",
}

HOST_CONTEXT = {
    "platform": "slack", "profile": "default", "chat_id": "D1", "user_id": "U1",
    "thread_id": "T1", "scope_id": "W1", "message_id": "M1",
    "session_key": "slack:W1:D1:U1",
}


class Gateway:
    def __init__(self, *, authorized=True, adapter=None):
        self.authorized = authorized
        self.adapter = adapter or object()

    def _is_user_authorized_for_source(self, source):
        return self.authorized and source.user_id == "U1"

    def _adapter_for_source(self, _source):
        return self.adapter

    def _session_key_for_source(self, source):
        return f"slack:{source.scope_id}:{source.chat_id}:{source.user_id}"


def event(*, args="update skill-1", command="wisdom", internal=False, user="U1",
          profile="default", message="M1"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="slack"), profile=profile, chat_type="dm",
        chat_id="D1", user_id=user, user_name="shannon", thread_id="T1",
        scope_id="W1",
    )
    return SimpleNamespace(
        source=source, message_id=message, internal=internal,
        get_command=lambda: command, get_command_args=lambda: args,
    )


def test_capture_is_one_shot_task_local_and_reauthorizes():
    async def scenario():
        clock = [10.0]
        captures = InvocationCapture(clock=lambda: clock[0], ttl_seconds=2)
        gateway = Gateway()
        captures.hook(event=event(), gateway=gateway)
        value = captures.consume_command("update skill-1", profile_name="default")
        assert value.actor_id == "U1"
        assert value.scope_id == "W1"
        with pytest.raises(PermissionError, match="current admitted"):
            captures.consume_command("update skill-1", profile_name="default")

        captures.hook(event=event(), gateway=gateway)
        gateway.authorized = False
        with pytest.raises(PermissionError, match="authorized"):
            captures.consume_command("update skill-1", profile_name="default")

        gateway.authorized = True
        captures.hook(event=event(), gateway=gateway)
        clock[0] = 13.0
        with pytest.raises(PermissionError, match="expired"):
            captures.consume_command("update skill-1", profile_name="default")

    asyncio.run(scenario())


def test_capture_rejects_unrelated_internal_profile_and_other_task():
    async def scenario():
        captures = InvocationCapture()
        gateway = Gateway()
        captures.hook(event=event(args="browse"), gateway=gateway)
        with pytest.raises(PermissionError, match="arguments"):
            captures.consume_command("update skill-1", profile_name="default")

        captures.hook(event=event(internal=True), gateway=gateway)
        with pytest.raises(PermissionError, match="current admitted"):
            captures.consume_command("update skill-1", profile_name="default")

        captures.hook(event=event(profile="other"), gateway=gateway)
        with pytest.raises(PermissionError, match="profile"):
            captures.consume_command("update skill-1", profile_name="default")

        captures.hook(event=event(), gateway=gateway)

        async def other_task():
            with pytest.raises(PermissionError, match="current admitted"):
                captures.consume_command("update skill-1", profile_name="default")

        await asyncio.create_task(other_task())

    asyncio.run(scenario())


def test_normal_admitted_turn_is_consumed_once_and_no_session_key_is_fabricated():
    async def scenario():
        captures = InvocationCapture()
        gateway = Gateway()
        captures.hook(event=event(command="", args=""), gateway=gateway)
        current = captures.consume_lifecycle(**LIFECYCLE)
        assert current is not None
        assert current.session_key == "slack:W1:D1:U1"
        assert current.host_session_id == "durable-session-1"
        assert captures.consume_lifecycle(**LIFECYCLE) is None

        bad_gateway = SimpleNamespace(
            _adapter_for_source=gateway._adapter_for_source,
            _is_user_authorized_for_source=gateway._is_user_authorized_for_source,
        )
        captures.hook(event=event(command="", args=""), gateway=bad_gateway)
        assert captures.consume_lifecycle(**LIFECYCLE) is None

    asyncio.run(scenario())


def test_normal_turn_capture_crosses_only_into_hosts_synchronous_worker():
    async def scenario():
        captures = InvocationCapture()
        gateway = Gateway()
        captures.hook(event=event(command="", args=""), gateway=gateway)

        current = await asyncio.to_thread(
            captures.consume_lifecycle, **LIFECYCLE
        )
        assert current is not None
        assert current.session_key == "slack:W1:D1:U1"

        captures.hook(event=event(command="", args=""), gateway=gateway)

        async def copied_child():
            with pytest.raises(PermissionError, match="current admitted"):
                captures.consume_lifecycle(**LIFECYCLE)

        await asyncio.create_task(copied_child())

    asyncio.run(scenario())


def test_lifecycle_admission_is_one_shot_across_independent_context_copies():
    async def scenario():
        captures = InvocationCapture()
        captures.hook(event=event(command="", args=""), gateway=Gateway())
        first = contextvars.copy_context()
        replay = contextvars.copy_context()

        invocation = await asyncio.to_thread(
            first.run, captures.consume_lifecycle, **LIFECYCLE
        )
        assert invocation is not None
        with pytest.raises(PermissionError, match="current admitted"):
            await asyncio.to_thread(
                replay.run, captures.consume_lifecycle, **LIFECYCLE
            )

    asyncio.run(scenario())


def test_cached_turn_recovers_fresh_capture_from_exact_host_route():
    async def scenario():
        captures = InvocationCapture()
        captures.hook(event=event(command="", args=""), gateway=Gateway())

        invocation = await asyncio.to_thread(
            contextvars.Context().run,
            captures.consume_lifecycle,
            **LIFECYCLE,
            host_context=HOST_CONTEXT,
        )

        assert invocation is not None
        assert invocation.message_id == "M1"
        with pytest.raises(PermissionError, match="current admitted"):
            await asyncio.to_thread(
                contextvars.Context().run,
                captures.consume_lifecycle,
                **LIFECYCLE,
                host_context=HOST_CONTEXT,
            )

    asyncio.run(scenario())


def test_cached_turn_recovery_rejects_a_different_host_route():
    async def scenario():
        captures = InvocationCapture()
        captures.hook(event=event(command="", args=""), gateway=Gateway())
        changed = {**HOST_CONTEXT, "message_id": "other-message"}

        with pytest.raises(PermissionError, match="current admitted"):
            await asyncio.to_thread(
                contextvars.Context().run,
                captures.consume_lifecycle,
                **LIFECYCLE,
                host_context=changed,
            )

    asyncio.run(scenario())


def test_cached_turn_fallback_has_a_hard_capacity_limit():
    async def scenario():
        captures = InvocationCapture()
        for index in range(captures._MAX_PENDING_LIFECYCLES + 20):
            captures.hook(
                event=event(command="", args="", message=f"M{index}"), gateway=Gateway()
            )

        assert len(captures._pending_lifecycle) == captures._MAX_PENDING_LIFECYCLES
        assert captures._host_route_key(HOST_CONTEXT) not in captures._pending_lifecycle

    asyncio.run(scenario())


def test_cached_turn_lookup_prunes_expired_fallbacks_without_new_capture():
    async def scenario():
        clock = [10.0]
        captures = InvocationCapture(clock=lambda: clock[0], ttl_seconds=2.0)
        for index in range(3):
            captures.hook(
                event=event(command="", args="", message=f"M{index}"), gateway=Gateway()
            )
        clock[0] = 13.0

        changed = {**HOST_CONTEXT, "message_id": "missing"}
        with pytest.raises(PermissionError, match="current admitted"):
            await asyncio.to_thread(
                contextvars.Context().run,
                captures.consume_lifecycle,
                **LIFECYCLE,
                host_context=changed,
            )
        assert not captures._pending_lifecycle

    asyncio.run(scenario())
