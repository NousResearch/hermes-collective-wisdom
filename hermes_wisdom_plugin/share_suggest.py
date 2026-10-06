"""Ask Hermes, on request, which of the owner's top skills are worth sharing.

``/wisdom share-suggest`` starts one ordinary Hermes turn in the same private
conversation through ``PluginContext.inject_message``. The model may only
suggest: sharing still requires ``/wisdom share <skill>`` and its review. Where
the host cannot start that turn, the user gets the ranked list and a
ready-to-send prompt instead.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable

from .top_command import render_page, render_rows, state_text
from .top_skills import DEFAULT_PER_PAGE, SUGGEST_LIMIT, TopSkill, last_used_label

SHARE_SUGGEST_USAGE = "Usage: /wisdom share-suggest"
SUGGEST_COUNT = 10
ACCEPTED_TEXT = (
    "Asking Hermes which of your skills are worth sharing. Hermes can only suggest; "
    "sharing still needs /wisdom share <skill> and its review."
)
CHAT_POINTER = (
    "To ask Hermes which of these are worth sharing, run /wisdom share-suggest in a "
    "Hermes chat (CLI, TUI, Desktop, or a private messaging chat). Hermes only "
    "suggests; sharing still needs /wisdom share <skill> and its review."
)


def share_command(name: str) -> str:
    """Return the exact share command for one skill name."""
    return "/wisdom share " + shlex.quote(name)


def build_prompt(items: tuple[TopSkill, ...] | list[TopSkill]) -> str:
    """Build the user message that starts the suggestion turn."""
    rows = [
        f"{index}. {item.name}: {item.description or '(no description)'}"
        f" (uses {item.uses}, edits {item.edits}, last used {last_used_label(item.last_used)}"
        + (", already shared" if item.shared else "") + ")"
        for index, item in enumerate(items[:SUGGEST_LIMIT], start=1)
    ]
    return "\n".join((
        "I ran /wisdom share-suggest. Below are up to "
        f"{SUGGEST_LIMIT} of my own local Hermes skills, ranked by recorded usage.",
        "",
        f"Suggest up to {SUGGEST_COUNT} of them that are most worth sharing with my team "
        "through Collective Wisdom, ranked best first. For each, give a short reason and "
        "the exact command to run, for example `/wisdom share my-skill`.",
        "You may read the skill files with your normal tools to judge quality and fit. "
        "Skip skills that look personal, environment-specific or that contain secrets.",
        "Do not share, publish, upload or modify any skill. You cannot share skills: "
        "sharing only happens when I run /wisdom share <skill> and complete its review.",
        "",
        *rows,
    ))


def _compact_prompt(items: tuple[TopSkill, ...]) -> str:
    names = ", ".join(item.name for item in items[:SUGGEST_LIMIT])
    return (
        f"Suggest up to {SUGGEST_COUNT} of my local skills worth sharing with my team "
        "through Collective Wisdom, ranked, each with a short reason and the exact "
        "/wisdom share <skill> command. You may read the skill files. Do not share or "
        f"change anything. Candidates, ranked by usage: {names}"
    )


def fallback_text(items: tuple[TopSkill, ...], plugin_id: str) -> str:
    """Ranked list plus a ready-to-send prompt when Hermes cannot start the turn."""
    first = {
        "page": 1, "per_page": DEFAULT_PER_PAGE,
        "items": [item.as_dict() for item in items[:DEFAULT_PER_PAGE]],
    }
    return "\n".join((
        "Hermes could not start the suggestion turn here. Your top skills:",
        "",
        *render_rows(first),
        "",
        "Send this message to Hermes to get suggestions:",
        "",
        _compact_prompt(items),
        "",
        "To let /wisdom share-suggest start the turn itself in TUI, Desktop or messaging "
        "chats, an operator can run:",
        f"hermes config set plugins.entries.{plugin_id}.allow_gateway_injection true",
    ))


def _injection_target(route: dict[str, Any]) -> dict[str, Any]:
    if route.get("kind") == "messaging":
        return {"origin": dict(route["origin"])}
    return {"session_key": route["session_key"]} if route.get("session_key") else {}


def _inject(inject: Callable[..., Any], content: str, route: dict[str, Any]) -> bool:
    """Start the turn; hosts before ``origin`` support continue the existing session."""
    target = _injection_target(route)
    try:
        return inject(content, role="user", **target) is True
    except TypeError:
        if "origin" not in target or not route.get("session_key"):
            raise
    return inject(content, role="user", session_key=route["session_key"]) is True


def slash_share_suggest(  # pylint: disable=too-many-arguments
    args: list[str], service: Any, route: dict[str, Any], *,
    inject: Callable[..., Any] | None, plugin_id: str,
) -> str:
    """Start the suggestion turn for an admitted private route, or fall back."""
    if args:
        return SHARE_SUGGEST_USAGE
    ranked = service.top_skills()
    fixed = state_text(ranked.state)
    if fixed is not None:
        return fixed
    accepted = False
    if callable(inject):
        try:
            accepted = _inject(inject, build_prompt(ranked.items), route)
        except Exception:  # pylint: disable=broad-exception-caught
            accepted = False
    return ACCEPTED_TEXT if accepted else fallback_text(ranked.items, plugin_id)


def run_share_suggest(args: Any, service: Any) -> int:
    """Print the first ranked page and point to the chat command; no model call."""
    data = service.top_skills().page(1, DEFAULT_PER_PAGE)
    if args.json:
        print(json.dumps({**data, "next": "/wisdom share-suggest"}, sort_keys=True))
        return 0
    fixed = state_text(data["state"])
    if fixed is not None:
        print(fixed)
        return 0
    print(render_page(
        data, next_command="hermes wisdom top --page {page}",
        previous_command="hermes wisdom top --page {page}",
    ))
    print("\n" + CHAT_POINTER)
    return 0
