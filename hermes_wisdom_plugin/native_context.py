"""Capture real gateway identity without acting before host admission.

``pre_gateway_dispatch`` is pre-authentication.  It may only snapshot the current
human event.  The slash handler or ``pre_llm_call`` lifecycle hook consumes that
snapshot later in the same task and re-runs the gateway authorization decision.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class NativeInvocation:  # pylint: disable=too-many-instance-attributes
    """Immutable identity and route facts derived by the real host."""

    platform: str
    profile: str
    chat_type: str
    chat_id: str
    actor_id: str
    actor_name: str
    thread_id: str
    scope_id: str
    message_id: str
    session_key: str
    adapter: Any
    host_session_id: str = ""
    task_id: str = ""
    turn_id: str = ""


def adapter_profile(adapter: Any) -> str:
    """Return the host-owned profile binding; ``None`` is the host default."""
    if not hasattr(adapter, "_owner_profile"):
        raise PermissionError("Collective Wisdom adapter profile binding is unavailable.")
    owner_profile = getattr(adapter, "_owner_profile")
    if owner_profile is None:
        return "default"
    profile = str(owner_profile or "")
    if not profile:
        raise PermissionError("Collective Wisdom adapter profile binding is unavailable.")
    return profile


@dataclass(frozen=True, slots=True)
class _Captured:  # pylint: disable=too-many-instance-attributes
    invocation: NativeInvocation
    source_snapshot: Any
    gateway: Any
    command: str
    raw_args: str
    task: asyncio.Task[Any]
    captured_at: float
    admission: "_Admission"


class _Admission:  # pylint: disable=too-few-public-methods
    """Per-invocation one-shot shared by every copied ContextVar context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consumed = False

    def claim(self) -> bool:
        """Atomically consume this captured host admission once."""
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True


class InvocationCapture:
    """Task-local one-shot bridge from the pre-auth hook to admitted code."""

    _MAX_PENDING_LIFECYCLES = 256

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = 30.0) -> None:
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self._pending_lock = threading.Lock()
        self._pending_lifecycle: OrderedDict[tuple[str, ...], _Captured] = OrderedDict()
        self._value: contextvars.ContextVar[_Captured | None] = contextvars.ContextVar(
            f"wisdom_native_invocation_{id(self)}", default=None
        )

    @staticmethod
    def _route_key(invocation: NativeInvocation) -> tuple[str, ...]:
        return tuple(str(value or "") for value in (
            invocation.platform, invocation.profile, invocation.chat_id,
            invocation.actor_id, invocation.thread_id, invocation.scope_id,
            invocation.message_id, invocation.session_key,
        ))

    @staticmethod
    def _host_route_key(host_context: Any) -> tuple[str, ...] | None:
        if not isinstance(host_context, dict):
            return None
        return tuple(
            str(host_context.get(name) or ("default" if name == "profile" else ""))
            for name in (
                "platform", "profile", "chat_id", "user_id", "thread_id", "scope_id",
                "message_id", "session_key",
            )
        )

    def _prune_lifecycle_locked(self, now: float) -> None:
        while self._pending_lifecycle:
            _key, oldest = next(iter(self._pending_lifecycle.items()))
            if now - oldest.captured_at <= self.ttl_seconds:
                break
            self._pending_lifecycle.popitem(last=False)

    def _remember_lifecycle(self, captured: _Captured) -> None:
        """Keep a bounded exact-route fallback when a cached worker drops fresh ContextVars."""
        key = self._route_key(captured.invocation)
        with self._pending_lock:
            self._prune_lifecycle_locked(self.clock())
            self._pending_lifecycle.pop(key, None)
            self._pending_lifecycle[key] = captured
            while len(self._pending_lifecycle) > self._MAX_PENDING_LIFECYCLES:
                self._pending_lifecycle.popitem(last=False)

    def _take_lifecycle(self, host_context: Any) -> _Captured | None:
        key = self._host_route_key(host_context)
        if key is None:
            return None
        with self._pending_lock:
            self._prune_lifecycle_locked(self.clock())
            return self._pending_lifecycle.pop(key, None)

    def hook(self, *, event: Any, gateway: Any, **_kwargs: Any) -> None:
        """Snapshot only: no authorization, persistence, reply, or local inspection."""
        self._value.set(None)
        if bool(getattr(event, "internal", False)):
            return None
        source = getattr(event, "source", None)
        platform = str(
            getattr(getattr(source, "platform", None), "value", "") or ""
        ).casefold()
        if platform not in {"slack", "telegram"}:
            return None
        task = asyncio.current_task()
        session_for = getattr(gateway, "_session_key_for_source", None)
        adapter_for = getattr(gateway, "_delivery_adapter_for", None)
        if task is None or not callable(session_for) or not callable(adapter_for):
            return None
        try:
            session_key = str(session_for(source) or "")
            adapter = adapter_for(source)
            source_snapshot = copy.deepcopy(source)
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        if not session_key or adapter is None:
            return None
        invocation = NativeInvocation(
            platform=platform,
            # A dedicated default-profile adapter leaves source.profile unset.
            # Its host-owned adapter binding is the effective profile.
            profile=str(getattr(source, "profile", "") or adapter_profile(adapter)),
            chat_type=str(getattr(source, "chat_type", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            actor_id=str(getattr(source, "user_id", "") or ""),
            actor_name=str(getattr(source, "user_name", "") or ""),
            thread_id=str(getattr(source, "thread_id", "") or ""),
            scope_id=str(getattr(source, "scope_id", "") or ""),
            message_id=str(getattr(event, "message_id", "") or ""),
            session_key=session_key,
            adapter=adapter,
        )
        captured = _Captured(
            invocation=invocation,
            source_snapshot=source_snapshot,
            gateway=gateway,
            command=str(event.get_command() or "").casefold(),
            raw_args=str(event.get_command_args() or "").strip(),
            task=task,
            captured_at=self.clock(),
            admission=_Admission(),
        )
        self._value.set(captured)
        if not captured.command:
            self._remember_lifecycle(captured)
        return None

    def consume_command(self, raw_args: str, *, profile_name: str) -> NativeInvocation:
        """Consume the matching admitted slash command once."""
        return self._consume(
            profile_name=profile_name, expected_command="wisdom",
            expected_args=str(raw_args or "").strip(), allow_sync_worker=True,
        )

    def consume_lifecycle(  # pylint: disable=too-many-arguments
        self, *, profile_name: str, session_id: str, task_id: str, turn_id: str,
        sender_id: str, platform: str, parent_session_id: str,
        host_context: Any = None,
    ) -> NativeInvocation | None:
        """Consume a normal admitted main-agent turn and bind its durable host ids."""
        captured = self._value.get()
        if (captured is None and host_context is None) or (
            captured is not None and captured.command
        ):
            return None
        invocation = self._consume(
            profile_name=profile_name, allow_sync_worker=True,
            expected_actor=str(sender_id or ""),
            expected_platform=str(platform or "").casefold(),
            lifecycle_ids=(str(session_id or ""), str(task_id or ""), str(turn_id or "")),
            parent_session_id=str(parent_session_id or ""),
            host_context=host_context,
        )
        return replace(
            invocation, host_session_id=str(session_id), task_id=str(task_id), turn_id=str(turn_id)
        )

    def _consume(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
                 self, *, profile_name: str, expected_command: str | None = None,
                 expected_args: str | None = None,
                 allow_sync_worker: bool = False,
                 expected_actor: str | None = None,
                 expected_platform: str | None = None,
                 lifecycle_ids: tuple[str, str, str] | None = None,
                 parent_session_id: str = "", host_context: Any = None) -> NativeInvocation:
        captured = self._take_lifecycle(host_context) if allow_sync_worker else None
        if captured is None:
            captured = self._value.get()
        self._value.set(None)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        # Gateway agent turns run synchronously in a context-copying worker.  The
        # captured ContextVar is the lineage proof there; on an event loop retain
        # strict task identity so an inherited child task cannot consume it.
        wrong_owner = (
            current_task is None and not allow_sync_worker
        ) or (
            current_task is not None and captured is not None
            and captured.task is not current_task
        )
        if captured is None or wrong_owner:
            raise PermissionError("Native Wisdom action has no current admitted invocation.")
        if not captured.command:
            with self._pending_lock:
                self._pending_lifecycle.pop(self._route_key(captured.invocation), None)
        if not captured.admission.claim():
            raise PermissionError("Native Wisdom action has no current admitted invocation.")
        if self.clock() - captured.captured_at > self.ttl_seconds:
            raise PermissionError("Native Wisdom invocation expired; try again.")
        if expected_command is not None and captured.command != expected_command:
            raise PermissionError("Native Wisdom invocation command changed.")
        if expected_args is not None and captured.raw_args != expected_args:
            raise PermissionError("Native Wisdom invocation arguments changed.")
        invocation = captured.invocation
        if lifecycle_ids is not None and (not all(lifecycle_ids) or parent_session_id):
            raise PermissionError("Native Wisdom lifecycle is not an owning human turn.")
        if expected_actor is not None and invocation.actor_id != expected_actor:
            raise PermissionError("Native Wisdom lifecycle sender changed.")
        if expected_platform is not None and invocation.platform != expected_platform:
            raise PermissionError("Native Wisdom lifecycle platform changed.")
        if invocation.profile and invocation.profile != profile_name:
            raise PermissionError("Native Wisdom invocation belongs to another profile.")
        if invocation.chat_type.casefold() not in {"dm", "private", "im"}:
            raise PermissionError("Open a private session to review this installation.")
        if not all((invocation.actor_id, invocation.chat_id, invocation.session_key)):
            raise PermissionError("Native Wisdom invocation is missing routed identity.")
        # Fence the copied source against the immutable identity before passing it
        # back through the host's current authorization policy.
        source = captured.source_snapshot
        if any(str(getattr(source, attr, "") or "") != value for attr, value in (
            ("chat_id", invocation.chat_id), ("user_id", invocation.actor_id),
            ("thread_id", invocation.thread_id), ("scope_id", invocation.scope_id),
        )):
            raise PermissionError("Native Wisdom invocation identity changed.")
        source_profile = str(
            getattr(source, "profile", "") or adapter_profile(invocation.adapter)
        )
        if source_profile != invocation.profile:
            raise PermissionError("Native Wisdom invocation identity changed.")
        authorize = getattr(captured.gateway, "_is_user_authorized_for_source", None)
        try:
            authorized = bool(authorize(source)) if callable(authorize) else False
        except Exception as exc:
            raise PermissionError("Native Wisdom actor authorization is unavailable.") from exc
        if not authorized:
            raise PermissionError("Native Wisdom actor is not authorized by this gateway.")
        return invocation
