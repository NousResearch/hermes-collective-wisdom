"""Local CLI/TUI consumer behavior over the durable mediation outbox."""
# pylint: disable=missing-function-docstring,missing-class-docstring,duplicate-code
# pylint: disable=protected-access,too-few-public-methods,too-many-locals

from __future__ import annotations

import asyncio
import contextvars
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.client import WisdomMuteResponse
from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.mediation import AssessmentBridge
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.native_tools import NativeModelTools
from hermes_wisdom_plugin.store import WisdomStore


NOW = 1_000.0


def _harness(tmp_path: Path):
    owner = {
        "profile_name": "default", "profile_home": str(tmp_path / "profile"),
        "account_id": "member", "organization_id": "org",
        "base_url": "https://gateway.invalid", "installation_id": "install",
    }
    store = WisdomStore(tmp_path / "state")
    store.activate_installation_identity("install", "org")
    store.bind_qualification_owner(owner)
    client = Mock(
        identity={"owner": "member"}, owner_user_id="member",
        expected_organization_id="org", display_org_id="org",
    )
    client.agent_led_policy.return_value = SimpleNamespace(
        org_id="org", max_recommendations_per_user_per_week=3,
        usage_evidence_window_days=7, min_aggregate_invocations=3,
        consecutive_day_usage_counts=True, repeated_edits_count=True,
        notification_defaults=SimpleNamespace(
            skill_ready_to_share=True, teammate_published=True, update_available=True,
        ),
    )
    client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org", muted=False, duration=None, muted_until=None,
        forever=False, revision=1,
    )
    client.recommendation_suppressions.return_value = []

    def response(request_id, reference, *, state="claimed"):
        return ClientDeliveryResponse(
            org_id="org", recipient_user_id="member", event_id="gateway-event",
            request_id=request_id, reference=reference, state=state, reason=None,
            lease_until=(datetime.fromtimestamp(NOW + 120, timezone.utc)
                         .isoformat().replace("+00:00", "Z")) if state == "claimed" else None,
        )

    client.claim_notification_delivery.side_effect = response
    client.settle_notification_delivery.side_effect = (
        lambda event_id, request_id, reference, *, outcome, receipt: response(
            request_id, reference, state=outcome
        )
    )
    current_owner = [dict(owner)]
    current_version = [1]
    service = SimpleNamespace(
        store=store,
        client=client,
        config={},
        native_owner=lambda: dict(current_owner[0]),
        _installation_owner=lambda: dict(current_owner[0]),
        native_skill_inspect=lambda skill_id, *, version: {
            "kind": "skill", "skill_id": skill_id, "version": current_version[0],
            "facts": {
                "skill": {"id": skill_id, "slug": "shared", "state": "active"},
                "version": {
                    "version": current_version[0], "state": "published",
                    "content_hash": "sha256:" + "a" * 64,
                    "editorial_name": "Shared workflow",
                    "published_by_user_id": "other-member",
                },
            },
        },
        close=Mock(),
    )
    queue = MediationStore(store, owner=owner, clock=lambda: NOW)
    session_key = "local:session-1"
    queue.register_session(
        "org", session_key=session_key, session_id="session-1", platform="local",
        actor_id="member", private=True, available=True, user_activity=True,
        address={"chat_id": "cli"},
    )
    assessment_id = queue.enqueue_owned(
        "org", "feed:event",
        {"kind": "skill", "event_id": "event", "skill_id": "skill", "version": 1},
    )
    job = queue.claim("org", session_key, assessment_only=True)[0]
    assert queue.save_advice("org", assessment_id, job["lease_token"], {
        "assessment_id": assessment_id, "relevance": "recommend",
        "title": "Shared workflow", "explanation": "Useful for recurring reviews.",
        "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
    })
    runtime = NativeSurfaceRuntime(
        tmp_path / "state", "default", lambda: service, object(),
        lambda coro, **_kwargs: asyncio.create_task(coro),
    )
    return SimpleNamespace(
        owner=owner, current_owner=current_owner, current_version=current_version,
        store=store, service=service,
        queue=queue, assessment_id=assessment_id, runtime=runtime,
    )


def _opportunity(notify, *, session_id="session-1", surface_id="cli"):
    return SimpleNamespace(
        profile_name="default", session_id=session_id, turn_id=f"turn-{session_id}",
        surface_id=surface_id, surface_generation=1, reason="reply", notify=notify,
    )


class _RecordingLlm:
    def __init__(self):
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        evidence = json.loads(kwargs["input"][0]["text"])["untrusted_evidence"]
        return SimpleNamespace(
            provider="active-provider", model="active-model",
            parsed={"advice": [{
                "assessment_id": item["assessment_id"],
                "relevance": "recommend", "title": "Shared workflow",
                "explanation": "Useful for recurring reviews.",
            } for item in evidence]},
        )


@pytest.mark.parametrize("change", ["owner", "missing_route"])
def test_captured_turn_cannot_cross_owner_or_new_unrouted_turn(tmp_path, monkeypatch, change):
    state = _harness(tmp_path)
    route: contextvars.ContextVar[Any] = contextvars.ContextVar("route", default=None)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(
        _RUNTIME_MAIN_CONTEXT=route,
    ))
    bridge = AssessmentBridge(
        lambda: state.service, _RecordingLlm(), get_delivery_mode=lambda: "agent",
        profile_name="default",
    )
    facts = {
        "platform": "cli", "session_id": "session-1", "task_id": "task-1",
        "turn_id": "turn-1", "model": "active-model", "conversation_history": [],
    }
    token = route.set({
        "session_id": "session-1", "provider": "active-provider", "model": "active-model",
    })
    bridge.run(**facts)
    route.reset(token)
    if change == "owner":
        state.current_owner[0] = {**state.owner, "account_id": "replacement"}
    else:
        bridge.run(**{**facts, "turn_id": "turn-2"})
    bridge._produce_due = Mock()
    bridge.run_local(
        state.service, state.current_owner[0], state.queue,
        _opportunity(None), admission_current=lambda: True,
    )
    bridge._produce_due.assert_not_called()


def test_two_interactive_sessions_assess_and_present_one_shared_item_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _harness(tmp_path)
    with state.store.transaction() as database:
        database.execute("DELETE FROM wisdom_shared_assessment")
    state.service.profile = ("default", tmp_path / "profile")
    state.service.check_owner = Mock(return_value={})
    state.service.notification_config = Mock(return_value={
        "notifications": {"delivery_mode": "agent"}
    })

    class DueConsumption:
        def __init__(self):
            self.polls = 0

        def poll_feed(self):
            self.polls += 1

        @staticmethod
        def refresh_metadata():
            return None

        @staticmethod
        def notifications(**_kwargs):
            return {"events": [{
                "event_id": "event", "category": "new_skill", "kind": "new",
                "skill_id": "skill", "skill_name": "Shared workflow", "version": 1,
                "occurred_at": "2026-09-15T00:00:00Z",
            }], "more": False}

    consumption = DueConsumption()
    state.service._consumption = Mock(return_value=consumption)

    def current_delivery_response(request_id, reference, *, state: Any = "claimed"):
        return ClientDeliveryResponse(
            org_id="org", recipient_user_id="member", event_id="gateway-event",
            request_id=request_id, reference=reference, state=state, reason=None,
            lease_until=(datetime.fromtimestamp(time.time() + 120, timezone.utc)
                         .isoformat().replace("+00:00", "Z")) if state == "claimed" else None,
        )

    state.service.client.claim_notification_delivery.side_effect = current_delivery_response
    state.service.client.settle_notification_delivery.side_effect = (
        lambda event_id, request_id, reference, *, outcome, receipt:
        current_delivery_response(request_id, reference, state=outcome)
    )
    runtime_main: contextvars.ContextVar[object] = contextvars.ContextVar(
        "fixture_runtime_main", default=None
    )
    monkeypatch.setitem(
        sys.modules, "agent.auxiliary_client",
        SimpleNamespace(_RUNTIME_MAIN_CONTEXT=runtime_main),
    )
    llm = _RecordingLlm()
    state.runtime.llm = llm
    assessment = AssessmentBridge(
        lambda: state.service, llm, get_delivery_mode=lambda: "agent",
        profile_name="default",
    )
    rendered = []

    async def notify(**kwargs):
        rendered.append(kwargs)
        return SimpleNamespace(outcome="rendered")

    for session_id in ("session-1", "session-2"):
        token = runtime_main.set({
            "session_id": session_id, "provider": "active-provider",
            "model": "active-model",
        })
        try:
            assessment.run(
                platform="cli", parent_session_id="", session_id=session_id,
                task_id=f"task-{session_id}", turn_id=f"turn-{session_id}",
                model="active-model",
                conversation_history=[{"role": "user", "content": session_id}],
            )
        finally:
            runtime_main.reset(token)

    async def scenario():
        await asyncio.gather(*(
            state.runtime.present_local_assessments(
                _opportunity(
                    notify, session_id=session_id,
                    surface_id=f"cli:{session_id}",
                ),
                get_delivery_mode=lambda: "agent",
                assessment=assessment,
            )
            for session_id in ("session-1", "session-2")
        ))

    asyncio.run(scenario())

    assert consumption.polls == 1
    rows = state.queue.assessments("org")
    assert len(llm.calls) == 1, (
        [(row["state"], row["owner_session"], row["last_error"]) for row in rows],
        state.service.client.method_calls,
    )
    assert len(rendered) == 1
    assert rows[0]["state"] == "delivered"


def test_background_pre_llm_cannot_claim_or_consume_local_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _harness(tmp_path)
    with state.store.transaction() as database:
        database.execute(
            """UPDATE wisdom_shared_assessment SET state='pending',advice_json=NULL,
            owner_session=NULL,lease_token=NULL,lease_until=NULL"""
        )
    runtime_main: contextvars.ContextVar[object] = contextvars.ContextVar(
        "background_runtime_main", default=None
    )
    monkeypatch.setitem(
        sys.modules, "agent.auxiliary_client",
        SimpleNamespace(_RUNTIME_MAIN_CONTEXT=runtime_main),
    )
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    factory = Mock(side_effect=AssertionError("background pre-LLM opened Wisdom"))
    llm = _RecordingLlm()
    assessment = AssessmentBridge(
        factory, llm, get_delivery_mode=lambda: "agent", profile_name="default",
    )
    token = runtime_main.set({
        "session_id": "background", "provider": "active-provider", "model": "active-model",
    })
    try:
        assessment.run(
            platform="cli", parent_session_id="", session_id="background",
            task_id="task", turn_id="turn", model="active-model",
            conversation_history=[],
        )
    finally:
        runtime_main.reset(token)
        monkeypatch.delenv("HERMES_SESSION_PLATFORM")

    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="rendered")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify, session_id="background"),
        get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        assessment=assessment,
    ))

    factory.assert_not_called()
    assert not llm.calls and not calls
    row = state.queue.assessments("org")[0]
    assert row["state"] == "pending" and row["owner_session"] is None


def test_delivered_item_is_not_replayed_after_runtime_recreation(tmp_path: Path) -> None:
    state = _harness(tmp_path)
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="rendered")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))
    recreated = NativeSurfaceRuntime(
        state.store.root, "default", lambda: state.service, object(),
        lambda coro, **_kwargs: asyncio.create_task(coro),
    )
    asyncio.run(recreated.present_local_assessments(
        _opportunity(notify, session_id="session-2", surface_id="tui:session-2"),
        get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))

    assert len(calls) == 1
    assert state.queue.assessments("org")[0]["state"] == "delivered"


@pytest.mark.parametrize("late_change", ["owner", "enrollment", "unload"])
def test_change_after_render_before_settlement_stays_unknown(tmp_path: Path, late_change: str):
    state = _harness(tmp_path)

    async def notify(**_kwargs):
        if late_change == "owner":
            replacement = {**state.owner, "account_id": "replacement"}
            state.current_owner[0] = replacement
            state.store.bind_qualification_owner(replacement)
        elif late_change == "enrollment":
            state.store.activate_installation_identity("replacement-install", "org")
        else:
            state.runtime.close()
        return SimpleNamespace(outcome="rendered")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))

    with state.store.transaction() as database:
        row = dict(database.execute(
            "SELECT state,delivered_at FROM wisdom_shared_assessment"
        ).fetchone())
    assert row["state"] == "delivery_uncertain"
    assert row["delivered_at"] is None


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [("not_rendered", "ready"), ("uncertain", "delivery_uncertain")],
)
def test_host_receipt_controls_retry_safety(
    tmp_path: Path, outcome: str, expected_state: str,
) -> None:
    state = _harness(tmp_path)
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome=outcome)

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))

    assert len(calls) == 1
    row = state.queue.assessments("org")[0]
    assert row["state"] == expected_state
    assert row["delivered_at"] is None


@pytest.mark.parametrize("denial", ["fixed", "mute"])
def test_fixed_or_muted_mode_does_not_present(tmp_path: Path, denial: str) -> None:
    state = _harness(tmp_path)
    if denial == "mute":
        state.service.client.recommendation_mute.return_value = WisdomMuteResponse(
            org_id="org", muted=True, duration="forever", muted_until=None,
            forever=True, revision=2,
        )
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="rendered")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify),
        get_delivery_mode=lambda: "fixed" if denial == "fixed" else "agent",
        clock=lambda: NOW,
    ))

    assert not calls
    assert state.queue.assessments("org")[0]["state"] == "ready"
    state.service.client.claim_notification_delivery.assert_not_called()


@pytest.mark.parametrize("late_change", ["content", "lease"])
def test_content_or_lease_change_after_render_stays_unknown(
    tmp_path: Path, late_change: str,
) -> None:
    state = _harness(tmp_path)

    async def notify(**_kwargs):
        if late_change == "content":
            state.current_version[0] = 2
        else:
            with state.store.transaction() as database:
                database.execute(
                    "UPDATE wisdom_shared_assessment SET lease_until=0"
                )
        return SimpleNamespace(outcome="rendered")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))

    with state.store.transaction() as database:
        row = database.execute(
            "SELECT state FROM wisdom_shared_assessment"
        ).fetchone()
    assert row[0] == "delivery_uncertain"


def test_unload_during_final_settlement_read_does_not_mark_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _harness(tmp_path)
    rendered = False
    original = MediationStore.delivery_ready

    async def notify(**_kwargs):
        nonlocal rendered
        rendered = True
        return SimpleNamespace(outcome="rendered")

    def delivery_ready(queue, *args, **kwargs):
        result = original(queue, *args, **kwargs)
        if rendered:
            state.runtime.close()
        return result

    monkeypatch.setattr(MediationStore, "delivery_ready", delivery_ready)
    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))
    assert state.queue.assessments("org")[0]["state"] == "delivery_uncertain"


def test_uncertain_first_item_stops_remaining_batch(tmp_path: Path) -> None:
    state = _harness(tmp_path)
    second_id = state.queue.enqueue_owned(
        "org", "feed:second",
        {"kind": "skill", "event_id": "second", "skill_id": "second", "version": 1},
    )
    second = state.queue.claim("org", "local:session-1", assessment_only=True)[0]
    assert state.queue.save_advice("org", second_id, second["lease_token"], {
        "assessment_id": second_id, "relevance": "recommend",
        "title": "Second workflow", "explanation": "Useful for another review.",
        "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
    })
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="uncertain")

    asyncio.run(state.runtime.present_local_assessments(
        _opportunity(notify), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    ))
    assert len(calls) == 1
    rows = state.queue.assessments("org")
    pending = [row for row in rows if row["state"] == "ready"]
    assert len(pending) == 1
    assert pending[0]["lease_token"] is None


def test_duplicate_opportunities_do_not_duplicate_output(tmp_path: Path) -> None:
    state = _harness(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def notify(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return SimpleNamespace(outcome="rendered")

    async def scenario():
        opportunity = _opportunity(notify)
        first = asyncio.create_task(state.runtime.present_local_assessments(
            opportunity, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        ))
        await started.wait()
        second = asyncio.create_task(state.runtime.present_local_assessments(
            opportunity, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        ))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())

    assert len(calls) == 1
    assert state.queue.assessments("org")[0]["state"] == "delivered"


def test_cancellation_after_dispatch_is_uncertain_and_not_replayed(tmp_path: Path) -> None:
    state = _harness(tmp_path)
    started = asyncio.Event()

    async def notify(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        opportunity = _opportunity(notify)
        task = asyncio.create_task(state.runtime.present_local_assessments(
            opportunity, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recreated = NativeSurfaceRuntime(
            state.store.root, "default", lambda: state.service, object(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        await recreated.present_local_assessments(
            opportunity, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        )

    asyncio.run(scenario())

    assert state.queue.assessments("org")[0]["state"] == "delivery_uncertain"


def test_cancelled_assessment_releases_host_without_closing_live_worker_service(tmp_path):
    state = _harness(tmp_path)
    started, release = threading.Event(), threading.Event()
    services, later_work = [], []

    def factory():
        service = SimpleNamespace(**{**vars(state.service), "close": Mock()})
        services.append(service)
        return service

    state.runtime.service_factory = factory

    class BlockingAssessment:
        def run_local(self, service, _owner, _queue, _opportunity, *, admission_current):
            started.set()
            assert release.wait(5)
            service.close.assert_not_called()
            if admission_current():
                later_work.append("stale operation")

    async def scenario():
        task = asyncio.create_task(state.runtime.present_local_assessments(
            _opportunity(None), get_delivery_mode=lambda: "agent", clock=lambda: NOW,
            assessment=BlockingAssessment(),
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.3)
            assert task in done, "host cancellation waited for a blocked provider"
            assert task.cancelled()
            assert len(services) == 2
            services[0].close.assert_called_once()
            services[1].close.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not later_work
    assert all(service.close.call_count == 1 for service in services)


@pytest.mark.parametrize("owner_change", [
    None, "account_id", "organization_id", "installation_id", "new_task",
    "unload", "expired", "missing_route", "late_unload",
])
def test_local_turn_can_follow_notice_to_exact_registered_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    owner_change: str | None,
) -> None:
    """A CLI/TUI recommendation handoff must reach the exact skill version."""

    state = _harness(tmp_path)
    route: contextvars.ContextVar[Any] = contextvars.ContextVar("route", default=None)
    monkeypatch.setitem(
        sys.modules, "agent.auxiliary_client", SimpleNamespace(_RUNTIME_MAIN_CONTEXT=route),
    )
    runtime_route = {
        "session_id": "session-1", "provider": "recording-provider",
        "model": "recording-model",
    }
    token = route.set(runtime_route)
    try:
        state.runtime.admit_local_tool(
            owner=state.owner,
            surface="cli",
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            route=runtime_route,
        )
        tools = NativeModelTools(SimpleNamespace(
            native=state.runtime,
            _closed=False,
            _service=lambda: state.service,
        ))
        if owner_change:
            if owner_change == "new_task":
                state.runtime.revoke_local_tool(session_id="session-1")
                state.runtime.admit_local_tool(
                    owner=state.owner, surface="cli", session_id="session-1",
                    task_id="task-2", turn_id="turn-2", route=runtime_route,
                )
            elif owner_change == "unload":
                state.runtime.close()
            elif owner_change == "expired":
                now = time.monotonic()
                monkeypatch.setattr(time, "monotonic", lambda: now + 601)
            elif owner_change == "missing_route":
                route.set(None)
            elif owner_change == "late_unload":
                def owner_then_unload():
                    state.runtime.close()
                    return dict(state.owner)
                state.service._installation_owner = owner_then_unload
            else:
                state.current_owner[0][owner_change] = "replacement"
            with pytest.raises(PermissionError):
                tools.inspect(
                    {"kind": "skill", "identity": "skill", "version": 1},
                    task_id="task-1", session_id="session-1",
                )
            return
        result = json.loads(tools.inspect(
            {"kind": "skill", "identity": "skill", "version": 1},
            task_id="task-1",
            session_id="session-1",
        ))
        with pytest.raises(PermissionError):
            tools.present(
                {"kind": "skill", "identity": "skill", "version": 1,
                 "title": "Review", "explanation": "Inspect is not approval"},
                task_id="task-1", session_id="session-1",
            )
    finally:
        route.reset(token)

    assert result["skill_id"] == "skill"
    assert result["version"] == 1
    assert result["facts"]["version"]["version"] == 1
    with pytest.raises(PermissionError):
        tools.inspect(
            {"kind": "skill", "identity": "skill", "version": 1},
            task_id="task-1",
            session_id="session-1",
        )
