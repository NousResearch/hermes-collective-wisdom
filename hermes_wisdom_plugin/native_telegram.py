"""Reference-shaped python-telegram-bot rendering and callback identity."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .native_context import NativeInvocation, adapter_profile
from .native_review import NativeView, callback_value, telegram_view_text


def text(view: NativeView) -> str:
    """Render the bounded plain-text fallback for Telegram."""
    return telegram_view_text(view)


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
            button_type(
                action.label,
                **(
                    {"url": action.url}
                    if action.url else
                    {"callback_data": callback_value(record_id, action.action or "")}
                ),
            )
            for action in actions if action.action or action.url
        ]
        if controls:
            rows.append(controls)
    return markup_type(rows) if rows else None


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
