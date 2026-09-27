"""Native Hermes entrypoint for terminal setup and safe skill discovery."""
# pylint: disable=too-many-locals,too-many-statements

from typing import Any
import asyncio
import threading



def _register_end_turn_awareness(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock,
    poll_lock, synchronous_turn_thread,
):
    from .runtime import current_profile  # pylint: disable=import-outside-toplevel
    def on_session_end(**facts: Any) -> None:
        lifecycle.on_session_end(**facts)
        runtime.native.end_turn(**facts)
        key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
        with awareness_lock:
            owner = awareness_turns.pop(key, None)
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


def _register_local_awareness(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock, poll_lock,
):
    from .runtime import current_profile  # pylint: disable=import-outside-toplevel
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
        key = (str(facts.get("task_id") or ""), str(facts.get("session_id") or ""))
        if (str(facts.get("platform") or "").casefold() not in {"cli", "tui"}
                or not all(key) or facts.get("parent_session_id") or current_profile() != profile
                or not synchronous_turn_thread()):
            return
        try:
            owner = _local_awareness_owner(runtime, profile)
            if owner is None:
                return
            with awareness_lock:
                if len(awareness_turns) >= 128:
                    awareness_turns.pop(next(iter(awareness_turns)))
                awareness_turns[key] = owner
        except Exception:  # pylint: disable=broad-exception-caught
            return  # Signed out or not enrolled: no remote work.

    runtime.registrations.append(
        ctx.register_hook("pre_llm_call", pre_llm_call)
    )
    runtime.registrations.append(ctx.register_hook(
        "post_llm_call", runtime.native.capture_context,
    ))
    runtime.registrations.append(
        ctx.register_hook("on_skill_lifecycle", lifecycle.on_skill_lifecycle)
    )
    _register_end_turn_awareness(
        ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock,
        poll_lock, synchronous_turn_thread,
    )


def register(ctx: Any) -> None:
    """Register lazily, including when signed out; start no background work."""
    from .commands import build_parser  # pylint: disable=import-outside-toplevel

    from .native_tools import register_native_tools  # pylint: disable=import-outside-toplevel
    from .qualification import LifecycleBridge  # pylint: disable=import-outside-toplevel
    from .runtime import CommandRuntime, current_profile  # pylint: disable=import-outside-toplevel

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
        state=ctx.state, subagent_lifecycle=getattr(ctx, "subagent_lifecycle", None),
    )
    lifecycle = LifecycleBridge(
        ctx.state.data_dir, profile, state=ctx.state, llm=ctx.llm, get_config=config_value,
    )
    awareness_turns: dict[tuple[str, str], dict[str, str]] = {}
    awareness_lock = threading.Lock()
    poll_lock = threading.Lock()

    runtime.registrations.append(ctx.register_auxiliary_task(
        "hermes_collective_wisdom_refinement",
        display_name="Collective Wisdom refinement review",
        description="Classify only ambiguous local skill refinements after an owning human turn.",
    ))

    def close() -> None:
        with awareness_lock:
            awareness_turns.clear()
        lifecycle.close()
        runtime.close()

    ctx.on_unload(close)
    runtime.registrations.append(
        ctx.register_hook("pre_gateway_dispatch", runtime.native.hook)
    )

    _register_local_awareness(
        ctx, runtime, lifecycle, profile, awareness_turns, awareness_lock, poll_lock,
    )
    ctx.register_platform_handler("slack", runtime.native.wire_slack)
    ctx.register_telegram_handler(runtime.native.wire_telegram)
    runtime.registrations.extend(register_native_tools(ctx, runtime))
    runtime.registrations.append(ctx.register_cli_command(
        "wisdom", "Collective Wisdom setup and discovery", build_parser, runtime.cli
    ))
    registration = ctx.register_command(
        "wisdom", runtime.slash, description="Browse shared Collective Wisdom skills",
        args_hint="help | status | browse | show | versions", argument_mode="text",
    )
    if registration is not None:
        runtime.registrations.append(registration)
