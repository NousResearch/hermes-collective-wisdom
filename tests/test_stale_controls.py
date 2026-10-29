"""Old or unsupported card controls answer with a bounded notice and have no effect.

Includes the composed live-shaped cell: real publication/invocation state with a
retained submitted private-draft operation, the registered Slack callback, the
Block Kit render/chat_update path and slack-sdk-shaped responses.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access
from __future__ import annotations

import asyncio
import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_commands import _native_invocation
from test_reshare_pending import Kernel, _abandoned_private_draft
from test_restored_native_flow import Service

from hermes_wisdom_plugin.errors import (
    CALLBACK_FAILED_MESSAGE, STALE_CONTROL_MESSAGE, WisdomError,
)
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.review_state import ReviewState

LEGACY_CONTROLS = [
    "portal.draft", "settings", "suggest.keep", "suggest.refresh", "refresh.review",
    "defer", "review", "unknown.action",
]


def _sdk_response(client, data, verb="chat.postMessage"):
    return AsyncSlackResponse(
        client=client, http_verb="POST", api_url=f"https://slack.invalid/api/{verb}",
        req_args={}, data=data, headers={}, status_code=200,
    )


def _sdk_harness(root, service):
    ui = SlackHarness(root, service)
    ui.post_response = lambda _payload: _sdk_response(
        ui, {"ok": True, "channel": "D1", "ts": f"card-{len(ui.posts)}"},
    )
    original_update = ui.chat_update

    async def sdk_update(**payload):
        await original_update(**payload)
        return _sdk_response(ui, {"ok": True, "channel": payload["channel"],
                                  "ts": payload["ts"]}, "chat.update")

    ui.chat_update = sdk_update
    return ui


async def _candidate_card(ui):
    """Post the current one-click share card and return its record id."""
    identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
    await ui.flush()
    return ui, identity


def _notices(ui):
    if isinstance(ui, TelegramHarness):
        return [answer for answer in ui.answers if answer]
    return ui.ephemerals


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("control", LEGACY_CONTROLS)
def test_stale_control_answers_once_without_effect(
    tmp_path, caplog, surface, control,
):
    async def scenario():
        service = Service()
        ui, identity = await _candidate_card(surface(tmp_path, service))
        before = ui.record(identity)
        edits = len(ui.edits)
        with caplog.at_level(logging.WARNING):
            await ui.click(identity, control)
        assert ui.record(identity) == before
        assert not service.applied
        assert len(ui.edits) == edits
        notices = _notices(ui)
        assert len(notices) == 1
        assert STALE_CONTROL_MESSAGE in str(notices[0])
        assert "not offered" not in str(notices[0])
        if surface is SlackHarness:
            assert notices[0]["user"] == "U1" and notices[0]["channel"] == "D1"
        else:
            assert notices[0] == {"text": STALE_CONTROL_MESSAGE, "show_alert": True}
        assert "no longer offered" in caplog.text
        assert "Traceback" not in caplog.text
        assert not [r for r in caplog.records if r.exc_info]
        ui.runtime.close()
    asyncio.run(scenario())


async def _raw_click(ui, value):
    """Deliver an arbitrary callback value through the registered handler."""
    if isinstance(ui, TelegramHarness):
        from telegram.ext import ApplicationHandlerStop  # pylint: disable=import-outside-toplevel
        query = SimpleNamespace(
            data=value, from_user=SimpleNamespace(id=11, username="fixture"),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private"), chat_id=11, message_id=123,
                message_thread_id=None, get_bot=lambda: ui,
            ),
            answer=ui._answer, edit_message_text=ui.chat_update,
        )
        with pytest.raises(ApplicationHandlerStop):
            await ui.handler(SimpleNamespace(callback_query=query), None)
        return

    async def ack():
        return None

    await ui.handler(ack, {"user": {"id": "U1"}, "channel": {"id": "D1"},
                           "team": {"id": "W1"}, "message": {"ts": "card-1"}},
                     {"value": value})


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("value", [
    "wi:agent:confirm:gone-record-1", "wi:agent:portal.draft:gone-record-1",
    "wi:agent:broken", "wi:agent::x",
])
def test_missing_record_and_malformed_ids_are_stale(tmp_path, surface, value):
    async def scenario():
        ui = surface(tmp_path, Service())
        await _raw_click(ui, value)
        notices = _notices(ui)
        assert len(notices) == 1 and STALE_CONTROL_MESSAGE in str(notices[0])
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_unauthorized_clicker_gets_no_notice(tmp_path, surface):
    async def scenario():
        ui, identity = await _candidate_card(surface(tmp_path, Service()))
        changes = ({"actor_id": 22} if surface is TelegramHarness
                   else {"user": {"id": "U-other"}})
        await ui.click(identity, "portal.draft", **changes)
        assert not _notices(ui)
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_real_failure_keeps_generic_message_without_exception_text(
    tmp_path, monkeypatch, caplog, surface,
):
    async def scenario():
        service = Service()
        ui, identity = await _candidate_card(surface(tmp_path, service))

        def broken(*_args, **_kwargs):
            raise WisdomError("internal detail token=abc123")

        monkeypatch.setattr(ui.runtime.reviews, "act", broken)
        with caplog.at_level(logging.WARNING):
            await ui.click(identity, "confirm")
        notices = _notices(ui)
        if surface is SlackHarness:
            assert [n["text"] for n in notices] == [CALLBACK_FAILED_MESSAGE]
        else:
            # Telegram acknowledged the click before the action; no second answer.
            assert not notices
        assert "internal detail" not in str(notices)
        assert "abc123" not in caplog.text
        assert "callback failed" in caplog.text
        ui.runtime.close()
    asyncio.run(scenario())


def test_slack_link_button_click_is_ignored(tmp_path, caplog):
    """Slack also posts block_actions for URL buttons; they are not callbacks."""
    async def scenario():
        ui = SlackHarness(tmp_path, Service())

        async def ack():
            return None

        with caplog.at_level(logging.WARNING):
            await ui.handler(ack, {"user": {"id": "U1"}, "channel": {"id": "D1"}}, {
                "type": "button", "action_id": "wisdom_native_url_0",
                "url": "https://portal.example/orgs/acme/wisdom/skills/abc",
            })
        assert not ui.ephemerals and not ui.edits
        assert "ValueError" not in caplog.text and "callback failed" not in caplog.text
        ui.runtime.close()
    asyncio.run(scenario())


def _resumed_share(tmp_path):
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    ui = _sdk_harness(tmp_path / "ui", kernel)
    return kernel, operation_id, ui


def test_live_shaped_legacy_card_through_registered_slack_callback(
    tmp_path, caplog,
):
    async def scenario():
        kernel, operation_id, ui = _resumed_share(tmp_path)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        record = ui.record(identity)
        assert record["plan"]["operation_id"] == operation_id
        assert record["message_id"] == "card-1" and record["display_ready"] is True
        operation = kernel.publication.state.publication(operation_id)
        assert (operation["phase"], operation["requested_action"]) == (
            "policy_review_required", "private_draft")
        calls = Counter(kernel.client.state.calls)
        posts, edits = len(ui.posts), len(ui.edits)

        with caplog.at_level(logging.WARNING):
            await ui.click(identity, "portal.draft")

        assert kernel.client.state.calls == calls, "no private-draft effect"
        assert kernel.publication.state.publication(operation_id) == operation
        assert ui.record(identity) == record
        assert [n["text"] for n in ui.ephemerals] == [STALE_CONTROL_MESSAGE]
        assert (len(ui.posts), len(ui.edits)) == (posts, edits), "no duplicate send/edit"
        assert "ValueError" not in caplog.text and "Traceback" not in caplog.text
        assert not [r for r in caplog.records if r.exc_info]

        # The current card still works afterwards: Share publishes the retained draft once.
        await ui.click(identity, "confirm")
        assert ui.record(identity)["state"] == "completed"
        assert kernel.client.state.calls == Counter(
            upload=1, submit=1, approve=1, head=1, publish=1)
        assert len(ui.ephemerals) == 1
        ui.runtime.close()
    asyncio.run(scenario())


def _concurrent(count, work):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait()
        try:
            return ("ok", work(index))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return ("error", exc)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))


def test_two_simultaneous_share_clicks_publish_once(tmp_path):
    kernel = Kernel(tmp_path)
    _abandoned_private_draft(kernel)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    record = controller.request_suggest(kernel, caller, "reviewed-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    results = _concurrent(2, lambda _i: controller.act(record["id"], "confirm", kernel, clicked))

    completed = [value for kind, value in results if kind == "ok" and value["state"] == "completed"]
    assert len(completed) == 1, results
    assert kernel.client.state.calls["publish"] == 1
    assert kernel.client.state.calls["upload"] == 1


def test_two_separately_resumed_cards_publish_once(tmp_path):
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    cards = []
    for index in range(2):
        record = controller.request_suggest(kernel, caller, "reviewed-skill")
        assert record["plan"]["operation_id"] == operation_id
        controller.bind_message(record["id"], caller, f"card-{index}")
        cards.append((record["id"], replace(caller, message_id=f"card-{index}")))
    assert cards[0][0] != cards[1][0]

    results = _concurrent(
        2, lambda i: controller.act(cards[i][0], "confirm", kernel, cards[i][1]))

    published = [
        value for kind, value in results
        if kind == "ok" and value.get("result", {}).get("publication_state") == "published"
    ]
    assert published, results
    assert kernel.client.state.calls["publish"] == 1, results
    assert kernel.client.state.calls["upload"] == 1
    assert kernel.publication.state.publication(operation_id)["status"] == "done"


def test_two_simultaneous_slack_share_callbacks_publish_once(tmp_path, caplog):
    """Both clicks go through the registered Slack callback on separate threads."""
    async def post():
        kernel, _operation_id, ui = _resumed_share(tmp_path)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        return kernel, ui, identity

    kernel, ui, identity = asyncio.run(post())
    with caplog.at_level(logging.WARNING):
        results = _concurrent(2, lambda _i: asyncio.run(ui.click(identity, "confirm")))
    assert all(kind == "ok" for kind, _ in results), results
    assert ui.record(identity)["state"] == "completed"
    assert kernel.client.state.calls["publish"] == 1
    assert kernel.client.state.calls["upload"] == 1
    assert "Traceback" not in caplog.text
    ui.runtime.close()


def _overlap_gate(parties):
    """Wrap a callable so every thread waits at it once, proving real overlap at that seam."""
    barrier = threading.Barrier(parties, timeout=10)
    seen = set()
    lock = threading.Lock()
    met = []

    def wrap(func):
        def gated(*args, **kwargs):
            me = threading.get_ident()
            with lock:
                first = me not in seen
                seen.add(me)
            if first:
                barrier.wait()
                met.append(me)
            return func(*args, **kwargs)
        return gated
    return wrap, met


def test_same_card_share_clicks_overlap_at_the_claim_and_publish_once(tmp_path):
    """Both clicks have read the pending card before either claims it (deterministic)."""
    kernel = Kernel(tmp_path)
    _abandoned_private_draft(kernel)
    state = ReviewState(JsonState(tmp_path), tmp_path)
    controller = NativeReviewController(state)
    caller = _native_invocation()
    record = controller.request_suggest(kernel, caller, "reviewed-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    wrap, met = _overlap_gate(2)
    state.compare_replace = wrap(state.compare_replace)

    results = _concurrent(2, lambda _i: controller.act(record["id"], "confirm", kernel, clicked))

    assert len(met) == 2, "both clicks reached the claim together"
    completed = [value for kind, value in results if kind == "ok" and value["state"] == "completed"]
    assert len(completed) == 1, results
    assert kernel.client.state.calls["publish"] == 1
    assert kernel.client.state.calls["upload"] == 1


def test_resumed_cards_overlap_inside_publication_submit_and_publish_once(tmp_path):
    """Two resumed cards both enter the publication submit before either takes its lock."""
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()
    cards = []
    for index in range(2):
        record = controller.request_suggest(kernel, caller, "reviewed-skill")
        controller.bind_message(record["id"], caller, f"card-{index}")
        cards.append((record["id"], replace(caller, message_id=f"card-{index}")))
    wrap, met = _overlap_gate(2)
    kernel.publication._submit = wrap(kernel.publication._submit)  # pylint: disable=protected-access

    results = _concurrent(
        2, lambda i: controller.act(cards[i][0], "confirm", kernel, cards[i][1]))

    assert len(met) == 2, "both submits overlapped before the publication lock"
    assert kernel.client.state.calls["publish"] == 1, results
    assert kernel.client.state.calls["upload"] == 1
    assert kernel.publication.state.publication(operation_id)["status"] == "done"


def test_telegram_failed_answer_is_not_answered_twice(tmp_path, caplog):
    """If the first answer_callback_query raises, the error handler must not answer again."""
    async def scenario():
        service = Service()
        ui, identity = await _candidate_card(TelegramHarness(tmp_path, service))
        calls = []

        async def failing_answer(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("telegram answer failed")

        ui._answer = failing_answer  # pylint: disable=protected-access
        with caplog.at_level(logging.WARNING):
            await ui.click(identity, "confirm")
        assert len(calls) == 1, calls
        assert "Traceback" not in caplog.text
        ui.runtime.close()
    asyncio.run(scenario())
