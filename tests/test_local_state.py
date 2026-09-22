"""Qualification state uses the injected host PluginState directly."""
# pylint: disable=missing-function-docstring

from pathlib import Path
from datetime import datetime, timedelta, timezone

from installed_test_support import JsonState
from hermes_wisdom_plugin.local_state import QualificationState
from hermes_wisdom_plugin.qualification import QualificationController


def owner(root: Path):
    return {
        "profile_name": "default",
        "profile_home": str(root),
        "account_id": "user-1",
        "organization_id": "org-1",
        "base_url": "https://gateway.invalid",
        "installation_id": "install-1",
    }


def test_qualification_records_persist_and_switching_owner_archives_inactive_records(
    tmp_path: Path,
):
    state = JsonState(tmp_path)
    first = QualificationState(state, tmp_path)
    first.bind_owner(owner(tmp_path))
    skill = tmp_path / "skills" / "one"
    skill.mkdir(parents=True)
    skill_id = first.register_skill(skill, content_hash="sha256:a", source_kind="local")
    first.record_usage_day(skill_id, "2026-09-18", timezone_name="UTC", retain_after="2026-08-01")
    event_id = first.emit_candidate(
        skill_id=skill_id,
        content_hash="sha256:a",
        qualification="high_usage",
        payload={"skill_name": "one"},
    )

    reopened = QualificationState(state, tmp_path)
    assert reopened.usage_days(skill_id, since="2026-09-01", timezone_name="UTC") == [
        "2026-09-18"
    ]
    assert reopened.candidates()[0]["event_id"] == event_id

    changed = {**owner(tmp_path), "account_id": "user-2"}
    reopened.bind_owner(changed)
    assert reopened.candidates() == []
    saved = state.get("wisdom-qualification")
    assert saved is not None
    inactive = saved["inactive"]
    assert len(inactive) == 1
    assert inactive[0]["owner"] == owner(tmp_path)
    assert inactive[0]["usage"][skill_id] == {"UTC|2026-09-18": 1}
    assert inactive[0]["candidates"][skill_id + ":sha256:a"]["event_id"] == event_id

    reopened.bind_owner(owner(tmp_path))
    assert reopened.candidates() == []
    assert reopened.usage_days(skill_id, since="2026-09-01", timezone_name="UTC") == []
    saved = state.get("wisdom-qualification")
    assert saved is not None
    assert [record["owner"] for record in saved["inactive"]] == [
        owner(tmp_path), changed,
    ]


def test_qualification_state_rejects_profile_path_switch(tmp_path: Path):
    host = JsonState(tmp_path)
    local = QualificationState(host, tmp_path)
    host.path = tmp_path.parent / "other" / "state.json"
    try:
        local.candidates()
    except RuntimeError as exc:
        assert "profile root changed" in str(exc)
    else:
        raise AssertionError("path switch was accepted")


def test_seven_consecutive_business_day_loads_qualify_without_model(tmp_path: Path):
    host = JsonState(tmp_path)
    local = QualificationState(host, tmp_path)
    local.bind_owner(owner(tmp_path))
    skill = tmp_path / "skills" / "one"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: one\n---\nUseful instructions.\n")
    controller = QualificationController(
        store=local,
        skills_root=tmp_path / "skills",
        authorized=lambda: True,
        timezone_info=lambda: (timezone.utc, "UTC"),
        skill_resolver=lambda _name: skill,
    )
    day = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7, 8):
        controller.on_skill_lifecycle(
            action="loaded", provenance="local", skill_name="one",
            at=day + timedelta(days=offset),
        )

    candidates = local.candidates()
    assert len(candidates) == 1
    assert candidates[0]["qualification"] == "high_usage"


def test_usage_retention_is_owner_timezone_scoped_and_file_backed(tmp_path: Path):
    host = JsonState(tmp_path)
    store = QualificationState(host, tmp_path)
    store.bind_owner(owner(tmp_path))
    skill = store.register_skill(
        tmp_path / "skills" / "one", content_hash="one", source_kind="local",
    )
    store.record_usage_day(skill, "2026-08-01", timezone_name="UTC", retain_after="2026-08-01")
    store.record_usage_day(skill, "2026-08-01", timezone_name="UTC", retain_after="2026-08-01")
    store.record_usage_day(
        skill, "2026-08-02", timezone_name="Australia/Brisbane", retain_after="2026-08-01",
    )
    store.record_usage_day(skill, "2026-09-04", timezone_name="UTC", retain_after="2026-08-02")

    reopened = QualificationState(JsonState(tmp_path), tmp_path)
    assert reopened.usage_summary(
        skill, start="2026-08-01", end="2026-09-04", timezone_name="UTC",
    ) == (1, 1)
    assert reopened.usage_days(
        skill, since="2026-08-01", timezone_name="Australia/Brisbane",
    ) == ["2026-08-02"]


def test_duplicate_candidate_keeps_first_evidence_and_event_identity(tmp_path: Path):
    store = QualificationState(JsonState(tmp_path), tmp_path)
    store.bind_owner(owner(tmp_path))
    skill = store.register_skill(
        tmp_path / "skills" / "one", content_hash="one", source_kind="local",
    )
    first = store.emit_candidate(
        skill_id=skill, content_hash="one", qualification="high_usage",
        payload={"skill_name": "one", "local_reasons": {"days": 7}},
        session_id="session-1", task_id="task-1",
    )
    duplicate = store.emit_candidate(
        skill_id=skill, content_hash="one", qualification="refinement",
        payload={"skill_name": "forged"}, session_id="session-2", task_id="task-2",
    )
    assert duplicate == first
    assert store.candidates() == [{
        "event_id": first,
        "kind": "wisdom.candidate",
        "organization_id": "org-1",
        "local_skill_id": skill,
        "skill_id": skill,
        "content_hash": "one",
        "qualification": "high_usage",
        "payload": {"skill_name": "one", "local_reasons": {"days": 7}},
        "session_id": "session-1",
        "task_id": "task-1",
        "state": "unread",
    }]


def test_incomplete_owner_is_rejected_without_creating_state(tmp_path: Path):
    host = JsonState(tmp_path)
    store = QualificationState(host, tmp_path)
    for incomplete in ({}, {"account_id": "user"}, {"organization_id": "org"}):
        try:
            store.bind_owner(incomplete)
        except ValueError as exc:
            assert "incomplete" in str(exc)
        else:
            raise AssertionError("incomplete owner was accepted")
    assert not host.path.exists()


def test_refinement_uses_injected_observation_clock(tmp_path: Path):
    store = QualificationState(JsonState(tmp_path), tmp_path)
    observed = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    store.record_refinement(
        "skill", from_hash="before", to_hash="after", classification="meaningful",
        structural={"added": ["references/check.md"]}, at=observed,
    )
    assert store.meaningful_refinement_count(
        "skill", since=observed.isoformat(),
    ) == 1
    assert store.meaningful_refinement_count(
        "skill", since=(observed + timedelta(microseconds=1)).isoformat(),
    ) == 0
