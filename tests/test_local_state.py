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


def test_qualification_records_persist_and_switching_owner_clears_candidates(tmp_path: Path):
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
