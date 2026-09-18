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
from hermes_wisdom_plugin.notices import EndTurnNotices
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.local_state import QualificationState


@dataclass
class Page:
    events: list
    next_cursor: str | None
    has_more: bool


class Service:
    def __init__(self, pages=None, *, fail=False, candidates=None):
        self.pages = list(pages or [])
        self.fail = fail
        self.candidates = list(candidates or [])
        self.calls = []

    def notice_policy(self):
        self.calls.append("policy")
        return {"new_skill": True, "update_available": True, "skill_ready_to_share": True}

    def notice_preferences(self, references):
        self.calls.append(("preferences", references))
        return {"muted": False, "suppressed": {}}

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


def test_notice_payload_is_only_given_to_renderer_not_history(tmp_path: Path):
    service = Service([Page([event()], "cursor-1", False)])
    history = [{"role": "user", "content": "hello"}]
    rendered = []
    notices(tmp_path, service, [1000]).run(
        renderer=rendered.extend, conversation_history=history
    )
    assert rendered and history == [{"role": "user", "content": "hello"}]


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
