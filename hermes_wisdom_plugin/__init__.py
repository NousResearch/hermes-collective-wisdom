"""Native Hermes entrypoint for terminal setup and safe skill discovery."""
# pylint: disable=too-many-locals

from typing import Any


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
        state=ctx.state,
    )
    lifecycle = LifecycleBridge(
        ctx.state.data_dir, profile, state=ctx.state, llm=ctx.llm, get_config=config_value,
    )

    runtime.registrations.append(ctx.register_auxiliary_task(
        "hermes_collective_wisdom_refinement",
        display_name="Collective Wisdom refinement review",
        description="Classify only ambiguous local skill refinements after an owning human turn.",
    ))

    def close() -> None:
        lifecycle.close()
        runtime.close()

    ctx.on_unload(close)
    runtime.registrations.append(
        ctx.register_hook("pre_gateway_dispatch", runtime.native.hook)
    )

    def pre_llm_call(**facts: Any) -> None:
        runtime.native.lifecycle_hook(**facts)
        lifecycle.admit_turn(native_runtime=runtime.native, **facts)

    runtime.registrations.append(
        ctx.register_hook("pre_llm_call", pre_llm_call)
    )
    runtime.registrations.append(
        ctx.register_hook("on_skill_lifecycle", lifecycle.on_skill_lifecycle)
    )
    def on_session_end(**facts: Any) -> None:
        lifecycle.on_session_end(**facts)
        runtime.native.end_turn(**facts)

    runtime.registrations.append(ctx.register_hook("on_session_end", on_session_end))
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
