"""Durable navigation replay after transport replacement failures."""
# pylint: disable=missing-function-docstring,protected-access,duplicate-code
# pylint: disable=attribute-defined-outside-init,too-many-locals,too-many-statements
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from native_test_support import publication_review
from test_native_recommendation_controls import RecommendationService, shared_facts, shared_job
from test_native_surfaces import (
    FakeLlm, SlackAdapter, SlackApp, _slack_action, callback_body, invocation,
)

from hermes_wisdom_plugin.errors import SharePackagingError
from hermes_wisdom_plugin.native_review import NativeReviewController, parse_callback
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.store import WisdomStore


@pytest.mark.parametrize("kind", ["retry", "recommendation", "sharing"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_telegram_navigation_recovers_after_edit_error(tmp_path, kind, uncertain):
    pytest.importorskip("telegram")
    from telegram.ext import ApplicationHandlerStop  # pylint: disable=import-outside-toplevel,import-error
    from hermes_wisdom_plugin.native_telegram import callback_invocation  # pylint: disable=import-outside-toplevel

    async def scenario():
        store = WisdomStore(tmp_path)
        store.activate_installation_identity("install", "org")
        service = RecommendationService(store)
        service.client = SimpleNamespace(
            identity={"owner": "member"}, owner_user_id="member", display_org_id="org",
            recommendation_mute=Mock(side_effect=ConnectionError("offline")),
        )
        service.notification_mode = Mock(return_value="agent")
        service.notification_mode_writable = lambda: True
        service.set_notification_mode = Mock(side_effect=AssertionError("No settings consent"))
        service.native_candidate_prepare = Mock(side_effect=[SharePackagingError(), "draft-1"])
        service.publication_review = Mock(side_effect=publication_review)
        service.publish = Mock(side_effect=AssertionError("No publication consent"))
        posts, edits, answers, handlers = [], [], [], []
        fail = [True]
        visible = []

        async def send(**payload):
            posts.append(payload)
            return SimpleNamespace(message_id=101)

        async def edit(**payload):
            edits.append(payload)
            if uncertain or not fail[0]:
                visible[:] = [payload]
            if fail[0]:
                raise TimeoutError("ack lost") if uncertain else RuntimeError("edit failed")

        async def answer(**payload):
            answers.append(payload)

        adapter = SimpleNamespace(
            _owner_profile="default", _bot=SimpleNamespace(send_message=send),
            _is_callback_user_authorized=lambda *_args, **_kwargs: True,
        )
        app = SimpleNamespace(add_handler=lambda handler, **_kwargs: handlers.append(handler))
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=11, username="member"),
            message=SimpleNamespace(chat_id=11, chat=SimpleNamespace(type="private"),
                                    message_id=101, get_bot=lambda: adapter._bot),
            answer=answer, edit_message_text=edit,
        )
        route = callback_invocation(adapter, query, {"owner_session": "telegram-session"})
        runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: service, FakeLlm(), Mock())
        runtime.wire_telegram(app, adapter)
        if kind == "retry":
            record = runtime.reviews.request_candidate(service, route, "event-1")
            label = "Retry Packaging"
        elif kind == "recommendation":
            record = runtime.reviews.request_recommendation(
                service, route, shared_job(), shared_facts(),
            )
            label = "Mute Skill Recommendations"
        else:
            record = runtime.reviews.request_publication(service, route, "draft-1")
            label = "Snooze Collective Wisdom"
        await runtime._deliver_new(route, record)
        query.data = next(
            b.callback_data for row in posts[-1]["reply_markup"].inline_keyboard
            for b in row if b.text == label
        )
        with pytest.raises(ApplicationHandlerStop):
            await handlers[-1].callback(SimpleNamespace(callback_query=query), None)
        assert len(edits) == 1
        with store.transaction() as db:
            successor_id = db.execute(
                "SELECT id FROM wisdom_consent WHERE id != ?", (record["id"],),
            ).fetchone()[0]
        assert runtime.reviews.load(successor_id)["delivery_state"] == "edit_failed"
        calls = (service.native_candidate_prepare.call_count,
                 service.publication_review.call_count, service.notification_mode.call_count)
        runtime.close()
        runtime = NativeSurfaceRuntime(tmp_path, "default", lambda: service, FakeLlm(), Mock())
        runtime.wire_telegram(app, adapter)
        fail[0] = False
        with pytest.raises(ApplicationHandlerStop):
            await handlers[-1].callback(SimpleNamespace(callback_query=query), None)
        assert len(edits) == 2
        assert not any(answer.get("show_alert") for answer in answers)
        assert visible == [edits[-1]]
        expected = "Ready For Review" if kind == "retry" else "Notification settings"
        assert expected in edits[-1]["text"]
        assert runtime.reviews.load(successor_id)["delivery_state"] == "confirmed"
        assert runtime.reviews.load(successor_id)["state"] == "pending"
        assert calls == (service.native_candidate_prepare.call_count,
                         service.publication_review.call_count,
                         service.notification_mode.call_count)
        assert all(
            parse_callback(b.callback_data)[0] == successor_id
            for row in edits[-1]["reply_markup"].inline_keyboard for b in row if b.callback_data
        )
        service.set_notification_mode.assert_not_called()
        service.publish.assert_not_called()
        assert not service.planned
        runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    "actor_id", "profile", "session_key", "chat_id", "thread_id", "scope_id", "message_id",
    "account_id", "installation_id", "organization_id", "profile_home", "base_url",
    "original_expired", "successor_expired", "stale", "superseded", "deferred", "completed",
    "revision", "inactive_adapter", "original_confirm", "wrong_navigation",
])
def test_navigation_recovery_keeps_owner_expiry_and_stale_fences(tmp_path, change):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    service.native_candidate_prepare = Mock(side_effect=[SharePackagingError(), "draft-1"])
    service.publication_review = publication_review
    controller = NativeReviewController(store)
    route = invocation(SlackAdapter())
    record = controller.request_candidate(service, route, "event-1")
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(route, message_id="card-1")
    successor = controller.act(service, record["id"], click, "packaging.retry")
    controller.delivery_failed(successor["id"], "edit failed")
    controller = NativeReviewController(WisdomStore(tmp_path))
    action = "packaging.retry"
    if change in {
        "actor_id", "profile", "session_key", "chat_id", "thread_id", "scope_id", "message_id",
    }:
        click = replace(click, **{change: "other"})
    elif change in {"account_id", "installation_id", "organization_id", "profile_home", "base_url"}:
        changed_owner = {**service.native_owner(), change: "other"}
        service.native_owner = lambda: changed_owner
    elif change in {"original_expired", "successor_expired"}:
        target = record if change == "original_expired" else successor
        with store.transaction() as db:
            db.execute("UPDATE wisdom_consent SET expires_at=0 WHERE id=?", (target["id"],))
    elif change in {"stale", "superseded", "deferred", "completed"}:
        controller._set_state(successor, change)
    elif change == "revision":
        with store.transaction() as db:
            db.execute("UPDATE wisdom_consent SET state_revision=state_revision+1 WHERE id=?",
                       (successor["id"],))
    elif change == "original_confirm":
        action = "confirm"
    elif change == "wrong_navigation":
        action = "sharing.settings"
    with pytest.raises(PermissionError):
        controller.act(service, record["id"], click, action,
                       may_apply=lambda: change != "inactive_adapter")
    assert controller.load(record["id"])["state"] == "superseded"
    assert service.native_candidate_prepare.call_count == 2
    assert not service.planned



@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("packaging_fails", [False, True])
def test_retry_recovers_existing_successor_after_failed_slack_edit(
    tmp_path, uncertain, packaging_fails,
):
    async def scenario():
        store = WisdomStore(tmp_path)
        service = RecommendationService(store)
        calls = []

        def prepare(event):
            calls.append(event)
            if len(calls) == 1 or packaging_fails:
                raise SharePackagingError()
            return "draft-1"

        service.native_candidate_prepare = prepare
        service.publication_review = publication_review
        service.publish = Mock(side_effect=AssertionError("No publication consent"))
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app, adapter = SlackApp(), SlackAdapter()
        runtime.wire_slack(app, adapter)
        route = invocation(adapter)
        await runtime.begin_candidate(route, "event-1")
        button = _slack_action(app.client.posts[-1]["blocks"], "Retry Packaging")
        original_id, action = parse_callback(button["value"])

        async def ack():
            return None

        if uncertain:
            async def lost_ack(**payload):
                app.client.edits.append(payload)
                raise TimeoutError("edit accepted but acknowledgement lost")
            original_edit = app.client.chat_update
            app.client.chat_update = lost_ack
        app.client.fail_edit = True
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert len(app.client.edits) == 1
        with store.transaction() as db:
            successor_id = db.execute(
                "SELECT id FROM wisdom_consent WHERE id != ?", (original_id,),
            ).fetchone()[0]
        successor = runtime.reviews.load(successor_id)
        assert successor["delivery_state"] == "edit_failed"
        assert successor["state"] == "pending"
        plan = successor["plan"]
        runtime.close()

        # A process restart cannot lose the link or repeat preparation.
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        runtime.wire_slack(app, adapter)
        app.client.fail_edit = False
        if uncertain:
            app.client.chat_update = original_edit
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert len(app.client.edits) == 2
        assert not app.client.ephemeral
        expected = "Share Packaging failed" if packaging_fails else "Ready For Review"
        assert expected in app.client.edits[-1]["text"]
        assert parse_callback(_slack_action(
            app.client.edits[-1]["blocks"],
            "Retry Packaging" if packaging_fails else "Share My Skill",
        )["value"])[0] == successor_id
        recovered = runtime.reviews.load(successor_id)
        assert recovered["plan"] == plan
        assert recovered["delivery_state"] == "confirmed"
        assert recovered["state"] == "pending"
        assert calls == ["event-1", "event-1"]
        service.publish.assert_not_called()
        assert not service.planned
        assert runtime.reviews.load(original_id)["state"] == "superseded"
        click = replace(route, message_id="card-1")
        with pytest.raises(PermissionError):
            runtime.reviews.act(service, original_id, click, "confirm")
        # Even a duplicate of a confirmed navigation only projects that same card.
        assert runtime.reviews.act(service, original_id, click, action)["id"] == successor_id
        runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["recommendation", "sharing"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_settings_navigation_recovers_existing_card_after_failed_edit(tmp_path, kind, uncertain):
    async def scenario():
        store = WisdomStore(tmp_path)
        store.activate_installation_identity("install", "org")
        service = RecommendationService(store)
        service.client = SimpleNamespace(
            identity={"owner": "member"}, owner_user_id="member", display_org_id="org",
            recommendation_mute=Mock(side_effect=ConnectionError("offline")),
        )
        service.notification_mode = Mock(return_value="agent")
        service.notification_mode_writable = lambda: True
        service.set_notification_mode = Mock(side_effect=AssertionError("No settings consent"))
        service.publication_review = publication_review
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app, adapter = SlackApp(), SlackAdapter()
        runtime.wire_slack(app, adapter)
        route = invocation(adapter)
        if kind == "recommendation":
            record = runtime.reviews.request_recommendation(
                service, route, shared_job(), shared_facts(),
            )
            label = "Mute Skill Recommendations"
        else:
            record = runtime.reviews.request_publication(service, route, "draft-1")
            label = "Snooze Collective Wisdom"
        await runtime._deliver_new(route, record)
        button = _slack_action(app.client.posts[-1]["blocks"], label)

        async def ack():
            return None

        if uncertain:
            async def lost_ack(**payload):
                app.client.edits.append(payload)
                raise TimeoutError("edit accepted but acknowledgement lost")
            original_edit = app.client.chat_update
            app.client.chat_update = lost_ack
        app.client.fail_edit = True
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert len(app.client.edits) == 1
        with store.transaction() as db:
            successor_id = db.execute(
                "SELECT id FROM wisdom_consent WHERE operation='settings'",
            ).fetchone()[0]
        reads = service.notification_mode.call_count
        runtime.close()
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        runtime.wire_slack(app, adapter)
        app.client.fail_edit = False
        if uncertain:
            app.client.chat_update = original_edit
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert len(app.client.edits) == 2
        assert not app.client.ephemeral
        assert "Notification settings" in app.client.edits[-1]["text"]
        assert runtime.reviews.load(successor_id)["delivery_state"] == "confirmed"
        assert service.notification_mode.call_count == reads
        service.set_notification_mode.assert_not_called()
        assert store.get_metadata("recommendation_mute") is None
        assert not service.planned
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM wisdom_consent").fetchone()[0] == 2
        runtime.close()

    asyncio.run(scenario())
