"""Offline Q01-Q10 qualification acceptance over file-backed PluginState."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access
# pylint: disable=too-few-public-methods,too-many-arguments,too-many-locals
# pylint: disable=use-implicit-booleaness-not-comparison
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from installed_test_support import JsonState

from hermes_wisdom_plugin import local_state as local_state_module
from hermes_wisdom_plugin import qualification as qualification_module
from hermes_wisdom_plugin.errors import WisdomError
from hermes_wisdom_plugin.local_state import QualificationState
from hermes_wisdom_plugin.qualification import (
    LifecycleBridge,
    QualificationController,
    process_due_stability_jobs,
    record_mutation,
    record_successful_use,
    snapshot_tree,
)
from hermes_wisdom_plugin.runtime import InvocationService


OWNER = {
    "profile_name": "fixture",
    "profile_home": "/profiles/fixture",
    "account_id": "alice",
    "organization_id": "org",
    "base_url": "https://gateway.invalid",
    "installation_id": "install-1",
}


def environment(tmp_path, *, name="local", bind_owner=True):
    skill = tmp_path / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Workflow\n---\n# Workflow\nInspect the logs.\n",
        encoding="utf-8",
    )
    state = QualificationState(JsonState(tmp_path), tmp_path)
    if bind_owner:
        state.bind_owner(OWNER)
    args = {
        "store": state,
        "skills_root": skill.parent,
        "authorized": lambda: True,
        "skill_resolver": lambda _name: skill,
        "timezone_info": (timezone.utc, "UTC"),
    }
    return skill, state, args


def freeze_clocks(monkeypatch, instant):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz or timezone.utc).replace(tzinfo=tz)

    monkeypatch.setattr(qualification_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(local_state_module, "datetime", FrozenDateTime)


def register_snapshot(state, skill):
    content_hash, tree = snapshot_tree(skill)
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    return state.register_skill(
        skill, content_hash=content_hash, source_kind="local",
        tree=tree, snapshot_text=text,
    )


def add_support_change(skill, state, args, monkeypatch, *, index, at):
    freeze_clocks(monkeypatch, at)
    support = skill / "references"
    support.mkdir(exist_ok=True)
    (support / f"check-{index}.md").write_text(
        f"Verification procedure {index}.\n", encoding="utf-8",
    )
    record_mutation("local", at=at, session_id="own-session", task_id="own-task", **args)
    return state.latest_snapshot(register_snapshot(state, skill))["content_hash"]


def candidate_rows(state):
    return state.candidates()


def qualify_by_usage(args):
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    event_id = None
    for offset in (0, 1, 2, 3, 4, 7, 8):
        event_id = record_successful_use(
            "local", at=start + timedelta(days=offset), **args,
        )
    assert event_id is not None
    return event_id


def test_q01_seventh_business_date_emits_real_owner_bound_candidate(tmp_path):
    skill, state, args = environment(tmp_path)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7):
        assert record_successful_use("local", at=start + timedelta(days=offset), **args) is None
        assert candidate_rows(state) == []

    event = record_successful_use("local", at=start + timedelta(days=8), **args)
    content_hash, _ = snapshot_tree(skill)
    rows = candidate_rows(state)
    assert event == rows[0]["event_id"]
    assert len(rows) == 1
    assert rows[0]["qualification"] == "high_usage"
    assert rows[0]["content_hash"] == content_hash
    assert rows[0]["organization_id"] == OWNER["organization_id"]
    assert state.local_skill(rows[0]["skill_id"])["canonical_path"] == str(skill.resolve())


def test_q11_native_inspection_returns_real_qualification_event_id(tmp_path, monkeypatch):
    _skill, state, args = environment(tmp_path)
    event_id = qualify_by_usage(args)
    service = InvocationService(tmp_path, ("fixture", tmp_path), state=state.state)
    service._qualification = state
    service._installation_owner = lambda: OWNER
    monkeypatch.setattr(InvocationService, "qualification_state", property(lambda _self: state))
    monkeypatch.setattr(service, "check_owner", lambda: None)

    inspected = service.native_candidate_inspect(event_id)

    assert inspected["event_id"] == event_id
    assert inspected["content_hash"] == candidate_rows(state)[0]["content_hash"]
    assert inspected["networked"] is False


def test_q02_same_day_is_deduplicated_and_weekend_is_not_a_gap(tmp_path):
    _, state, args = environment(tmp_path)
    monday = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    for _ in range(4):
        assert record_successful_use("local", at=monday, **args) is None
    for offset in (1, 2, 3, 4, 7):
        assert record_successful_use("local", at=monday + timedelta(days=offset), **args) is None
    assert record_successful_use("local", at=monday + timedelta(days=8), **args)
    row = state.local_skill(candidate_rows(state)[0]["skill_id"])
    assert state.usage_summary(
        row["id"], start="2026-08-03", end="2026-08-11", timezone_name="UTC",
    ) == (10, 7)


def test_q02_omitted_weekday_breaks_the_sequence(tmp_path):
    _, state, args = environment(tmp_path)
    monday = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    for offset in (0, 1, 3, 4, 7, 8, 9):
        assert record_successful_use("local", at=monday + timedelta(days=offset), **args) is None
    assert candidate_rows(state) == []


def test_q03_profile_dates_cross_dst_without_double_credit(tmp_path):
    _, state, args = environment(tmp_path)
    zone = ZoneInfo("America/New_York")
    args["timezone_info"] = (zone, zone.key)
    local_days = (
        datetime(2026, 3, 5, 12, tzinfo=zone),
        datetime(2026, 3, 6, 12, tzinfo=zone),
        datetime(2026, 3, 9, 12, tzinfo=zone),
        datetime(2026, 3, 10, 12, tzinfo=zone),
        datetime(2026, 3, 11, 12, tzinfo=zone),
        datetime(2026, 3, 12, 12, tzinfo=zone),
        datetime(2026, 3, 13, 12, tzinfo=zone),
    )
    for instant in local_days[:-1]:
        assert record_successful_use("local", at=instant, **args) is None
    assert record_successful_use("local", at=local_days[-1], **args)
    skill_id = candidate_rows(state)[0]["skill_id"]
    assert state.usage_days(
        skill_id, since="2026-03-01", timezone_name=zone.key,
    ) == ["2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10",
          "2026-03-11", "2026-03-12", "2026-03-13"]


def test_q03_timezone_change_does_not_merge_calendar_histories(tmp_path):
    _, state, args = environment(tmp_path)
    instant = datetime(2026, 8, 3, 0, 30, tzinfo=timezone.utc)
    ny = ZoneInfo("America/New_York")
    for offset in (0, 1, 2, 3, 4, 7):
        assert record_successful_use(
            "local", at=instant + timedelta(days=offset),
            **{**args, "timezone_info": (ny, ny.key)},
        ) is None
    assert record_successful_use(
        "local", at=instant + timedelta(days=8),
        **{**args, "timezone_info": (timezone.utc, "UTC")},
    ) is None
    assert candidate_rows(state) == []


def test_q04_three_structural_changes_qualify_exactly_at_stability_boundary(
    tmp_path, monkeypatch,
):
    skill, state, args = environment(tmp_path)
    used_at = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    record_successful_use("local", at=used_at, **args)
    final_hash = None
    for index in range(3):
        changed_at = used_at + timedelta(hours=index + 1)
        final_hash = add_support_change(
            skill, state, args, monkeypatch, index=index, at=changed_at,
        )
    due = used_at + timedelta(hours=3, days=7)
    assert process_due_stability_jobs(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        at=due - timedelta(microseconds=1), timezone_info=(timezone.utc, "UTC"),
    ) == []
    assert candidate_rows(state) == []
    emitted = process_due_stability_jobs(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        at=due, timezone_info=(timezone.utc, "UTC"),
    )
    assert emitted == [candidate_rows(state)[0]["event_id"]]
    assert candidate_rows(state)[0]["qualification"] == "refinement"
    assert candidate_rows(state)[0]["content_hash"] == final_hash
    assert candidate_rows(state)[0]["payload"]["local_reasons"] == {
        "meaningful_refinements": 3,
        "stable_days": 7,
        "used_within_days": 30,
    }


def test_q05_two_changes_and_skill_text_only_edits_do_not_qualify(tmp_path, monkeypatch):
    skill, state, args = environment(tmp_path)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    record_successful_use("local", at=start, **args)
    for index in range(2):
        add_support_change(
            skill, state, args, monkeypatch, index=index,
            at=start + timedelta(hours=index + 1),
        )
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    (skill / "SKILL.md").write_text(
        text.replace("description: Workflow", "description: Better title")
    )
    record_mutation("local", at=start + timedelta(hours=3), **args)
    (skill / "SKILL.md").write_text(
        (skill / "SKILL.md").read_text().replace("Inspect the logs.", "Inspect   the logs."),
        encoding="utf-8",
    )
    record_mutation("local", at=start + timedelta(hours=4), **args)
    (skill / "SKILL.md").write_text(
        (skill / "SKILL.md").read_text() + "\nVerify the deployed behavior.\n", encoding="utf-8",
    )
    record_mutation("local", at=start + timedelta(hours=5), **args)
    skill_id = register_snapshot(state, skill)
    freeze_clocks(monkeypatch, start + timedelta(days=7, hours=5))
    assert state.meaningful_refinement_count(
        skill_id, since=start.isoformat(),
    ) == 2
    assert process_due_stability_jobs(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        at=start + timedelta(days=8), timezone_info=(timezone.utc, "UTC"),
    ) == []
    assert candidate_rows(state) == []


@pytest.mark.parametrize("failure", ["changed", "deleted", "no_recent_use"])
def test_q06_stale_or_ineligible_stability_job_is_conservative(
    tmp_path, monkeypatch, failure,
):
    skill, state, args = environment(tmp_path)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    if failure != "no_recent_use":
        record_successful_use("local", at=start, **args)
    else:
        register_snapshot(state, skill)
    for index in range(3):
        add_support_change(
            skill, state, args, monkeypatch, index=index,
            at=start + timedelta(hours=index + 1),
        )
    if failure == "changed":
        (skill / "references" / "check-2.md").write_text("Changed after scheduling.\n")
    elif failure == "deleted":
        state.mark_missing_skills(set())
    due = start + timedelta(days=8)
    assert process_due_stability_jobs(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        at=due, timezone_info=(timezone.utc, "UTC"),
    ) == []
    assert candidate_rows(state) == []
    if failure == "no_recent_use":
        assert state.due_stability_jobs(due.isoformat())


@pytest.mark.parametrize("stale", ["changed", "deleted"])
def test_q06_stale_qualified_source_cannot_be_offered_or_inspected(
    tmp_path, monkeypatch, stale,
):
    skill, state, args = environment(tmp_path)
    event_id = qualify_by_usage(args)
    if stale == "changed":
        (skill / "SKILL.md").write_text(
            (skill / "SKILL.md").read_text(encoding="utf-8")
            + "\nNew instructions after qualification.\n",
            encoding="utf-8",
        )
        register_snapshot(state, skill)
    else:
        state.mark_missing_skills(set())
    service = InvocationService(tmp_path, ("fixture", tmp_path), state=state.state)
    service._qualification = state
    service._installation_owner = lambda: OWNER
    monkeypatch.setattr(InvocationService, "qualification_state", property(lambda _self: state))

    assert service.notice_candidates() == []
    with pytest.raises(WisdomError, match="not found|changed"):
        service.native_candidate_inspect(event_id)


def test_q07_unauthorized_and_nonhuman_lifecycle_facts_do_nothing(tmp_path):
    skill, state, _ = environment(tmp_path)
    denied = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: False,
        skill_resolver=lambda _name: skill,
    )
    assert denied.on_skill_lifecycle(
        action="loaded", provenance="local", skill_name="local",
    ) is None
    allowed = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        skill_resolver=lambda _name: skill,
    )
    for provenance in ("managed", "shared", "remote", ""):
        assert allowed.on_skill_lifecycle(
            action="loaded", provenance=provenance, skill_name="local",
        ) is None
    assert not state._load()["usage"]


@pytest.mark.parametrize(
    "facts,runtime",
    [
        ({"platform": "slack"}, None),
        ({"platform": "slack"}, SimpleNamespace(tool_invocation=lambda **_facts: None)),
        ({"platform": "cron"}, None),
        ({"platform": "subagent", "parent_session_id": "parent"}, None),
        ({"platform": "cli", "parent_session_id": "parent"}, None),
        ({"platform": "telegram", "parent_session_id": "parent"}, None),
    ],
)
def test_q07_shared_background_and_subagent_turns_are_not_admitted(
    tmp_path, monkeypatch, facts, runtime,
):
    _, state, _ = environment(tmp_path)
    bridge = LifecycleBridge(
        tmp_path, ("default", tmp_path), state=state.state, llm=object(),
        get_config=lambda key, default=None: (
            True if key == "qualification.allow_model_review" else default
        ),
    )
    monkeypatch.setattr(bridge, "_get", lambda: SimpleNamespace(store=state))
    bridge.admit_turn(
        session_id="s", task_id="t", native_runtime=runtime, **facts,
    )
    assert bridge._admitted_turns == {}


@pytest.mark.parametrize(
    "platform,runtime",
    [
        ("cli", None),
        ("tui", None),
        ("telegram", SimpleNamespace(tool_invocation=lambda **_facts: object())),
    ],
)
def test_q07_main_local_and_private_native_turns_are_admitted(
    tmp_path, monkeypatch, platform, runtime,
):
    _, state, _ = environment(tmp_path)
    bridge = LifecycleBridge(
        tmp_path, ("default", tmp_path), state=state.state, llm=object(),
        get_config=lambda key, default=None: (
            True if key == "qualification.allow_model_review" else default
        ),
    )
    monkeypatch.setattr(bridge, "_get", lambda: SimpleNamespace(store=state))

    bridge.admit_turn(
        platform=platform, session_id="own-session", task_id="own-task",
        native_runtime=runtime,
    )

    assert bridge._admitted_turns == {("own-task", "own-session"): platform}


class RecordingModel:
    def __init__(self, parsed, during=None):
        self.parsed = parsed
        self.during = during
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.during:
            self.during()
        return SimpleNamespace(parsed=self.parsed, provider="fixture", model="fixture-model")


def model_bridge(tmp_path, state, skill, model, *, enabled=True):
    bridge = LifecycleBridge(
        tmp_path, ("default", tmp_path), state=state.state, llm=model,
        get_config=lambda key, default=None: (
            enabled if key == "qualification.allow_model_review" else default
        ),
    )
    controller = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        skill_resolver=lambda _name: skill, ambiguous_observer=bridge._queue_ambiguous,
        timezone_info=lambda: (timezone.utc, "UTC"),
    )
    bridge._controller = controller
    bridge._authorized = lambda expected_owner=None: (
        expected_owner is None or expected_owner == state.owner()
    )
    bridge._get = lambda: controller
    return bridge, controller


def test_q08_exact_bounded_opt_in_text_is_classified_and_recorded(tmp_path, monkeypatch):
    skill, state, _ = environment(tmp_path)
    before = "A" * 16000
    after = "A" * 15999 + "B"
    (skill / "SKILL.md").write_text(before, encoding="utf-8")
    register_snapshot(state, skill)
    model = RecordingModel({"classification": "meaningful"})
    bridge, controller = model_bridge(tmp_path, state, skill, model)
    bridge.admit_turn(platform="cli", session_id="s", task_id="t")
    (skill / "SKILL.md").write_text(after, encoding="utf-8")
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    freeze_clocks(monkeypatch, now)
    controller.on_skill_lifecycle(
        action="edited", provenance="local", skill_name="local",
        session_id="s", task_id="t", at=now,
    )
    bridge.on_session_end(
        platform="cli", session_id="s", task_id="t",
        completed=True, failed=False, interrupted=False,
    )
    payload = model.calls[0]["input"][0]["text"]
    assert before in payload and after in payload
    skill_id = register_snapshot(state, skill)
    assert state.meaningful_refinement_count(skill_id, since=now.isoformat()) == 1


def test_q08_oversize_or_revoked_review_never_reaches_model(tmp_path):
    skill, state, _ = environment(tmp_path)
    (skill / "SKILL.md").write_text("A" * 16001, encoding="utf-8")
    register_snapshot(state, skill)
    model = RecordingModel({"classification": "meaningful"})
    bridge, controller = model_bridge(tmp_path, state, skill, model)
    bridge.admit_turn(platform="cli", session_id="s", task_id="t")
    (skill / "SKILL.md").write_text("B" * 16001, encoding="utf-8")
    controller.on_skill_lifecycle(
        action="edited", provenance="local", skill_name="local", session_id="s", task_id="t",
    )
    bridge.on_session_end(
        platform="cli", session_id="s", task_id="t", completed=True,
        failed=False, interrupted=False,
    )
    assert model.calls == []

    (skill / "SKILL.md").write_text("short before", encoding="utf-8")
    register_snapshot(state, skill)
    bridge.admit_turn(platform="cli", session_id="s2", task_id="t2")
    (skill / "SKILL.md").write_text("short after", encoding="utf-8")
    controller.on_skill_lifecycle(
        action="edited", provenance="local", skill_name="local", session_id="s2", task_id="t2",
    )
    bridge.get_config = lambda _key, default=None: default
    bridge.on_session_end(
        platform="cli", session_id="s2", task_id="t2", completed=True,
        failed=False, interrupted=False,
    )
    assert model.calls == []


@pytest.mark.parametrize("parsed", [{"classification": "maybe"}, {}, None])
def test_q08_invalid_model_output_is_conservative(tmp_path, monkeypatch, parsed):
    skill, state, _ = environment(tmp_path)
    register_snapshot(state, skill)
    model = RecordingModel(parsed)
    bridge, controller = model_bridge(tmp_path, state, skill, model)
    bridge.admit_turn(platform="cli", session_id="s", task_id="t")
    (skill / "SKILL.md").write_text("Materially different instructions.\n", encoding="utf-8")
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    freeze_clocks(monkeypatch, now)
    controller.on_skill_lifecycle(
        action="edited", provenance="local", skill_name="local",
        session_id="s", task_id="t", at=now,
    )
    bridge.on_session_end(
        platform="cli", session_id="s", task_id="t", completed=True,
        failed=False, interrupted=False,
    )
    skill_id = register_snapshot(state, skill)
    assert model.calls
    assert state.meaningful_refinement_count(skill_id, since=now.isoformat()) == 0
    assert state.due_stability_jobs("9999-01-01T00:00:00+00:00") == []


@pytest.mark.parametrize("stale", ["owner", "content", "unload"])
def test_q08_in_flight_stale_result_is_discarded(tmp_path, monkeypatch, stale):
    skill, state, _ = environment(tmp_path)
    register_snapshot(state, skill)
    if stale == "owner":
        def during():
            state.bind_owner({**OWNER, "account_id": "mallory"})
    elif stale == "content":
        def during():
            (skill / "SKILL.md").write_text("changed in flight", encoding="utf-8")
    else:
        def during():
            return None
    model = RecordingModel({"classification": "meaningful"}, during=during)
    bridge, controller = model_bridge(tmp_path, state, skill, model)
    bridge.admit_turn(platform="cli", session_id="s", task_id="t")
    (skill / "SKILL.md").write_text("Candidate semantic edit.\n", encoding="utf-8")
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    freeze_clocks(monkeypatch, now)
    controller.on_skill_lifecycle(
        action="edited", provenance="local", skill_name="local",
        session_id="s", task_id="t", at=now,
    )
    if stale == "unload":
        bridge.close()
    bridge.on_session_end(
        platform="cli", session_id="s", task_id="t", completed=True,
        failed=False, interrupted=False,
    )
    assert len(model.calls) == (0 if stale == "unload" else 1)
    assert not any(
        row["classification"] == "meaningful" for row in state._load()["refinements"]
    )


def test_q09_duplicate_callbacks_and_session_end_are_idempotent(tmp_path, monkeypatch):
    skill, state, _ = environment(tmp_path)
    controller = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        timezone_info=lambda: (timezone.utc, "UTC"), skill_resolver=lambda _name: skill,
    )
    monday = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7, 8):
        facts = {
            "action": "loaded", "provenance": "local", "skill_name": "local",
            "at": monday + timedelta(days=offset),
        }
        first = controller.on_skill_lifecycle(**facts)
        second = controller.on_skill_lifecycle(**facts)
        assert second == first
    assert len(candidate_rows(state)) == 1
    skill_id = candidate_rows(state)[0]["skill_id"]
    before = state.usage_summary(
        skill_id, start="2026-08-03", end="2026-08-11", timezone_name="UTC",
    )
    freeze_clocks(monkeypatch, monday + timedelta(days=9))
    assert controller.on_session_end(completed=True) == []
    assert controller.on_session_end(completed=True) == []
    assert len(candidate_rows(state)) == 1
    assert state.usage_summary(
        skill_id, start="2026-08-03", end="2026-08-11", timezone_name="UTC",
    ) == before


def test_q09_session_end_evaluates_due_refinement_once_without_fabricating_use(
    tmp_path, monkeypatch,
):
    skill, state, args = environment(tmp_path)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    record_successful_use("local", at=start, **args)
    for index in range(3):
        add_support_change(
            skill, state, args, monkeypatch, index=index,
            at=start + timedelta(hours=index + 1),
        )
    controller = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        timezone_info=lambda: (timezone.utc, "UTC"), skill_resolver=lambda _name: skill,
    )
    skill_id = register_snapshot(state, skill)
    usage_before = state.usage_summary(
        skill_id, start="2026-08-03", end="2026-08-11", timezone_name="UTC",
    )
    freeze_clocks(monkeypatch, start + timedelta(days=7, hours=3))
    first = controller.on_session_end(completed=True)
    second = controller.on_session_end(completed=True)
    assert first == [candidate_rows(state)[0]["event_id"]]
    assert second == []
    assert candidate_rows(state)[0]["qualification"] == "refinement"
    assert state.usage_summary(
        skill_id, start="2026-08-03", end="2026-08-11", timezone_name="UTC",
    ) == usage_before


@pytest.mark.parametrize("source", ["managed", "shared", "remote"])
def test_q10_nonlocal_sources_are_ignored(tmp_path, source):
    skill, state, _ = environment(tmp_path)
    controller = QualificationController(
        store=state, skills_root=skill.parent, authorized=lambda: True,
        skill_resolver=lambda _name: skill,
    )
    for _ in range(7):
        assert controller.on_skill_lifecycle(
            action="loaded", provenance=source, skill_name="local",
        ) is None
    assert state._load()["skills"] == {}


@pytest.mark.parametrize("excluded", ["_org", "_wisdom", ".archive", ".hub"])
def test_q10_excluded_roots_cannot_qualify(tmp_path, excluded):
    skill = tmp_path / "skills" / excluded / "local"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Local\n")
    state = QualificationState(JsonState(tmp_path), tmp_path)
    state.bind_owner(OWNER)
    assert record_successful_use(
        "local", store=state, skills_root=tmp_path / "skills", authorized=lambda: True,
        skill_resolver=lambda _name: skill, timezone_info=(timezone.utc, "UTC"),
    ) is None
    assert state._load()["skills"] == {}


def test_q10_outside_root_and_unsafe_tree_are_rejected(tmp_path):
    _, state, args = environment(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\n")
    assert record_successful_use(
        "outside", **{**args, "skill_resolver": lambda _name: outside},
    ) is None
    unsafe = tmp_path / "skills" / "unsafe"
    unsafe.mkdir()
    (unsafe / "SKILL.md").write_text("# Unsafe\n")
    (unsafe / "escape").symlink_to(outside / "SKILL.md")
    assert record_successful_use(
        "unsafe", **{**args, "skill_resolver": lambda _name: unsafe},
    ) is None
    assert state._load()["skills"] == {}
