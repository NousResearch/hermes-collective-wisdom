"""Reference-derived qualification and host lifecycle contract tests."""
# pylint: disable=import-outside-toplevel,line-too-long,missing-function-docstring
# pylint: disable=too-many-locals,use-dict-literal,use-implicit-booleaness-not-comparison
# pylint: disable=missing-class-docstring,too-few-public-methods

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from hermes_wisdom_plugin.qualification import (
    HIGH_USAGE_CONSECUTIVE_BUSINESS_DAYS,
    RETENTION_DAYS,
    QualificationController,
    LifecycleBridge,
    UnsafeSnapshotError,
    process_due_stability_jobs,
    record_mutation,
    record_successful_use,
    snapshot_tree,
)
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.entitlement import EntitlementClaims


def _store(tmp_path: Path) -> WisdomStore:
    store = WisdomStore(tmp_path / "state")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    return store


def _skill(tmp_path: Path, name: str = "learned-skill") -> Path:
    path = tmp_path / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: learned-skill\ndescription: Reuse a learned workflow.\n---\n# One\n",
        encoding="utf-8",
    )
    return path


def _kwargs(skill: Path, store: WisdomStore, *, tz=ZoneInfo("UTC")) -> dict:
    return {
        "store": store,
        "skills_root": skill.parent,
        "skill_resolver": lambda _name: skill,
        "timezone_info": (tz, str(getattr(tz, "key", tz))),
        "authorized": lambda: True,
    }


def _authorized_bridge(tmp_path, monkeypatch, llm, *, enabled=True):
    profile = tmp_path / "profile"
    skill = _skill(profile)
    store = _store(profile)
    current = {
        "profile_name": "default", "profile_home": str(profile.resolve()),
        "account_id": "owner-1", "organization_id": "org-1",
        "base_url": "https://gateway.invalid", "allow_model_review": enabled,
    }
    owner = {
        key: current[key] for key in (
            "profile_name", "profile_home", "account_id", "organization_id", "base_url"
        )
    } | {"installation_id": store.existing_installation_identity()}
    store.set_metadata("terminal_setup", {
        **owner, "version": 1, "state": "active", "disclosure_accepted_at": "now"
    })

    def account():
        return SimpleNamespace(
            profile_name=current["profile_name"], profile_home=current["profile_home"],
            account_id=current["account_id"], organization_id=current["organization_id"],
            base_url=current["base_url"],
            entitlement_claims=lambda: EntitlementClaims(
                org_id=current["organization_id"],
                scopes=["wisdom:read"], expires_at=4_000_000_000
            ),
        )

    import hermes_wisdom_plugin.account as account_module
    import hermes_wisdom_plugin.runtime as runtime_module

    monkeypatch.setattr(account_module.HermesAccountAdapter, "current", staticmethod(account))
    monkeypatch.setattr(
        runtime_module, "current_profile",
        lambda: (current["profile_name"], Path(current["profile_home"]).resolve()),
    )
    bridge = LifecycleBridge(
        store.root, ("default", profile), llm=llm,
        get_config=lambda key, default=None: (
            current["allow_model_review"]
            if key == "qualification.allow_model_review" else default
        ),
    )
    controller = bridge._get()  # pylint: disable=protected-access
    assert controller is not None
    controller.skill_resolver = lambda _name: skill
    return bridge, skill, store, current


def _ambiguous_turn(bridge, skill, *, session_id="session-1", task_id="task-1"):
    bridge.admit_turn(
        platform="cli", session_id=session_id, task_id=task_id,
        parent_session_id="",
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id=session_id, task_id=task_id,
    )
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\ndescription: Reuse a learned workflow.\n---\n"
        "# One\nApply this workflow to incident triage.\n", encoding="utf-8"
    )
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id=session_id, task_id=task_id,
    )


def _apply_bridge_drift(drift, bridge, store, current):
    if drift == "account":
        current["account_id"] = "owner-2"
    elif drift == "profile":
        current["profile_name"] = "other"
    elif drift == "organization":
        current["organization_id"] = "org-2"
    elif drift == "installation":
        store.activate_installation_identity("hwi_replaced", "org-1")
    elif drift == "opt_in":
        current["allow_model_review"] = False
    else:
        assert drift == "unload"
        bridge.close()


def test_snapshot_refuses_file_replaced_with_fifo_before_open(tmp_path, monkeypatch):
    from hermes_wisdom_plugin import qualification

    skill = _skill(tmp_path)
    target = skill / "SKILL.md"
    real_open = os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == target:
            target.unlink()
            os.mkfifo(target)
            # Assert before opening so a regression cannot hang the test runner.
            assert flags & os.O_NONBLOCK, "snapshot open must not block on a swapped FIFO"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(qualification.os, "open", replace_before_open)
    with pytest.raises(UnsafeSnapshotError):
        snapshot_tree(skill)


def test_high_usage_threshold_uses_consecutive_business_days_and_deduplicates(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store, tz=ZoneInfo("Australia/Brisbane"))
    start = datetime(2026, 8, 3, 9, tzinfo=ZoneInfo("Australia/Brisbane"))
    offsets = (0, 1, 2, 3, 4, 7, 8)
    for offset in offsets[:-1]:
        assert record_successful_use("learned-skill", at=start + timedelta(days=offset), **args) is None
    event_id = record_successful_use(
        "learned-skill", at=start + timedelta(days=offsets[-1]),
        session_id="session-1", task_id="task-1", **args,
    )
    assert event_id
    assert record_successful_use(
        "learned-skill", at=start + timedelta(days=9), **args
    ) is None
    events = store.local_events(kind="wisdom.candidate")
    assert len(events) == 1
    assert events[0]["session_id"] == "session-1"
    assert events[0]["task_id"] == "task-1"
    assert events[0]["payload"]["networked"] is False
    assert events[0]["payload"]["consent_required"] is True
    assert events[0]["payload"]["local_reasons"] == {
        "consecutive_business_days": HIGH_USAGE_CONSECUTIVE_BUSINESS_DAYS,
        "business_day_timezone": "Australia/Brisbane",
        "business_week": "monday_friday",
    }


def test_weekend_usage_neither_advances_nor_breaks_business_day_streak(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 5, 6, 7):
        assert record_successful_use("learned-skill", at=start + timedelta(days=offset), **args) is None
    assert record_successful_use("learned-skill", at=start + timedelta(days=8), **args)


def test_midweek_gap_resets_then_rebuilds_business_day_streak(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for offset in (0, 1, 3, 4, 7, 8, 9, 10):
        assert record_successful_use("learned-skill", at=start + timedelta(days=offset), **args) is None
    assert record_successful_use("learned-skill", at=start + timedelta(days=11), **args)


def test_profile_timezone_controls_the_qualification_day(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    record_successful_use(
        "learned-skill", at=datetime(2026, 8, 2, 16, tzinfo=timezone.utc),
        **_kwargs(skill, store, tz=ZoneInfo("Australia/Brisbane")),
    )
    with store.transaction() as db:
        row = db.execute("SELECT day_local,timezone_name FROM usage_day").fetchone()
    assert tuple(row) == ("2026-08-03", "Australia/Brisbane")


def test_usage_retention_is_bounded(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset in range(RETENTION_DAYS + 8):
        record_successful_use("learned-skill", at=start + timedelta(days=offset * 2), **args)
    with store.transaction() as db:
        rows = db.execute("SELECT day_local FROM usage_day ORDER BY day_local").fetchall()
    assert len(rows) <= (RETENTION_DAYS + 1) // 2
    assert rows[0][0] >= (start.date() + timedelta(days=50)).isoformat()


def test_structural_refinements_schedule_restart_safe_stability(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    record_successful_use("learned-skill", at=start, **args)
    for index in range(3):
        refs = skill / "refs"
        refs.mkdir(exist_ok=True)
        (refs / f"decision-{index}.md").write_text(str(index), encoding="utf-8")
        record_mutation("learned-skill", at=start + timedelta(days=index + 1), **args)
    restarted = WisdomStore(store.root)
    event_id = process_due_stability_jobs(
        store=restarted, at=start + timedelta(days=10), skills_root=skill.parent,
        timezone_info=(timezone.utc, "UTC"), authorized=lambda: True,
    )[0]
    assert event_id
    event = restarted.local_events(kind="wisdom.candidate")[0]
    assert event["qualification"] == "refinement"
    assert event["payload"]["local_reasons"]["meaningful_refinements"] == 3


def test_due_stability_runs_without_another_use_and_keeps_session(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    record_successful_use("learned-skill", at=start, **args)
    for index in range(3):
        refs = skill / "refs"
        refs.mkdir(exist_ok=True)
        (refs / f"step-{index}.md").write_text(str(index), encoding="utf-8")
        record_mutation(
            "learned-skill", at=start + timedelta(days=index + 1),
            session_id="origin-session", task_id="origin-task", **args,
        )
    emitted = process_due_stability_jobs(
        store=WisdomStore(store.root), at=start + timedelta(days=10),
        skills_root=skill.parent, timezone_info=(timezone.utc, "UTC"), authorized=lambda: True,
    )
    assert len(emitted) == 1
    event = store.local_events(kind="wisdom.candidate")[0]
    assert event["session_id"] == "origin-session"
    assert event["task_id"] == "origin-task"


def test_due_stability_waits_for_recent_use(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    record_mutation("learned-skill", at=start, **args)
    for index in range(3):
        refs = skill / "refs"
        refs.mkdir(exist_ok=True)
        (refs / f"late-{index}.md").write_text(str(index), encoding="utf-8")
        record_mutation("learned-skill", at=start + timedelta(days=index + 1), **args)
    due = dict(store=store, at=start + timedelta(days=10), skills_root=skill.parent,
               timezone_info=(timezone.utc, "UTC"), authorized=lambda: True)
    assert process_due_stability_jobs(**due) == []
    assert len(store.due_stability_jobs((start + timedelta(days=10)).isoformat())) == 1
    assert record_successful_use("learned-skill", at=start + timedelta(days=11), **args)


def test_ineligible_and_unauthorized_events_do_not_replay(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    denied = _kwargs(skill, store)
    denied["authorized"] = lambda: False
    assert record_successful_use("learned-skill", **denied) is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM usage_day").fetchone()[0] == 0
    excluded = skill.parent / "_wisdom" / "managed"
    excluded.mkdir(parents=True)
    (excluded / "SKILL.md").write_text("managed", encoding="utf-8")
    denied = _kwargs(excluded, store)
    assert record_successful_use("managed", **denied) is None


def test_lifecycle_routes_only_real_supported_actions_and_retains_ids(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    controller = QualificationController(
        store=store, skills_root=skill.parent, authorized=lambda: True,
        timezone_info=lambda: (timezone.utc, "UTC"), skill_resolver=lambda _name: skill,
    )
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7):
        controller.on_skill_lifecycle(action="loaded", skill_name="learned-skill",
                                      provenance="local", task_id="task", session_id="session",
                                      at=start + timedelta(days=offset))
    event_id = controller.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        task_id="task", session_id="session", at=start + timedelta(days=8),
    )
    assert event_id
    event = store.local_events(kind="wisdom.candidate")[0]
    assert (event["task_id"], event["session_id"]) == ("task", "session")
    assert controller.on_skill_lifecycle(action="installed", skill_name="learned-skill", provenance="installed") is None
    controller.close()
    assert controller.on_skill_lifecycle(action="loaded", skill_name="learned-skill", provenance="local") is None


def test_snapshot_tree_uses_exact_file_bytes(tmp_path):
    skill = _skill(tmp_path)
    before, tree = snapshot_tree(skill)
    (skill / "refs").mkdir()
    (skill / "refs" / "x.md").write_bytes(b"\x00exact")
    after, next_tree = snapshot_tree(skill)
    assert before != after and tree != next_tree


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_snapshot_tree_rejects_symlinks_and_special_files_without_following(tmp_path):
    skill = _skill(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (skill / "linked").symlink_to(outside)
    with pytest.raises(UnsafeSnapshotError, match="symlink"):
        snapshot_tree(skill)
    (skill / "linked").unlink()
    os.mkfifo(skill / "pipe")
    with pytest.raises(UnsafeSnapshotError, match="special"):
        snapshot_tree(skill)


def test_unsafe_or_symlinked_skill_is_not_recorded_as_partial_evidence(tmp_path):
    skill, store = _skill(tmp_path), _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside", encoding="utf-8")
    linked = skill.parent / "linked-skill"
    linked.symlink_to(outside, target_is_directory=True)
    assert record_successful_use(
        "linked-skill", store=store, skills_root=skill.parent,
        skill_resolver=lambda _name: linked, authorized=lambda: True,
        timezone_info=(timezone.utc, "UTC"),
    ) is None
    os_path = skill / "pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(os_path)
        assert record_successful_use("learned-skill", **_kwargs(skill, store)) is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM usage_day").fetchone()[0] == 0


def test_authorized_ambiguous_refinement_runs_after_turn_and_stays_advisory(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    skill = _skill(profile)
    store = _store(profile)
    owner = {
        "profile_name": "default", "profile_home": str(profile.resolve()),
        "account_id": "owner-1", "organization_id": "org-1",
        "base_url": "https://gateway.invalid",
        "installation_id": store.existing_installation_identity(),
    }
    store.set_metadata("terminal_setup", {
        **owner, "version": 1, "state": "active", "disclosure_accepted_at": "now"
    })

    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                parsed={"classification": "meaningful"},
                provider="recording", model="aux-model",
            )

    def account():
        return SimpleNamespace(
            profile_name="default", profile_home=str(profile.resolve()),
            account_id="owner-1", organization_id="org-1",
            base_url="https://gateway.invalid",
            entitlement_claims=lambda: EntitlementClaims(
                org_id="org-1", scopes=["wisdom:read"], expires_at=4_000_000_000
            ),
        )

    import hermes_wisdom_plugin.account as account_module
    import hermes_wisdom_plugin.runtime as runtime_module

    monkeypatch.setattr(account_module.HermesAccountAdapter, "current", staticmethod(account))
    monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", profile.resolve()))
    llm = RecordingLlm()
    bridge = LifecycleBridge(
        store.root, ("default", profile), llm=llm,
        get_config=lambda key, default=None: (
            True if key == "qualification.allow_model_review" else default
        ),
    )
    controller = bridge._get()  # pylint: disable=protected-access
    assert controller is not None
    controller.skill_resolver = lambda _name: skill
    bridge.admit_turn(
        platform="cli", session_id="session-1", task_id="task-1",
        parent_session_id="",
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\ndescription: Reuse a learned workflow.\n---\n"
        "# One\nApply this workflow to incident triage.\n", encoding="utf-8"
    )
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    assert llm.calls == []

    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["task"] == "hermes_collective_wisdom_refinement"
    with store.transaction() as db:
        refinement = db.execute(
            "SELECT classification,structural_json FROM refinement"
        ).fetchone()
        jobs = db.execute("SELECT COUNT(*) FROM stability_job").fetchone()[0]
    assert refinement[0] == "meaningful"
    assert "classification_provenance" in refinement[1]
    assert jobs == 1
    assert store.local_events(kind="wisdom.candidate") == []


def test_ambiguous_refinement_sends_exact_multiline_skill_text(tmp_path, monkeypatch):
    before = (
        "---\nname: learned-skill\ndescription: Reuse a learned workflow.\n---\n"
        "# One\n\n```python\ndef triage(event):\n    return event.owner\n```\n"
    )
    after = before.replace("return event.owner", "return event.remediation_owner")

    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed={"classification": "non_meaningful"})

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    (skill / "SKILL.md").write_text(before, encoding="utf-8")
    bridge.admit_turn(
        platform="cli", session_id="session-1", task_id="task-1", parent_session_id=""
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    (skill / "SKILL.md").write_text(after, encoding="utf-8")
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )

    assert len(llm.calls) == 1
    assert json.loads(llm.calls[0]["input"][0]["text"]) == {
        "before": before,
        "after": after,
        "structural_diff": {"added": [], "removed": [], "changed": ["SKILL.md"]},
    }
    with store.transaction() as db:
        stored_texts = {
            row[0] for row in db.execute("SELECT skill_text FROM snapshot").fetchall()
        }
    assert stored_texts == {before, after}


def test_stale_stored_skill_text_digest_denies_model_transmission(tmp_path, monkeypatch):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("mismatched stored text reached model transport")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    bridge.admit_turn(
        platform="cli", session_id="session-1", task_id="task-1", parent_session_id=""
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    with store.transaction() as db:
        db.execute("UPDATE snapshot SET skill_text='normalized stale text'")
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\n---\n# One\nA meaningful-looking edit.\n",
        encoding="utf-8",
    )
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )

    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == (
            "non_meaningful"
        )


def test_model_review_opt_in_and_private_human_turn_are_both_required(
    tmp_path, monkeypatch
):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("unauthorized model call")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(
        tmp_path, monkeypatch, llm, enabled=False
    )
    _ambiguous_turn(bridge, skill)
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "non_meaningful"


def test_frontmatter_and_whitespace_only_edit_never_calls_model(tmp_path, monkeypatch):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("trivial edit called model")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    bridge.admit_turn(
        platform="cli", session_id="session-1", task_id="task-1", parent_session_id=""
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\ndescription: New metadata copy.\n---\n\n# One  \n",
        encoding="utf-8",
    )
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "non_meaningful"


def test_clear_structural_edit_never_calls_model(tmp_path, monkeypatch):
    class RecordingLlm:
        calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("clear structural edit called model")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    bridge.admit_turn(
        platform="cli", session_id="session-1", task_id="task-1", parent_session_id=""
    )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    (skill / "refs").mkdir()
    (skill / "refs" / "verification.md").write_text("Run pytest.\n", encoding="utf-8")
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "meaningful"


@pytest.mark.parametrize("outcome", ["non_meaningful", "invalid", "error"])
def test_model_nonmeaningful_invalid_and_error_results_are_conservative(
    tmp_path, monkeypatch, outcome
):
    class OutcomeLlm:
        def complete_structured(self, **_kwargs):
            if outcome == "error":
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(
                parsed=(
                    {"classification": "non_meaningful"}
                    if outcome == "non_meaningful" else {
                        "classification": "meaningful", "explanation": "not in schema"
                    }
                ),
                provider="recording", model="aux-model",
            )

    bridge, skill, store, _current = _authorized_bridge(
        tmp_path, monkeypatch, OutcomeLlm()
    )
    _ambiguous_turn(bridge, skill)
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "non_meaningful"
        assert db.execute("SELECT COUNT(*) FROM stability_job").fetchone()[0] == 0


@pytest.mark.parametrize("origin", ["subagent", "shared_chat", "session_drift"])
def test_non_owning_turns_never_call_model(tmp_path, monkeypatch, origin):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("non-owning turn called model")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    if origin == "subagent":
        bridge.admit_turn(
            platform="cli", session_id="session-1", task_id="task-1",
            parent_session_id="parent-session",
        )
    elif origin == "shared_chat":
        native = SimpleNamespace(
            tool_invocation=lambda **_kwargs: (_ for _ in ()).throw(PermissionError())
        )
        bridge.admit_turn(
            native_runtime=native, platform="slack",
            session_id="session-1", task_id="task-1", parent_session_id="",
        )
    else:
        bridge.admit_turn(
            platform="cli", session_id="other-session", task_id="task-1",
            parent_session_id="",
        )
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\n---\n# Material new instructions\n",
        encoding="utf-8",
    )
    bridge.on_skill_lifecycle(
        action="patched", skill_name="learned-skill", provenance="local",
        session_id="session-1", task_id="task-1",
    )
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "non_meaningful"


@pytest.mark.parametrize(
    "drift", ["account", "profile", "organization", "installation", "opt_in", "unload"]
)
def test_revocation_before_deferred_request_transmits_nothing(tmp_path, monkeypatch, drift):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("revoked observation reached model transport")

    llm = RecordingLlm()
    bridge, skill, store, current = _authorized_bridge(tmp_path, monkeypatch, llm)
    _ambiguous_turn(bridge, skill)
    _apply_bridge_drift(drift, bridge, store, current)

    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )

    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "ambiguous"


def test_unobserved_disk_drift_before_deferred_request_transmits_nothing(
    tmp_path, monkeypatch
):
    class RecordingLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("stale skill text reached model transport")

    llm = RecordingLlm()
    bridge, skill, store, _current = _authorized_bridge(tmp_path, monkeypatch, llm)
    _ambiguous_turn(bridge, skill)
    (skill / "SKILL.md").write_text(
        "---\nname: learned-skill\n---\n# Replaced outside the host hook\n",
        encoding="utf-8",
    )

    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )

    assert llm.calls == []
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "ambiguous"


@pytest.mark.parametrize(
    "drift", ["account", "profile", "organization", "installation", "opt_in", "unload"]
)
def test_current_authority_is_rechecked_after_model_result(tmp_path, monkeypatch, drift):
    state = {}

    class DriftingLlm:
        def complete_structured(self, **_kwargs):
            _apply_bridge_drift(
                drift, state["bridge"], state["store"], state["current"]
            )
            return SimpleNamespace(
                parsed={"classification": "meaningful"},
                provider="recording", model="aux-model",
            )

    bridge, skill, store, current = _authorized_bridge(
        tmp_path, monkeypatch, DriftingLlm()
    )
    state.update(bridge=bridge, store=store, current=current)
    _ambiguous_turn(bridge, skill)
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "ambiguous"
        assert db.execute("SELECT COUNT(*) FROM stability_job").fetchone()[0] == 0


def test_unobserved_disk_drift_during_model_call_rejects_result(tmp_path, monkeypatch):
    state = {}

    class MutatingLlm:
        def complete_structured(self, **_kwargs):
            (state["skill"] / "SKILL.md").write_text(
                "---\nname: learned-skill\n---\n# A newer edit\n", encoding="utf-8"
            )
            return SimpleNamespace(
                parsed={"classification": "meaningful"},
                provider="recording", model="aux-model",
            )

    bridge, skill, store, _current = _authorized_bridge(
        tmp_path, monkeypatch, MutatingLlm()
    )
    state.update(skill=skill)
    _ambiguous_turn(bridge, skill)
    bridge.on_session_end(
        platform="cli", session_id="session-1", task_id="task-1",
        completed=True, failed=False, interrupted=False,
    )
    with store.transaction() as db:
        assert db.execute("SELECT classification FROM refinement").fetchone()[0] == "ambiguous"
        assert db.execute("SELECT COUNT(*) FROM stability_job").fetchone()[0] == 0


def test_same_org_account_switch_denies_then_reenrollment_starts_fresh_evidence(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    skill = _skill(profile)
    store = _store(profile)
    owner1 = {
        "profile_name": "default", "profile_home": str(profile.resolve()),
        "account_id": "owner-1", "organization_id": "org-1",
        "base_url": "https://gateway.invalid",
        "installation_id": store.existing_installation_identity(),
    }
    store.set_metadata("terminal_setup", {
        **owner1, "version": 1, "state": "active", "disclosure_accepted_at": "now"
    })
    store.bind_qualification_owner(owner1)
    args = _kwargs(skill, store)
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7):
        record_successful_use("learned-skill", at=start + timedelta(days=offset), **args)

    current = {"account_id": "owner-2"}

    def account():
        return SimpleNamespace(
            profile_name="default", profile_home=str(profile.resolve()),
            account_id=current["account_id"], organization_id="org-1",
            base_url="https://gateway.invalid",
            entitlement_claims=lambda: EntitlementClaims(
                org_id="org-1", scopes=["wisdom:read"], expires_at=4_000_000_000
            ),
        )

    import hermes_wisdom_plugin.account as account_module
    import hermes_wisdom_plugin.runtime as runtime_module

    monkeypatch.setattr(account_module.HermesAccountAdapter, "current", staticmethod(account))
    monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", profile.resolve()))
    bridge = LifecycleBridge(store.root, ("default", profile))
    assert bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local"
    ) is None
    with store.transaction() as db:
        assert db.execute("SELECT SUM(use_count) FROM usage_day").fetchone()[0] == 6

    owner2 = {**owner1, "account_id": "owner-2", "installation_id": "hwi_owner2"}
    store.activate_installation_identity("hwi_owner2", "org-1")
    store.set_metadata("terminal_setup", {
        **owner2, "version": 1, "state": "active", "disclosure_accepted_at": "later"
    })
    controller = bridge._get()  # pylint: disable=protected-access
    assert controller is not None
    controller.skill_resolver = lambda _name: skill
    bridge.on_skill_lifecycle(
        action="loaded", skill_name="learned-skill", provenance="local",
        task_id="owner2-task", session_id="owner2-session",
    )
    with store.transaction() as db:
        active = db.execute(
            "SELECT id FROM local_skill WHERE deleted_at IS NULL"
        ).fetchall()
        counts = db.execute(
            "SELECT skill_id,SUM(use_count) FROM usage_day GROUP BY skill_id ORDER BY SUM(use_count)"
        ).fetchall()
    assert len(active) == 1
    assert [row[1] for row in counts] == [1, 6]
    assert active[0][0] == counts[0][0]
