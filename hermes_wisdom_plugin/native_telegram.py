"""Reference-shaped python-telegram-bot rendering and callback identity."""

from __future__ import annotations

import html
import re
from dataclasses import replace
from importlib import import_module
from typing import Any

from .native_context import NativeInvocation, adapter_profile
from .native_review import (
    NativeItem, NativeView, action_label, callback_value, telegram_view_text,
)


def text(view: NativeView) -> str:
    """Render the bounded plain-text fallback for Telegram."""
    return telegram_view_text(view)


RICH_TEXT_LIMIT = 32_768
PLAIN_HTML_LIMIT = 4_000


class RichProjectionUnsupported(ValueError):
    """The complete rich controls cannot fit before any provider request."""


def _escaped(value: object, limit: int) -> str:
    """Escape text and bound the encoded result without cutting an entity."""

    source = str(value or "")
    rendered = html.escape(source).replace("\n", "<br/>")
    if len(rendered) <= limit:
        return rendered
    low, high = 0, len(source)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = html.escape(source[:middle]).replace("\n", "<br/>")
        if len(candidate) + 1 <= limit:
            low = middle
        else:
            high = middle - 1
    return html.escape(source[:low]).replace("\n", "<br/>").rstrip() + "…"


def _compact_receipt(view: NativeView) -> bool:
    """A dismissed one-line receipt renders as plain text on every Telegram path."""
    return view.dismissed and not any((
        view.summary, view.items, view.actions, view.navigation_actions, view.notice,
        view.fields,
    ))


def _plain_detail(item: NativeItem) -> str:
    if item.literal:
        return html.escape(item.detail)
    return "".join(
        (f"<b>{html.escape(label)}</b>" if label else "") + html.escape(rest)
        for label, rest in item.detail_parts()
    )


def _structured_html(view: NativeView) -> str:
    """Bold title and labels, one fact per line; same lines as ``view.to_text()``."""
    if _compact_receipt(view):
        return html.escape(view.title)
    lines = [f"<b>{html.escape(view.title)}</b>"]
    if view.summary:
        lines.extend(("", html.escape(view.summary)))
    if view.fields:
        lines.append("")
        lines.extend(f"<b>{html.escape(fact.label)}:</b> {html.escape(fact.value)}"
                     for fact in view.fields)
    if view.table is not None:
        for row in view.table.rows:
            lines.extend(("", f"<b>{html.escape(row.name)}</b> · {html.escape(row.compact)}"))
            if row.detail:
                lines.append(html.escape(row.detail))
        if view.notice:
            lines.extend(("", f"<i>{html.escape(view.notice)}</i>"))
        lines.extend(f"{html.escape(action.label)}: {html.escape(action.url)}"
                     for action in [*view.table.actions, *view.actions] if action.url)
        return "\n".join(lines)
    for item in view.items:
        if item.preamble:
            lines.extend(("", f"<i>{html.escape(item.preamble)}</i>"))
        lines.extend(("", html.escape(item.title) if item.literal
                      else f"<b>{html.escape(item.title)}</b>"))
        if item.detail:
            lines.append(_plain_detail(item))
        lines.extend(f"{html.escape(action.label)}: {html.escape(action.url)}"
                     for action in item.actions if action.url)
    if view.notice:
        lines.extend(("", f"<i>{html.escape(view.notice)}</i>"))
    lines.extend(f"{html.escape(action.label)}: {html.escape(action.url)}"
                 for action in view.actions if action.url)
    return "\n".join(lines)


def _markup_text(rendered: str) -> str:
    """Remove only the owned formatting tags; dynamic text is always escaped."""
    return html.unescape(re.sub(r"</?[bi]>", "", rendered))


def plain_html(view: NativeView) -> str:
    """Render bounded HTML-parse-mode text with bold labels and literal newlines."""
    rendered = _structured_html(view)
    if len(rendered) <= PLAIN_HTML_LIMIT:
        return rendered
    return _escaped(text(view), PLAIN_HTML_LIMIT).replace("<br/>", "\n")


def plain_html_is_lossless(view: NativeView) -> bool:
    """Require complete approval facts even after a rich-format fallback."""
    return _markup_text(plain_html(view)) == view.to_text()


def _plain_fallback_view(view: NativeView) -> NativeView:
    """Keep non-consent navigation but remove approvals when fallback loses facts."""
    if plain_html_is_lossless(view):
        return view
    return replace(
        view,
        navigation_actions=[action for action in view.navigation_actions if not action.primary],
        items=[
            replace(item, actions=[action for action in item.actions if not action.primary])
            for item in view.items
        ],
        actions=[action for action in view.actions if not action.primary],
        table=replace(view.table, actions=[
            action for action in view.table.actions if not action.primary
        ]) if view.table is not None else None,
        notice=(
            view.notice + " " if view.notice else ""
        ) + "Approval is unavailable because this fallback cannot show every review fact.",
    )


def _rich_button(action: Any, record_id: str) -> str:
    label = _escaped(action_label(action.label), 300)
    if action.url:
        return (
            '<tg-button type="url" url="'
            + html.escape(str(action.url), quote=True)
            + f'">{label}</tg-button>'
        )
    if not action.action:
        return ""
    style = (
        ' style="danger"' if action.destructive
        else ' style="primary"' if action.primary else ""
    )
    return (
        f'<tg-button type="callback_data"{style} data="'
        + html.escape(callback_value(record_id, action.action), quote=True)
        + f'">{label}</tg-button>'
    )


def _rich_row(actions: list[Any], record_id: str) -> str:
    controls = [_rich_button(action, record_id) for action in actions]
    controls = [control for control in controls if control]
    return (
        '<tg-button-row align="left">' + " ".join(controls) + "</tg-button-row>"
        if controls else ""
    )


def _rows(actions: list[Any]) -> list[list[Any]]:
    """Group global controls by their declared row, preserving declaration order."""
    grouped: dict[int, list[Any]] = {}
    for action in actions:
        grouped.setdefault(action.row, []).append(action)
    return [grouped[row] for row in sorted(grouped)]


def _rich_rows(actions: list[Any], record_id: str) -> str:
    return "".join(_rich_row(row, record_id) for row in _rows(actions))


def _rich_fields(view: NativeView) -> str:
    if not view.fields:
        return ""
    return "<p>" + "<br/>".join(
        f"<b>{_escaped(fact.label, 200)}:</b> {_escaped(fact.value, 1_000)}"
        for fact in view.fields
    ) + "</p>"


def _rich_detail(item: NativeItem) -> str:
    """Style owned prefixes only, after escaping every source fragment."""
    rendered = "".join(
        (f"<b>{html.escape(label)}</b>" if label else "")
        + html.escape(rest).replace("\n", "<br/>")
        for label, rest in item.detail_parts()
    )
    return rendered


def _rich_table(view: NativeView) -> str:
    """A Bot API rich ``<table>``: bold header cells, every value escaped."""
    table = view.table
    if table is None:
        return ""
    header = "<tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in table.columns) + "</tr>"
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row.cells) + "</tr>"
        for row in table.rows
    )
    return f"<table bordered>{header}{body}</table>"


def _rich_table_html(view: NativeView, record_id: str) -> str:
    parts = [f"<h5>{_escaped(view.title, 1_000)}</h5>"]
    if view.summary:
        parts.append(f"<p>{_escaped(view.summary, 2_000)}</p>")
    parts.append(_rich_table(view))
    if view.notice:
        parts.append(f"<p><i>{_escaped(view.notice, 2_000)}</i></p>")
    table_actions = view.table.actions if view.table is not None else []
    parts.append(_rich_rows([*table_actions, *view.actions], record_id))
    return "".join(parts)


def rich_html(view: NativeView, record_id: str) -> str:
    """Render a bounded Bot API rich-message card with embedded controls."""

    if view.table is not None:
        rendered = _rich_table_html(view, record_id)
        if len(rendered) <= RICH_TEXT_LIMIT:
            return rendered
        view = replace(view, table=None)
    heading_tag = "p" if _compact_receipt(view) else "h5"
    heading = f"<{heading_tag}>{_escaped(view.title, 1_000)}</{heading_tag}>"
    parts = [heading]
    navigation = _rich_row(view.navigation_actions, record_id)
    if navigation:
        parts.append(navigation)
    if view.summary:
        parts.append(f"<p>{_escaped(view.summary, 2_000)}</p>")
    fields = _rich_fields(view)
    if fields:
        parts.append(fields)
    for item in view.items[:5]:
        body = _rich_detail(item)
        if len(body) > 5_000:
            body = _escaped(item.detail, 5_000)
        preamble = (
            f"<p><i>{_escaped(item.preamble, 1_000)}</i></p>" if item.preamble else ""
        )
        parts.append(
            preamble
            + f"<p><b>{_escaped(item.title, 1_000)}</b>"
            + (f"<br/>{body}" if body else "")
            + "</p>"
            + _rich_row(item.actions, record_id)
        )
    if view.notice:
        parts.append(f"<p><i>{_escaped(view.notice, 2_000)}</i></p>")
    actions = _rich_rows(view.actions, record_id)
    if actions:
        parts.append(actions)
    rendered = "".join(parts)
    if len(rendered) > RICH_TEXT_LIMIT:
        controls = [navigation]
        controls.extend(_rich_row(item.actions, record_id) for item in view.items[:5])
        controls.append(actions)
        controls_html = "".join(control for control in controls if control)
        available = RICH_TEXT_LIMIT - len(heading) - len(controls_html) - len("<p></p>")
        if available < 1:
            raise RichProjectionUnsupported("Telegram rich controls exceed the API limit")
        rendered = heading + f"<p>{_escaped(view.to_text(), available)}</p>" + controls_html
    return rendered


def rich_html_is_lossless(view: NativeView, record_id: str) -> bool:
    """Prove the actual rich approval projection contains every fact and control."""

    try:
        rendered = rich_html(view, record_id)
    except (ValueError, TypeError):
        return False
    if view.table is not None:
        controls = [*view.table.actions, *view.actions]
        return (
            _rich_table(view) in rendered
            and all(_rich_button(action, record_id) in rendered for action in controls)
        )
    values = [view.title, view.summary, view.notice]
    values.extend(value for item in view.items for value in (item.preamble, item.title))
    controls = [*view.navigation_actions, *view.actions]
    controls.extend(action for item in view.items for action in item.actions)
    return (
        len(view.items) <= 5
        and _rich_fields(view) in rendered
        and all(
            _escaped(fact.label, 200) == html.escape(fact.label).replace("\n", "<br/>")
            and _escaped(fact.value, 1_000) == html.escape(fact.value).replace("\n", "<br/>")
            for fact in view.fields
        )
        and all(
            html.escape(str(value)).replace("\n", "<br/>") in rendered
            for value in values if value
        )
        and all(
            len(_rich_detail(item)) <= 5_000
            and _rich_detail(item) in rendered
            for item in view.items if item.detail
        )
        and all(_rich_button(action, record_id) in rendered for action in controls)
    )


def _rich_projection(view: NativeView, record_id: str) -> str:
    """Reject lossy approval cards before either rich transport makes a request."""
    actions = [*view.navigation_actions, *view.actions]
    actions.extend(action for item in view.items for action in item.actions)
    if view.table is not None:
        actions.extend(view.table.actions)
    if any(action.primary for action in actions) and not rich_html_is_lossless(view, record_id):
        raise RichProjectionUnsupported("Telegram rich approval facts exceed the API limit")
    return rich_html(view, record_id)


def _button(button_type: Any, action: Any, record_id: str) -> Any:
    values = (
        {"url": action.url} if action.url
        else {"callback_data": callback_value(record_id, action.action or "")}
    )
    style = "danger" if action.destructive else "primary" if action.primary else None
    label = action_label(action.label)
    if style:
        try:
            return button_type(label, style=style, **values)
        except TypeError:
            pass
    return button_type(label, **values)


def keyboard(view: NativeView, record_id: str) -> Any:
    """Navigation, item, then global action rows (globals grouped by declared row)."""
    telegram = import_module("telegram")
    button_type = telegram.InlineKeyboardButton
    markup_type = telegram.InlineKeyboardMarkup

    rows = []
    groups = [
        view.navigation_actions,
        *(_rows(view.table.actions) if view.table is not None
          else (item.actions for item in view.items)),
        *_rows(view.actions),
    ]
    for actions in groups:
        controls = [
            _button(button_type, action, record_id)
            for action in actions if action.action or action.url
        ]
        if controls:
            rows.append(controls)
    return markup_type(rows) if rows else None


def definite_format_rejection(exc: Exception) -> bool:
    """Return true only when a second, plain request cannot duplicate delivery."""

    try:
        from telegram.error import BadRequest  # pylint: disable=import-outside-toplevel
    except (ImportError, AttributeError):
        BadRequest = ()  # type: ignore[assignment]
    message = str(exc).lower()
    return bool(
        exc.__class__.__name__.lower() == "endpointnotfound"
        or (
            BadRequest and isinstance(exc, BadRequest)
            and any(phrase in message for phrase in (
                "can't parse rich message", "cannot parse rich message",
                "can't parse entities", "method not found", "no such method",
            ))
        )
    )


def _chat_id(value: str) -> int | str:
    return int(value) if str(value).lstrip("-").isdigit() else value


async def send(bot: Any, view: NativeView, record_id: str, *, chat_id: str,
               thread_id: str = "") -> Any:
    """Send rich once; use plain fallback only after a definite rejection."""

    raw = getattr(bot, "do_api_request", None)
    rendered = ""
    if callable(raw):
        try:
            rendered = _rich_projection(view, record_id)
        except RichProjectionUnsupported:
            rendered = ""
        if not rendered:
            raw = None
    if callable(raw):
        payload: dict[str, Any] = {
            "chat_id": _chat_id(chat_id),
            "rich_message": {"html": rendered},
            "link_preview_options": {"is_disabled": True},
        }
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        try:
            return await raw("sendRichMessage", api_kwargs=payload)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not definite_format_rejection(exc):
                raise
    fallback = _plain_fallback_view(view)
    return await bot.send_message(
        chat_id=_chat_id(chat_id),
        text=plain_html(fallback), parse_mode="HTML",
        reply_markup=keyboard(fallback, record_id),
        **({"message_thread_id": int(thread_id)} if thread_id else {}),
    )


async def edit(
    query: Any, view: NativeView, record_id: str, *, require_change: bool = False,
) -> None:
    """Rich-edit the original message, with only definite-rejection fallback."""

    message = getattr(query, "message", None)
    bot = getattr(message, "get_bot", lambda: None)() if message is not None else None
    raw = getattr(bot, "do_api_request", None)
    rendered = ""
    if message is not None and callable(raw):
        try:
            rendered = _rich_projection(view, record_id)
        except RichProjectionUnsupported:
            rendered = ""
        if not rendered:
            raw = None
    if message is not None and callable(raw):
        payload = {
            "chat_id": _chat_id(str(getattr(message, "chat_id", ""))),
            "message_id": int(getattr(message, "message_id")),
            "rich_message": {"html": rendered},
            "link_preview_options": {"is_disabled": True},
        }
        try:
            await raw("editMessageText", api_kwargs=payload)
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if not require_change and "message is not modified" in str(exc).lower():
                return
            if not definite_format_rejection(exc):
                raise
    try:
        fallback = _plain_fallback_view(view)
        await query.edit_message_text(
            text=plain_html(fallback), parse_mode="HTML",
            reply_markup=keyboard(fallback, record_id),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if require_change or "message is not modified" not in str(exc).lower():
            raise


def callback_invocation(
    adapter: Any, query: Any, record: dict[str, Any]
) -> NativeInvocation:
    """Bind a Telegram callback to its authenticated actor and original route."""
    user = getattr(query, "from_user", None)
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    actor_id = str(getattr(user, "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    chat_type = str(getattr(chat, "type", "") or "")
    thread_id = str(getattr(message, "message_thread_id", "") or "")
    authorize = getattr(adapter, "_is_callback_user_authorized", None)
    try:
        allowed = bool(authorize(
            actor_id, chat_id=chat_id, chat_type=chat_type,
            thread_id=thread_id or None,
            user_name=str(
                getattr(user, "username", "")
                or getattr(user, "first_name", "") or ""
            ),
        )) if callable(authorize) else False
    except Exception as exc:
        raise PermissionError("Telegram callback authorization is unavailable.") from exc
    if not allowed:
        raise PermissionError("You are not authorized to use this Collective Wisdom control.")
    bot = getattr(adapter, "_bot", None)
    callback_bot = getattr(message, "get_bot", lambda: None)()
    if callback_bot is not None and bot is not None and callback_bot is not bot:
        raise PermissionError("Collective Wisdom control belongs to another bot.")
    return NativeInvocation(
        platform="telegram",
        profile=adapter_profile(adapter),
        chat_type=chat_type, chat_id=chat_id, actor_id=actor_id,
        actor_name=str(
            getattr(user, "username", "") or getattr(user, "first_name", "") or ""
        ),
        thread_id=thread_id, scope_id="", message_id=str(getattr(message, "message_id", "") or ""),
        session_key=str(record.get("owner_session") or ""), adapter=adapter,
    )
