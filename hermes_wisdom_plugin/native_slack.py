"""Reference Slack Block Kit renderer and authenticated callback extraction."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .native_context import NativeInvocation, adapter_profile
from .native_review import NativeView, action_label, callback_value, slack_section_text

_MAX_ITEMS = 5
_MAX_FIELDS = 10
_MAX_FIELD_TEXT = 2000
# Native table block limits (host plugins/platforms/slack/block_kit.py).
MAX_TABLE_ROWS = 100
MAX_TABLE_COLS = 20
MAX_TABLE_CHARS = 10000


def is_link_button(action: dict[str, Any]) -> bool:
    """True for a Slack link-button click, which carries a URL and no callback value."""
    return bool(action.get("url")) and not action.get("value")


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
            "text": {"type": "plain_text", "text": action_label(action.label), "emoji": True},
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


def _field_text(label: str, value: str) -> str:
    return f"*{slack_section_text(label)}*\n{slack_section_text(value)}"


def _fields_are_lossless(view: NativeView) -> bool:
    return len(view.fields) <= _MAX_FIELDS and all(
        _section_is_lossless(fact.label) and _section_is_lossless(fact.value)
        and len(_field_text(fact.label, fact.value)) <= _MAX_FIELD_TEXT
        for fact in view.fields
    )


def _item_bodies(view: NativeView) -> list[str]:
    bodies = []
    for item in view.items:
        body = item.preamble + "\n\n" if item.preamble else ""
        body += f"*{item.title}*"
        if item.detail:
            body += "\n" + "".join(
                (f"*{label}*" if label else "") + rest
                for label, rest in item.detail_parts()
            )
        bodies.append(body)
    return bodies


def _lead_blocks(view: NativeView) -> list[dict[str, Any]]:
    """Description and label/value facts share one section, as Block Kit intends."""
    if not view.fields:
        return ([{"type": "section", "text": {
            "type": "mrkdwn", "text": slack_section_text(view.summary),
        }}] if view.summary else [])
    section: dict[str, Any] = {"type": "section", "fields": [
        {"type": "mrkdwn", "text": _trim(_field_text(fact.label, fact.value), _MAX_FIELD_TEXT)}
        for fact in view.fields[:_MAX_FIELDS]
    ]}
    if view.summary:
        section["text"] = {"type": "mrkdwn", "text": slack_section_text(view.summary)}
    return [section]


def _table_cell(text: str, *, bold: bool = False) -> dict[str, Any]:
    element: dict[str, Any] = {"type": "text", "text": text}
    if bold:
        element["style"] = {"bold": True}
    return {"type": "rich_text", "elements": [
        {"type": "rich_text_section", "elements": [element]},
    ]}


def table_block(view: NativeView) -> dict[str, Any] | None:
    """A native Block Kit ``table`` for a listing, or None when Slack would reject it."""
    table = view.table
    if table is None or not table.rows:
        return None
    grid = [table.columns, *(row.cells for row in table.rows)]
    if (len(grid) > MAX_TABLE_ROWS or len(table.columns) > MAX_TABLE_COLS
            or any(len(cells) != len(table.columns) for cells in grid)
            or sum(len(cell) for cells in grid for cell in cells) > MAX_TABLE_CHARS
            or any(not cell.strip() or not cell.isprintable() for cells in grid for cell in cells)):
        return None
    return {"type": "table", "rows": [
        [_table_cell(cell, bold=True) for cell in table.columns],
        *([_table_cell(cell) for cell in cells] for cells in grid[1:]),
    ]}


def blocks(view: NativeView, record_id: str) -> list[dict[str, Any]]:
    """Header, facts, items (or a listing table), context line, divider, actions."""
    table = table_block(view)
    if table is not None:
        return _table_blocks(view, table, record_id)
    item_bodies = _item_bodies(view)
    approval_is_lossless = (
        len(view.items) <= _MAX_ITEMS
        and (not view.summary or _section_is_lossless(view.summary))
        and _fields_are_lossless(view)
        and all(_section_is_lossless(body) for body in item_bodies)
        and (not view.notice or _section_is_lossless(view.notice))
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
    rendered.extend(_lead_blocks(view))
    for item_index, (item, body) in enumerate(zip(view.items[:_MAX_ITEMS], item_bodies)):
        if item.literal:
            body = item.preamble + "\n\n" if item.preamble else ""
            body += item.title
            if item.detail:
                body += "\n" + item.detail
        if item.literal and len(body) > 2900:
            raise ValueError("Exact native inspection section exceeds the safe display limit")
        rendered.append({"type": "section", "text": {
            "type": "plain_text" if item.literal else "mrkdwn",
            "text": body if item.literal else slack_section_text(body),
        }})
        rendered.extend(_action_blocks(
            item.actions, record_id, row=f"item_{item_index}",
            allow_primary=approval_is_lossless,
        ))
    if view.notice:
        rendered.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": slack_section_text(view.notice)}]})
    actions = _action_blocks(
        view.actions, record_id, row="global", allow_primary=approval_is_lossless,
    )
    if actions and len(rendered) > 1:
        rendered.append({"type": "divider"})
    rendered.extend(actions)
    return rendered[:50]


def _table_blocks(
    view: NativeView, table: dict[str, Any], record_id: str,
) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": _trim(view.title, 150), "emoji": True},
    }]
    rendered.extend(_lead_blocks(view))
    rendered.append(table)
    if view.notice:
        rendered.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": slack_section_text(view.notice)}]})
    controls = [*(view.table.actions if view.table else []), *view.actions]
    for row in sorted({action.row for action in controls}):
        rendered.extend(_action_blocks(
            [action for action in controls if action.row == row], record_id,
            row=f"table_{row}",
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
