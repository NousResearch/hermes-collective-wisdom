"""Native Hermes entrypoint for terminal setup and safe skill discovery."""
# pylint: disable=too-many-locals

from typing import Any


def register(ctx: Any) -> None:
    """Register lazily, including when signed out; start no background work."""
    from .commands import build_parser  # pylint: disable=import-outside-toplevel
    from .mediation import AssessmentBridge  # pylint: disable=import-outside-toplevel
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
    request_review = getattr(ctx, "request_review", None)
    can_request_review = getattr(ctx, "can_request_review", None)
    is_review_current = getattr(ctx, "is_review_current", None)
    review_action_type = None
    review_outcome_type = None
    if callable(request_review):
        try:
            from hermes_cli.plugins import (  # pylint: disable=import-outside-toplevel
                ReviewAction,
                ReviewOutcome,
            )
        except (ImportError, AttributeError):
            request_review = None
        else:
            review_action_type = ReviewAction
            review_outcome_type = ReviewOutcome
    runtime = CommandRuntime(
        ctx.state.data_dir, profile, llm=ctx.llm, spawn_task=ctx.spawn_task,
        config=config, get_config=get_config, set_config=set_config,
        request_review=request_review,
        can_request_review=can_request_review if callable(can_request_review) else None,
        is_review_current=is_review_current if callable(is_review_current) else None,
        review_action_type=review_action_type,
        review_outcome_type=review_outcome_type,
    )
    lifecycle = LifecycleBridge(
        ctx.state.data_dir, profile, llm=ctx.llm, get_config=config_value,
    )
    assessment = AssessmentBridge(
        runtime._service, ctx.llm,  # pylint: disable=protected-access
        get_delivery_mode=lambda: str(
            config_value("notifications.delivery_mode", "agent") or "fixed"
        ),
        profile_name=profile[0],
    )
    runtime.registrations.append(ctx.register_auxiliary_task(
        "hermes_collective_wisdom_refinement",
        display_name="Collective Wisdom refinement review",
        description="Classify only ambiguous local skill refinements after an owning human turn.",
    ))

    def close() -> None:
        assessment.close()
        lifecycle.close()
        runtime.close()

    ctx.on_unload(close)
    runtime.registrations.append(
        ctx.register_hook("pre_gateway_dispatch", runtime.native.hook)
    )

    def pre_llm_call(**facts: Any) -> None:
        runtime.native.lifecycle_hook(**facts)
        assessment.run(native_runtime=runtime.native, **facts)
        lifecycle.admit_turn(native_runtime=runtime.native, **facts)

    runtime.registrations.append(
        ctx.register_hook("pre_llm_call", pre_llm_call)
    )
    runtime.registrations.append(
        ctx.register_hook("on_skill_lifecycle", lifecycle.on_skill_lifecycle)
    )
    runtime.registrations.append(
        ctx.register_hook("on_session_end", lifecycle.on_session_end)
    )
    register_presentation = getattr(ctx, "register_presentation_handler", None)
    if callable(register_presentation):
        async def present_ready_advice(opportunity: Any) -> None:
            await runtime.native.present_local_assessments(
                opportunity,
                get_delivery_mode=lambda: str(
                    config_value("notifications.delivery_mode", "agent") or "fixed"
                ),
                assessment=assessment,
            )

        runtime.registrations.append(register_presentation(present_ready_advice))
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
