"""Shared feed, preference, mediation, and recovery acceptance slices."""
# pylint: disable=line-too-long,missing-function-docstring,import-outside-toplevel

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.client import Feed, FeedEvent, WisdomMuteResponse
from hermes_wisdom_plugin.consumption import WisdomConsumption
from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.preferences import WisdomPreferences, suppression_key
from hermes_wisdom_plugin.store import WisdomStore


def _feed(*events: FeedEvent, cursor="next", more=False):
    return Feed(events=list(events), next_cursor=cursor, has_more=more)


def _event(identity="event-1", *, version=2):
    return FeedEvent(
        event_id=identity,
        kind="updated",
        skill_id="skill-1",
        version=version,
        takedown_generation=0,
        installation_id=None,
        update_mode=None,
        occurred_at=datetime.now(timezone.utc),
    )


def _service(tmp_path, *, owner="account-1"):
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("hwi_test", "org-1")
    client = Mock(
        expected_organization_id="org-1",
        owner_user_id=owner,
        identity={"owner": owner},
        display_org_id="org-1",
        base_url="https://gateway.example",
    )
    client.feed.return_value = _feed(_event())
    client.list_skills.return_value = SimpleNamespace(
        skills=[SimpleNamespace(id="skill-1", slug="shared", model_dump=lambda **_: {
            "id": "skill-1", "slug": "shared", "latest_version": 2,
            "author_description": "Useful shared workflow", "security_check": None,
            "professionalism_check": None,
        })],
        next_cursor=None,
    )
    service = SimpleNamespace(store=store, client=client)
    return service


def test_feed_cursor_is_owner_bound_and_retry_deduplicates(tmp_path):
    service = _service(tmp_path)
    consumption = WisdomConsumption(service.store, service.client)

    first = consumption.poll_feed()
    second = consumption.poll_feed()

    assert first == {"inserted": 1, "pages": 1, "cursor": "next"}
    assert second["inserted"] == 0
    checkpoint = service.store.feed_checkpoint()
    assert checkpoint["organization_id"] == "org-1"
    assert checkpoint["installation_id"] == "hwi_test"
    assert service.client.feed.call_args_list[0].args == (None,)
    assert service.client.feed.call_args_list[1].args == ("next",)

    service.store.activate_installation_identity("hwi_other", "org-2")
    service.client.expected_organization_id = "org-2"
    service.client.display_org_id = "org-2"
    service.client.feed.return_value = _feed(_event("event-2"), cursor="other")
    consumption.poll_feed()
    assert [row["event_id"] for row in service.store.feed_events()] == ["event-2"]


def test_exact_version_self_publisher_is_not_advised_but_next_teammate_version_is(tmp_path):
    service = _service(tmp_path)
    service.client.feed.return_value = _feed(
        _event("own-version", version=2), _event("teammate-version", version=3)
    )
    service.client.version.side_effect = [
        SimpleNamespace(
            version={"version": 2, "published_by_user_id": "account-1"},
            skill={"id": "skill-1", "slug": "shared"},
        ),
        SimpleNamespace(
            version={"version": 3, "published_by_user_id": "account-2"},
            skill={"id": "skill-1", "slug": "shared"},
        ),
    ]
    consumption = WisdomConsumption(
        service.store,
        service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()
    assert consumption.notifications()["events"] == []
    assert service.client.version.call_count == 0
    assert consumption.refresh_metadata() == {"attempted": 2, "enriched": 2, "more": False}

    projected = consumption.notifications()

    assert [item["event_id"] for item in projected["events"]] == ["teammate-version"]
    assert [item["event_id"] for item in consumption.activity()["events"]] == [
        "own-version", "teammate-version",
    ]


def test_passive_reads_reject_changed_owner_before_network_or_rebinding(tmp_path):
    service = _service(tmp_path)
    consumption = WisdomConsumption(
        service.store,
        service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()
    checkpoint = service.store.feed_checkpoint()
    service.client.reset_mock()

    service.store.activate_installation_identity("next-installation", "org-2")
    service.client.expected_organization_id = "org-2"
    service.client.display_org_id = "org-2"
    service.client.owner_user_id = "account-2"
    service.client.identity = {"owner": "account-2"}

    with pytest.raises(WisdomAuthError, match="refresh"):
        consumption.activity()
    assert service.client.method_calls == []
    assert service.store.feed_checkpoint() == checkpoint
    assert [row["event_id"] for row in service.store.feed_events()] == ["event-1"]


def test_maintenance_metadata_enrichment_is_bounded_and_validates_exact_reference(tmp_path):
    service = _service(tmp_path)
    events = [
        FeedEvent(
            event_id=f"event-{index}", kind="new", skill_id=f"skill-{index}", version=1,
            takedown_generation=0, installation_id=None, update_mode=None,
            occurred_at=datetime.now(timezone.utc),
        )
        for index in range(10)
    ]
    service.client.feed.return_value = _feed(*events)

    def detail(skill_id, version):
        assert version == 1
        return SimpleNamespace(
            skill={"id": skill_id, "slug": f"shared-{skill_id}"},
            version={"version": version, "published_by_user_id": "account-2"},
        )

    service.client.version.side_effect = detail
    consumption = WisdomConsumption(
        service.store, service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()

    assert consumption.notifications() == {"events": [], "more": True}
    assert service.client.version.call_count == 0
    assert consumption.refresh_metadata() == {"attempted": 8, "enriched": 8, "more": True}
    assert service.client.version.call_count == 8
    assert len(consumption.notifications()["events"]) == 8
    assert service.client.version.call_count == 8


def test_mismatched_version_metadata_never_authorizes_advice(tmp_path):
    service = _service(tmp_path)
    service.client.version.return_value = SimpleNamespace(
        skill={"id": "different-skill", "slug": "wrong"},
        version={"version": 999, "published_by_user_id": "account-2"},
    )
    consumption = WisdomConsumption(
        service.store, service.client,
        config={"notifications": {"new_skills": "immediate"}},
    )
    consumption.poll_feed()

    assert consumption.refresh_metadata() == {"attempted": 1, "enriched": 0, "more": True}
    assert consumption.notifications()["events"] == []


def test_mediation_deduplicates_and_foreign_org_is_rejected(tmp_path):
    service = _service(tmp_path)
    queue = MediationStore(service.store, clock=lambda: 1000.0)
    first = queue.enqueue("org-1", "feed:event-1", {"kind": "skill", "skill_id": "skill-1", "version": 2})
    assert queue.enqueue("org-1", "feed:event-1", {"kind": "changed"}) == first
    assert len(queue.assessments("org-1")) == 1

    service.store.activate_installation_identity("other", "org-2")
    with pytest.raises(ValueError, match="organization"):
        queue.assessments("org-1")


def test_pending_mute_is_locally_protective_but_unmute_cannot_override_remote(tmp_path):
    service = _service(tmp_path)
    service.require_setup = Mock()
    service.client.recommendation_suppressions.return_value = []
    service.client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=False, duration=None, muted_until=None,
        forever=False, revision=0,
    )
    service.client.set_recommendation_mute.side_effect = TimeoutError
    preferences = WisdomPreferences(service, clock=lambda: 1000.0)
    reference = {"kind": "candidate", "content_hash": "sha256:" + "a" * 64}

    preferences.stage_mute("org-1", "forever", expected_revision=0)
    assert preferences.check("org-1", [reference])["muted"] is True

    preferences.stage_mute("org-1", None, expected_revision=0)
    service.client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=True, duration="forever", muted_until=None,
        forever=True, revision=0,
    )
    assert preferences.check("org-1", [reference])["muted"] is True
    assert "account-1" not in suppression_key(reference)
    assert "candidate" not in suppression_key(reference)


def test_mute_status_is_local_only_and_explicit_sync_flushes_pending_intent(tmp_path):
    from hermes_wisdom_plugin.sync_recovery import WisdomSyncRecovery

    service = _service(tmp_path)
    service.require_setup = Mock()
    service.client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=True, duration="forever", muted_until=None,
        forever=True, revision=1, mutation_id="mutation-1",
    )
    preferences = WisdomPreferences(service, clock=lambda: 1000.0)
    preferences.stage_mute("org-1", "forever", expected_revision=0)
    service.client.reset_mock()

    status = preferences.native_mute_command("status")

    assert status["sync"]["preference_sync"] == "pending"
    assert service.client.method_calls == []

    WisdomSyncRecovery(service, clock=lambda: 1000.0).retry()
    assert service.client.set_recommendation_mute.call_count == 1


def test_sync_status_is_redacted_and_read_only(tmp_path):
    from hermes_wisdom_plugin.sync_recovery import WisdomSyncRecovery

    service = _service(tmp_path)
    MediationStore(service.store)
    before = service.store.path.read_bytes()
    result = WisdomSyncRecovery(service).status()
    after = service.store.path.read_bytes()

    assert result["can_retry"] is False
    assert set(result) == {"delivery", "operation", "can_retry"}
    assert json.dumps(result).find("account-1") == -1
    assert before == after
