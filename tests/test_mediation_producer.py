"""Actual bounded maintenance-to-mediation producer tests."""
# pylint: disable=missing-function-docstring,duplicate-code,wrong-import-order
# pylint: disable=unused-argument,import-outside-toplevel,too-many-locals,protected-access

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from hermes_wisdom_plugin.client import (
    AgentLedNotificationDefaults,
    AgentLedPolicyResponse,
    WisdomMuteResponse,
)
from hermes_wisdom_plugin.consumption import WisdomConsumption
from hermes_wisdom_plugin.mediation import QueuedAssessment
from hermes_wisdom_plugin.mediation_producer import (
    MaintenanceMediationProducer,
    _candidate_reference,
)
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.runtime import InvocationService
from test_consumption_shared import _feed, _service
from test_weekly_queue import NOW, _skill, _use


def _owner(service, account="account-1"):
    return {
        "profile_name": "default", "profile_home": "/profiles/default",
        "account_id": account, "organization_id": "org-1",
        "base_url": "https://gateway.example", "installation_id": "hwi_test",
    }


def _policy(**notifications):
    return AgentLedPolicyResponse(
        org_id="org-1", usage_evidence_window_days=7,
        min_aggregate_invocations=3, consecutive_day_usage_counts=True,
        repeated_edits_count=True, max_recommendations_per_user_per_week=3,
        publication_mode="moderated", install_popularity_threshold=2,
        notification_defaults=AgentLedNotificationDefaults(
            skill_ready_to_share=notifications.get("skill_ready_to_share", True),
            teammate_published=notifications.get("teammate_published", True),
            update_available=notifications.get("update_available", True),
        ),
        manager_review_email_cadence="immediate", not_now_suppression_days=7,
        version=1, updated_by_user_id="manager",
    )


def _preferences(client, *, muted=False, suppressions=None):
    client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=muted, duration="forever" if muted else None,
        muted_until=None, forever=muted, revision=1,
    )
    client.recommendation_suppressions.return_value = suppressions or []
    client.set_recommendation_mute = Mock()
    client.suppress_recommendation = Mock()


def _candidate_item(*signals):
    return {
        "local_skill_id": "local-1",
        "content_hash": "sha256:" + "a" * 64,
        "evidence": {"supporting_signals": list(signals)},
    }


def _candidate_event(qualification, reasons, **changes):
    event = {
        "id": "event-1", "kind": "wisdom.candidate", "state": "unread",
        "skill_id": "local-1", "content_hash": "sha256:" + "a" * 64,
        "qualification": qualification,
        "payload": {
            "local_reasons": reasons,
            "editorial_name": "Do not persist this display text",
            "nested": {"arbitrary": "payload"},
        },
    }
    event.update(changes)
    return event


def test_candidate_reference_preserves_only_bounded_high_usage_reason():
    store = Mock()
    store.local_events.return_value = [_candidate_event(
        "high_usage",
        {
            "consecutive_business_days": 7,
            "business_day_timezone": "Australia/Brisbane",
            "business_week": "monday_friday",
            "unknown_nested": {"display": "never persist"},
        },
    )]

    reference = _candidate_reference(store, _candidate_item("high_usage"))

    assert reference == {
        "kind": "candidate", "event_id": "event-1",
        "local_skill_id": "local-1", "content_hash": "sha256:" + "a" * 64,
        "qualification": "high_usage",
        "qualification_reason": {"consecutive_business_days": 7},
    }
    store.local_events.assert_called_once_with(kind="wisdom.candidate")


def test_candidate_reference_preserves_only_bounded_refinement_reason():
    store = Mock()
    store.local_events.return_value = [_candidate_event(
        "refinement",
        {
            "meaningful_refinements": 3,
            "stable_days": 7,
            "used_within_days": 30,
            "unknown": "never persist",
        },
    )]

    assert _candidate_reference(store, _candidate_item("refinement")) == {
        "kind": "candidate", "event_id": "event-1",
        "local_skill_id": "local-1", "content_hash": "sha256:" + "a" * 64,
        "qualification": "refinement",
        "qualification_reason": {"meaningful_refinements": 3},
    }


def test_candidate_reference_omits_invalid_or_unknown_evidence_without_dropping_candidate():
    invalid = (None, True, 0, -1, 366, "7", {"count": 7})
    for value in invalid:
        store = Mock()
        store.local_events.return_value = [_candidate_event(
            "high_usage", {"consecutive_business_days": value},
        )]
        assert _candidate_reference(store, _candidate_item("high_usage")) == {
            "kind": "candidate", "event_id": "event-1",
            "local_skill_id": "local-1", "content_hash": "sha256:" + "a" * 64,
        }

    store = Mock()
    store.local_events.return_value = [_candidate_event(
        "future_qualification", {"consecutive_business_days": 7},
    )]
    assert _candidate_reference(store, _candidate_item("future_qualification")) == {
        "kind": "candidate", "event_id": "event-1",
        "local_skill_id": "local-1", "content_hash": "sha256:" + "a" * 64,
    }


def test_candidate_reference_still_rejects_stale_or_mismatched_event_identity():
    store = Mock()
    store.local_events.return_value = []  # The store excludes stale/read events.
    assert _candidate_reference(store, _candidate_item("high_usage")) is None

    store.local_events.return_value = [_candidate_event(
        "high_usage", {"consecutive_business_days": 7},
        content_hash="sha256:" + "b" * 64,
    )]
    assert _candidate_reference(store, _candidate_item("high_usage")) is None


def test_maintenance_queues_enriched_feed_and_weekly_evidence_without_model_or_send(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    service.require_setup = Mock()
    service.client.feed.return_value = _feed()
    service.client.skill.return_value = SimpleNamespace(
        skill={"id": "skill-feed", "slug": "team-skill"},
        versions=[{"version": 2, "published_by_user_id": "account-2"}],
    )
    from hermes_wisdom_plugin.client import FeedEvent
    service.client.feed.return_value = _feed(FeedEvent(
        event_id="feed-1", kind="new", skill_id="skill-feed", version=2,
        takedown_generation=0, installation_id=None, update_mode=None,
        occurred_at=datetime.now(timezone.utc),
    ))
    root = tmp_path / "skills"
    path, _content_hash = _skill(root)
    _use(service.store, path)
    owner = _owner(service)
    service.store.bind_qualification_owner(owner)
    _preferences(service.client)
    consumption = WisdomConsumption(
        service.store, service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()
    consumption.refresh_metadata()
    service.client.reset_mock()
    _preferences(service.client)

    result = MaintenanceMediationProducer(
        service, consumption, owner, skills_root=root,
        config={"notifications": {"delivery_mode": "agent"}}, at=NOW,
        timezone_info=(timezone.utc, "UTC"),
    ).produce(_policy())

    rows = MediationStore(service.store, owner=owner).assessments("org-1")
    assert result == {"queued": 2, "feed": 1, "weekly": 1, "more": False}
    assert {row["reference"]["kind"] for row in rows} == {"skill", "candidate"}
    candidate = next(row["reference"] for row in rows if row["reference"]["kind"] == "candidate")
    event = service.store.local_event(candidate["event_id"])
    assert event is not None
    assert (event["skill_id"], event["content_hash"]) == (
        candidate["local_skill_id"], candidate["content_hash"],
    )
    assert candidate["qualification"] == "high_usage"
    assert candidate["qualification_reason"] == {"consecutive_business_days": 3}
    assert all("evidence" not in row["reference"] for row in rows)
    assert service.client.recommendation_mute.call_count == 1
    assert service.client.recommendation_suppressions.call_count == 1
    assert not any(
        name in {"call_llm", "deliver", "send"}
        for name, *_ in service.client.method_calls
    )

    service._installation_owner = Mock(return_value=owner)
    service.client.agent_led_policy.return_value = _policy()
    result = QueuedAssessment(
        service, owner, None, get_delivery_mode=lambda: "agent",
    ).run(
        session_key="local:session-1", session_id="session-1", platform="local",
        actor_id="account-1", model="", history=[], address={"chat_id": "terminal"},
    )
    assert result["ready"] == 1
    invocation = InvocationService(tmp_path / "wisdom", ("default", tmp_path))
    invocation._onboarding = Mock(store=service.store)
    monkeypatch.setattr(invocation, "require_setup", lambda: None)
    monkeypatch.setattr(invocation, "check_owner", lambda: None)
    monkeypatch.setattr(invocation, "_installation_owner", lambda: owner)
    inbox_candidate = next(
        row for row in invocation.inbox()["assessments"]
        if row["reference"]["kind"] == "candidate"
    )
    assert inbox_candidate["reference"] == {
        "kind": "candidate", "event_id": event["id"],
        "local_skill_id": event["skill_id"],
        "content_hash": event["content_hash"],
    }
    inspected = invocation.native_candidate_inspect(inbox_candidate["reference"]["event_id"])
    assert inspected["event_id"] == event["id"] and inspected["networked"] is False


def test_self_publisher_contract_metadata_drains_without_queue(tmp_path):
    service = _service(tmp_path)
    service.require_setup = Mock()
    from hermes_wisdom_plugin.client import FeedEvent

    service.client.feed.return_value = _feed(FeedEvent(
        event_id="own-feed", kind="new", skill_id="skill-feed", version=2,
        takedown_generation=0, installation_id=None, update_mode=None,
        occurred_at=datetime.now(timezone.utc),
    ))
    service.client.skill.return_value = SimpleNamespace(
        skill={"id": "skill-feed", "slug": "team-skill"},
        versions=[{"version": 2, "published_by_user_id": "account-1"}],
    )
    owner = _owner(service)
    service.store.bind_qualification_owner(owner)
    _preferences(service.client)
    consumption = WisdomConsumption(
        service.store, service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )

    consumption.poll_feed()
    assert consumption.refresh_metadata() == {
        "attempted": 1, "enriched": 1, "more": False,
    }
    result = MaintenanceMediationProducer(
        service, consumption, owner, skills_root=tmp_path / "skills",
        config={"notifications": {"delivery_mode": "agent"}}, at=NOW,
        timezone_info=(timezone.utc, "UTC"),
    ).produce(_policy())

    assert result == {"queued": 0, "feed": 0, "weekly": 0, "more": False}
    assert consumption.notifications() == {"events": [], "more": False}
    assert [item["event_id"] for item in consumption.activity()["events"]] == [
        "own-feed",
    ]
    assert consumption.refresh_metadata() == {
        "attempted": 0, "enriched": 0, "more": False,
    }


def test_repeated_maintenance_reaches_feed_tail_and_weekly_candidate_once(tmp_path):
    service = _service(tmp_path)
    service.require_setup = Mock()
    from hermes_wisdom_plugin.client import FeedEvent

    events = [
        FeedEvent(
            event_id=f"feed-{index}", kind="new", skill_id=f"skill-{index}",
            version=1, takedown_generation=0, installation_id=None, update_mode=None,
            occurred_at=datetime.now(timezone.utc),
        )
        for index in range(9)
    ]
    service.client.feed.return_value = _feed(*events)
    service.client.skill.side_effect = lambda skill_id: SimpleNamespace(
        skill={"id": skill_id, "slug": skill_id},
        versions=[{"version": 1, "published_by_user_id": "account-2"}],
    )
    root = tmp_path / "skills"
    path, _content_hash = _skill(root)
    local_skill_id, _ = _use(service.store, path)
    owner = _owner(service)
    service.store.bind_qualification_owner(owner)
    consumption = WisdomConsumption(
        service.store, service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()
    assert consumption.refresh_metadata()["enriched"] == 8
    assert consumption.refresh_metadata()["enriched"] == 1
    service.client.reset_mock()
    _preferences(service.client)
    producer = MaintenanceMediationProducer(
        service, consumption, owner, skills_root=root,
        config={"notifications": {"delivery_mode": "agent"}}, at=NOW,
        timezone_info=(timezone.utc, "UTC"),
    )

    results = [producer.produce(_policy()) for _ in range(3)]

    rows = MediationStore(service.store, owner=owner).assessments("org-1")
    assert results == [
        {"queued": 8, "feed": 8, "weekly": 0, "more": True},
        {"queued": 2, "feed": 1, "weekly": 1, "more": False},
        {"queued": 0, "feed": 0, "weekly": 0, "more": False},
    ]
    assert len(rows) == 10
    feed_ids = {
        row["reference"].get("event_id")
        for row in rows if row["reference"]["kind"] == "skill"
    }
    assert feed_ids == {f"feed-{index}" for index in range(9)}
    assert [
        row["reference"]["local_skill_id"]
        for row in rows if row["reference"]["kind"] == "candidate"
    ] == [local_skill_id]
    assert all(
        service.store.local_event(row["reference"]["event_id"]) is not None
        for row in rows if row["reference"]["kind"] == "candidate"
    )
    assert all(row["state"] == "pending" for row in rows)
    assert all(row["local_seen_at"] is None for row in service.store.feed_events())
    assert service.store.weekly_assessments("org-1") == []
    assert service.client.recommendation_mute.call_count == 2
    assert service.client.recommendation_suppressions.call_count == 2


def test_owner_change_mute_opt_out_and_policy_gate_prevent_inheritance_or_queue(tmp_path):
    service = _service(tmp_path)
    service.require_setup = Mock()
    owner = _owner(service)
    service.store.bind_qualification_owner(owner)
    queue = MediationStore(service.store, owner=owner)
    queue.enqueue_owned(
        "org-1", "candidate:event-1",
        {"kind": "candidate", "event_id": "event-1", "local_skill_id": "local-1",
         "content_hash": "sha256:" + "a" * 64},
    )
    assert len(queue.assessments("org-1")) == 1

    changed = _owner(service, account="account-2")
    service.store.bind_qualification_owner(changed)
    assert MediationStore(service.store, owner=changed).assessments("org-1") == []

    consumption = Mock()
    consumption.notifications.return_value = {"events": [{
        "event_id": "feed-2", "category": "new_skill", "kind": "new",
        "skill_id": "skill-2", "skill_name": "two", "version": 1,
    }], "more": False}
    _preferences(service.client, muted=True)
    muted = MaintenanceMediationProducer(
        service, consumption, changed, skills_root=tmp_path / "skills", at=NOW,
    ).produce(_policy())
    assert muted["queued"] == 0

    service.client.reset_mock()
    fixed = MaintenanceMediationProducer(
        service, consumption, changed, skills_root=tmp_path / "skills",
        config={"notifications": {"delivery_mode": "fixed"}}, at=NOW,
    ).produce(_policy())
    assert fixed["queued"] == 0
    assert service.client.method_calls == []
