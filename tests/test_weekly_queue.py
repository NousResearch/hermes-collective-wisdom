"""Reference-derived deterministic weekly qualification queue tests."""
# pylint: disable=line-too-long,missing-function-docstring,unnecessary-lambda
# pylint: disable=use-dict-literal,use-implicit-booleaness-not-comparison

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.client import WisdomMuteResponse
from hermes_wisdom_plugin.mediation_producer import MaintenanceMediationProducer
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.preferences import WisdomPreferences

from hermes_wisdom_plugin.qualification import snapshot_tree
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.weekly_queue import (
    MAX_REVIEW_CANDIDATES,
    WeeklyPolicy,
    defer_candidate,
    enqueue_weekly_review,
    process_weekly_review,
    record_delivery_state,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> WisdomStore:
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    return store


def _skill(root: Path, name="release-notes") -> tuple[Path, str]:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Draft release notes.\n---\n# Notes\n",
        encoding="utf-8",
    )
    return path, snapshot_tree(path)[0]


def _use(store: WisdomStore, path: Path, offsets=(-2, -1, 0), *, count=1):
    content_hash, tree = snapshot_tree(path)
    skill_id = store.register_skill(path, content_hash=content_hash, source_kind="local", tree=tree)
    for offset in offsets:
        for _ in range(count):
            store.record_usage_day(
                skill_id, (NOW + timedelta(days=offset)).date().isoformat(),
                timezone_name="UTC", retain_after="2026-01-01",
            )
    store.emit_local_event(
        kind="wisdom.candidate", skill_id=skill_id, content_hash=content_hash,
        payload={
            "skill_name": path.name, "editorial_name": path.name,
            "editorial_description": "Draft release notes.",
            "local_reasons": {"consecutive_business_days": 3},
            "consent_required": True, "networked": False,
        }, session_id="origin", task_id="task",
        qualification="high_usage",
    )
    return skill_id, content_hash


def _args(store, root, policy=None, at=NOW):
    return dict(
        store=store, skills_root=root, policy=policy or WeeklyPolicy(), at=at,
        timezone_info=(timezone.utc, "UTC"), authorized=lambda: True,
    )


def test_producer_is_idempotent_across_connections(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    other = WisdomStore(store.root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: enqueue_weekly_review(**_args(value, root)), [store, other]))
    assert results[0]["assessment_id"] == results[1]["assessment_id"]
    assert len(store.weekly_assessments("org-1")) == 1


def test_dry_run_is_evidence_only_and_does_not_write(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    result = enqueue_weekly_review(dry_run=True, **_args(store, root))
    assert result["dry_run"] and len(result["candidates"]) == 1
    assert store.weekly_assessments("org-1") == []


def test_usage_window_excludes_future_and_old_days(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    _use(store, path, offsets=(-8, 1), count=100)
    result = enqueue_weekly_review(dry_run=True, **_args(store, root))
    evidence = result["candidates"][0]["evidence"]
    assert evidence["invocation_count"] == 3
    assert evidence["days_used"] == 3
    assert "path" not in evidence and "frontmatter" not in evidence


def test_disabled_policy_and_signed_out_profile_are_inert(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    for policy in (
        WeeklyPolicy(enabled=False), WeeklyPolicy(max_candidates=0),
        WeeklyPolicy(notification_enabled=False),
    ):
        result = enqueue_weekly_review(**_args(store, root, policy=policy))
        assert result["queued"] is False
    with store.transaction() as db:
        db.execute("UPDATE installation_identity SET verified_org_id=NULL")
    assert enqueue_weekly_review(**_args(store, root)) == {
        "queued": False, "skipped_reason": "not_entitled"
    }


def test_policy_cap_and_hard_cap_are_deterministic(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    for index in range(5):
        path, _ = _skill(root, f"skill-{index}")
        _use(store, path, count=index + 1)
    result = enqueue_weekly_review(
        dry_run=True, **_args(store, root, policy=WeeklyPolicy(max_candidates=2))
    )
    assert [item["skill_name"] for item in result["candidates"]] == [
        "skill-4", "skill-3", "skill-2", "skill-1", "skill-0"
    ]
    assert result["max_candidates"] == 2
    assert len(result["candidates"]) <= MAX_REVIEW_CANDIDATES


def test_model_cannot_invent_or_exceed_cap_and_provenance_is_actual(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    queued = enqueue_weekly_review(**_args(store, root))
    with pytest.raises(ValueError, match="invented"):
        process_weekly_review(
            store=store, assessment_id=queued["assessment_id"], session_id="human",
            runtime={"provider": "actual-provider", "model": "actual-model"},
            reviewer=lambda _payload: [{"skill_id": "invented", "content_hash": "bad"}],
            authorized=lambda: True, human_context=True,
        )
    result = process_weekly_review(
        store=store, assessment_id=queued["assessment_id"], session_id="human",
        runtime={"provider": "actual-provider", "model": "actual-model"},
        reviewer=lambda payload: [{
            "skill_id": payload["candidates"][0]["local_skill_id"],
            "content_hash": payload["candidates"][0]["content_hash"],
            "editorial_name": "Release Notes", "editorial_description": "Draft clear notes.",
        }], authorized=lambda: True, human_context=True,
    )
    assert result["selected"][0]["provenance"] == {
        "provider": "actual-provider", "model": "actual-model"
    }
    events = [event for event in store.local_events(kind="wisdom.candidate") if event["qualification"] == "weekly_usage"]
    assert len(events) == 1 and events[0]["session_id"] == "human"


def test_child_context_never_runs_review_or_presents(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    _use(store, path)
    queued = enqueue_weekly_review(**_args(store, root))
    called = []
    assert process_weekly_review(
        store=store, assessment_id=queued["assessment_id"], session_id="child",
        runtime={}, reviewer=lambda payload: called.append(payload),
        authorized=lambda: True, human_context=False,
    ) == {"processed": False, "skipped_reason": "human_context_required"}
    assert called == []


def test_later_week_does_not_repeat_unchanged_or_immediate_candidate(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    skill_id, _content_hash = _use(store, path)
    first = enqueue_weekly_review(**_args(store, root))
    process_weekly_review(
        store=store, assessment_id=first["assessment_id"], session_id="human", runtime={},
        reviewer=lambda payload: [payload["candidates"][0]], authorized=lambda: True,
        human_context=True,
    )
    later_at = NOW + timedelta(days=7)
    for offset in (-2, -1, 0):
        store.record_usage_day(
            skill_id, (later_at + timedelta(days=offset)).date().isoformat(),
            timezone_name="UTC", retain_after="2026-01-01",
        )
    later = enqueue_weekly_review(dry_run=True, **_args(store, root, at=later_at))
    assert later["candidates"] == []
    assert "release-notes" in later["excluded"]["previously_handled"]


@pytest.mark.parametrize("days", [3, 30, 60])
@pytest.mark.parametrize("delivery_state", ["delivered", "delivery_uncertain"])
def test_expired_deferral_allows_one_fresh_weekly_assessment_only_when_delivered(
    tmp_path, days, delivery_state
):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    skill_id, _content_hash = _use(store, path)
    first = enqueue_weekly_review(**_args(store, root))
    processed = process_weekly_review(
        store=store, assessment_id=first["assessment_id"], session_id="human", runtime={},
        reviewer=lambda payload: [payload["candidates"][0]], authorized=lambda: True,
        human_context=True,
    )
    candidate_assessment = processed["selected"][0]["assessment_id"]
    record_delivery_state(store, candidate_assessment, delivery_state)
    defer_candidate(store, candidate_assessment, days=days, at=NOW)
    before = enqueue_weekly_review(dry_run=True, **_args(store, root))
    assert before["candidates"] == []
    later_at = NOW + timedelta(days=days + 7)
    for offset in (-2, -1, 0):
        store.record_usage_day(
            skill_id, (later_at + timedelta(days=offset)).date().isoformat(),
            timezone_name="UTC", retain_after="2026-01-01",
        )
    result = enqueue_weekly_review(dry_run=True, **_args(store, root, at=later_at))
    if delivery_state == "delivered":
        assert len(result["candidates"]) == 1
    else:
        assert result["candidates"] == []


def test_changed_hash_and_dismissal_are_excluded(tmp_path):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    skill_id, content_hash = _use(store, path)
    store.dismiss_candidate(skill_id, content_hash)
    result = enqueue_weekly_review(dry_run=True, **_args(store, root))
    assert result["candidates"] == []
    assert "release-notes" in result["excluded"]["dismissed"]


def test_deferred_mediated_candidate_can_be_requeued_with_fresh_id_after_gate(  # pylint: disable=too-many-locals
    tmp_path,
):
    root, store = tmp_path / "skills", _store(tmp_path)
    path, _ = _skill(root)
    skill_id, content_hash = _use(store, path)
    owner = {
        "profile_name": "default", "profile_home": str(tmp_path / "profile"),
        "account_id": "member", "organization_id": "org-1",
        "base_url": "https://g.invalid", "installation_id": store.installation_identity(),
    }
    store.bind_qualification_owner(owner)
    event = next(
        row for row in store.local_events(kind="wisdom.candidate")
        if row["skill_id"] == skill_id and row["content_hash"] == content_hash
    )
    queue = MediationStore(store, owner=owner, clock=lambda: NOW.timestamp())
    original = queue.enqueue_owned(
        "org-1", f"candidate:{skill_id}:{content_hash}",
        {"kind": "candidate", "event_id": event["id"],
         "local_skill_id": skill_id, "content_hash": content_hash},
    )
    client = Mock(
        identity={"owner": "member"}, owner_user_id="member",
        display_org_id="org-1", expected_organization_id="org-1",
    )
    client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=False, duration=None, muted_until=None,
        forever=False, revision=1,
    )
    client.recommendation_suppressions.return_value = []
    service = SimpleNamespace(store=store, client=client)
    reference = {
        "kind": "candidate", "event_id": event["id"],
        "local_skill_id": skill_id, "content_hash": content_hash,
    }
    with store.transaction() as database:
        WisdomPreferences(service, clock=lambda: 1_000_000_000_000.0).stage_suppression(
            database, org="org-1", user="member", reference=reference, days=7,
        )
        database.execute(
            """UPDATE wisdom_shared_assessment SET state='deferred',
            event_key=event_key||':deferred:'||id WHERE id=?""", (original,)
        )
    policy = SimpleNamespace(
        org_id="org-1", usage_evidence_window_days=7, min_aggregate_invocations=3,
        max_recommendations_per_user_per_week=3, consecutive_day_usage_counts=True,
        repeated_edits_count=True,
        notification_defaults=SimpleNamespace(
            skill_ready_to_share=True, teammate_published=True, update_available=True,
        ),
    )
    producer = MaintenanceMediationProducer(
        service, SimpleNamespace(notifications=lambda **_kwargs: {"events": [], "more": False}),
        owner, skills_root=root, at=NOW, timezone_info=(timezone.utc, "UTC"),
    )

    blocked = producer.produce(policy)
    assert blocked["weekly"] == 0
    assert len(queue.assessments("org-1")) == 1

    with store.transaction() as database:
        database.execute(
            "UPDATE wisdom_preference_outbox SET suppress_until=0,state='expired'"
        )
    result = producer.produce(policy)
    candidates = [
        row for row in queue.assessments("org-1")
        if row["reference"].get("kind") == "candidate"
    ]
    assert result["weekly"] == 1
    assert len(candidates) == 2
    assert candidates[0]["id"] == original
    assert candidates[1]["id"] != original
