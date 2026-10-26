"""Let this plugin start Hermes turns in the owner's chat, unless the operator chose.

``/wisdom share-suggest`` and automatic share suggestions start a turn through
``PluginContext.inject_message``. Outside the classic CLI chat Hermes allows
that only with ``plugins.entries.<plugin_id>.allow_gateway_injection: true``,
which names this plugin alone. ``hermes wisdom setup`` sets it with
``hermes config set`` when the key is absent. Any value the operator already
wrote, ``false`` included, is theirs and stays as it is: ``false`` is the
opt-out. ``hermes wisdom status`` reports the state and the command to change it.
"""

from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from typing import Any, Callable, Mapping, cast

from .share_suggest import hermes_command

KEY = "allow_gateway_injection"
_WRITE_TIMEOUT_SECONDS = 60


def config_key(plugin_id: str) -> str:
    """The full config path Hermes reads for this plugin's injection permission."""
    return f"plugins.entries.{plugin_id}.{KEY}"


def _host_loader() -> Callable[[], Mapping[str, Any] | None] | None:
    try:
        module = import_module("hermes_cli.config")
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    for name in ("load_config_readonly", "load_config"):
        loader = getattr(module, name, None)
        if callable(loader):
            return cast(Callable[[], Mapping[str, Any] | None], loader)
    return None


def injection_status(
    plugin_id: str, *, load_config: Callable[[], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Read the effective value and whether anyone has written the key."""
    loader = load_config or _host_loader()
    try:
        config = (loader() if loader is not None else None) or {}
        entries = (config.get("plugins") or {}).get("entries") or {}
        entry = entries.get(plugin_id) if isinstance(entries, Mapping) else None
    except Exception:  # pylint: disable=broad-exception-caught
        return {"key": config_key(plugin_id), "configured": False, "enabled": False,
                "readable": False}
    configured = isinstance(entry, Mapping) and KEY in entry
    return {
        "key": config_key(plugin_id),
        "configured": configured,
        # Hermes grants injection only for the literal boolean true.
        "enabled": isinstance(entry, Mapping) and entry.get(KEY) is True,
        "readable": loader is not None,
    }


def write_command(plugin_id: str, profile: str, value: bool) -> list[str]:
    """``hermes [-p <profile>] config set <key> <value>`` run with this Hermes's Python."""
    profile_args = [] if profile in {"", "default"} else ["-p", profile]
    return [sys.executable, "-m", "hermes_cli.main", *profile_args, "config", "set",
            config_key(plugin_id), "true" if value else "false"]


def ensure_injection_default(
    plugin_id: str, profile: str, *,
    load_config: Callable[[], Mapping[str, Any] | None] | None = None,
    run: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Set the key to true only when it is absent; report what happened."""
    current = injection_status(plugin_id, load_config=load_config)
    if current["configured"] or not current["readable"]:
        return {**current, "changed": False}
    try:
        completed = (run or subprocess.run)(
            write_command(plugin_id, profile, True), capture_output=True, text=True,
            timeout=_WRITE_TIMEOUT_SECONDS, check=False,
        )
        succeeded = getattr(completed, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        succeeded = False
    after = injection_status(plugin_id, load_config=load_config)
    return {**after, "changed": bool(succeeded and after["enabled"]),
            **({} if after["configured"] else {"write_failed": True})}


def injection_text(status: Mapping[str, Any], profile: str = "default") -> str:
    """One status line plus the command that changes it, in this profile."""
    key = str(status.get("key") or config_key("collective-wisdom"))
    command = f"{hermes_command(profile)} config set {key}"
    if status.get("enabled"):
        lead = ("Chat suggestions: on (setup allowed Collective Wisdom to start turns in "
                "your chats)." if status.get("changed") else "Chat suggestions: on.")
        return f"{lead}\nTo turn them off: {command} false"
    if status.get("configured"):
        return f"Chat suggestions: off (set by an operator).\nTo turn them on: {command} true"
    if status.get("write_failed"):
        return ("Chat suggestions: off. Setup couldn't change the Hermes config.\n"
                f"To turn them on: {command} true")
    return f"Chat suggestions: off until setup turns them on.\nTo turn them on: {command} true"
