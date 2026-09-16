"""Native Hermes entrypoint for terminal setup and safe skill discovery."""


def register(ctx: object) -> None:
    """Register lazily, including when signed out; start no background work."""
    from .commands import build_parser  # pylint: disable=import-outside-toplevel
    from .runtime import CommandRuntime, current_profile  # pylint: disable=import-outside-toplevel

    runtime = CommandRuntime(ctx.state.data_dir, current_profile())
    ctx.on_unload(runtime.close)
    runtime.registrations.append(ctx.register_cli_command(
        "wisdom", "Collective Wisdom setup and discovery", build_parser, runtime.cli
    ))
    registration = ctx.register_command(
        "wisdom", runtime.slash, description="Browse shared Collective Wisdom skills",
        args_hint="help | status | browse | show | versions", argument_mode="text",
    )
    if registration is not None:
        runtime.registrations.append(registration)
