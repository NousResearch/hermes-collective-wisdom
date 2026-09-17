"""Bounded production diagnostics for Telegram callback stages."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
# pylint: disable=too-many-locals,too-many-arguments,no-self-argument,unused-argument
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.store import WisdomStore

PRIVATE_SENTINELS = (
    "payload-token-secret", "private-actor-7001", "private-chat-8002",
    "private-record-9003", "/Users/private/profile-home",
    "https://private.example/callback?token=secret", "private package contents",
)


class CatalogService:
    def __init__(self, store, *, show_error: Exception | None = None):
        self.store = store
        self.show_error = show_error
        self.show_calls = 0
        self.owner = {
            "profile_name": "default",
            "profile_home": PRIVATE_SENTINELS[4],
            "account_id": "private-account-1004",
            "organization_id": "private-org-1005",
            "base_url": PRIVATE_SENTINELS[5],
            "installation_id": "private-installation-1006",
        }

    def native_owner(self):
        return dict(self.owner)

    def close(self):
        pass

    def search_skills(self, _query=None):
        return [{
            "id": "skill-1", "slug": "demo", "latest_version": 1,
            "author_description": PRIVATE_SENTINELS[6],
            "security_check": {"status": "pass"},
            "professionalism_check": {"status": "unavailable"},
            "_install_action": "none",
        }]

    def resolve_skill(self, target):
        return self.show(target)

    def show(self, skill_id):
        self.show_calls += 1
        if self.show_error is not None:
            raise self.show_error
        return {
            "skill": {"id": skill_id, "slug": "demo"},
            "versions": [{"version": 1}],
            "latest_version_detail": {"version": {
                "version": 1, "author_description": "detail",
                "security_check": {"status": "pass"},
                "professionalism_check": {"status": "unavailable"},
            }},
            "portal_url": "https://portal.invalid",
        }

    def versions(self, _skill_id):
        return [{"version": 1}]


class RecordingRequest:
    """PTB request transport with deterministic endpoint faults."""

    def __init__(self, base_request, *, fail_answer_calls=(), fail_edit=False):
        class Request(base_request):
            @property
            def read_timeout(inner_self):
                return 5

            async def initialize(inner_self):
                return None

            async def shutdown(inner_self):
                return None

            async def do_request(inner_self, url, method, request_data=None, **_kwargs):
                endpoint = url.rsplit("/", 1)[-1]
                params = dict(request_data.parameters if request_data else {})
                self.calls.append((endpoint, params))
                if endpoint == "answerCallbackQuery":
                    self.answer_count += 1
                    if self.answer_count in self.fail_answer_calls:
                        raise RuntimeError(
                            "transport exploded " + " ".join(PRIVATE_SENTINELS)
                        )
                if endpoint == "editMessageText" and self.fail_edit:
                    raise TimeoutError("edit timed out " + " ".join(PRIVATE_SENTINELS))
                if endpoint == "getMe":
                    result = {"id": 999, "is_bot": True, "first_name": "Recorder"}
                elif endpoint == "editMessageText":
                    result = {
                        "message_id": int(params["message_id"]),
                        "chat": {"id": 7, "type": "private"},
                        "text": params.get("text", ""),
                    }
                elif endpoint == "answerCallbackQuery":
                    result = True
                else:
                    result = {
                        "message_id": 41, "chat": {"id": 7, "type": "private"},
                        "text": params.get("text", ""),
                    }
                return 200, json.dumps({"ok": True, "result": result}).encode()

        self.request = Request()
        self.calls = []
        self.answer_count = 0
        self.fail_answer_calls = set(fail_answer_calls)
        self.fail_edit = fail_edit


async def _run_callback(
    tmp_path, caplog, *, authorized=True, show_error=None,
    fail_answer_calls=(), fail_edit=False, log_level=logging.DEBUG,
):
    telegram = pytest.importorskip("telegram")
    telegram_ext = pytest.importorskip("telegram.ext")
    telegram_request = pytest.importorskip("telegram.request")

    store = WisdomStore(tmp_path)
    service = CatalogService(store, show_error=show_error)
    transport = RecordingRequest(
        telegram_request.BaseRequest,
        fail_answer_calls=fail_answer_calls,
        fail_edit=fail_edit,
    )
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, lambda *_args, **_kwargs: None,
    )
    app = telegram_ext.Application.builder().token("123:ABC").request(transport.request).build()
    adapter = SimpleNamespace(
        _owner_profile="default", _bot=None,
        _is_callback_user_authorized=lambda *_args, **_kwargs: authorized,
    )
    catch_all = Mock()

    async def host_callback(_update, _context):
        catch_all()

    caplog.set_level(log_level, logger="hermes_wisdom_plugin.native_runtime")
    runtime.wire_telegram(app, adapter)
    app.add_handler(telegram_ext.CallbackQueryHandler(host_callback), group=0)
    await app.initialize()
    adapter._bot = app.bot

    invocation = NativeInvocation(
        "telegram", "default", "private", "7", "7001",
        "private-user", "", "", "41", "private-session", adapter,
    )
    record = runtime.reviews.request_catalog(service, invocation, "browse")
    runtime.reviews.bind_delivery(record["id"], invocation, "41")
    view = runtime.reviews.view(record)
    callback = next(action.action for action in view.items[0].actions if action.label == "View")
    raw = {
        "update_id": 1,
        "callback_query": {
            "id": "payload-token-secret", "chat_instance": "private-chat-instance",
            "from": {
                "id": 7001, "is_bot": False, "first_name": "private-actor-7001",
                "username": "payload-token-secret",
            },
            "message": {
                "message_id": 41, "date": 1,
                "chat": {"id": 7, "type": "private"}, "text": PRIVATE_SENTINELS[6],
            },
            "data": f"wi:agent:{callback}:{record['id']}",
        },
    }
    await app.process_update(telegram.Update.de_json(raw, app.bot))
    stored = runtime.reviews.load(record["id"])
    await app.shutdown()
    runtime.close()
    plugin_records = [
        record for record in caplog.records
        if record.name == "hermes_wisdom_plugin.native_runtime"
    ]
    return SimpleNamespace(
        logs=[record.getMessage() for record in plugin_records],
        text="\n".join(record.getMessage() for record in plugin_records),
        calls=transport.calls, answers=transport.answer_count, catch_all=catch_all,
        service=service, stored=stored,
    )


def _assert_private_sentinels_absent(text):
    for sentinel in PRIVATE_SENTINELS:
        assert sentinel not in text
    for sentinel in (
        "private-account-1004", "private-org-1005", "private-installation-1006",
        "private-session", "private-user", "private-chat-instance",
    ):
        assert sentinel not in text


def test_callback_ack_failure_is_logged_and_aborts_before_authorization(tmp_path, caplog):
    result = asyncio.run(_run_callback(tmp_path, caplog, fail_answer_calls=(1,)))

    assert result.answers == 1
    assert result.service.show_calls == 0
    assert result.catch_all.call_count == 0
    assert result.logs == [
        "Collective Wisdom Telegram callback (stage=wiring; outcome=success; "
        "reason=handler_registered; action_class=unknown)",
        "Collective Wisdom Telegram callback (stage=received; outcome=success; "
        "reason=callback_received; action_class=unknown)",
        "Collective Wisdom Telegram callback (stage=acknowledged; outcome=failure; "
        "reason=transport_failure; action_class=unknown)",
    ]
    _assert_private_sentinels_absent(result.text)


def test_callback_authorization_failure_is_logged_and_alerted(tmp_path, caplog):
    result = asyncio.run(_run_callback(tmp_path, caplog, authorized=False))

    assert result.answers == 2
    assert result.service.show_calls == 0
    assert result.catch_all.call_count == 0
    assert any(
        "stage=authorized; outcome=failure; reason=authorization_denied; "
        "action_class=catalog" in message
        for message in result.logs
    )
    assert result.logs[-1].endswith(
        "stage=alert; outcome=success; reason=alert_completed; action_class=catalog)"
    )
    _assert_private_sentinels_absent(result.text)


def test_callback_service_action_failure_is_logged_without_exception_text(tmp_path, caplog):
    failure = RuntimeError("service failed " + " ".join(PRIVATE_SENTINELS))
    result = asyncio.run(_run_callback(tmp_path, caplog, show_error=failure))

    assert result.answers == 2
    assert result.service.show_calls == 1
    assert any(
        "stage=action; outcome=failure; reason=service_failure; action_class=catalog"
        in message for message in result.logs
    )
    assert not any("stage=edit" in message for message in result.logs)
    _assert_private_sentinels_absent(result.text)


def test_callback_edit_failure_is_logged_after_single_action(tmp_path, caplog):
    result = asyncio.run(_run_callback(tmp_path, caplog, fail_edit=True))

    assert result.answers == 1
    assert result.service.show_calls == 1
    assert result.catch_all.call_count == 0
    assert any(
        "stage=action; outcome=success; reason=action_completed; action_class=catalog"
        in message for message in result.logs
    )
    assert any(
        "stage=edit; outcome=failure; reason=edit_failure; action_class=catalog"
        in message for message in result.logs
    )
    _assert_private_sentinels_absent(result.text)


def test_callback_alert_failure_is_logged_without_masking_original_failure(tmp_path, caplog):
    failure = RuntimeError("service failed " + " ".join(PRIVATE_SENTINELS))
    result = asyncio.run(_run_callback(
        tmp_path, caplog, show_error=failure, fail_answer_calls=(2,),
    ))

    assert result.answers == 2
    assert result.service.show_calls == 1
    assert any(
        "stage=action; outcome=failure; reason=service_failure; action_class=catalog"
        in message for message in result.logs
    )
    assert result.logs[-1].endswith(
        "stage=alert; outcome=failure; reason=transport_failure; action_class=catalog)"
    )
    _assert_private_sentinels_absent(result.text)


def test_callback_success_edits_original_card_and_logs_bounded_completion(tmp_path, caplog):
    result = asyncio.run(_run_callback(tmp_path, caplog, log_level=logging.INFO))
    assert len(result.logs) == 3

    edits = [payload for endpoint, payload in result.calls if endpoint == "editMessageText"]
    assert len(edits) == 1 and int(edits[0]["message_id"]) == 41
    assert result.answers == 1
    assert result.service.show_calls == 1
    assert result.catch_all.call_count == 0
    assert result.logs[0].endswith(
        "stage=wiring; outcome=success; reason=handler_registered; action_class=unknown)"
    )
    assert result.stored["plan"]["kind"] == "show"
    assert result.logs[-1].endswith(
        "stage=completed; outcome=success; reason=callback_completed; action_class=catalog)"
    )
    _assert_private_sentinels_absent(result.text)
