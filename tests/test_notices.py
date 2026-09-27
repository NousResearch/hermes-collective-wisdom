"""Best-effort end-turn notices without delivery queues or history injection."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access
# pylint: disable=too-many-lines

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from installed_test_support import JsonState
from hermes_wisdom_plugin.client import AgentLedPolicyResponse, VersionDetail
from hermes_wisdom_plugin.discovery import Discovery
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomNotFound, WisdomUnavailable
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
    # pylint: disable=too-many-instance-attributes
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
        return {"recommendation_mode": "promote_all", **self.policy}

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

    def notice_version(self, skill_id, version):
        self.calls.append(("version", skill_id, version))
        return {"skill": {"id": skill_id, "state": "active", "takedown_generation": 0},
                "version": {"version": version, "author_description": "Helps with reports",
                            "published_by_user_id": "publisher-1"}}


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


def test_inbox_does_not_consume_human_notice_across_controllers(tmp_path):
    service = Service([Page([event()], "after-event", False),
                       Page([event()], "after-event", False),
                       Page([event()], "after-event", False)])
    assert notices(tmp_path, service, [1000]).run(renderer=None)["polled"]
    assert notices(tmp_path, service, [1001]).run(renderer=None)["offered"] == 0
    shown = []
    assert notices(tmp_path, service, [1002]).run(renderer=shown.extend)["offered"] == 1
    assert [row["event_id"] for row in shown] == ["event-1"]
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None), ("feed", "after-event"), ("feed", None)]

def test_old_rendered_tombstone_cannot_erase_newer_awareness(tmp_path):
    service = Service([Page([event("old", 2)], "old", True),
                       Page([{**event("archive", None), "kind": "archived"},
                             {**event("restored", 3), "kind": "restored"}], "new", False),
                       Page([{**event("archive", None), "kind": "archived"}], "old", True)])
    assert notices(tmp_path, service, [1000]).run(renderer=None)["more"]
    assert notices(tmp_path, service, [1001]).run(renderer=None)["more"] is False
    assert notices(tmp_path, service, [1002]).run(renderer=lambda _: None)["polled"]
    assert DirectState(JsonState(tmp_path), tmp_path).known_publications()[0]["version"] == 3

def test_nullable_nonpublication_versions_do_not_stall_or_offer(tmp_path):
    rows = [{**event("restore"), "kind": "restored", "version": None},
            {**event("installation"), "kind": "installation_updated", "version": None},
            event("published")]
    service = Service([Page(rows, "done", False)])
    shown = []
    assert notices(tmp_path, service, [1000]).run(renderer=shown.extend)["offered"] == 1
    assert [row["event_id"] for row in shown] == ["published"]
    assert DirectState(JsonState(tmp_path), tmp_path).known_publications()[0]["version"] == 2

def test_muted_inbox_preserves_human_cursor_and_owner_reset(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({"account_id": "alice", "organization_id": "org-1"})
    service = Service([Page([event()], "after", False), Page([event()], "after", False)],
                      preferences={"muted": True, "suppressed": {}})
    assert EndTurnNotices(state, lambda: service).run(renderer=None)["polled"]
    shown = []
    assert EndTurnNotices(DirectState(JsonState(tmp_path), tmp_path), lambda: service).run(
        renderer=shown.extend)["offered"] == 0
    assert not shown
    assert state.notice_state()["cursor"] == "after"
    assert state.notice_state()["awareness_cursor"] == "after"
    assert state.known_publications()[0]["version"] == 2
    state.set_identity({"account_id": "bob", "organization_id": "org-2"})
    assert state.notice_state()["cursor"] is None
    assert state.notice_state()["awareness_cursor"] is None
    assert not state.known_publications()

def test_awareness_error_preserves_both_independent_checkpoints(tmp_path):
    service = Service([Page([event()], "after", False)])
    runner = notices(tmp_path, service, [1000])
    assert runner.run(renderer= lambda _: None)["offered"] == 1
    failing = Service([Page([event("next", 3)], "later", False)])
    failing.notice_version = lambda *_: (_ for _ in ()).throw(WisdomUnavailable(status=503))
    failed = EndTurnNotices(runner.state, lambda: failing).run(renderer=None)
    assert failed["error"] == "WisdomUnavailable"
    assert runner.state.notice_state()["cursor"] == "after"
    assert runner.state.notice_state()["awareness_cursor"] == "after"

def test_contradictory_revision_state_is_rejected(tmp_path):
    service = Service([Page([event()], "next", False)])
    service.notice_version = lambda skill_id, version: {
        "skill": {"id": skill_id, "state": "active"},
        "version": {"version": version, "state": "draft"}}
    result = notices(tmp_path, service, [1000]).run(renderer=None)
    assert result["error"] == "WisdomAuthError"
    assert DirectState(JsonState(tmp_path), tmp_path).notice_state()["cursor"] is None

def test_real_version_envelope_through_client_discovery_and_invocation(tmp_path):
    payload = {"skill": {"id": "skill-1", "state": "active", "takedown_generation": 0},
               "version": {"version": 2, "author_description": "Report helper",
                           "published_by_user_id": "publisher-" + "x" * 210}}
    wire = VersionDetail.model_validate(payload)
    owner = {"account_id": "alice", "organization_id": "org-1"}
    client = SimpleNamespace(list_skills=lambda: None, skill=lambda _: None,
                             version=lambda skill_id, number: wire)
    discovery = Discovery(client, lambda: owner, "https://portal.example", "org-1")
    real = object.__new__(InvocationService)
    real._installation_owner = lambda: dict(owner)
    real.__dict__["_discovery"] = lambda: discovery
    service = Service([Page([event()], "done", False)])
    service.notice_version = real.notice_version
    state = notices(tmp_path, service, [1000]).state
    assert EndTurnNotices(state, lambda: service).run(renderer=None)["more"] is False
    assert state.known_publications()[0]["publisher"] == payload["version"]["published_by_user_id"]
    assert state.known_publications()[0]["description"] == "Report helper"
    assert state.known_publications()[0]["slug"] == ""

@pytest.mark.parametrize(("response", "error"), [
    (WisdomNotFound("missing", status=404), None),
    (WisdomAuthError("forbidden", status=403), "WisdomAuthError"),
    (VersionDetail.model_validate({"skill": {"id": "other", "state": "active"},
                                   "version": {"version": 2}}), "WisdomValidationError"),
])
def test_exact_read_discovery_errors_preserve_or_consume_checkpoint(tmp_path, response, error):
    def wire_version(_skill_id, _number):
        if isinstance(response, Exception):
            raise response
        return response

    owner = {"account_id": "alice", "organization_id": "org-1"}
    client = SimpleNamespace(list_skills=lambda: None, skill=lambda _: None,
                             version=wire_version)
    discovery = Discovery(client, lambda: dict(owner), "https://portal.example", "org-1")
    real = object.__new__(InvocationService)
    real._installation_owner = lambda: dict(owner)
    real.__dict__["_discovery"] = lambda: discovery
    service = Service([Page([event()], "done", False)])
    service.notice_version = real.notice_version
    runner = notices(tmp_path, service, [1000])
    result = runner.run(renderer=None)
    if error:
        assert result["error"] == error
        assert runner.state.notice_state()["awareness_cursor"] is None
    else:
        assert "error" not in result
        assert runner.state.notice_state()["awareness_cursor"] == "done"
    assert not runner.state.known_publications()

def test_not_now_deferral_suppresses_the_feed_version_not_new_versions(tmp_path: Path):
    owner = {"account_id": "account-1", "organization_id": "org-1"}
    stored = {}

    def suppress(key):
        stored[key] = "2027-01-01T00:00:00+00:00"
        return SimpleNamespace(org_id="org-1", key=key, suppress_until=stored[key])

    client = SimpleNamespace(
        agent_led_policy=lambda: AgentLedPolicyResponse.model_construct(
            org_id="org-1", not_now_suppression_days=30,
        ),
        suppress_recommendation=suppress,
        recommendation_mute=lambda: SimpleNamespace(org_id="org-1", muted=False),
        recommendation_suppressions=lambda keys: [
            SimpleNamespace(key=key, suppress_until=stored[key])
            for key in keys if key in stored
        ],
    )
    real = object.__new__(InvocationService)
    real.__dict__["client"] = client
    real.native_owner = lambda: dict(owner)
    real._installation_owner = lambda: dict(owner)
    plan = {"operation": "install", "owner": owner, "skill_id": "skill-1", "version": 2}
    prepared = real.prepare_review_deferral(plan)
    assert real.defer_review(prepared, may_apply=lambda: True)["preference_sync"] == "confirmed"

    class ReofferService(Service):
        def notice_preferences(self, references):
            return real.notice_preferences(references)

    service = ReofferService([Page([event("repeat", 2), event("repeat-again", 2),
                                   event("updated", 3)], "next", False)])
    rendered = []
    result = notices(tmp_path, service, [1000]).run(renderer=rendered.extend)
    assert result["offered"] == 1
    assert [item["event_id"] for item in rendered] == ["updated"]


def test_missing_renderer_is_successful_noop_before_network_or_state(tmp_path: Path):
    service = Service([Page([], "cursor-1", False)])
    result = notices(tmp_path, service, [1000]).run(renderer=None)
    assert result["supported"] is False
    assert result["polled"] is True
    assert service.calls[0] == ("feed", None)

def test_known_publication_survives_mute_renderer_absence_and_restart(tmp_path):
    service = Service([Page([event()], "cursor-1", False)],
                      preferences={"muted": True, "suppressed": {}})
    first = notices(tmp_path, service, [1000])
    assert first.run(renderer=None)["offered"] == 0
    state = DirectState(JsonState(tmp_path), tmp_path)
    assert state.notice_state()["awareness_cursor"] == "cursor-1"
    assert state.known_publications()[0]["description"] == "Helps with reports"
    assert state.known_publications()[0]["inspect_reference"] == "skill-1@v2"
    restarted = notices(tmp_path, Service([Page([], "cursor-2", False)]), [1001])
    assert restarted.state.known_publications() == state.known_publications()
    assert state.known_publications()[0]["publisher"] == "publisher-1"
    assert state.known_publications()[0]["title"] == ""
    assert state.known_publications()[0]["slug"] == ""


def test_exact_version_editorial_metadata_is_projected_without_creator_fallback(tmp_path):
    service = Service([Page([event()], "done", False)])
    original = service.notice_version

    def editorial(skill_id, version):
        facts = original(skill_id, version)
        facts["version"]["title"] = "Incident report helper"
        facts["version"]["published_by_user_id"] = "alice"
        return facts

    service.notice_version = editorial
    runner = notices(tmp_path, service, [1000])
    assert runner.run(renderer=None)["more"] is False
    row = runner.state.known_publications()[0]
    assert row["title"] == "Incident report helper"
    assert row["publisher"] == "alice"


def test_projection_failure_does_not_advance_cursor(tmp_path):
    service = Service([Page([event()], "cursor-1", True)])
    service.notice_version = lambda *_: (_ for _ in ()).throw(RuntimeError("offline"))
    result = notices(tmp_path, service, [1000]).run(renderer=None)
    assert result["error"] == "RuntimeError"
    state = DirectState(JsonState(tmp_path), tmp_path)
    assert state.notice_state()["cursor"] is None
    assert not state.known_publications()

def test_renderer_free_catch_up_pages_and_fails_closed_on_foreign_org(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({"owner": {"organization_id": "org-1", "account_id": "alice"}})
    service = Service([Page([event("first")], "after-first", True),
                       Page([event("second", 3)], "after-second", False)])
    runner = EndTurnNotices(state, lambda: service, clock=lambda: 1000)
    assert runner.run(renderer=None)["more"] is True
    assert state.notice_state()["awareness_cursor"] == "after-first"
    assert runner.run(renderer=None)["more"] is False
    assert state.known_publications()[0]["version"] == 3
    assert state.known_publications()[0]["team"] == ""
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None), ("feed", "after-first")]

    foreign = Service([Page([event("foreign", 4)], "foreign-cursor", False)])
    foreign.notice_version = lambda skill_id, version: {
        "skill": {"id": skill_id, "state": "active",
                  "organization_id": "org-2"},
        "version": {"version": version}}
    assert EndTurnNotices(state, lambda: foreign).run(renderer=None)["error"] == "WisdomAuthError"
    assert state.notice_state()["awareness_cursor"] == "after-second"
    assert state.known_publications()[0]["version"] == 3
    state.set_identity({"owner": {"organization_id": "org-2", "account_id": "bob"}})
    assert not state.known_publications()
    assert state.notice_state()["cursor"] is None


def test_mixed_tombstones_and_authenticated_missing_versions_across_pages(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({"owner": {"organization_id": "org-1"}})
    missing = {("old", 2), ("new-missing", 2)}
    service = Service([
        Page([{**event("old"), "skill_id": "old"},
              {**event("fresh"), "skill_id": "fresh"}], "page-one", True),
        Page([{**event("removed"), "kind": "taken_down", "skill_id": "fresh"},
              {**event("missing"), "skill_id": "new-missing"},
              {**event("newer", 3), "skill_id": "old"}], "page-two", False),
    ])
    original = service.notice_version

    def revision(skill_id, version):
        if (skill_id, version) in missing:
            raise WisdomNotFound("Gateway skill version not found", status=404)
        return original(skill_id, version)

    service.notice_version = revision
    runner = EndTurnNotices(state, lambda: service)
    assert runner.run(renderer=None)["more"] is True
    assert [row["skill_id"] for row in state.known_publications()] == ["fresh"]
    assert runner.run(renderer=None)["more"] is False
    assert state.notice_state()["awareness_cursor"] == "page-two"
    assert [row["inspect_reference"] for row in state.known_publications()] == ["old@v3"]



@pytest.mark.parametrize("failure", [
    WisdomAuthError("denied", status=403), WisdomUnavailable(status=503),
    WisdomNotFound("opaque miss"), ValueError("malformed metadata"),
])
def test_unknown_metadata_failure_retains_existing_facts_and_checkpoint(tmp_path, failure):
    first = Service([Page([{**event(), "skill_id": "old"}], "page-one", True)])
    state = notices(tmp_path, first, [1000]).state
    assert EndTurnNotices(state, lambda: first).run(renderer=None)["more"] is True
    old = state.known_publications()
    second = Service([Page([{**event("next"), "skill_id": "new"}], "page-two", False)])

    def unavailable(skill_id, version):
        del skill_id, version
        raise failure

    second.notice_version = unavailable
    result = EndTurnNotices(state, lambda: second).run(renderer=None)
    assert result["error"] == type(failure).__name__
    assert result["more"] is True
    assert state.notice_state()["awareness_cursor"] == "page-one"
    assert state.known_publications() == old
    retry = Service([Page([{**event("next"), "skill_id": "new"}], "page-two", False)])
    assert EndTurnNotices(state, lambda: retry).run(renderer=None)["more"] is False
    assert {row["skill_id"] for row in state.known_publications()} == {"old", "new"}


def test_stale_checkpoint_cannot_replace_newer_projection(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.finish_notice_poll(identity=None, cursor="new", has_more=False, keys=[],
                             now=1, expected_cursor=None,
                             publications=[{"skill_id": "new", "version": 3,
                                            "state": "published"}])
    with pytest.raises(Exception, match="checkpoint changed"):
        state.finish_notice_poll(identity=None, cursor="stale", has_more=False,
                                 keys=[], now=2, expected_cursor=None,
                                 publications=[{"skill_id": "new", "version": 2,
                                                "state": "published"}])
    assert state.notice_state()["cursor"] == "new"
    assert state.known_publications()[0]["version"] == 3


def test_interrupted_page_commits_no_partial_publication_facts(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    rows = [{**event("first"), "skill_id": "first"},
            {**event("second"), "skill_id": "second"}]
    service = Service([Page(rows, "after", False)])
    original = service.notice_version

    def broken(skill_id, version):
        if skill_id == "second":
            raise WisdomUnavailable(status=503)
        return original(skill_id, version)

    service.notice_version = broken
    result = EndTurnNotices(state, lambda: service).run(renderer=None)
    assert result["error"] == "WisdomUnavailable"
    assert state.notice_state()["cursor"] is None
    assert not state.known_publications()
    service = Service([Page(rows, "after", False)])
    assert EndTurnNotices(state, lambda: service).run(renderer=None)["more"] is False
    assert {row["skill_id"] for row in state.known_publications()} == {"first", "second"}


def test_exact_version_read_checks_owner_even_on_gateway_404():
    real = object.__new__(InvocationService)
    owner = [{"account_id": "alice", "organization_id": "org-1"}]
    real._installation_owner = lambda: dict(owner[0])
    calls = []

    def version(skill_id, number):
        calls.append((skill_id, number))
        owner[0] = {"account_id": "bob", "organization_id": "org-2"}
        raise WisdomNotFound("Gateway version missing", status=404)

    real.__dict__["_discovery"] = lambda: SimpleNamespace(version=version)
    with pytest.raises(WisdomAuthError, match="owner changed"):
        real.notice_version("skill-1", 2)
    assert calls == [("skill-1", 2)]


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

    clock[0] += 1
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
    service.fail = False
    service.pages.append(Page([event()], "recovered", False))
    shown = []
    recovered = EndTurnNotices(state, lambda: service, clock=lambda: now[0]).run(
        renderer=shown.extend
    )
    assert recovered["polled"] is True
    assert recovered["offered"] == 1
    assert state.notice_state()["cursor"] == "recovered"


def test_each_turn_polls_but_owner_local_dedup_prevents_repeated_notice(tmp_path: Path):
    service = Service([Page([event()], "cursor-1", False), Page([event()], "cursor-2", False)])
    now = [1000.0]
    rendered = []
    client = notices(tmp_path, service, now)
    client.run(renderer=rendered.extend)
    assert client.run(renderer=rendered.extend)["polled"] is True
    assert [item["event_id"] for item in rendered] == ["event-1"]


def test_file_backed_offer_marker_survives_end_turn_runner_restart(tmp_path: Path):
    service = Service([
        Page([event()], "cursor-1", False),
        Page([event()], "cursor-2", False),
    ])
    rendered = []

    notices(tmp_path, service, [1000.0]).run(renderer=rendered.extend)
    restarted = notices(tmp_path, service, [1000.0])
    result = restarted.run(renderer=rendered.extend)

    assert result["offered"] == 0
    assert [item["event_id"] for item in rendered] == ["event-1"]


@pytest.mark.parametrize("saved_deadline", [None, 999999.0])
def test_new_event_is_offered_on_next_turn_after_empty_poll(tmp_path: Path, saved_deadline):
    service = Service([
        Page([], "cursor-1", False),
        Page([event("fresh")], "cursor-2", False),
    ])
    now = [1000.0]
    client = notices(tmp_path, service, now)
    if saved_deadline is not None:
        with client.state.change() as value:
            value["notices"]["next_poll_at"] = saved_deadline
    shown = []

    assert client.run(renderer=shown.extend)["polled"] is True
    assert not shown
    now[0] += 1
    result = client.run(renderer=shown.extend)
    assert result["polled"] is True
    assert result["offered"] == 1
    assert [row["event_id"] for row in shown] == ["fresh"]
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == "feed"] == [
        ("feed", None),
        ("feed", "cursor-1"),
    ]


def test_overlapping_poll_is_skipped_and_next_turn_is_not_delayed(tmp_path: Path):
    second_service = Service([Page([event()], "cursor-2", False)])
    second = notices(tmp_path, second_service, [1000.0])
    first_service = Service()
    shown = []

    def overlapping_feed(cursor):
        assert cursor is None
        result = second.run(renderer=shown.extend)
        assert result["polled"] is False
        assert not second_service.calls
        return Page([event()], "cursor-1", False)

    first_service.notice_feed = overlapping_feed
    first = notices(tmp_path, first_service, [1000.0])
    assert first.run(renderer=shown.extend)["offered"] == 1
    assert second.run(renderer=shown.extend)["polled"] is True
    assert [row["event_id"] for row in shown] == ["event-1"]
    assert second.state.notice_state()["cursor"] == "cursor-2"


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

    clock[0] += 1
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


def test_candidate_not_now_uses_exact_existing_suppression_policy():
    owner = {"account_id": "alice", "organization_id": "org"}
    calls = []
    service: Any = InvocationService.__new__(InvocationService)
    service.native_owner = lambda: owner
    service.native_candidate_inspect = lambda event_id: {
        "event_id": event_id, "content_hash": "sha256:revision-b"
    }
    service.client = SimpleNamespace(
        agent_led_policy=lambda: {
            "org_id": "org",
            "usage_evidence_window_days": 30,
            "min_aggregate_invocations": 5,
            "consecutive_day_usage_counts": True,
            "repeated_edits_count": True,
            "max_recommendations_per_user_per_week": 3,
            "publication_mode": "moderated",
            "install_popularity_threshold": 10,
            "manager_review_email_cadence": "daily",
            "not_now_suppression_days": 14,
            "version": 1,
            "updated_by_user_id": None,
            "notification_defaults": {
                "teammate_published": True,
                "update_available": True,
                "skill_ready_to_share": True,
            },
        },
        suppress_recommendation=lambda key: calls.append(key) or SimpleNamespace(
            suppress_until="later"
        ),
    )

    result = service.defer_candidate("candidate-1", may_apply=lambda: True)

    assert calls == [suppression_key({
        "kind": "candidate", "content_hash": "sha256:revision-b"
    })]
    assert result == {
        "deferred": True,
        "event_id": "candidate-1",
        "not_now_suppression_days": 14,
        "preference_sync": "confirmed",
        "suppress_until": "later",
    }


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
    ] == ["feed", "version", "version", "candidates", "policy", "preferences"]


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
    clock[0] += 1
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
        org_id='org', recommendation_mode='promote_all', notification_defaults=SimpleNamespace(
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
        org_id='org', recommendation_mode='promote_all', notification_defaults=SimpleNamespace(
            teammate_published=True, update_available=True, skill_ready_to_share=True,
        ),
    ))

    assert service.notice_policy() == {"recommendation_mode": "promote_all", **expected}


def test_new_candidate_is_checked_on_the_next_turn(tmp_path):
    service = Service([Page([], None, False), Page([], None, False)])
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
    assert result['polled'] is True
    assert [row['local_skill_id'] for row in shown] == ['fresh']
    assert [call for call in service.calls if isinstance(call, tuple) and call[0] == 'feed'] == [
        ('feed', None), ('feed', None)
    ]
