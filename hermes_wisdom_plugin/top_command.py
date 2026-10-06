"""Terminal and private-chat projections of the owner's top local skills."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .top_skills import (
    DEFAULT_PER_PAGE,
    EMPTY_TEXT,
    MAX_PER_PAGE,
    UNAVAILABLE_TEXT,
    is_messaging,
    is_private_chat,
    last_used_label,
)

TOP_USAGE = "Usage: /wisdom top [page]"
_MAX_PAGE = 10_000


def refusal(command: str) -> str:
    """Refuse a private ranking outside a private chat without naming any skill."""
    return (
        "Your skill usage is private. Open /wisdom " + command
        + " in a private chat with Hermes; no skill names were shown here."
    )


def private_route(
    invocation: Any, gateway_command_pending: bool, surface: dict[str, str],
) -> dict[str, Any] | None:
    """Return where this private request came from, or ``None`` to refuse.

    An admitted native invocation is already fenced to a private Telegram/Slack
    chat. A captured but unadmitted gateway command is a group or stale route.
    Otherwise the host-bound session decides: messaging chats must be private;
    the CLI, TUI and Desktop are the owner's own local sessions.
    """
    if invocation is not None:
        return {"kind": "messaging", "origin": {
            key: value for key, value in {
                "platform": invocation.platform, "chat_id": invocation.chat_id,
                "chat_type": invocation.chat_type, "thread_id": invocation.thread_id,
                "user_id": invocation.actor_id, "user_name": invocation.actor_name,
                "scope_id": invocation.scope_id,
            }.items() if value
        }}
    if gateway_command_pending:
        return None
    if is_messaging(surface):
        if not is_private_chat(surface.get("chat_type", "")) or not surface.get("chat_id"):
            return None
        platform = surface.get("platform") or surface.get("source") or ""
        return {"kind": "messaging", "origin": {
            key: value for key, value in {
                "platform": platform.strip().casefold(), "chat_id": surface.get("chat_id"),
                "chat_type": surface.get("chat_type"), "thread_id": surface.get("thread_id"),
                "user_id": surface.get("user_id"),
            }.items() if value
        }}
    return {"kind": "local", "session_key": surface.get("session_key", "")}


def page_argument(args: list[str]) -> int:
    """Parse the optional slash page number."""
    if not args:
        return 1
    if len(args) != 1:
        raise ValueError(TOP_USAGE)
    try:
        page = int(args[0])
    except ValueError as exc:
        raise ValueError(TOP_USAGE) from exc
    if not 1 <= page <= _MAX_PAGE:
        raise ValueError(TOP_USAGE)
    return page


def state_text(state: str) -> str | None:
    """Return the fixed text for an empty or unavailable ranking."""
    return {"unavailable": UNAVAILABLE_TEXT, "empty": EMPTY_TEXT}.get(state)


def render_rows(data: dict[str, Any]) -> list[str]:
    """Render one page as numbered single-line rows."""
    start = (data["page"] - 1) * data["per_page"]
    return [
        f"{start + index}. {row['name']} | {row['uses']} uses | {row['edits']} edits"
        f" | last used {last_used_label(row['last_used'])}"
        + (" | Shared" if row["shared"] else "")
        for index, row in enumerate(data["items"], start=1)
    ]


def render_page(data: dict[str, Any], *, next_command: str, previous_command: str) -> str:
    """Render a page with plain-text Next/Previous hints."""
    fixed = state_text(data["state"])
    if fixed is not None:
        return fixed
    if not data["items"]:
        return f"No skills on page {data['page']}. There are {data['pages']} pages."
    lines = [
        f"Your top skills (page {data['page']} of {data['pages']}, {data['total']} skills)",
        "",
        *render_rows(data),
    ]
    hints = []
    if data["page"] > 1:
        hints.append("Previous: " + previous_command.format(page=data["page"] - 1))
    if data["page"] < data["pages"]:
        hints.append("Next: " + next_command.format(page=data["page"] + 1))
    if hints:
        lines.extend(("", " | ".join(hints)))
    return "\n".join(lines)


def run_top(args: Any, service: Any) -> int:
    """Print one page of the owner's top skills for ``hermes wisdom top``."""
    data = service.top_skills().page(args.page, args.per_page)
    if args.json:
        print(json.dumps(data, sort_keys=True))
        return 0
    per_page = "" if args.per_page == DEFAULT_PER_PAGE else f" --per-page {args.per_page}"
    print(render_page(
        data,
        next_command="hermes wisdom top --page {page}" + per_page,
        previous_command="hermes wisdom top --page {page}" + per_page,
    ))
    return 0


def slash_top(args: list[str], service: Any) -> str:
    """Render ``/wisdom top [page]`` for an already-admitted private route."""
    try:
        page = page_argument(args)
    except ValueError as exc:
        return str(exc)
    return render_page(
        service.top_skills().page(page, DEFAULT_PER_PAGE),
        next_command="/wisdom top {page}", previous_command="/wisdom top {page}",
    )


def positive_page(value: str) -> int:
    """argparse type for ``--page``."""
    try:
        page = int(value)
    except ValueError:
        page = 0
    if not 1 <= page <= _MAX_PAGE:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {_MAX_PAGE}")
    return page


def page_size(value: str) -> int:
    """argparse type for ``--per-page``."""
    try:
        size = int(value)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_PER_PAGE:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {MAX_PER_PAGE}")
    return size
