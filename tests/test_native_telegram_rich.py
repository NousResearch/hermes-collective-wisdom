"""Telegram rich-card transport behavior without live Telegram calls."""

# Recording doubles intentionally mirror optional SDK protocols and exercise
# private transport seams directly.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# pylint: disable=import-outside-toplevel,protected-access,line-too-long
# pylint: disable=use-implicit-booleaness-not-comparison

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeView, project_view,
)
from hermes_wisdom_plugin.native_telegram import rich_html


class BadRequest(Exception):
    """Definite Telegram payload rejection used by the recording transport."""


class Button:
    def __init__(self, text, **values):
        self.text = text
        self.values = values


class Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


@pytest.fixture(autouse=True)
def telegram_sdk(monkeypatch):
    telegram = ModuleType("telegram")
    telegram.InlineKeyboardButton = Button
    telegram.InlineKeyboardMarkup = Markup
    error = ModuleType("telegram.error")
    error.BadRequest = BadRequest
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", error)


def hostile_view() -> NativeView:
    return NativeView(
        "Wisdom <owner> & team",
        "Summary </h3><script>alert(1)</script>",
        [NativeItem(
            "Skill <unsafe>",
            "first & second\nthird > fourth",
            [
                NativeAction("Open <portal>", url="https://example.invalid/?a=1&b=2"),
                NativeAction("Remove & stop", "remove", destructive=True),
            ],
        )],
        [NativeAction("Install & continue", "confirm", primary=True)],
        notice="Nothing <changed>.",
    )


def test_rich_html_escapes_hostile_text_and_embeds_styled_controls():
    rendered = rich_html(hostile_view(), "record-1")

    assert rendered.startswith("<h3>Wisdom &lt;owner&gt; &amp; team</h3>")
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "first &amp; second<br/>third &gt; fourth" in rendered
    assert 'type="url" url="https://example.invalid/?a=1&amp;b=2"' in rendered
    assert 'style="danger" data="wi:agent:remove:record-1"' in rendered
    assert 'style="primary" data="wi:agent:confirm:record-1"' in rendered


def test_fallback_keyboard_uses_native_primary_and_danger_styles():
    from hermes_wisdom_plugin.native_telegram import keyboard

    markup = keyboard(hostile_view(), "record-1")
    controls = [button for row in markup.inline_keyboard for button in row]

    assert next(button for button in controls if button.text == "Remove & stop").values["style"] == "danger"
    assert next(button for button in controls if button.text == "Install & continue").values["style"] == "primary"


def test_pending_moderation_decline_is_presented_as_destructive():
    view = project_view({
        "operation": "publish", "state": "completed", "checks_expanded": False,
        "assessment_expanded": False, "inspection": None, "platform": "telegram",
        "plan": {"draft_id": "draft", "editorial_name": "Skill"},
        "result": {
            "publication_state": "pending_moderation",
            "portal_url": "https://example.invalid/review/draft",
        },
    })

    decline = next(action for action in view.actions if action.label == "Decline")
    assert decline.destructive is True
    assert 'style="danger"' in rich_html(view, "record-1")


def test_rich_renderer_bounds_long_output_without_emitting_partial_markup():
    view = NativeView(
        "Long", "summary", [NativeItem("Huge", "<&>" * 20_000)],
        actions=[NativeAction("Confirm exact setup", "confirm", primary=True)],
        navigation_actions=[NativeAction("Back", "back")],
    )

    rendered = rich_html(view, "record-1")

    assert len(rendered) <= 32_768
    assert 'data="wi:agent:confirm:record-1"' in rendered
    assert 'data="wi:agent:back:record-1"' in rendered
    assert "…" in rendered


@pytest.mark.parametrize("detail", ["x" * 4_500, "&" * 850])
def test_approval_requires_lossless_plain_fallback_too(detail):
    from hermes_wisdom_plugin.native_review import _approval_is_lossless
    from hermes_wisdom_plugin.native_telegram import rich_html_is_lossless

    view = NativeView("Review", "", items=[NativeItem("Exact command", detail)])
    assert rich_html_is_lossless(view, "record-1")
    assert not _approval_is_lossless({"platform": "telegram", "id": "record-1"}, view)


class RecordingBot:
    def __init__(self, rich_error=None, *, rich_response=None):
        self.rich_error = rich_error
        self.rich_response = rich_response or {"message_id": 41}
        self.raw = []
        self.plain = []

    async def do_api_request(self, endpoint, *, api_kwargs):
        self.raw.append((endpoint, api_kwargs))
        if self.rich_error is not None:
            raise self.rich_error
        return self.rich_response

    async def send_message(self, **payload):
        self.plain.append(payload)
        return SimpleNamespace(message_id=42)


def test_native_runtime_sends_rich_card_with_thread_and_no_plain_duplicate(tmp_path):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime

    async def scenario():
        bot = RecordingBot()
        adapter = SimpleNamespace(_bot=bot)
        runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: None, None, lambda *_a, **_k: None)
        invocation = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="7", actor_name="", thread_id="9", scope_id="",
            message_id="", session_key="telegram:123:7", adapter=adapter,
        )
        runtime._native[("telegram", id(adapter))] = object()

        response = await runtime._post_native(invocation, hostile_view(), "record-1")

        assert response == {"message_id": 41}
        assert bot.plain == []
        endpoint, payload = bot.raw[0]
        assert endpoint == "sendRichMessage"
        assert payload["chat_id"] == 123 and payload["message_thread_id"] == 9
        assert payload["rich_message"]["html"].startswith("<h3>")

    asyncio.run(scenario())


def test_uncertain_rich_send_is_not_retried_from_same_durable_record(tmp_path):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime

    bound_owner = {
        "profile_name": "default", "profile_home": "/tmp/profile",
        "account_id": "member", "organization_id": "org",
        "base_url": "https://g.invalid", "installation_id": "install",
    }

    class DeliveryService:
        def native_owner(self):
            return dict(bound_owner)

        def update_plan(self, skill_id, **_choices):
            return {
                "operation": "update", "owner": dict(bound_owner), "allowed": True,
                "skill_id": skill_id, "slug": "managed", "from_version": 1,
                "version": 2, "receipt": "receipt", "files": [],
            }

        def close(self):
            return None

    async def scenario():
        bot = RecordingBot(TimeoutError("delivery acknowledgement timed out"))
        adapter = SimpleNamespace(_bot=bot)
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", DeliveryService, None, lambda *_a, **_k: None,
        )
        invocation = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="member", actor_name="", thread_id="", scope_id="",
            message_id="request-1", session_key="telegram:123:member", adapter=adapter,
        )
        runtime._native[("telegram", id(adapter))] = object()

        warning = await runtime.begin(invocation, "skill-1")
        record_id = runtime.reviews.pending(bound_owner, invocation=invocation)[0]["id"]
        uncertain = runtime.reviews.load(record_id)
        sends = len(bot.raw)
        duplicate_warning = await runtime._deliver_new(invocation, uncertain)

        assert "could not confirm" in warning
        assert duplicate_warning == warning
        assert uncertain["delivery_state"] == "delivery_uncertain"
        assert len(bot.raw) == sends

    asyncio.run(scenario())


def test_claimed_but_unbound_send_attempt_becomes_uncertain_without_resend(tmp_path):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime

    bound_owner = {
        "profile_name": "default", "profile_home": "/tmp/profile",
        "account_id": "member", "organization_id": "org",
        "base_url": "https://g.invalid", "installation_id": "install",
    }

    class DeliveryService:
        def native_owner(self):
            return dict(bound_owner)

        def update_plan(self, skill_id, **_choices):
            return {
                "operation": "update", "owner": dict(bound_owner), "allowed": True,
                "skill_id": skill_id, "slug": "managed", "from_version": 1,
                "version": 2, "receipt": "receipt", "files": [],
            }

        def close(self):
            return None

    async def scenario():
        bot = RecordingBot()
        adapter = SimpleNamespace(_bot=bot)
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", DeliveryService, None, lambda *_a, **_k: None,
        )
        invocation = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="member", actor_name="", thread_id="", scope_id="",
            message_id="request-1", session_key="telegram:123:member", adapter=adapter,
        )
        runtime._native[("telegram", id(adapter))] = object()
        record = runtime.reviews.request(DeliveryService(), invocation, "skill-1")
        runtime.reviews.delivery_projection(record["id"], initial=True)

        warning = await runtime._deliver_new(invocation, record)
        stored = runtime.reviews.load(record["id"])

        assert warning is not None and "could not confirm" in warning
        assert stored["delivery_state"] == "delivery_uncertain"
        assert bot.raw == [] and bot.plain == []

    asyncio.run(scenario())


def test_definite_rich_rejection_falls_back_once_but_uncertain_failure_never_resends(tmp_path):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime

    async def invoke(error):
        bot = RecordingBot(error)
        adapter = SimpleNamespace(_bot=bot)
        runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: None, None, lambda *_a, **_k: None)
        invocation = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="7", actor_name="", thread_id="", scope_id="",
            message_id="", session_key="telegram:123:7", adapter=adapter,
        )
        runtime._native[("telegram", id(adapter))] = object()
        if isinstance(error, BadRequest):
            response = await runtime._post_native(invocation, hostile_view(), "record-1")
            assert response.message_id == 42
            assert len(bot.raw) == 1 and len(bot.plain) == 1
            assert bot.plain[0]["parse_mode"] == "HTML"
            assert "<br/>" not in bot.plain[0]["text"]
            assert "\n" in bot.plain[0]["text"]
        else:
            with pytest.raises(TimeoutError):
                await runtime._post_native(invocation, hostile_view(), "record-1")
            assert len(bot.raw) == 1 and bot.plain == []

    asyncio.run(invoke(BadRequest("can't parse rich message")))
    asyncio.run(invoke(TimeoutError("delivery acknowledgement timed out")))


@pytest.mark.parametrize(
    "error",
    [TypeError("response field missing"), AttributeError("decoder missing attribute")],
)
def test_post_send_sdk_errors_are_uncertain_and_never_trigger_plain_duplicate(error):
    from hermes_wisdom_plugin.native_telegram import send

    async def scenario():
        bot = RecordingBot(error)
        with pytest.raises(type(error)):
            await send(bot, hostile_view(), "record-1", chat_id="123")
        assert len(bot.raw) == 1
        assert bot.plain == []

    asyncio.run(scenario())


def test_negative_or_missing_rich_ack_is_uncertain_and_never_bound(tmp_path):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime

    bound_owner = {
        "profile_name": "default", "profile_home": "/tmp/profile",
        "account_id": "member", "organization_id": "org",
        "base_url": "https://g.invalid", "installation_id": "install",
    }

    class DeliveryService:
        def native_owner(self):
            return dict(bound_owner)

        def update_plan(self, skill_id, **_choices):
            return {
                "operation": "update", "owner": dict(bound_owner), "allowed": True,
                "skill_id": skill_id, "slug": "managed", "from_version": 1,
                "version": 2, "receipt": "receipt", "files": [],
            }

        def close(self):
            return None

    async def scenario(response, root):
        bot = RecordingBot(rich_response=response)
        adapter = SimpleNamespace(_bot=bot)
        runtime = NativeSurfaceRuntime(
            root, "default", DeliveryService, None, lambda *_a, **_k: None,
        )
        invocation = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="member", actor_name="", thread_id="", scope_id="",
            message_id="request-1", session_key="telegram:123:member", adapter=adapter,
        )
        runtime._native[("telegram", id(adapter))] = object()

        warning = await runtime.begin(invocation, "skill-1")
        record = runtime.reviews.pending(bound_owner, invocation=invocation)[0]
        stored = runtime.reviews.load(record["id"])

        assert warning is not None and "could not confirm" in warning
        assert stored["delivery_state"] == "delivery_uncertain"
        assert stored["message_id"] is None
        assert len(bot.raw) == 1 and bot.plain == []

    asyncio.run(scenario({"ok": False, "result": {"message_id": 41}}, tmp_path / "negative"))
    asyncio.run(scenario({"ok": True, "result": {}}, tmp_path / "missing"))


class RecordingQuery:
    def __init__(self, bot):
        self.message = SimpleNamespace(
            chat_id=123, message_id=77, get_bot=lambda: bot,
        )
        self.plain_edits = []

    async def edit_message_text(self, **payload):
        self.plain_edits.append(payload)


def test_rich_edit_updates_same_message_without_plain_duplicate():
    from hermes_wisdom_plugin.native_telegram import edit

    async def scenario():
        bot = RecordingBot()
        query = RecordingQuery(bot)

        await edit(query, hostile_view(), "record-1")

        assert query.plain_edits == []
        assert bot.raw[0][0] == "editMessageText"
        assert bot.raw[0][1]["message_id"] == 77
        assert bot.raw[0][1]["chat_id"] == 123

    asyncio.run(scenario())


def test_rich_edit_definite_rejection_falls_back_but_timeout_preserves_original():
    from hermes_wisdom_plugin.native_telegram import edit

    async def scenario():
        rejected = RecordingQuery(RecordingBot(BadRequest("can't parse rich message")))
        await edit(rejected, hostile_view(), "record-1")
        assert len(rejected.plain_edits) == 1

        uncertain = RecordingQuery(RecordingBot(TimeoutError("timed out")))
        with pytest.raises(TimeoutError):
            await edit(uncertain, hostile_view(), "record-1")
        assert uncertain.plain_edits == []

    asyncio.run(scenario())


def test_rich_edit_message_not_modified_is_success_without_second_edit():
    from hermes_wisdom_plugin.native_telegram import edit

    async def scenario():
        query = RecordingQuery(RecordingBot(BadRequest("Message is not modified")))
        await edit(query, hostile_view(), "record-1")
        assert query.plain_edits == []

    asyncio.run(scenario())


def test_rich_dismissal_edits_original_card_without_embedded_or_fallback_controls():
    from hermes_wisdom_plugin.native_runtime import _edit_telegram_projection

    async def scenario():
        bot = RecordingBot()
        query = RecordingQuery(bot)
        dismissed = NativeView(
            "Wisdom card", "Original summary", [NativeItem("Skill", "Original detail")],
            notice="Deferred without approval.", dismissed=True,
        )

        await _edit_telegram_projection(query, dismissed, "record-1")

        assert query.plain_edits == []
        endpoint, payload = bot.raw[0]
        assert endpoint == "editMessageText"
        assert "Original summary" in payload["rich_message"]["html"]
        assert "<tg-button" not in payload["rich_message"]["html"]

    asyncio.run(scenario())


def test_dismissal_fallback_and_not_modified_are_idempotent():
    from hermes_wisdom_plugin.native_runtime import _edit_telegram_projection

    class FallbackQuery(RecordingQuery):
        def __init__(self, rich_error, plain_error=None):
            super().__init__(RecordingBot(rich_error))
            self.plain_error = plain_error

        async def edit_message_text(self, **payload):
            self.plain_edits.append(payload)
            if self.plain_error is not None:
                raise self.plain_error

    async def scenario():
        dismissed = NativeView("Wisdom card", "Original summary", dismissed=True)
        fallback = FallbackQuery(BadRequest("can't parse rich message"))
        await _edit_telegram_projection(fallback, dismissed, "record-1")
        assert fallback.plain_edits[0]["reply_markup"] is None
        assert fallback.plain_edits[0]["parse_mode"] == "HTML"
        assert "<br/>" not in fallback.plain_edits[0]["text"]
        assert "\n" in fallback.plain_edits[0]["text"]

        rich_unchanged = FallbackQuery(BadRequest("Message is not modified"))
        await _edit_telegram_projection(rich_unchanged, dismissed, "record-1")
        assert rich_unchanged.plain_edits == []

        plain_unchanged = FallbackQuery(
            BadRequest("can't parse rich message"), BadRequest("Message is not modified"),
        )
        await _edit_telegram_projection(plain_unchanged, dismissed, "record-1")
        assert len(plain_unchanged.plain_edits) == 1

    asyncio.run(scenario())
