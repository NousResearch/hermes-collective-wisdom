"""Direct native review state and explicit action tests."""
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods,protected-access,invalid-name

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from installed_test_support import JsonState
from native_test_support import inspect_for_confirmation
from hermes_wisdom_plugin.errors import (
    WisdomAuthError, WisdomConflict, WisdomValidationError,
)
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController, callback_value
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.native_telegram import callback_invocation as telegram_callback
from hermes_wisdom_plugin.review_state import ReviewState


def owner():
    return {
        "profile_name": "default", "profile_home": "/profiles/default",
        "account_id": "account-1", "organization_id": "org-1",
        "base_url": "https://gateway.invalid", "installation_id": "install-1",
    }


def invocation():
    return NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="account-1", actor_name="Shannon", thread_id="", scope_id="",
        message_id="M1", session_key="slack:C1", adapter=object(),
    )


class Service:
    def __init__(self):
        self.applied = []

    def native_owner(self):
        return owner()

    def close(self):
        return None

    def install_plan(self, reference, *, update_mode=None):
        del update_mode
        return {
            "operation": "install", "owner": owner(), "skill_id": reference,
            "version": 2, "receipt": "receipt-1", "allowed": True,
        }

    def native_install_inspect(self, receipt):
        assert receipt in {"receipt-1", "partial-receipt", "blocked-receipt"}
        return [{"path": "SKILL.md", "content": "offline fixture"}]

    def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
        del accept_partial
        assert may_apply is not None and may_apply() is True
        self.applied.append(receipt)
        return {"installed": True, "skill_id": "skill-1", "version": 2}


def test_native_actor_id_is_independent_from_portal_account_id(tmp_path: Path):
    """Gateway-authorized native actors do not share Portal's identifier namespace."""
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = replace(invocation(), actor_id="U_SLACK_NATIVE_USER")

    record = controller.request_install(Service(), caller, "skill-1")

    assert record["route"]["actor_id"] == "U_SLACK_NATIVE_USER"
    assert record["owner"]["account_id"] == "account-1"



def test_review_state_persists_explicit_review_across_instances(tmp_path: Path):
    state = JsonState(tmp_path)
    controller = NativeReviewController(ReviewState(state, tmp_path))

    created = controller.request_install(Service(), invocation(), "skill-1")
    loaded = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path)).load(created["id"])

    assert loaded["state"] == "pending"
    assert loaded["plan"]["receipt"] == "receipt-1"
    assert loaded["owner"] == owner()


def test_click_executes_only_current_exact_review_and_records_result(tmp_path: Path):
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")
    created = inspect_for_confirmation(
        controller, created["id"], service, replace(invocation(), message_id="card-1")
    )

    result = controller.act(
        created["id"], "confirm", service, replace(invocation(), message_id="card-1")
    )

    assert service.applied == ["receipt-1"]
    assert result["state"] == "completed"
    assert result["result"]["installed"] is True
    assert controller.pending(owner()) == []


def test_cancelled_review_revokes_inflight_effect_callback(tmp_path: Path, monkeypatch):
    """Cancellation after confirm admission still revokes the effect boundary."""
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")
    created = inspect_for_confirmation(
        controller, created["id"], service, replace(invocation(), message_id="card-1")
    )
    clicked = replace(invocation(), message_id="card-1")

    def apply(receipt, *, accept_partial=False, may_apply):
        del accept_partial
        controller.act(created["id"], "cancel", service, clicked)
        if not may_apply():
            raise WisdomAuthError("cancelled before effect")
        service.applied.append(receipt)
        return {"installed": True}

    monkeypatch.setattr(service, "install_apply", apply)
    with pytest.raises((WisdomAuthError, WisdomConflict)):
        controller.act(created["id"], "confirm", service, clicked)

    assert not service.applied
    assert controller.load(created["id"])["state"] == "cancelled"


def test_native_catalog_renders_actual_results(tmp_path: Path, monkeypatch):
    """Browse cards retain the authoritative names and descriptions they fetched."""
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    service = Service()
    monkeypatch.setattr(service, "search_skills", lambda _query: [{
        "id": "skill-1", "slug": "incident-triage",
        "author_description": "Diagnose incident failures", "latest_version": 2,
    }], raising=False)

    record = controller.request_catalog(service, invocation(), "browse", "incident")
    view = controller.view(record)
    text = view.to_text()

    assert "incident-triage" in text
    assert "Diagnose incident failures" in text
    assert "v2" in text
    assert view.items[0].actions[0].label == "Review install"

    controller.bind_message(record["id"], invocation(), "card-1")
    review = controller.act(
        record["id"], "catalog.install.0", service,
        replace(invocation(), message_id="card-1"),
    )

    assert review["operation"] == "install"
    assert review["plan"]["skill_id"] == "skill-1@v2"
    assert not service.applied


def test_review_action_requires_the_bound_original_card(tmp_path: Path):
    """A callback from another message cannot spend an otherwise valid review."""
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")

    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(
            created["id"], "confirm", service,
            replace(invocation(), message_id="card-2"),
        )

    assert not service.applied


def test_slack_post_binds_the_sdk_returned_original_card(tmp_path: Path):
    """The posted card ID, not the inbound command ID, becomes callback authority."""

    async def scenario():
        state = JsonState(tmp_path)
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, None,
            lambda *_args, **_kwargs: None, installed_state=state,
        )
        caller = invocation()

        class Client:
            async def chat_postMessage(self, **_payload):
                return {"ok": True, "channel": "C1", "ts": "card-1"}

        runtime._native[("slack", id(caller.adapter))] = SimpleNamespace(client=Client())
        record = runtime.reviews.request_install(Service(), caller, "skill-1")

        await runtime._post_review(caller, record)

        assert runtime.reviews.load(record["id"])["message_id"] == "card-1"

    asyncio.run(scenario())


def test_telegram_post_binds_the_sdk_returned_original_card(tmp_path: Path):
    """The Telegram Bot API result supplies the callback-authorizing message ID."""

    async def scenario():
        class Bot:
            async def do_api_request(self, endpoint, *, api_kwargs):
                assert endpoint == "sendRichMessage"
                assert api_kwargs["chat_id"] == 123
                return {
                    "ok": True,
                    "result": {"message_id": 77, "chat": {"id": 123}},
                }

        bot = Bot()
        adapter = SimpleNamespace(_bot=bot)
        caller = NativeInvocation(
            platform="telegram", profile="default", chat_type="private",
            chat_id="123", actor_id="native-7", actor_name="Shannon",
            thread_id="", scope_id="", message_id="command-1",
            session_key="telegram:123:native-7", adapter=adapter,
        )
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, None,
            lambda *_args, **_kwargs: None, installed_state=JsonState(tmp_path),
        )
        runtime._native[("telegram", id(adapter))] = object()
        record = runtime.reviews.request_install(Service(), caller, "skill-1")

        await runtime._post_review(caller, record)

        assert runtime.reviews.load(record["id"])["message_id"] == "77"

    asyncio.run(scenario())


def test_telegram_callback_adapter_rejects_a_stale_original_card(tmp_path: Path):
    """The real callback adapter's message ID is checked against the bound card."""
    bot = object()

    class Adapter:
        _owner_profile = "default"
        _bot = bot

        @staticmethod
        def _is_callback_user_authorized(user_id, **_kwargs):
            return user_id == "native-7"

    adapter = Adapter()
    caller = NativeInvocation(
        platform="telegram", profile="default", chat_type="private",
        chat_id="123", actor_id="native-7", actor_name="Shannon",
        thread_id="", scope_id="", message_id="command-1",
        session_key="telegram:123:native-7", adapter=adapter,
    )
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    service = Service()
    record = controller.request_install(service, caller, "skill-1")
    controller.bind_message(record["id"], caller, "77")
    query = SimpleNamespace(
        from_user=SimpleNamespace(id="native-7", username="Shannon", first_name=""),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"), chat_id=123,
            message_thread_id=None, message_id=78, get_bot=lambda: bot,
        ),
    )

    callback = telegram_callback(adapter, query, record)
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(record["id"], "confirm", service, callback)

    assert not service.applied


def test_runtime_unload_revokes_an_inflight_slack_effect(tmp_path: Path, monkeypatch):
    """A callback admitted before unload cannot cross the later effect boundary."""

    async def scenario():
        class Adapter:
            _owner_profile = "default"

            @staticmethod
            def _is_interactive_user_authorized(user_id, **_kwargs):
                return user_id == "U_SLACK_NATIVE_USER"

        class Client:
            async def chat_update(self, **_payload):
                return {"ok": True}

        class App:
            def __init__(self):
                self.client = Client()
                self.handler: Any = None

            def action(self, _pattern):
                def register(handler):
                    self.handler = handler
                    return handler
                return register

        service = Service()
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, None,
            lambda *_args, **_kwargs: None, installed_state=JsonState(tmp_path),
        )
        app, adapter = App(), Adapter()
        runtime.wire_slack(app, adapter)
        caller = replace(
            invocation(), adapter=adapter, actor_id="U_SLACK_NATIVE_USER",
            message_id="command-1",
        )
        record = runtime.reviews.request_install(service, caller, "skill-1")
        runtime.reviews.bind_message(record["id"], caller, "card-1")
        inspect_for_confirmation(
            runtime.reviews, record["id"], service, replace(caller, message_id="card-1")
        )

        def apply(receipt, *, accept_partial=False, may_apply):
            del accept_partial
            runtime.close()
            if not may_apply():
                raise WisdomAuthError("runtime unloaded before effect")
            service.applied.append(receipt)
            return {"installed": True}

        monkeypatch.setattr(service, "install_apply", apply)
        body = {
            "user": {"id": "U_SLACK_NATIVE_USER", "name": "Shannon"},
            "channel": {"id": "C1"}, "team": {"id": "W1"},
            "message": {"ts": "card-1"}, "container": {"message_ts": "card-1"},
        }

        async def ack():
            return None

        assert app.handler is not None
        await app.handler(
            ack, body, {"value": callback_value(record["id"], "confirm")}
        )

        assert not service.applied
        assert runtime.reviews.load(record["id"])["state"] == "uncertain"

    asyncio.run(scenario())


def test_owner_switch_revokes_effect_authority(tmp_path: Path, monkeypatch):
    """Portal ownership is re-read at the effect boundary without comparing actor IDs."""
    service = Service()
    changed = [False]
    monkeypatch.setattr(
        service, "native_owner",
        lambda: {**owner(), "account_id": "account-2"} if changed[0] else owner(),
    )
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")
    created = inspect_for_confirmation(
        controller, created["id"], service, replace(invocation(), message_id="card-1")
    )
    clicked = replace(invocation(), message_id="card-1")

    def apply(receipt, *, accept_partial=False, may_apply):
        del accept_partial
        changed[0] = True
        if not may_apply():
            raise WisdomAuthError("owner changed before effect")
        service.applied.append(receipt)
        return {"installed": True}

    monkeypatch.setattr(service, "install_apply", apply)
    with pytest.raises(WisdomAuthError, match="owner changed"):
        controller.act(created["id"], "confirm", service, clicked)

    assert not service.applied
    assert controller.load(created["id"])["state"] == "uncertain"


def test_unknown_apply_outcome_is_not_replayable(tmp_path: Path, monkeypatch):
    """A lost acknowledgement records uncertainty and blocks duplicate confirmation."""
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")
    created = inspect_for_confirmation(
        controller, created["id"], service, replace(invocation(), message_id="card-1")
    )
    clicked = replace(invocation(), message_id="card-1")

    def apply(receipt, *, accept_partial=False, may_apply):
        del accept_partial
        assert may_apply()
        service.applied.append(receipt)
        raise TimeoutError("provider acknowledgement lost")

    monkeypatch.setattr(service, "install_apply", apply)
    with pytest.raises(TimeoutError, match="acknowledgement"):
        controller.act(created["id"], "confirm", service, clicked)
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(created["id"], "confirm", service, clicked)

    assert service.applied == ["receipt-1"]
    assert controller.load(created["id"])["state"] == "uncertain"


def test_second_confirmation_cannot_enter_an_applying_review(tmp_path: Path, monkeypatch):
    """Only one confirmation can claim a pending review."""
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(created["id"], invocation(), "card-1")
    created = inspect_for_confirmation(
        controller, created["id"], service, replace(invocation(), message_id="card-1")
    )
    clicked = replace(invocation(), message_id="card-1")

    def apply(receipt, *, accept_partial=False, may_apply):
        del accept_partial
        with pytest.raises(WisdomAuthError, match="no longer current"):
            controller.act(created["id"], "confirm", service, clicked)
        assert may_apply()
        service.applied.append(receipt)
        return {"installed": True}

    monkeypatch.setattr(service, "install_apply", apply)
    result = controller.act(created["id"], "confirm", service, clicked)

    assert result["state"] == "completed"
    assert service.applied == ["receipt-1"]


def test_settings_card_applies_an_exact_mode_choice(tmp_path: Path, monkeypatch):
    """Settings retain current facts and expose the supported owner-fenced mutation."""
    service = Service()
    changed = []
    monkeypatch.setattr(service, "notification_settings", lambda **_kwargs: {
        "owner": owner(), "delivery_mode": "agent", "mode_writable": True,
        "mode_meanings": {"agent": "Proactive on", "fixed": "Manual only"},
        "gateway_status": "available", "gateway_mute": {"revision": 7},
        "mute_control": {"revision": 7},
    }, raising=False)

    def set_mode(mode, *, expected_mode, may_apply):
        assert may_apply()
        changed.append((mode, expected_mode))
        return {"delivery_mode": mode}

    monkeypatch.setattr(service, "set_notification_mode", set_mode, raising=False)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_settings(service, invocation())
    controller.bind_message(record["id"], invocation(), "card-1")
    view = controller.view(record)

    assert "Delivery mode: agent" in view.to_text()
    assert any(action.label == "Fixed notifications" for action in view.actions)

    result = controller.act(
        record["id"], "settings.mode.fixed", service,
        replace(invocation(), message_id="card-1"),
    )

    assert changed == [("fixed", "agent")]
    assert result["state"] == "completed"


def test_update_and_setup_requests_use_real_service_interfaces(tmp_path: Path, monkeypatch):
    """Native review preparation calls retained update/setup APIs, not invented adapters."""
    service = Service()
    calls = []
    monkeypatch.setattr(service, "update_plan", lambda skill_id: {
        "operation": "update", "skill_id": skill_id, "slug": "managed",
        "version": 3, "receipt": "update-receipt", "allowed": True,
    }, raising=False)

    def setup_plan(skill_id, *, version, phase, index, command):
        calls.append((skill_id, version, phase, index, command))
        return {
            "operation": "setup", "skill_id": skill_id, "version": version,
            "step": {"phase": phase, "index": index, "command": command},
            "receipt": "setup-receipt", "allowed": True,
        }

    monkeypatch.setattr(service, "installed_setup_plan", setup_plan, raising=False)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))

    update = controller.request_update(service, invocation(), "skill-1")
    setup = controller.request_setup(service, invocation(), ("skill-1", {
        "version": 3, "step": {
            "phase": "setup", "index": 1, "command": "python configure.py",
        },
    }))

    assert update["plan"]["receipt"] == "update-receipt"
    assert setup["plan"]["receipt"] == "setup-receipt"
    assert calls == [("skill-1", 3, "setup", 1, "python configure.py")]


def test_model_setup_request_queues_setup_not_install(tmp_path: Path, monkeypatch):
    """The retained native tool kind selects the matching explicit review."""
    service = Service()
    monkeypatch.setattr(service, "installed_setup_plan", lambda skill_id, **values: {
        "operation": "setup", "skill_id": skill_id, "version": values["version"],
        "step": {
            "phase": values["phase"], "index": values["index"],
            "command": values["command"],
        },
        "receipt": "setup-receipt", "allowed": True,
    }, raising=False)

    def discard(coroutine, **_kwargs):
        coroutine.close()

    runtime = NativeSurfaceRuntime(
        tmp_path, "default", lambda: service, None, discard,
        installed_state=JsonState(tmp_path),
    )
    caller = invocation()
    result = runtime.queue_model_consent(caller, {
        "kind": "setup", "identity": "skill-1", "version": 3,
        "step": {"phase": "setup", "index": 0, "command": "python configure.py"},
    })

    review = runtime.reviews.load(result["review_id"])
    assert review["operation"] == "setup"
    assert review["plan"]["receipt"] == "setup-receipt"


def test_expired_native_review_cannot_apply(tmp_path: Path):
    """An exact receipt still needs a current unexpired native review."""
    now = [10.0]
    service = Service()
    controller = NativeReviewController(
        ReviewState(JsonState(tmp_path), tmp_path), clock=lambda: now[0],
        ttl_seconds=5.0,
    )
    record = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(record["id"], invocation(), "card-1")
    now[0] = 15.0

    with pytest.raises(WisdomAuthError, match="expired"):
        controller.act(
            record["id"], "confirm", service,
            replace(invocation(), message_id="card-1"),
        )

    cancelled = controller.act(
        record["id"], "cancel", service,
        replace(invocation(), message_id="card-1"),
    )
    assert cancelled["state"] == "cancelled"
    assert not service.applied


def test_partial_install_requires_the_exact_native_acknowledgement(
    tmp_path: Path, monkeypatch,
):
    """Compatibility acknowledgement is an explicit action and reaches apply intact."""
    service = Service()
    applied = []
    monkeypatch.setattr(service, "install_plan", lambda *_args, **_kwargs: {
        "operation": "install", "owner": owner(), "skill_id": "skill-1",
        "version": 2, "receipt": "partial-receipt", "allowed": True,
        "compatibility": {"outcome": "compatible_after_setup"},
        "policy": {"compatibility_requires_confirmation": True},
    })

    def apply(receipt, *, accept_partial=False, may_apply):
        assert may_apply()
        applied.append((receipt, accept_partial))
        return {"installed": True, "setup_required": True}

    monkeypatch.setattr(service, "install_apply", apply)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(record["id"], invocation(), "card-1")
    record = inspect_for_confirmation(
        controller, record["id"], service, replace(invocation(), message_id="card-1")
    )
    clicked = replace(invocation(), message_id="card-1")
    view = controller.view(record)

    assert "confirm" not in [action.action for action in view.actions]
    assert "confirm.partial" in [action.action for action in view.actions]
    with pytest.raises(WisdomValidationError, match="action"):
        controller.act(record["id"], "confirm", service, clicked)
    result = controller.act(record["id"], "confirm.partial", service, clicked)

    assert result["state"] == "completed"
    assert applied == [("partial-receipt", True)]


def test_blocked_review_rejects_a_forged_confirm(tmp_path: Path, monkeypatch):
    """Callback data cannot bypass the reviewed plan's allowed decision."""
    service = Service()
    monkeypatch.setattr(service, "install_plan", lambda *_args, **_kwargs: {
        "operation": "install", "owner": owner(), "skill_id": "skill-1",
        "version": 2, "receipt": "blocked-receipt", "allowed": False,
        "permitted_actions": ["recheck"],
    })
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_install(service, invocation(), "skill-1")
    controller.bind_message(record["id"], invocation(), "card-1")
    record = inspect_for_confirmation(
        controller, record["id"], service, replace(invocation(), message_id="card-1")
    )

    with pytest.raises(WisdomValidationError, match="action"):
        controller.act(
            record["id"], "confirm", service,
            replace(invocation(), message_id="card-1"),
        )

    assert controller.load(record["id"])["state"] == "pending"
    assert not service.applied
