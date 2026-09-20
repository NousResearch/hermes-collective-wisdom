"""Reference Slack Block Kit renderer and authenticated callback extraction."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .native_context import NativeInvocation, adapter_profile
from .native_review import NativeView, callback_value, slack_section_text

_MAX_ITEMS = 5
_MAX_BUTTON_LABEL = 75


def _action_id(action: str, index: int) -> str:
    """Return a per-row unique ID while callback values remain authoritative."""
    slug = re.sub(r"[^a-z0-9]+", "_", action.casefold()).strip("_") or "action"
    return f"wisdom_native_{slug}_{index}"


def _trim(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return (
        text if len(text) <= limit
        else text[: max(0, limit - 1)].rstrip() + "…"
    )


def _action_blocks(
    actions: Iterable[Any], record_id: str, *, row: str, allow_primary: bool = True,
) -> list[dict[str, Any]]:
    elements = []
    for index, action in enumerate(actions):
        if not action.action and not action.url:
            continue
        if action.primary and not allow_primary:
            continue
        button: dict[str, Any] = {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": _trim(action.label, _MAX_BUTTON_LABEL),
                "emoji": True,
            },
        }
        if action.url:
            button["url"] = action.url
            button["action_id"] = _action_id("url", index)
        else:
            button["action_id"] = _action_id(action.action, index)
            button["value"] = callback_value(record_id, action.action)
        if action.primary:
            button["style"] = "primary"
        elements.append(button)
    return [{"type": "actions", "block_id": f"hermes_wisdom_{row}_{offset}",
             "elements": elements[offset:offset + 5]}
            for offset in range(0, len(elements), 5)]


def _section_is_lossless(value: object) -> bool:
    escaped = (
        str(value or "").replace("&", "&amp;")
        .replace("<", "&lt;").replace(">", "&gt;").strip()
    )
    return slack_section_text(value) == escaped


def blocks(view: NativeView, record_id: str) -> list[dict[str, Any]]:
    """Transplanted presentation-neutral WisdomView Block Kit shape."""
    item_bodies = []
    for item in view.items:
        body = item.preamble + "\n\n" if item.preamble else ""
        body += f"*{item.title}*"
        if item.detail:
            body += "\n" + item.detail
        item_bodies.append(body)
    approval_is_lossless = (
        len(view.items) <= _MAX_ITEMS
        and (not view.summary or _section_is_lossless(view.summary))
        and all(_section_is_lossless(body) for body in item_bodies)
        and (not view.notice or _section_is_lossless(f"_{view.notice}_"))
    )
    rendered: list[dict[str, Any]] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": _trim(view.title, 150), "emoji": True},
    }]
    rendered.extend(
        _action_blocks(
            view.navigation_actions, record_id, row="navigation",
            allow_primary=approval_is_lossless,
        )
    )
    if view.summary:
        rendered.append({"type": "section", "text": {
            "type": "mrkdwn", "text": slack_section_text(view.summary),
        }})
    for item_index, (item, body) in enumerate(zip(view.items[:_MAX_ITEMS], item_bodies)):
        rendered.append({"type": "section", "text": {
            "type": "mrkdwn", "text": slack_section_text(body),
        }})
        rendered.extend(_action_blocks(
            item.actions, record_id, row=f"item_{item_index}",
            allow_primary=approval_is_lossless,
        ))
    if view.notice:
        rendered.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": slack_section_text(f"_{view.notice}_")}]})
    rendered.extend(_action_blocks(
        view.actions, record_id, row="global", allow_primary=approval_is_lossless,
    ))
    return rendered[:50]


def callback_invocation(  # pylint: disable=too-many-locals
    adapter: Any, body: dict[str, Any], record: dict[str, Any]
) -> NativeInvocation:
    """Bind a Slack callback to its authenticated actor and original route."""
    user = body.get("user") or {}
    channel = body.get("channel") or {}
    team = body.get("team") or {}
    message = body.get("message") or {}
    container = body.get("container") or {}
    actor_id = str(user.get("id") or "")
    chat_id = str(channel.get("id") or container.get("channel_id") or "")
    scope_id = str(team.get("id") or body.get("team_id") or "")
    thread_id = str(message.get("thread_ts") or container.get("thread_ts") or "")
    message_id = str(message.get("ts") or container.get("message_ts") or "")
    authorize = getattr(adapter, "_is_interactive_user_authorized", None)
    try:
        allowed = bool(authorize(
            actor_id, channel_id=chat_id, user_name=str(user.get("name") or ""),
            team_id=scope_id,
        )) if callable(authorize) else False
    except Exception as exc:
        raise PermissionError("Slack callback authorization is unavailable.") from exc
    if not allowed:
        raise PermissionError("You are not authorized to use this Collective Wisdom control.")
    return NativeInvocation(
        platform="slack",
        profile=adapter_profile(adapter),
        chat_type="dm", chat_id=chat_id, actor_id=actor_id,
        actor_name=str(user.get("name") or ""), thread_id=thread_id, scope_id=scope_id,
        message_id=message_id, session_key=str(record.get("owner_session") or ""), adapter=adapter,
    )
