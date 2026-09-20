"""Thin supported native routes for explicit reviews and best-effort notices."""
# Lifecycle and platform-adapter methods are self-explanatory at their call sites.
# pylint: disable=missing-function-docstring,line-too-long

from __future__ import annotations

import asyncio
from dataclasses import replace
from importlib import import_module
import re
import threading
import time
from typing import Any, Callable

from .errors import WisdomValidationError
from .native_context import InvocationCapture, NativeInvocation, adapter_profile
from .native_review import NativeReviewController, parse_callback
from .review_state import ReviewState


def _message_id(response: Any, *, platform: str, chat_id: str) -> str:
    """Extract the exact posted message identity from real Slack/Telegram SDK shapes."""
    if isinstance(response, dict):
        result = response.get("result")
        payload: dict[str, Any] = result if isinstance(result, dict) else response
        returned_chat = payload.get("channel") or payload.get("chat_id")
        if returned_chat is not None and str(returned_chat) != str(chat_id):
            raise WisdomValidationError("Collective Wisdom review returned another route")
        value = payload.get("ts") if platform == "slack" else payload.get("message_id")
    else:
        returned_chat = getattr(response, "chat_id", None)
        chat = getattr(response, "chat", None)
        returned_chat = returned_chat or getattr(chat, "id", None)
        if returned_chat is not None and str(returned_chat) != str(chat_id):
            raise WisdomValidationError("Collective Wisdom review returned another route")
        value = getattr(response, "ts", None) if platform == "slack" else getattr(
            response, "message_id", None
        )
    if not str(value or ""):
        raise WisdomValidationError("Collective Wisdom review message identity is unavailable")
    return str(value)


class NativeSurfaceRuntime:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Bind supported Slack/Telegram SDK routes without advisory queues or receipts."""

    def __init__(  # pylint: disable=too-many-arguments
        self, root, profile_name: str, service_factory: Callable[[], Any], llm: Any,
        spawn_task: Callable[..., Any], *, installed_state: Any | None = None,
        session_context: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        del llm
        self.root = root
        self.profile_name = profile_name
        self.service_factory = service_factory
        self.spawn_task = spawn_task
        self.session_context = session_context
        if installed_state is None:
            raise TypeError("host PluginState is required")
        self._state = installed_state
        self.capture = InvocationCapture()
        self._reviews: NativeReviewController | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._native: dict[tuple[str, int], Any] = {}
        self._leases: dict[tuple[str, int], object] = {}
        self._turns: dict[
            tuple[str, str], tuple[float, NativeInvocation, dict[str, str], object]
        ] = {}
        self._local_tools: dict[tuple[str, str], tuple[float, NativeInvocation, dict[str, str]]] = {}

    @property
    def reviews(self) -> NativeReviewController:
        with self._lock:
            if self._reviews is None:
                self._reviews = NativeReviewController(ReviewState(self._state, self.root))
            return self._reviews

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._native.clear()
            self._leases.clear()
            self._turns.clear()
            self._local_tools.clear()

    def hook(self, **kwargs: Any) -> None:
        if not self._closed:
            self.capture.hook(**kwargs)

    def lifecycle_hook(self, **facts: Any) -> None:
        """Remember an admitted private native turn only until its end-turn check."""
        if self._closed:
            return
        host_context = None
        if self.session_context is not None:
            try:
                host_context = self.session_context()
            except Exception:  # pylint: disable=broad-exception-caught
                return
        try:
            invocation = self.capture.consume_lifecycle(
                profile_name=self.profile_name,
                session_id=str(facts.get("session_id") or ""),
                task_id=str(facts.get("task_id") or ""),
                turn_id=str(facts.get("turn_id") or ""),
                sender_id=str(facts.get("sender_id") or ""),
                platform=str(facts.get("platform") or ""),
                parent_session_id=str(facts.get("parent_session_id") or ""),
                host_context=host_context,
            )
        except PermissionError:
            return
        if invocation is None or not self._host_route_current(invocation):
            return
        invocation = replace(invocation, profile=self.profile_name)
        grant = self._native_turn_grant(invocation)
        if grant is None:
            return
        owner, token, native = grant
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            if (
                self._closed or self._leases.get(route) is not token
                or self._native.get(route) is not native
            ):
                return
            self._turns[(invocation.task_id, invocation.host_session_id)] = (
                time.monotonic() + 600.0, invocation, dict(owner), token
            )

    def _host_route_current(self, invocation: NativeInvocation) -> bool:
        if self.session_context is None:
            return True
        try:
            context = self.session_context()
            expected = {
                "platform": invocation.platform, "chat_id": invocation.chat_id,
                "user_id": invocation.actor_id, "thread_id": invocation.thread_id,
                "scope_id": invocation.scope_id, "message_id": invocation.message_id,
                "session_key": invocation.session_key,
            }
            if (str(context.get("profile") or "default") != self.profile_name
                    or any(str(context.get(key) or "") != value
                           for key, value in expected.items())):
                return False
            session_id = str(context.get("session_id") or "")
            if not session_id:
                # Cached host turns omit this env value. Never use a process-wide fallback.
                auxiliary = import_module("agent.auxiliary_client")
                active = auxiliary._RUNTIME_MAIN_CONTEXT.get() or {}  # pylint: disable=protected-access
                session_id = str(active.get("session_id") or "")
            return bool(session_id) and session_id == invocation.host_session_id
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def _native_turn_grant(
        self, invocation: NativeInvocation,
    ) -> tuple[dict[str, str], object, Any] | None:
        """Bind a captured native route to its current renderer and Portal owner."""
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            token = self._leases.get(route)
            native = self._native.get(route)
        try:
            if (
                token is None or native is None
                or adapter_profile(invocation.adapter) != self.profile_name
            ):
                return None
        except PermissionError:
            return None
        try:
            service = self.service_factory()
            try:
                owner = service.native_owner()
            finally:
                service.close()
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        if (
            not isinstance(owner, dict)
            or owner.get("profile_name") != self.profile_name
            or not owner.get("account_id")
        ):
            return None
        return dict(owner), token, native

    def end_turn(self, **facts: Any) -> dict[str, Any]:
        """Poll and dispatch only when the completed turn has a supported private route."""
        key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
        with self._lock:
            grant = self._turns.pop(key, None)
        if grant is None or grant[0] < time.monotonic() or self._closed:
            return {"supported": False, "offered": 0, "polled": False}
        invocation = grant[1]
        service = self.service_factory()

        def render(rows: list[dict[str, Any]]) -> None:
            coroutine = self._send_notice(invocation, rows)
            if self._closed:
                coroutine.close()
                return
            self.spawn_task(coroutine, name=f"wisdom-notice:{invocation.session_key}")

        try:
            return service.end_turn_notices(render)
        finally:
            service.close()

    async def _send_notice(self, invocation: NativeInvocation, rows: list[dict[str, Any]]) -> None:
        if not rows or self._closed:
            return
        names = [str(row.get("skill_name") or row.get("slug") or row.get("skill_id") or "a skill") for row in rows]
        text = "Collective Wisdom: " + ", ".join(names[:3]) + " available. Use /wisdom to review."
        native = self._native.get((invocation.platform, id(invocation.adapter)))
        if native is None:
            return
        if invocation.platform == "slack":
            payload: dict[str, Any] = {"channel": invocation.chat_id, "text": text}
            if invocation.thread_id:
                payload["thread_ts"] = invocation.thread_id
            await native.client.chat_postMessage(**payload)
            return
        bot = getattr(invocation.adapter, "_bot", None)
        if bot is not None:
            options = {"chat_id": invocation.chat_id, "text": text}
            if invocation.thread_id:
                options["message_thread_id"] = int(invocation.thread_id)
            await bot.send_message(**options)

    def consume_request(self, raw_args: str) -> NativeInvocation | None:
        try:
            return self.capture.consume_command(raw_args, profile_name=self.profile_name)
        except PermissionError:
            return None

    def _lease(self, platform: str, native: Any, adapter: Any) -> object:
        token = object()
        with self._lock:
            self._native[(platform, id(adapter))] = native
            self._leases[(platform, id(adapter))] = token
        return token

    def _check_lease(self, platform: str, adapter: Any, token: object) -> None:
        with self._lock:
            if self._closed or self._leases.get((platform, id(adapter))) is not token:
                raise PermissionError("Collective Wisdom native route changed")

    def _lease_current(self, platform: str, adapter: Any, token: object) -> bool:
        with self._lock:
            return (
                not self._closed
                and self._leases.get((platform, id(adapter))) is token
            )

    def wire_slack(self, native: Any, adapter: Any) -> None:
        token = self._lease("slack", native, adapter)

        async def callback(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
            await ack()
            try:
                self._check_lease("slack", adapter, token)
                record_id, selected = parse_callback(str(action.get("value") or ""))
                record = self.reviews.load(record_id)
                from .native_slack import (  # pylint: disable=import-outside-toplevel
                    blocks, callback_invocation,
                )
                invocation = callback_invocation(adapter, body, record)
                service = self.service_factory()
                try:
                    updated = await asyncio.to_thread(
                        self.reviews.act, record_id, selected, service, invocation,
                        authority=lambda: self._lease_current("slack", adapter, token),
                    )
                finally:
                    service.close()
                view = self.reviews.view(updated)
                await native.client.chat_update(
                    channel=invocation.chat_id, ts=invocation.message_id,
                    text=view.to_text(), blocks=blocks(view, record_id),
                )
            except Exception:  # pylint: disable=broad-exception-caught
                return

        native.action(re.compile(r"^wisdom_native_[a-z0-9_]+_\d+$"))(callback)

    def wire_telegram(self, native: Any, adapter: Any) -> None:
        from telegram.ext import (  # pylint: disable=import-outside-toplevel
            ApplicationHandlerStop, CallbackQueryHandler,
        )
        token = self._lease("telegram", native, adapter)

        async def callback(update: Any, _context: Any) -> None:
            query = getattr(update, "callback_query", None)
            if query is None:
                return
            await query.answer()
            try:
                self._check_lease("telegram", adapter, token)
                record_id, selected = parse_callback(str(getattr(query, "data", "") or ""))
                record = self.reviews.load(record_id)
                from .native_telegram import (  # pylint: disable=import-outside-toplevel
                    callback_invocation, edit,
                )
                invocation = callback_invocation(adapter, query, record)
                service = self.service_factory()
                try:
                    updated = await asyncio.to_thread(
                        self.reviews.act, record_id, selected, service, invocation,
                        authority=lambda: self._lease_current("telegram", adapter, token),
                    )
                finally:
                    service.close()
                await edit(query, self.reviews.view(updated), record_id)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            raise ApplicationHandlerStop

        native.add_handler(CallbackQueryHandler(callback, pattern=r"^wi:agent:", block=True), group=-10)

    async def _post_review(self, invocation: NativeInvocation, record: dict[str, Any]) -> None:
        view = self.reviews.view(record)
        native = self._native.get((invocation.platform, id(invocation.adapter)))
        if native is None:
            return
        if invocation.platform == "slack":
            from .native_slack import blocks  # pylint: disable=import-outside-toplevel
            payload: dict[str, Any] = {
                "channel": invocation.chat_id, "text": view.to_text(),
                "blocks": blocks(view, record["id"]),
            }
            if invocation.thread_id:
                payload["thread_ts"] = invocation.thread_id
            response = await native.client.chat_postMessage(**payload)
        else:
            from .native_telegram import send  # pylint: disable=import-outside-toplevel
            response = await send(
                invocation.adapter._bot, view, record["id"],  # pylint: disable=protected-access
                chat_id=invocation.chat_id, thread_id=invocation.thread_id,
            )
        message_id = _message_id(
            response, platform=invocation.platform, chat_id=invocation.chat_id
        )
        with self._lock:
            if self._closed or self._native.get(
                (invocation.platform, id(invocation.adapter))
            ) is not native:
                return
        self.reviews.bind_message(record["id"], invocation, message_id)

    def _begin(
        self, invocation: NativeInvocation, build: Callable[[Any], dict[str, Any]], *,
        expected_owner: dict[str, str] | None = None,
    ) -> dict[str, str]:
        service = self.service_factory()
        try:
            if expected_owner is not None and service.native_owner() != expected_owner:
                raise PermissionError("Collective Wisdom model-tool ownership changed")
            record = build(service)
        finally:
            service.close()
        coroutine = self._post_review(invocation, record)
        if self._closed:
            coroutine.close()
            return {"state": "unavailable"}
        self.spawn_task(coroutine, name=f"wisdom-review:{invocation.session_key}")
        return {"state": "queued", "review_id": record["id"]}

    def begin_install_modes(self, invocation: NativeInvocation, reference: str):
        return self._begin(invocation, lambda service: self.reviews.request_install_modes(service, invocation, reference))

    def begin_update(self, invocation: NativeInvocation, skill_id: str):
        return self._begin(
            invocation,
            lambda service: self.reviews.request_update(service, invocation, skill_id),
        )

    def begin_publication(self, invocation: NativeInvocation, draft_id: str):
        return self._begin(invocation, lambda service: self.reviews.request_publication(service, invocation, draft_id))

    def begin_suggest(self, invocation: NativeInvocation, skill_name: str):
        return self._begin(
            invocation,
            lambda service: self.reviews.request_suggest(service, invocation, skill_name),
        )

    def begin_catalog(self, invocation: NativeInvocation, command: str, target: str = ""):
        return self._begin(invocation, lambda service: self.reviews.request_catalog(service, invocation, command, target))

    def begin_settings(self, invocation: NativeInvocation):
        return self._begin(invocation, lambda service: self.reviews.request_settings(service, invocation))

    def begin_setup(
        self, invocation: NativeInvocation, skill_id: str, inspection: dict[str, Any]
    ):
        return self._begin(
            invocation,
            lambda service: self.reviews.request_setup(
                service, invocation, (skill_id, inspection)
            ),
        )

    def begin_candidate(self, invocation: NativeInvocation, event_id: str):
        return self._begin(
            invocation,
            lambda service: self.reviews.request_candidate(service, invocation, event_id),
        )

    def begin(self, invocation: NativeInvocation, target: str):
        return self._begin(invocation, lambda service: self.reviews.request_install(service, invocation, target))

    def queue_model_consent(
        self, invocation: NativeInvocation, args: dict[str, Any], *,
        expected_owner: dict[str, str] | None = None,
    ):
        kind = str(args.get("kind") or "")
        target = str(
            args.get("identity") or args.get("reference") or args.get("skill_id") or ""
        )
        if not target:
            return {"state": "unavailable"}
        if kind == "candidate":
            return self._begin(
                invocation,
                lambda service: self.reviews.request_candidate(service, invocation, target),
                expected_owner=expected_owner,
            )
        if kind == "setup":
            step = args.get("step")
            if not isinstance(step, dict):
                return {"state": "unavailable"}
            inspection = {
                "version": args.get("version"), "step": dict(step),
            }
            return self._begin(
                invocation,
                lambda service: self.reviews.request_setup(
                    service, invocation, (target, inspection)
                ),
                expected_owner=expected_owner,
            )
        version = args.get("version")
        reference = f"{target}@v{version}" if isinstance(version, int) else target
        return self._begin(
            invocation,
            lambda service: self.reviews.request_install(service, invocation, reference),
            expected_owner=expected_owner,
        )

    def admit_local_tool(  # pylint: disable=too-many-arguments
        self, *, owner: dict[str, str], surface: str, session_id: str,
        task_id: str, turn_id: str, route: dict[str, str],
    ) -> None:
        del route
        if surface not in {"cli", "tui"} or owner.get("profile_name") != self.profile_name:
            raise PermissionError("Collective Wisdom local tool admission is unavailable")
        invocation = NativeInvocation(
            platform=surface, profile=self.profile_name, chat_type="private", chat_id=surface,
            actor_id=owner["account_id"], actor_name="", thread_id="", scope_id="",
            message_id="", session_key=f"local:{session_id}", adapter=None,
            host_session_id=session_id, task_id=task_id, turn_id=turn_id,
        )
        with self._lock:
            self._local_tools[(task_id, session_id)] = (time.monotonic() + 600.0, invocation, dict(owner))

    def revoke_local_tool(self, *, session_id: str) -> None:
        with self._lock:
            for key in list(self._local_tools):
                if key[1] == session_id:
                    self._local_tools.pop(key, None)

    def tool_invocation(self, *, task_id: str, session_id: str) -> NativeInvocation | None:
        key = (task_id, session_id)
        with self._lock:
            if self._closed:
                return None
            local = self._local_tools.get(key)
            if local is not None:
                if local[0] >= time.monotonic():
                    return local[1]
                self._local_tools.pop(key, None)
            row = self._turns.get(key)
            if row is None:
                return None
            expiry, invocation, _owner, token = row
            route = (invocation.platform, id(invocation.adapter))
            current = (
                expiry >= time.monotonic()
                and invocation.profile == self.profile_name
                and invocation.chat_type.casefold() in {"private", "dm", "direct", "im"}
                and self._leases.get(route) is token
                and self._native.get(route) is not None
                and self._host_route_current(invocation)
            )
            try:
                current = current and adapter_profile(invocation.adapter) == self.profile_name
            except PermissionError:
                current = False
            if not current:
                self._turns.pop(key, None)
                return None
            return invocation

    def validate_local_tool_owner(self, invocation: NativeInvocation, service: Any) -> None:
        current = self.tool_invocation(
            task_id=invocation.task_id, session_id=invocation.host_session_id,
        )
        with self._lock:
            row = self._local_tools.get((invocation.task_id, invocation.host_session_id))
        if (
            current != invocation or row is None or row[1] != invocation
            or row[2] != service.native_owner()
        ):
            raise PermissionError("Collective Wisdom local tool ownership changed")

    def validate_tool_owner(self, invocation: NativeInvocation, service: Any) -> None:
        """Recheck the owner bound to either supported model-tool admission."""
        if invocation.platform in {"cli", "tui"}:
            self.validate_local_tool_owner(invocation, service)
            return
        owner = self._native_tool_owner(invocation)
        if owner != service.native_owner():
            raise PermissionError("Collective Wisdom model-tool ownership changed")

    def _native_tool_owner(self, invocation: NativeInvocation) -> dict[str, str]:
        if invocation.platform not in {"slack", "telegram"}:
            raise PermissionError("Collective Wisdom native renderer is unavailable")
        current = self.tool_invocation(
            task_id=invocation.task_id, session_id=invocation.host_session_id,
        )
        with self._lock:
            row = self._turns.get((invocation.task_id, invocation.host_session_id))
            if current != invocation or row is None:
                raise PermissionError("Collective Wisdom model-tool admission changed")
            return dict(row[2])
