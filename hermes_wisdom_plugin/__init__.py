"""Native Hermes entrypoint for terminal setup and safe skill discovery."""
# pylint: disable=too-many-locals,too-many-statements

from typing import Any
import asyncio
import contextvars
import shlex
import threading
import time



def _register_end_turn_awareness(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock,
    poll_lock, synchronous_turn_thread, offer_turns,
):
    from .runtime import current_profile  # pylint: disable=import-outside-toplevel
    def on_session_end(**facts: Any) -> None:
        lifecycle.on_session_end(**facts)
        runtime.native.end_turn(**facts)
        key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
        with awareness_lock:
            owner = awareness_turns.pop(key, None)
            offer_turns.pop(key, None)
        if (  # pylint: disable=too-many-boolean-expressions
                owner is None or facts.get("completed") is not True
                or facts.get("failed") is not False or facts.get("interrupted") is not False
                or str(facts.get("platform") or "").casefold() not in {"cli", "tui"}
                or current_profile() != profile
                or not synchronous_turn_thread()
                or not poll_lock.acquire(blocking=False)):  # pylint: disable=consider-using-with
            return
        try:
            service = runtime._service()  # pylint: disable=protected-access,consider-using-with
            try:
                if service.native_owner() == owner:
                    service.end_turn_notices(None)
            finally:
                runtime._close_service(service)  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # Offline/auth failures must not fail the owning turn.
        finally:
            poll_lock.release()

    runtime.registrations.append(ctx.register_hook("on_session_end", on_session_end))

def _local_awareness_owner(runtime, profile):
    service = runtime._service()  # pylint: disable=protected-access
    try:
        owner = service.native_owner()
    finally:
        runtime._close_service(service)  # pylint: disable=protected-access
    return owner if owner.get("profile_name") == profile[0] else None


def _local_candidate_text(plan, owner):
    """Present checked private draft facts, never a model-authored instruction."""
    from .native_review import _safe  # pylint: disable=import-outside-toplevel
    from .native_inspection import sharing_checks  # pylint: disable=import-outside-toplevel
    facts = plan.get("facts") or {}
    editorial = facts.get("editorial") or {}
    draft_id = plan.get("draft_id")
    if (not isinstance(draft_id, str) or not draft_id.startswith("local:")
            or len(draft_id) > 160 or not all(
                character.isascii() and (character.isalnum() or character in ":_-")
                for character in draft_id
            )):
        return None
    complete = facts.get("editorial_status") == "complete"
    _safe_checks, checks = sharing_checks(plan)
    return "\n\nCollective Wisdom — local sharing suggestion (not shared)\n" + "\n".join((
        f"Skill: {_safe(editorial.get('editorial_name') or facts.get('name'))}",
        "What it does: " + _safe(editorial.get("editorial_description"),
                                "Editorial description pending"),
        "Why teammates might benefit: " + _safe(editorial.get("coworker_benefit"),
                                               "Benefit pending"),
        f"Destination (verified organization ID): {_safe(owner.get('organization_id'))}",
        f"Editorial: {'complete' if complete else 'incomplete; review or edit private draft'}",
        "Local checks (not a Gateway approval):\n" + checks,
        "Nothing has been uploaded or published. Review/edit the exact private draft first:",
        "  hermes wisdom prepared review " + shlex.quote(draft_id),
        "Sharing later requires a separate exact publication review and explicit approval.",
    ))


def _prepare_local_offer(runtime, grant):
    """Recheck local admission before preparation, then bind the notice receipt."""
    from .notices import _key  # pylint: disable=import-outside-toplevel
    service = runtime._service()  # pylint: disable=protected-access
    try:
        if service.native_owner() != grant["owner"]:
            return None
        # Do not prepare (possibly with editorial model I/O) while muted
        # or suppressed. The existing notice pipeline rechecks at offer time.
        candidate = next((row for row in service.notice_candidates()
                          if row.get("event_id") == grant["event_id"]), None)
        if (candidate is None
                or service.notice_policy().get("skill_ready_to_share") is not True):
            return None
        reference = {"kind": "candidate", "content_hash": candidate["content_hash"],
                     "notice_key": _key(candidate)}
        preferences = service.notice_preferences([reference])
        if (preferences.get("muted")
                or (preferences.get("suppressed") or {}).get(_key(candidate))):
            return None
        plan = service.prepare_candidate_offer(grant["event_id"])
        event = service.qualification_state.local_event(grant["event_id"])
        binding = (event.get("payload") or {}).get("preparation") if event else None
        plan["draft_id"] = (binding or {}).get("draft_id")
        text = _local_candidate_text(plan, grant["owner"])
        if not text or service.native_owner() != grant["owner"]:
            return None
        rows = []
        service.end_turn_notices(rows.extend, candidate_event_id=grant["event_id"])
        if len(rows) != 1 or rows[0].get("event_id") != grant["event_id"]:
            return None
        return text
    finally:
        runtime._close_service(service)  # pylint: disable=protected-access


def _register_local_offer_hook(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    ctx, runtime, profile, offer_turns, awareness_lock, poll_lock, synchronous_turn_thread,
):
    from .runtime import current_profile  # pylint: disable=import-outside-toplevel

    def transform_llm_output(**facts: Any) -> str | None:
        response = facts.get("response_text")
        key_turn = (str(facts.get("session_id") or ""), str(facts.get("turn_id") or ""))
        if (  # pylint: disable=too-many-boolean-expressions
                not isinstance(response, str) or not response or not all(key_turn)
                or str(facts.get("platform") or "").casefold() not in {"cli", "tui"}
                or current_profile() != profile or not synchronous_turn_thread()):
            return None
        with awareness_lock:
            matches = [grant for (task, session), grant in offer_turns.items()
                       if task and session == key_turn[0] and grant["turn_id"] == key_turn[1]
                       and grant["event_id"]]
            if len(matches) != 1:
                return None
            grant = dict(matches[0])
            # Fail closed on ambiguous turns; no response-text replay can re-offer.
            matches[0]["event_id"] = None
        if not poll_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
            return None
        try:
            text = _prepare_local_offer(runtime, grant)
            return response + text if text else None
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        finally:
            poll_lock.release()

    runtime.registrations.append(ctx.register_hook("transform_llm_output", transform_llm_output))


def _admit_local_turn(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    runtime, profile, awareness_turns, offer_turns, awareness_lock, facts,
):
    """Capture the owning turn and bound owner before any skill load occurs."""
    from .runtime import current_profile  # pylint: disable=import-outside-toplevel
    key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
    turn_id = str(facts.get("turn_id") or "")
    if (  # pylint: disable=too-many-boolean-expressions
            str(facts.get("platform") or "").casefold() not in {"cli", "tui"}
            or not all(key) or not turn_id or facts.get("parent_session_id")
            or current_profile() != profile):
        return
    owner = _local_awareness_owner(runtime, profile)
    if owner is None:
        return
    with awareness_lock:
        if len(awareness_turns) >= 128:
            oldest = next(iter(awareness_turns))
            awareness_turns.pop(oldest)
            offer_turns.pop(oldest, None)
        awareness_turns[key] = owner
        offer_turns[key] = {"turn_id": turn_id, "owner": owner, "event_id": None}


def _record_local_skill_load(lifecycle, offer_turns, awareness_lock, facts):
    event_id = lifecycle.on_skill_lifecycle(**facts)
    if not event_id or facts.get("action") != "loaded":
        return
    key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
    with awareness_lock:
        if key in offer_turns:
            offer_turns[key]["event_id"] = event_id


def _register_local_awareness(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock, poll_lock,
    offer_turns,
):
    """Register qualification capture and proactive notice hooks (dormant in v0)."""
    def synchronous_turn_thread() -> bool:
        """Never do credential or feed I/O on a host event-loop thread."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        return False

    def pre_llm_call(**facts: Any) -> None:
        runtime.native.lifecycle_hook(**facts)
        lifecycle.admit_turn(native_runtime=runtime.native, **facts)
        # Local chat has no native renderer. Admission is independent of the
        # optional qualification model-review setting.
        if not synchronous_turn_thread():
            return
        try:
            _admit_local_turn(runtime, profile, awareness_turns, offer_turns,
                              awareness_lock, facts)
        except Exception:  # pylint: disable=broad-exception-caught
            return  # Signed out or not enrolled: no remote work.

    runtime.registrations.append(
        ctx.register_hook("pre_llm_call", pre_llm_call)
    )
    def skill_lifecycle(**facts: Any) -> None:
        _record_local_skill_load(lifecycle, offer_turns, awareness_lock, facts)

    runtime.registrations.append(ctx.register_hook("on_skill_lifecycle", skill_lifecycle))

    _register_local_offer_hook(ctx, runtime, profile, offer_turns, awareness_lock,
                               poll_lock, synchronous_turn_thread)
    _register_end_turn_awareness(
        ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock,
        poll_lock, synchronous_turn_thread, offer_turns,
    )


_DORMANT_AWARENESS_HOOKS = frozenset({"pre_llm_call", "on_skill_lifecycle", "on_session_end"})
_NOTE_SESSIONS = 64


class _TeamUpdates:
    """Session-start note and rate-limited auto-update for installed team skills.

    The note is computed from local state only and frozen per session at
    ``on_session_start``; it is appended once to that session's first local reply.
    The auto-update check runs off the turn thread, at most once per 10 minutes
    per profile (claimed in plugin state before any credential read), so offline
    or signed-out profiles never retry hot.
    """

    def __init__(self, runtime: Any, profile: Any) -> None:
        self.runtime = runtime
        self.profile = profile
        self.notes: dict[str, str] = {}
        self.lock = threading.Lock()
        self.running = False
        self.last_trigger = 0.0

    def session_start(self, **facts: Any) -> None:
        from .runtime import current_profile  # pylint: disable=import-outside-toplevel
        from .team_status import (  # pylint: disable=import-outside-toplevel
            note_platform_allowed, session_note,
        )

        session_id = str(facts.get("session_id") or "")
        try:
            if current_profile() != self.profile:
                return
        except Exception:  # pylint: disable=broad-exception-caught
            return
        if session_id and note_platform_allowed(facts.get("platform")):
            note = session_note(self.runtime, consume=True)
            if note:
                with self.lock:
                    if len(self.notes) >= _NOTE_SESSIONS:
                        self.notes.pop(next(iter(self.notes)))
                    self.notes[session_id] = note
        self.trigger()

    def gateway_dispatch(self, **kwargs: Any) -> Any:
        result = self.runtime.native.hook(**kwargs)
        self.trigger()
        return result

    def first_reply(self, **facts: Any) -> str | None:
        from .team_status import note_platform_allowed  # pylint: disable=import-outside-toplevel

        response = facts.get("response_text")
        if not isinstance(response, str) or not response:
            return None
        if not note_platform_allowed(facts.get("platform")):
            return None
        with self.lock:
            note = self.notes.pop(str(facts.get("session_id") or ""), None)
        return f"{response}\n\n{note}" if note else None

    def trigger(self) -> None:
        """Start one background maintenance pass unless one is already running."""
        now = time.monotonic()
        with self.lock:
            # Cheap in-process debounce; the durable 10-minute claim is in plugin state.
            if self.running or (self.last_trigger and now - self.last_trigger < 60):
                return
            self.running = True
            self.last_trigger = now
        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run, args=(self._run,), name="collective-wisdom-auto-update",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            with self.lock:
                self.running = False

    def _run(self) -> None:
        from .runtime import current_profile  # pylint: disable=import-outside-toplevel

        service = None
        try:
            if current_profile() != self.profile:
                return
            service = self.runtime._service()  # pylint: disable=protected-access
            service.maintenance_tick()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # Signed out, offline or not enrolled: try again next interval.
        finally:
            if service is not None:
                self.runtime._close_service(service)  # pylint: disable=protected-access
            with self.lock:
                self.running = False


def _register_team_updates(ctx: Any, runtime: Any, profile: Any, scope: Any) -> None:
    """Register the cw/v1 session-start note and auto-update trigger."""
    updates = _TeamUpdates(runtime, profile)
    runtime.registrations.append(
        ctx.register_hook("pre_gateway_dispatch", updates.gateway_dispatch)
    )
    if "on_session_start" in scope.hooks:
        runtime.registrations.append(ctx.register_hook("on_session_start", updates.session_start))
    if "transform_llm_output" in scope.hooks:
        runtime.registrations.append(ctx.register_hook("transform_llm_output", updates.first_reply))


def register(ctx: Any) -> None:
    """Register lazily, including when signed out; start no background work."""
    from .commands import build_parser  # pylint: disable=import-outside-toplevel

    from .native_tools import register_native_tools  # pylint: disable=import-outside-toplevel
    from .qualification import LifecycleBridge  # pylint: disable=import-outside-toplevel
    from .runtime import CommandRuntime, current_profile  # pylint: disable=import-outside-toplevel
    from .commands import slash_args_hint  # pylint: disable=import-outside-toplevel
    from .release_scope import active  # pylint: disable=import-outside-toplevel

    scope = active()
    profile = current_profile()
    get_config = getattr(ctx, "get_config", None)
    set_config = getattr(ctx, "set_config", None)

    def config_value(key: str, default: Any = None) -> Any:
        return get_config(key, default) if callable(get_config) else default

    config = {"notifications": {
        key: config_value(f"notifications.{key}", default)
        for key, default in {
            "delivery_mode": "agent",
            "new_skills": "immediate",
            "updates": "immediate",
            "team_published": "immediate",
        }.items()
    }}
    runtime = CommandRuntime(
        ctx.state.data_dir, profile, llm=ctx.llm, spawn_task=ctx.spawn_task,
        config=config, get_config=get_config, set_config=set_config,
        state=ctx.state, inject_message=getattr(ctx, "inject_message", None),
        plugin_id=str(getattr(ctx, "plugin_id", "") or "collective-wisdom"),
    )
    lifecycle = LifecycleBridge(
        ctx.state.data_dir, profile, state=ctx.state, llm=ctx.llm, get_config=config_value,
    )
    awareness_turns: dict[tuple[str, str], dict[str, str]] = {}
    offer_turns: dict[tuple[str, str], dict[str, Any]] = {}
    awareness_lock = threading.Lock()
    poll_lock = threading.Lock()

    if "hermes_collective_wisdom_assist" in scope.auxiliary_tasks:
        runtime.registrations.append(ctx.register_auxiliary_task(
            "hermes_collective_wisdom_assist",
            display_name="Collective Wisdom assist",
            description=(
                "Combine your edits with a teammate's new skill version and suggest a "
                "folder when you share a skill."
            ),
        ))
    if "hermes_collective_wisdom_refinement" in scope.auxiliary_tasks:
        runtime.registrations.append(ctx.register_auxiliary_task(
            "hermes_collective_wisdom_refinement",
            display_name="Collective Wisdom refinement review",
            description=(
                "Classify only ambiguous local skill refinements after an owning human turn."
            ),
        ))

    def close() -> None:
        with awareness_lock:
            awareness_turns.clear()
            offer_turns.clear()
        lifecycle.close()
        runtime.close()

    ctx.on_unload(close)
    # The private /wisdom dispatch hook also drives the rate-limited auto-update
    # check on the messaging gateway (no plugin housekeeping-tick hook exists).
    _register_team_updates(ctx, runtime, profile, scope)

    # Suggestion capture, proactive notices and model tools stay implemented but
    # are registered only by a release scope that ships them.
    if set(scope.hooks) & _DORMANT_AWARENESS_HOOKS:
        _register_local_awareness(
            ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock, poll_lock,
            offer_turns,
        )
    ctx.register_platform_handler("slack", runtime.native.wire_slack)
    ctx.register_telegram_handler(runtime.native.wire_telegram)
    runtime.registrations.extend(register_native_tools(ctx, runtime, scope.model_tools))
    runtime.registrations.append(ctx.register_cli_command(
        "wisdom", "Share and install team skills", build_parser, runtime.cli
    ))
    registration = ctx.register_command(
        "wisdom", runtime.slash, description="Share and install team skills",
        args_hint=slash_args_hint(), argument_mode="text",
    )
    if registration is not None:
        runtime.registrations.append(registration)
