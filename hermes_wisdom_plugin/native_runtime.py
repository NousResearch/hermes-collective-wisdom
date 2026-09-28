"""Thin supported native routes for explicit reviews and best-effort notices."""
# Lifecycle and platform-adapter methods are self-explanatory at their call sites.
# pylint: disable=missing-function-docstring,line-too-long,too-many-lines

from __future__ import annotations

import asyncio
from dataclasses import replace
from importlib import import_module
import logging
import re
import threading
import time
from typing import Any, Callable

from .errors import WisdomUnavailable, WisdomValidationError
from .native_context import InvocationCapture, NativeInvocation, adapter_profile
from .native_context import message_id as posted_message_id
from .native_review import NativeItem, NativeReviewController, NativeView, parse_callback
from .native_progress import PROGRESS_TIMEOUT, edit_progress
from .package import PackagePolicyError
from .terminal_ui import has_reviewable_warnings, render_operation_outcome
from .review_state import ReviewState


logger = logging.getLogger(__name__)


def _already_installed_view(
    service: Any, reference: str, error: PackagePolicyError, current: Callable[[], bool]
) -> NativeView:
    """Classify only an exact managed duplicate with a fresh owner-bound readback."""
    if str(error) != "skill is already managed; use the update workflow":
        raise error
    rows = service.list_installations()
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("state") == "active" and row.get("slug") == reference]
    if len(matches) != 1:
        raise error
    skill_id, version = matches[0].get("skill_id"), matches[0].get("version")
    if not isinstance(skill_id, str) or not skill_id or not isinstance(version, int):
        raise error
    status = service.installed_setup_inspect(skill_id, version=version)
    if (not isinstance(status, dict) or status.get("files_installed") is not True
            or status.get("slug") != reference or status.get("version") != version):
        raise error
    if not current():
        raise error
    if status.get("ready_to_use") is True:
        return NativeView("Collective Wisdom", f"✅ {reference} is already installed and ready to use", dismissed=True)

    guidance = status.get("guidance")
    commands = guidance.get("setup_commands") if isinstance(guidance, dict) else None
    progress = status.get("setup_progress")
    passed_unverified = (isinstance(commands, list) and bool(commands)
                         and isinstance(progress, list) and len(progress) == len(commands)
                         and isinstance(status.get("verification"), dict)
                         and status["verification"].get("state") == "not_recorded"
                         and all(isinstance(step, dict) and step.get("phase") == "setup"
                                 and step.get("index") == index and step.get("state") == "passed"
                                 for index, step in enumerate(progress)))
    next_step = guidance.get("verification_step") if isinstance(guidance, dict) else None
    if not isinstance(next_step, str) or not next_step.strip():
        next_step = "Ask the skill's author how to verify its setup."
    summary = (f"{reference} is already installed (v{version}). Setup ran successfully; "
               "readiness is not verified." if passed_unverified else
               f"{reference} is already installed (v{version}); setup incomplete")
    return NativeView(
        "Collective Wisdom", summary,
        [NativeItem("Setup", "Setup commands passed; verification not recorded." if
                    passed_unverified else
                    "Setup is not complete; check its recorded progress before continuing."),
         NativeItem("Next step", next_step)],
        notice="Don't install it again. It isn't ready to use until setup is verified.",
        dismissed=True,
    )



def _require_direct_install_owner(service, invocation):
    if service.native_owner().get("profile_name") != invocation.profile:
        raise PermissionError("Collective Wisdom installation owner changed")


def _direct_install_blocked(plan, reference):
    policy = plan.get("policy") if isinstance(plan, dict) else None
    scan = plan.get("local_scan") if isinstance(plan, dict) else None
    compatible = plan.get("compatibility") if isinstance(plan, dict) else None
    clean = (
        isinstance(plan, dict) and plan.get("operation") == "install"
        and plan.get("allowed") is True and not has_reviewable_warnings(plan)
        and isinstance(scan, dict) and scan.get("allowed") is True
        and scan.get("available") is True
        and isinstance(compatible, dict) and compatible.get("outcome") == "compatible"
        and isinstance(policy, dict)
        and policy.get("scan_requires_confirmation") is False
        and policy.get("compatibility_requires_confirmation") is False
        and isinstance(plan.get("setup_commands"), list)
    )
    if not clean:
        display_name = plan.get("slug") if isinstance(plan, dict) else None
        if not isinstance(display_name, str) or not display_name:
            display_name = reference
        return NativeView(
            "Collective Wisdom", f"⚠️ {display_name} wasn't installed",
            notice="A safety check or setup is required. No skill was installed.",
            dismissed=True,
        )
    return None


def _direct_install_outcome(result, slug):
    if not isinstance(result, dict) or result.get("pending") is True:
        return NativeView(
            "Collective Wisdom", f"{slug} installation needs checking",
            notice="The outcome is uncertain. Check installed skills before trying again.",
            dismissed=True,
        )
    if (result.get("installed") is not True or result.get("rejected") is True
            or result.get("error")):
        return NativeView(
            "Collective Wisdom", f"{slug} installation needs attention",
            notice="Check its installation status; do not retry blindly.", dismissed=True,
        )
    return NativeView(
        "Collective Wisdom",
        render_operation_outcome("install", result, skill_name=slug), dismissed=True,
    )


class NativeSurfaceRuntime:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Bind supported Slack/Telegram SDK routes without advisory queues or receipts."""

    def __init__(  # pylint: disable=too-many-arguments
        self, root, profile_name: str, service_factory: Callable[[], Any], _llm: Any,
        spawn_task: Callable[..., Any], *, installed_state: Any | None = None,
        session_context: Callable[[], dict[str, str]] | None = None,
    ) -> None:
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
        self._loops: dict[tuple[str, int], asyncio.AbstractEventLoop] = {}
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
            self._loops.clear()
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
        invocation, expected_owner, token = grant[1], grant[2], grant[3]
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            native = self._native.get(route)
            loop = self._loops.get(route)
        if (
            native is None or loop is None or loop.is_closed()
            or not self._notice_route_current(invocation, native, token, loop)
        ):
            return {"supported": False, "offered": 0, "polled": False, "scheduled": False}
        try:
            loop.call_soon_threadsafe(
                self._schedule_notice_poll,
                invocation, expected_owner, native, token, loop,
            )
        except RuntimeError:
            return {"supported": False, "offered": 0, "polled": False, "scheduled": False}
        return {"supported": True, "offered": 0, "polled": False, "scheduled": True}

    def _notice_route_current(
        self, invocation: NativeInvocation, native: Any, token: object,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            current = (
                not self._closed
                and self._leases.get(route) is token
                and self._native.get(route) is native
                and self._loops.get(route) is loop
            )
        if not current or not loop.is_running() or invocation.profile != self.profile_name:
            return False
        try:
            return adapter_profile(invocation.adapter) == self.profile_name
        except PermissionError:
            return False

    def _schedule_notice_poll(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, invocation: NativeInvocation, expected_owner: dict[str, str],
        native: Any, token: object, loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Create the supervised task only from the adapter's owning event loop."""
        if not self._notice_route_current(invocation, native, token, loop):
            return
        coroutine = self._run_notice_poll(
            invocation, expected_owner, native=native, token=token, loop=loop,
        )
        try:
            self.spawn_task(coroutine, name=f"wisdom-notice:{invocation.session_key}")
        except Exception:  # pylint: disable=broad-exception-caught
            coroutine.close()

    def _poll_notice_rows(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        service = self.service_factory()
        try:
            result = service.end_turn_notices(rows.extend)
            return result, rows
        finally:
            service.close()

    async def _run_notice_poll(  # pylint: disable=too-many-arguments
        self, invocation: NativeInvocation, expected_owner: dict[str, str], *,
        native: Any, token: object, loop: asyncio.AbstractEventLoop,
    ) -> dict[str, Any]:
        """Preflight, poll off-loop, then render on the adapter's loop."""
        if not self._notice_route_current(invocation, native, token, loop):
            return {"supported": False, "offered": 0, "polled": False}
        owner_current = await asyncio.to_thread(self._post_owner_current, expected_owner)
        if (
            not owner_current
            or not self._notice_route_current(invocation, native, token, loop)
        ):
            return {"supported": False, "offered": 0, "polled": False}
        result, rows = await asyncio.to_thread(self._poll_notice_rows)
        if rows and self._notice_route_current(invocation, native, token, loop):
            await self._send_notice(
                invocation, rows, expected_owner=expected_owner,
                route_authority=lambda: self._notice_route_current(
                    invocation, native, token, loop
                ),
            )
        return result

    def _build_notice_review(
        self, invocation: NativeInvocation, row: dict[str, Any],
        expected_owner: dict[str, str] | None,
    ) -> dict[str, Any]:
        service = self.service_factory()
        try:
            if expected_owner is not None and service.native_owner() != expected_owner:
                raise PermissionError("Collective Wisdom notice ownership changed")
            return self._notice_review(service, invocation, row)
        finally:
            service.close()

    async def _send_notice(
        self, invocation: NativeInvocation, rows: list[dict[str, Any]], *,
        expected_owner: dict[str, str] | None = None,
        route_authority: Callable[[], bool] | None = None,
    ) -> None:
        # Existing policy/preferences already selected this bounded best-effort batch.
        # Reuse the explicit review route; no jobs, delivery claims or retry queue.
        if self._closed or (invocation.platform, id(invocation.adapter)) not in self._native:
            return
        for row in rows:
            try:
                if route_authority is not None and route_authority() is not True:
                    return
                record = await asyncio.to_thread(
                    self._build_notice_review, invocation, row, expected_owner,
                )
                if route_authority is not None and route_authority() is not True:
                    return
                await self._post_review(
                    invocation, record, route_authority=route_authority,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Collective Wisdom native notice failed (%s)", type(exc).__name__,
                )
                continue  # A missed notice must not fail the owning turn.

    def _notice_review(self, service: Any, invocation: NativeInvocation, row: dict[str, Any]):
        category = row.get("category")
        if category == "skill_ready_to_share":
            return self.reviews.request_candidate(service, invocation, row["event_id"])
        if category == "update_available":
            return self.reviews.request_update(
                service, invocation, row["skill_id"], publisher_fact=row.get("publisher_fact"))
        if category == "new_skill":
            reference = f"{row['skill_id']}@v{row['version']}"
            return self.reviews.request_install(
                service, invocation, reference, publisher_fact=row.get("publisher_fact"))
        raise ValueError("Unsupported Wisdom notice")

    def consume_request(self, raw_args: str) -> NativeInvocation | None:
        try:
            return self.capture.consume_command(raw_args, profile_name=self.profile_name)
        except PermissionError:
            return None

    def _direct_route(self, invocation: NativeInvocation) -> tuple[Any, object, asyncio.AbstractEventLoop]:
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            native, token, loop = (
                self._native.get(route), self._leases.get(route), self._loops.get(route)
            )
        if (native is None or token is None or loop is None
                or not self._notice_route_current(invocation, native, token, loop)):
            raise PermissionError("Collective Wisdom command route is no longer current")
        return native, token, loop

    def _direct_authorized(
        self, invocation: NativeInvocation, native: Any, token: object,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        if (not self._notice_route_current(invocation, native, token, loop)
                or invocation.chat_type.casefold() not in {"dm", "private", "im"}
                or not all((invocation.actor_id, invocation.chat_id, invocation.session_key))):
            return False
        adapter = invocation.adapter
        try:
            if invocation.platform == "slack":
                authorize = getattr(adapter, "_is_interactive_user_authorized", None)
                return bool(authorize(
                    invocation.actor_id, channel_id=invocation.chat_id,
                    user_name=invocation.actor_name, team_id=invocation.scope_id,
                )) if callable(authorize) else False
            if invocation.platform == "telegram":
                authorize = getattr(adapter, "_is_callback_user_authorized", None)
                return bool(authorize(
                    invocation.actor_id, chat_id=invocation.chat_id,
                    chat_type=invocation.chat_type, thread_id=invocation.thread_id or None,
                    user_name=invocation.actor_name,
                )) if callable(authorize) else False
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        return False

    def install_direct(  # pylint: disable=too-many-locals
        self, invocation: NativeInvocation, reference: str
    ) -> NativeView | dict[str, str]:
        """Admitted install applies clean files or reviews undisclosed setup."""
        native, token, loop = self._direct_route(invocation)
        def current() -> bool:
            return self._direct_authorized(invocation, native, token, loop)
        if not current():
            raise PermissionError("Collective Wisdom installation is no longer authorized")
        service = self.service_factory()
        try:
            _require_direct_install_owner(service, invocation)
            try:
                plan = service.install_plan(reference)
            except PackagePolicyError as exc:
                return _already_installed_view(service, reference, exc, current)
            blocked = _direct_install_blocked(plan, reference)
            if blocked is not None:
                return blocked
            receipt, slug = plan.get("receipt"), plan.get("slug")
            if not isinstance(receipt, str) or not receipt or not isinstance(slug, str) or not slug:
                raise WisdomValidationError("Collective Wisdom install plan is incomplete")
            if not current():
                raise PermissionError("Collective Wisdom installation is no longer authorized")
            if plan["setup_commands"]:
                # A slash command cannot approve commands it has not yet shown.
                return self._begin(
                    invocation,
                    lambda review_service: self.reviews.request_install(
                        review_service, invocation, reference, plan=plan,
                    ),
                )
            result = service.install_apply(receipt, may_apply=current)
            return _direct_install_outcome(result, slug)
        finally:
            service.close()

    async def send_direct_result(self, invocation: NativeInvocation, view: NativeView) -> None:
        """Send one result card without creating another consent/review record."""
        native, token, loop = self._direct_route(invocation)
        if asyncio.get_running_loop() is not loop or not self._direct_authorized(invocation, native, token, loop):
            raise PermissionError("Collective Wisdom command route changed")
        if invocation.platform == "slack":
            from .native_slack import blocks  # pylint: disable=import-outside-toplevel
            payload: dict[str, Any] = {
                "channel": invocation.chat_id, "text": view.to_text(),
                "blocks": blocks(view, ""),
            }
            if invocation.thread_id:
                payload["thread_ts"] = invocation.thread_id
            await native.client.chat_postMessage(**payload)
        else:
            from .native_telegram import send  # pylint: disable=import-outside-toplevel
            await send(
                invocation.adapter._bot, view, "",  # pylint: disable=protected-access
                chat_id=invocation.chat_id, thread_id=invocation.thread_id,
            )

    def _lease(self, platform: str, native: Any, adapter: Any) -> object:
        loop = asyncio.get_running_loop()
        token = object()
        with self._lock:
            self._native[(platform, id(adapter))] = native
            self._leases[(platform, id(adapter))] = token
            self._loops[(platform, id(adapter))] = loop
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

    async def _callback_action(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, record_id: str, selected: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
        progress: Callable[[NativeView, dict[str, Any]], None],
        active: threading.Event, progress_done: asyncio.Event, bounded: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Bound pre-edit ownership waits without abandoning an effect after its edit."""
        loop = asyncio.get_running_loop()

        def work():
            try:
                def marked_progress(view, claimed):
                    progress(view, claimed)
                    loop.call_soon_threadsafe(progress_done.set)

                updated = self.reviews.act(
                    record_id, selected, service, invocation,
                    authority=authority, progress=marked_progress,
                )
                display = (updated.get("failure") != "package_preparation_failed"
                           or self.reviews.display_current(updated, service, invocation, authority))
                return updated, display
            finally:
                service.close()

        task = asyncio.create_task(asyncio.to_thread(work))
        pending_progress = asyncio.create_task(progress_done.wait()) if bounded else None
        try:
            if pending_progress is None:
                return await task
            done, _ = await asyncio.wait(
                {task, pending_progress}, timeout=PROGRESS_TIMEOUT + 0.05,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                active.clear()
                task.add_done_callback(lambda finished: finished.exception()
                                       if not finished.cancelled() else None)
                raise TimeoutError("Collective Wisdom progress check timed out")
            return await task
        except asyncio.CancelledError:
            active.clear()
            task.add_done_callback(lambda finished: finished.exception()
                                   if not finished.cancelled() else None)
            raise
        finally:
            if pending_progress is not None:
                pending_progress.cancel()

    def wire_slack(self, native: Any, adapter: Any) -> None:
        token = self._lease("slack", native, adapter)

        @native.command("/wisdom")
        async def slash(ack: Any, command: dict[str, Any]) -> None:
            """Route the app's slash through ordinary Gateway admission.

            The host's 50-command matcher can omit an already-registered plugin
            command; its public message path still owns authentication and dispatch.
            """
            chat_id = str(command.get("channel_id") or "")
            if not chat_id.startswith("D"):
                await ack(response_type="ephemeral", text="Open a private chat to use Collective Wisdom.")
                return
            await ack()
            if not self._lease_current("slack", adapter, token) or adapter_profile(adapter) != self.profile_name:
                return
            from gateway.platforms.event import MessageEvent, MessageType  # pylint: disable=import-outside-toplevel,import-error
            source = adapter.build_source(
                chat_id=chat_id, chat_type="dm", user_id=str(command.get("user_id") or ""),
                scope_id=str(command.get("team_id") or "") or None,
            )
            await adapter.handle_message(MessageEvent(
                text="/wisdom " + str(command.get("text") or ""),
                message_type=MessageType.COMMAND, source=source, raw_message=command,
            ))

        async def callback(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:  # pylint: disable=too-many-locals
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
                loop = asyncio.get_running_loop()
                active = threading.Event()
                active.set()
                progress_done = asyncio.Event()
                def authority():
                    return active.is_set() and self._lease_current("slack", adapter, token)
                def progress(view, claimed):
                    edit_progress(
                        loop, lambda: native.client.chat_update(
                            channel=invocation.chat_id, ts=invocation.message_id,
                            text=view.to_text(), blocks=blocks(view, record_id)),
                        lambda: self.reviews.display_current(claimed, service, invocation, authority),
                        slack=True, allowed=active.is_set,
                    )
                updated, display = await self._callback_action(
                    record_id, selected, service, invocation, authority, progress,
                    active, progress_done,
                    selected == record.get("operation") and selected in {"install", "update"}
                    or selected.startswith("catalog.install.") and record.get("operation") == "catalog",
                )
                if display:
                    view = self.reviews.view(updated)
                    with self.reviews.displaying(updated):
                        await native.client.chat_update(
                            channel=invocation.chat_id, ts=invocation.message_id,
                            text=view.to_text(), blocks=blocks(view, record_id),
                        )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Collective Wisdom Slack callback failed (%s)", type(exc).__name__,
                )
                return

        native.action(re.compile(r"^wisdom_native_[a-z0-9_]+_\d+$"))(callback)

    def wire_telegram(self, native: Any, adapter: Any) -> None:
        from telegram.ext import (  # pylint: disable=import-outside-toplevel
            ApplicationHandlerStop, CallbackQueryHandler,
        )
        token = self._lease("telegram", native, adapter)

        async def callback(update: Any, _context: Any) -> None:  # pylint: disable=too-many-locals
            query = getattr(update, "callback_query", None)
            if query is None:
                return
            read_failed = False
            action_completed = False
            acknowledged = False
            invocation = None
            record = None
            selected = ""
            try:
                self._check_lease("telegram", adapter, token)
                record_id, selected = parse_callback(str(getattr(query, "data", "") or ""))
                record = self.reviews.load(record_id)
                if selected not in {"details", "review"} or record.get("operation") not in {"install", "update"}:
                    await query.answer()
                    acknowledged = True
                from .native_telegram import (  # pylint: disable=import-outside-toplevel
                    callback_invocation, edit,
                )
                invocation = callback_invocation(adapter, query, record)
                updated, display = await self._telegram_action(
                    query, record, record_id, selected, adapter, token, invocation, edit,
                )
                action_completed = True
                if not acknowledged:
                    await query.answer()
                    acknowledged = True
                if display:
                    with self.reviews.displaying(updated):
                        await edit(query, self.reviews.view(updated), record_id)
            except WisdomUnavailable:
                read_failed = (not action_completed and record is not None
                               and selected in {"details", "review"}
                               and record.get("operation") in {"install", "update"})
                await self._telegram_unavailable(
                    query, read_failed, invocation, record, adapter, token, acknowledged,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                if not acknowledged:
                    await query.answer()
            raise ApplicationHandlerStop

        native.add_handler(CallbackQueryHandler(callback, pattern=r"^wi:agent:", block=True), group=-10)

    async def _telegram_action(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, query, record, record_id, selected, adapter, token, invocation, edit,
    ):
        service = self.service_factory()
        active = threading.Event()
        active.set()
        progress_done = asyncio.Event()
        def authority():
            return active.is_set() and self._lease_current("telegram", adapter, token)
        def progress(view, claimed, loop=asyncio.get_running_loop()):
            edit_progress(
                loop,
                lambda: edit(query, view, record_id, require_change=True),
                lambda: self.reviews.display_current(claimed, service, invocation, authority),
                allowed=active.is_set,
            )
        return await self._callback_action(
            record_id, selected, service, invocation, authority, progress,
            active, progress_done,
            selected == record.get("operation") and selected in {"install", "update"}
            or selected.startswith("catalog.install.") and record.get("operation") == "catalog",
        )

    async def _telegram_unavailable(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, query, read_failed, invocation, record, adapter, token, acknowledged,
    ):
        show_alert = False
        if read_failed and invocation is not None and isinstance(record, dict):
            expected_owner = record.get("owner")
            show_alert = (
                isinstance(expected_owner, dict)
                and invocation.chat_type == "private"
                and self._lease_current("telegram", adapter, token)
                and await asyncio.to_thread(
                    self._post_owner_current, expected_owner
                )
            )
        if show_alert and not acknowledged:
            await query.answer(
                text="Collective Wisdom is temporarily unavailable. Try this read-only control again later.",
                show_alert=True,
            )
        elif not acknowledged:
            await query.answer()

    def _post_owner_current(self, expected_owner: dict[str, str]) -> bool:
        try:
            service = self.service_factory()
            try:
                return service.native_owner() == expected_owner
            finally:
                service.close()
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    async def _discard_stale_post(
        self, invocation: NativeInvocation, native: Any, response: Any,
        record: dict[str, Any],
    ) -> None:
        cancelled = {
            **record, "state": "cancelled", "updated_at": self.reviews.clock(),
        }
        self.reviews.state.compare_replace(record, cancelled)
        try:
            message_id = posted_message_id(
                response, platform=invocation.platform, chat_id=invocation.chat_id
            )
        except (TypeError, ValueError, WisdomValidationError):
            # An uncertain acknowledgement is not permission to invent an ID.
            return
        try:
            if invocation.platform == "slack":
                remove: Any = getattr(native.client, "chat_delete", None)
                if callable(remove):
                    await remove(channel=invocation.chat_id, ts=message_id)
            else:
                bot = invocation.adapter._bot  # pylint: disable=protected-access
                remove = getattr(bot, "delete_message", None)
                if callable(remove):
                    chat_id = (
                        int(invocation.chat_id)
                        if invocation.chat_id.lstrip("-").isdigit()
                        else invocation.chat_id
                    )
                    await remove(chat_id=chat_id, message_id=int(message_id))
        except Exception:  # pylint: disable=broad-exception-caught
            return

    async def _post_review(
        self, invocation: NativeInvocation, record: dict[str, Any], *,
        route_authority: Callable[[], bool] | None = None,
    ) -> None:
        if route_authority is not None and route_authority() is not True:
            return
        if not await asyncio.to_thread(self._post_owner_current, record["owner"]):
            return
        if route_authority is not None and route_authority() is not True:
            return
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
        if not self._post_owner_current(record["owner"]):
            await self._discard_stale_post(invocation, native, response, record)
            return
        message_id = posted_message_id(
            response, platform=invocation.platform, chat_id=invocation.chat_id
        )
        with self._lock:
            if self._closed or self._native.get(
                (invocation.platform, id(invocation.adapter))
            ) is not native:
                return
        self.reviews.bind_message(record["id"], invocation, message_id)

    def _schedule_review_post(
        self, invocation: NativeInvocation, record: dict[str, Any],
        native: Any, token: object, loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Create the supervised review task on its adapter's event loop."""
        if not self._notice_route_current(invocation, native, token, loop):
            return
        coroutine = self._post_review(
            invocation, record,
            route_authority=lambda: self._notice_route_current(invocation, native, token, loop),
        )
        try:
            self.spawn_task(coroutine, name=f"wisdom-review:{invocation.session_key}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            coroutine.close()
            logger.warning("Collective Wisdom review dispatch failed (%s)", type(exc).__name__)

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
        route = (invocation.platform, id(invocation.adapter))
        with self._lock:
            native = self._native.get(route)
            token = self._leases.get(route)
            loop = self._loops.get(route)
        if (native is None or token is None or loop is None
                or not self._notice_route_current(invocation, native, token, loop)):
            return {"state": "unavailable"}
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            self._schedule_review_post(invocation, record, native, token, loop)
        else:
            try:
                loop.call_soon_threadsafe(
                    self._schedule_review_post, invocation, record, native, token, loop,
                )
            except RuntimeError:
                return {"state": "unavailable"}
        return {"state": "queued", "review_id": record["id"]}

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
        try:
            return self._begin(
                invocation,
                lambda service: self.reviews.request_catalog(service, invocation, command, target),
            )
        except WisdomUnavailable as exc:
            if invocation.platform not in {"telegram", "slack"}:
                raise
            if invocation.platform == "slack":
                return f"{exc} Resend your /wisdom {command} command to retry."
            return (
                "Collective Wisdom is temporarily unavailable. "
                f"Resend /wisdom {command}" + (f" {target}" if target else "") + " to try again."
            )

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
            lambda service: self.reviews.request_install(
                service, invocation, reference, explanation=str(args.get("explanation") or ""),
            ),
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
