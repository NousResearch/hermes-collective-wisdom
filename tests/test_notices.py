"""Best-effort end-turn notices without delivery queues or history injection."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from installed_test_support import JsonState
from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.notices import EndTurnNotices, suppression_key
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.local_state import QualificationState


@dataclass
class Page:
    events: list
    next_cursor: str | None
    has_more: bool


class Service:
    def __init__(
        self, pages=None, *, fail=False, candidates=None, policy=None, preferences=None
    ):
        self.pages = list(pages or [])
        self.fail = fail
        self.candidates = list(candidates or [])
        self.policy = policy if policy is not None else {
            "new_skill": True,
            "update_available": True,
            "skill_ready_to_share": True,
        }
        self.preferences = preferences if preferences is not None else {
            "muted": False,
            "suppressed": {},
        }
        self.calls = []

    def notice_policy(self):
        self.calls.append("policy")
        return self.policy

    def notice_preferences(self, references):
        self.calls.append(("preferences", references))
        return self.preferences

    def notice_feed(self, cursor):
        self.calls.append(("feed", cursor))
        if self.fail:
            raise RuntimeError("offline")
        return self.pages.pop(0)

    def notice_candidates(self):
        self.calls.append("candidates")
        return self.candidates


def notices(tmp_path: Path, service: Service, now: list[float]) -> EndTurnNotices:
    state = DirectState(JsonState(tmp_path), tmp_path)
    return EndTurnNotices(state, lambda: service, clock=lambda: now[0])


def event(identity="event-1", version=2):
    return {
        "event_id": identity,
        "category": "new_skill",
        "skill_id": "skill-1",
        "skill_name": "Useful skill",
        "version": version,
    }


def test_missing_renderer_is_successful_noop_before_network_or_state(tmp_path: Path):
    service = Service()
    result = notices(tmp_path, service, [1000]).run(renderer=None)
    assert result == {"supported": False, "offered": 0, "polled": False}
    assert not service.calls
    assert not (tmp_path / "state.json").exists()


def test_poll_consumes_one_page_and_attempt_is_marked_before_render(tmp_path: Path):
    service = Service([Page([event()], "cursor-1", True)])
    now = [1000.0]
    seen = []
    client = notices(tmp_path, service, now)

    def render(items):
        snapshot = DirectState(JsonState(tmp_path), tmp_path).notice_state()
        seen.append((items, snapshot))

    result = client.run(renderer=render)
    assert result == {"supported": True, "offered": 1, "polled": True, "more": True}
    assert seen[0][0][0]["event_id"] == "event-1"
    assert "feed:event-1" in seen[0][1]["offered"]
    assert seen[0][1]["cursor"] == "cursor-1"
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None)
    ]


def test_feed_cursor_waits_until_every_row_in_an_oversized_page_is_consumed(tmp_path: Path):
    rows = [event(f"event-{index}") for index in range(9)]
    service = Service([
        Page(rows, "cursor-1", True),
        Page(rows, "cursor-1", True),
    ])
    clock = [1000.0]
    shown = []
    client = notices(tmp_path, service, clock)

    first = client.run(renderer=shown.extend)
    checkpoint = DirectState(JsonState(tmp_path), tmp_path).notice_state()
    assert first["offered"] == 8
    assert checkpoint["cursor"] is None
    assert checkpoint["has_more"] is True

    clock[0] += 601
    second = client.run(renderer=shown.extend)
    checkpoint = DirectState(JsonState(tmp_path), tmp_path).notice_state()
    assert second["offered"] == 1
    assert checkpoint["cursor"] == "cursor-1"
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None),
        ("feed", None),
    ]


def test_bounded_feed_batch_reserves_room_for_a_new_local_candidate(tmp_path: Path):
    candidate = {
        "local_skill_id": "local-fresh",
        "content_hash": "sha256:fresh",
        "category": "skill_ready_to_share",
    }
    service = Service(
        [Page([event(f"event-{index}") for index in range(12)], "cursor-1", True)],
        candidates=[candidate],
    )
    shown = []

    result = notices(tmp_path, service, [1000.0]).run(renderer=shown.extend)

    assert result["offered"] == 8
    assert len(shown) == 8
    assert candidate in shown


def test_suppressed_rows_do_not_starve_later_eligible_row_in_same_page(tmp_path: Path):
    rows = [event(f"event-{index}") for index in range(9)]
    suppressed = {f"feed:event-{index}": "later" for index in range(8)}
    service = Service(
        [Page(rows, "cursor-1", False)],
        preferences={"muted": False, "suppressed": suppressed},
    )
    shown = []

    result = notices(tmp_path, service, [1000.0]).run(renderer=shown.extend)

    assert result["offered"] == 1
    assert [row["event_id"] for row in shown] == ["event-8"]
    assert DirectState(JsonState(tmp_path), tmp_path).notice_state()["cursor"] == "cursor-1"


def test_failed_feed_preserves_cursor_and_never_breaks_turn(tmp_path: Path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.finish_notice_poll(
        cursor="saved", has_more=False, identity=state.identity(), keys=[], now=0,
    )
    service = Service(fail=True)
    now = [2000.0]
    result = EndTurnNotices(state, lambda: service, clock=lambda: now[0]).run(
        renderer=lambda _items: (_ for _ in ()).throw(AssertionError("must not render"))
    )
    assert result["offered"] == 0
    assert result["error"] == "RuntimeError"
    assert state.notice_state()["cursor"] == "saved"


def test_poll_interval_and_owner_local_dedup_prevent_repeated_notice(tmp_path: Path):
    service = Service([Page([event()], "cursor-1", False), Page([event()], "cursor-2", False)])
    now = [1000.0]
    rendered = []
    client = notices(tmp_path, service, now)
    client.run(renderer=rendered.extend)
    assert client.run(renderer=rendered.extend)["polled"] is False
    now[0] += 601
    client.run(renderer=rendered.extend)
    assert [item["event_id"] for item in rendered] == ["event-1"]


def test_file_backed_offer_marker_survives_end_turn_runner_restart(tmp_path: Path):
    service = Service([
        Page([event()], "cursor-1", False),
        Page([event()], "cursor-2", False),
    ])
    rendered = []

    notices(tmp_path, service, [1000.0]).run(renderer=rendered.extend)
    restarted = notices(tmp_path, service, [1600.0])
    result = restarted.run(renderer=rendered.extend)

    assert result["offered"] == 0
    assert [item["event_id"] for item in rendered] == ["event-1"]


def test_feed_poll_is_due_at_exactly_600_seconds(tmp_path: Path):
    service = Service([
        Page([event("first")], "cursor-1", False),
        Page([event("second")], "cursor-2", False),
    ])
    now = [1000.0]
    client = notices(tmp_path, service, now)

    assert client.run(renderer=lambda _rows: None)["polled"] is True
    now[0] = 1599.999
    assert client.run(renderer=lambda _rows: None)["polled"] is False
    now[0] = 1600.0
    assert client.run(renderer=lambda _rows: None)["polled"] is True
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None),
        ("feed", "cursor-1"),
    ]


def test_notice_payload_is_only_given_to_renderer_not_history(tmp_path: Path):
    service = Service([Page([event()], "cursor-1", False)])
    history = [{"role": "user", "content": "hello"}]
    rendered = []
    notices(tmp_path, service, [1000]).run(
        renderer=rendered.extend, conversation_history=history
    )
    assert rendered and history == [{"role": "user", "content": "hello"}]


def test_clock_failure_is_isolated_before_service_or_state_work(tmp_path: Path):
    calls = []

    def broken_clock():
        raise ArithmeticError("clock unavailable")

    result = EndTurnNotices(
        DirectState(JsonState(tmp_path), tmp_path),
        lambda: calls.append("service") or Service(),
        clock=broken_clock,
    ).run(renderer=lambda _rows: None)

    assert result == {
        "supported": True,
        "offered": 0,
        "polled": False,
        "error": "ArithmeticError",
    }
    assert not calls
    assert not (tmp_path / "state.json").exists()


def test_cursor_decode_failure_preserves_previous_cursor(tmp_path: Path):
    class BrokenCursorPage:  # pylint: disable=too-few-public-methods
        events = [event()]
        has_more = True

        @property
        def next_cursor(self):
            raise ValueError("malformed cursor")

    state = DirectState(JsonState(tmp_path), tmp_path)
    state.finish_notice_poll(
        cursor="saved", has_more=False, identity=state.identity(), keys=[], now=0,
    )
    service = Service([BrokenCursorPage()])

    result = EndTurnNotices(state, lambda: service, clock=lambda: 1000.0).run(
        renderer=lambda _rows: None
    )

    assert result["error"] == "ValueError"
    assert state.notice_state()["cursor"] == "saved"


def test_uncertain_renderer_failure_is_marked_and_never_resent(tmp_path: Path):
    service = Service([
        Page([event()], "cursor-1", False),
        Page([event()], "cursor-2", False),
    ])
    clock = [1000.0]
    client = notices(tmp_path, service, clock)
    attempts = []

    def uncertain_renderer(rows):
        attempts.extend(rows)
        raise TimeoutError("send acknowledgement lost")

    first = client.run(renderer=uncertain_renderer)
    assert first["error"] == "TimeoutError"
    assert DirectState(JsonState(tmp_path), tmp_path).notice_state()["offered"] == {
        "feed:event-1": 1000.0
    }

    clock[0] += 600
    second = client.run(renderer=attempts.extend)
    assert second["offered"] == 0
    assert [row["event_id"] for row in attempts] == ["event-1"]


def test_new_qualified_revision_is_offered_without_model_assessment(tmp_path: Path):
    candidate = {
        "local_skill_id": "local-1",
        "content_hash": "sha256:new",
        "category": "skill_ready_to_share",
        "skill_name": "Local helper",
        "qualification": "high_usage",
    }
    service = Service([Page([], None, False)], candidates=[candidate])
    rendered = []
    result = notices(tmp_path, service, [1000]).run(renderer=rendered.extend)
    assert result["offered"] == 1
    assert rendered == [candidate]


def test_real_qualified_candidate_shape_can_be_offered(tmp_path):
    host = JsonState(tmp_path)
    local = QualificationState(host, tmp_path)
    local.bind_owner({'account_id': 'alice', 'organization_id': 'org'})
    local.emit_candidate(skill_id='local-1', content_hash='sha256:source',
                         qualification='high_usage', payload={})
    rows = [{**row, 'category': 'skill_ready_to_share'} for row in local.candidates()]
    service = Service([Page([], None, False)], candidates=rows)
    shown = []
    result = EndTurnNotices(DirectState(host, tmp_path), lambda: service,
                           clock=lambda: 1000).run(renderer=shown.extend)
    assert result.get('error') is None, result
    assert len(shown) == 1


def test_suppression_lookup_uses_existing_wire_identity():
    owner = {'account_id': 'alice', 'organization_id': 'org'}
    calls = []
    service: Any = InvocationService.__new__(InvocationService)
    service._installation_owner = lambda: owner
    service.client = SimpleNamespace(
        recommendation_mute=lambda: SimpleNamespace(org_id='org', muted=False),
        recommendation_suppressions=lambda keys: calls.append(keys) or [],
    )
    service.notice_preferences([{'kind': 'skill', 'skill_id': 'skill-1',
                                'version': 2, 'notice_key': 'feed:event'}])
    expected = 'sha256:' + hashlib.sha256(json.dumps(
        ['wisdom-suppression-v1', 'skill', 'skill-1', 2], separators=(',', ':')
    ).encode()).hexdigest()
    assert calls == [[expected]]


def test_candidate_suppression_identity_is_bound_to_exact_content_hash():
    reference = {"kind": "candidate", "content_hash": "sha256:revision-b"}
    expected = "sha256:" + hashlib.sha256(json.dumps(
        ["wisdom-suppression-v1", "candidate", "sha256:revision-b"],
        separators=(",", ":"),
    ).encode()).hexdigest()

    assert suppression_key(reference) == expected


def test_suppressing_candidate_notice_keeps_manual_candidate_review_available(tmp_path: Path):
    host = JsonState(tmp_path)
    local = QualificationState(host, tmp_path)
    local.bind_owner({"account_id": "alice", "organization_id": "org"})
    local.emit_candidate(
        skill_id="local-1",
        content_hash="sha256:source",
        qualification="high_usage",
        payload={"skill_name": "Local helper"},
    )
    candidate = {**local.candidates()[0], "category": "skill_ready_to_share"}
    notice_key = f"candidate:{candidate['local_skill_id']}:{candidate['content_hash']}"
    service = Service(
        [Page([], None, False)],
        candidates=[candidate],
        preferences={"muted": False, "suppressed": {notice_key: "later"}},
    )

    result = EndTurnNotices(
        DirectState(host, tmp_path), lambda: service, clock=lambda: 1000.0
    ).run(renderer=lambda _rows: None)

    assert result["offered"] == 0
    assert local.candidates()[0]["state"] == "unread"
    assert local.candidates()[0]["local_skill_id"] == "local-1"


@pytest.mark.parametrize(
    ("policy", "preferences"),
    [
        (
            {"new_skill": False, "update_available": True, "skill_ready_to_share": True},
            {"muted": False, "suppressed": {}},
        ),
        (
            {"new_skill": True, "update_available": True, "skill_ready_to_share": True},
            {"muted": True, "suppressed": {}},
        ),
        (
            {"new_skill": True, "update_available": True, "skill_ready_to_share": True},
            {"muted": False, "suppressed": {"feed:event-1": "later"}},
        ),
    ],
    ids=["category-policy", "organization-mute", "item-suppression"],
)
def test_policy_mute_and_suppression_each_independently_block_rendering(
    tmp_path: Path, policy, preferences
):
    service = Service(
        [Page([event()], "cursor-1", False)],
        policy=policy,
        preferences=preferences,
    )
    shown = []

    result = notices(tmp_path, service, [1000.0]).run(renderer=shown.extend)

    assert result["offered"] == 0
    assert not shown
    assert DirectState(JsonState(tmp_path), tmp_path).notice_state()["cursor"] == "cursor-1"


def test_notice_preserves_exact_review_targets_without_performing_actions(tmp_path: Path):
    update = {
        **event("update-event", version=7),
        "category": "update_available",
        "skill_id": "remote-update",
    }
    publication = {
        **event("publication-event", version=3),
        "skill_id": "remote-new",
    }
    candidate = {
        "local_skill_id": "local-candidate",
        "content_hash": "sha256:exact-revision",
        "category": "skill_ready_to_share",
    }
    service = Service(
        [Page([publication, update], "cursor-1", False)],
        candidates=[candidate],
    )
    shown = []

    result = notices(tmp_path, service, [1000.0]).run(renderer=shown.extend)

    assert result["offered"] == 3
    assert [(row.get("skill_id"), row.get("version")) for row in shown[:2]] == [
        ("remote-new", 3),
        ("remote-update", 7),
    ]
    assert shown[2]["local_skill_id"] == "local-candidate"
    assert shown[2]["content_hash"] == "sha256:exact-revision"
    assert [
        call if isinstance(call, str) else call[0] for call in service.calls
    ] == ["feed", "candidates", "policy", "preferences"]


def test_separate_profile_state_allows_owner_local_best_effort_offers(tmp_path: Path):
    rendered = []
    for profile in (tmp_path / "device-a", tmp_path / "device-b"):
        service = Service([Page([event()], "cursor-1", False)])
        notices(profile, service, [1000.0]).run(renderer=rendered.extend)

    assert [row["event_id"] for row in rendered] == ["event-1", "event-1"]


def test_previously_offered_candidates_do_not_starve_new_revision(tmp_path):
    rows = [{'local_skill_id': f'local-{i}', 'content_hash': f'sha256:{i}',
             'category': 'skill_ready_to_share'} for i in range(9)]
    service = Service([Page([], None, False), Page([], None, False)], candidates=rows)
    clock = [1000.0]
    shown = []
    client = EndTurnNotices(DirectState(JsonState(tmp_path), tmp_path), lambda: service,
                           clock=lambda: clock[0])
    client.run(renderer=shown.extend)
    clock[0] += 601
    client.run(renderer=shown.extend)
    assert 'local-8' in [row['local_skill_id'] for row in shown]


def test_changed_account_does_not_reuse_another_owners_cursor(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({'account_id': 'alice', 'organization_id': 'first'})
    state.finish_notice_poll(
        cursor='first-org-cursor', has_more=False, identity=state.identity(), keys=[], now=0,
    )
    state.set_identity({'account_id': 'bob', 'organization_id': 'second'})
    assert state.notice_state()['cursor'] is None


def test_account_change_during_feed_discards_old_results(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({'account_id': 'alice'})
    service = Service()

    def switched_feed(cursor):
        del cursor
        state.set_identity({'account_id': 'bob'})
        return Page([event()], 'alice-cursor', False)

    service.notice_feed = switched_feed
    shown = []
    EndTurnNotices(state, lambda: service).run(renderer=shown.extend)
    assert not shown
    assert state.notice_state()['cursor'] is None
    assert state.notice_state()['offered'] == {}


def test_account_change_after_marking_prevents_rendering_stale_content(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({'account_id': 'alice'})
    original_finish = state.finish_notice_poll

    def finish_then_switch(**kwargs):
        selected = original_finish(**kwargs)
        state.set_identity({'account_id': 'bob'})
        return selected

    state.finish_notice_poll = finish_then_switch
    shown = []
    result = EndTurnNotices(
        state,
        lambda: Service([Page([event()], 'alice-cursor', False)]),
        clock=lambda: 1000.0,
    ).run(renderer=shown.extend)

    assert not shown
    assert result['offered'] == 0
    assert result['error'] == 'WisdomAuthError'
    assert state.notice_state()['cursor'] is None


@pytest.mark.parametrize("mode", ["agent", "fixed", "invalid"])
@pytest.mark.parametrize("frequency", ["immediate", "off"])
def test_notice_policy_respects_current_profile_settings(mode, frequency):
    owner = {'account_id': 'alice', 'organization_id': 'org'}
    service: Any = InvocationService.__new__(InvocationService)
    service._installation_owner = lambda: owner
    service.notification_config = lambda: {'notifications': {
        'delivery_mode': mode, 'new_skills': frequency,
        'updates': frequency, 'team_published': 'immediate',
    }}
    service.client = SimpleNamespace(agent_led_policy=lambda: SimpleNamespace(
        org_id='org', notification_defaults=SimpleNamespace(
            teammate_published=True, update_available=True, skill_ready_to_share=True,
        ),
    ))
    policy = service.notice_policy()
    enabled = mode == 'agent'
    assert policy['new_skill'] is (enabled and frequency == 'immediate')
    assert policy['update_available'] is (enabled and frequency == 'immediate')
    assert policy['skill_ready_to_share'] is enabled


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("new_skills", {"new_skill": False, "update_available": True,
                        "skill_ready_to_share": True}),
        ("team_published", {"new_skill": False, "update_available": True,
                            "skill_ready_to_share": True}),
        ("updates", {"new_skill": True, "update_available": False,
                     "skill_ready_to_share": True}),
    ],
)
def test_notice_category_settings_are_independent(setting, expected):
    owner = {'account_id': 'alice', 'organization_id': 'org'}
    settings = {
        'delivery_mode': 'agent',
        'new_skills': 'immediate',
        'updates': 'immediate',
        'team_published': 'immediate',
    }
    settings[setting] = 'off'
    service: Any = InvocationService.__new__(InvocationService)
    service._installation_owner = lambda: owner
    service.notification_config = lambda: {'notifications': settings}
    service.client = SimpleNamespace(agent_led_policy=lambda: SimpleNamespace(
        org_id='org', notification_defaults=SimpleNamespace(
            teammate_published=True, update_available=True, skill_ready_to_share=True,
        ),
    ))

    assert service.notice_policy() == expected


def test_new_candidate_is_checked_between_feed_polls(tmp_path):
    service = Service([Page([], None, False)])
    clock = [1000.0]
    client = notices(tmp_path, service, clock)
    shown = []
    client.run(renderer=shown.extend)
    service.candidates.append({
        'local_skill_id': 'fresh', 'content_hash': 'sha256:fresh',
        'category': 'skill_ready_to_share',
    })
    clock[0] += 1
    result = client.run(renderer=shown.extend)
    assert result['polled'] is False
    assert [row['local_skill_id'] for row in shown] == ['fresh']
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == 'feed'] == [
        ('feed', None)
    ]
