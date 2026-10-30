"""Wire automatic share suggestions to the Hermes host and this plugin's runtime.

Kept apart from ``auto_suggest`` so the controller never imports the runtime
(which imports the command layer that reads the suggestion store).
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Mapping

from .auto_suggest import AutoSuggest, SuggestionStore, self_learning_interval
from .gateway_endpoint import host_config_loader
from . import runtime as _runtime
from .top_skills import _home_override, _profile_home, session_surface


def host_session() -> dict[str, str]:
    """The host-bound session facts of the calling turn (empty outside a session)."""
    facts = dict(session_surface())
    try:
        context = _runtime.current_session_context()
        facts.update({key: value for key, value in context.items() if value})
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return facts


def _core_config(home: Path) -> Mapping[str, Any]:
    """Hermes's own config of this plugin's profile (read-only).

    Raises when it cannot be read for this profile, which turns suggestions off.
    """
    loader = host_config_loader(import_module("hermes_cli.config"))
    override = _home_override()
    if loader is None or override is None:
        raise LookupError("Hermes config is unavailable for this profile")
    with _profile_home(home, override):
        return loader() or {}


def _method(ctx: Any, name: str) -> Callable[..., Any] | None:
    value = getattr(ctx, name, None)
    return value if callable(value) else None


def build(ctx: Any, runtime: Any, profile: tuple[str, Path],
          setting: Callable[[str, Any], Any], *, fork_task: str | None = None) -> AutoSuggest:
    """Wire the controller to this plugin's host context and runtime.

    ``ctx.fork_session`` and ``ctx.side_question`` are detected here, at register
    time; which one runs is decided per turn by the capability the user granted.
    """
    def candidate(name: str) -> dict[str, Any] | None:
        service = runtime._service()  # pylint: disable=protected-access
        try:
            return None if service.setup_pending() else service.share_candidate(name)
        finally:
            runtime._close_service(service)  # pylint: disable=protected-access

    return AutoSuggest(
        store=SuggestionStore(ctx.state, runtime.root),
        side_question=_method(ctx, "side_question"), has_capability=ctx.has_capability,
        fork_session=_method(ctx, "fork_session"), fork_task=fork_task,
        plugin_id=str(getattr(runtime, "plugin_id", "") or "collective-wisdom"),
        inject=getattr(ctx, "inject_message", None), setting=setting,
        self_learning=lambda: self_learning_interval(lambda: _core_config(profile[1])),
        candidate=candidate, session=host_session,
        native_available=runtime.native.auto_suggest_available,
        present=runtime.native.present_auto_suggest,
        profile=profile[0], profile_current=lambda: _runtime.current_profile() == profile,
    )
