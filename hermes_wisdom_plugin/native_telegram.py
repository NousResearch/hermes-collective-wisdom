"""Reference-shaped python-telegram-bot rendering and callback identity."""

from __future__ import annotations

import html
from importlib import import_module
from typing import Any

from .native_context import NativeInvocation, adapter_profile
from .native_review import NativeView, callback_value, telegram_view_text


def text(view: NativeView) -> str:
    """Render the bounded plain-text fallback for Telegram."""
    return telegram_view_text(view)


RICH_TEXT_LIMIT = 32_768


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


def plain_html(view: NativeView) -> str:
    """Render bounded legacy HTML with literal newlines, not rich-only tags."""
    return _escaped(text(view), 4_000).replace("<br/>", "\n")


def plain_html_is_lossless(view: NativeView) -> bool:
    """Require complete approval facts even after a rich-format fallback."""
    return html.unescape(plain_html(view)) == view.to_text()


def _rich_button(action: Any, record_id: str) -> str:
    label = _escaped(str(action.label)[:48], 300)
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


def rich_html(view: NativeView, record_id: str) -> str:
    """Render a bounded Bot API rich-message card with embedded controls."""

    parts = [f"<h3>{_escaped(view.title, 1_000)}</h3>"]
    navigation = _rich_row(view.navigation_actions, record_id)
    if navigation:
        parts.append(navigation)
    if view.summary:
        parts.append(f"<p>{_escaped(view.summary, 2_000)}</p>")
    for item in view.items[:5]:
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
    actions = _rich_row(view.actions, record_id)
    if actions:
        parts.append(actions)
    rendered = "".join(parts)
    if len(rendered) > RICH_TEXT_LIMIT:
        controls = [navigation]
        controls.extend(_rich_row(item.actions, record_id) for item in view.items[:5])
        controls.append(actions)
        controls_html = "".join(control for control in controls if control)
        heading = f"<h3>{_escaped(view.title, 1_000)}</h3>"
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
    values = [view.title, view.summary, view.notice]
    values.extend(
        value
        for item in view.items
        for value in (item.preamble, item.title, item.detail)
    )
    controls = [*view.navigation_actions, *view.actions]
    controls.extend(action for item in view.items for action in item.actions)
    return (
        len(view.items) <= 5
        and all(
            html.escape(str(value)).replace("\n", "<br/>") in rendered
            for value in values if value
        )
        and all(_rich_button(action, record_id) in rendered for action in controls)
    )


def _button(button_type: Any, action: Any, record_id: str) -> Any:
    values = (
        {"url": action.url} if action.url
        else {"callback_data": callback_value(record_id, action.action or "")}
    )
    style = "danger" if action.destructive else "primary" if action.primary else None
    if style:
        try:
            return button_type(action.label, style=style, **values)
        except TypeError:
            pass
    return button_type(action.label, **values)


def keyboard(view: NativeView, record_id: str) -> Any:
    """Preserve reference navigation, item, and global action rows."""
    telegram = import_module("telegram")
    button_type = telegram.InlineKeyboardButton
    markup_type = telegram.InlineKeyboardMarkup

    rows = []
    groups = [
        view.navigation_actions,
        *(item.actions for item in view.items),
        view.actions,
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
            rendered = rich_html(view, record_id)
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
    return await bot.send_message(
        chat_id=_chat_id(chat_id),
        text=plain_html(view), parse_mode="HTML",
        reply_markup=keyboard(view, record_id),
        **({"message_thread_id": int(thread_id)} if thread_id else {}),
    )


async def edit(query: Any, view: NativeView, record_id: str) -> None:
    """Rich-edit the original message, with only definite-rejection fallback."""

    message = getattr(query, "message", None)
    bot = getattr(message, "get_bot", lambda: None)() if message is not None else None
    raw = getattr(bot, "do_api_request", None)
    rendered = ""
    if message is not None and callable(raw):
        try:
            rendered = rich_html(view, record_id)
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
            if "message is not modified" in str(exc).lower():
                return
            if not definite_format_rejection(exc):
                raise
    try:
        await query.edit_message_text(
            text=plain_html(view), parse_mode="HTML",
            reply_markup=keyboard(view, record_id),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if "message is not modified" not in str(exc).lower():
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
