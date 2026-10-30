"""Automatic share suggestions: cadence gates, the side-question verdict and delivery."""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import json

import pytest
from installed_test_support import JsonState

from hermes_wisdom_plugin.auto_suggest import (
    CAPABILITY, AutoSuggest, SuggestionStore, approval_prompt, local_card, parse_verdict,
    self_learning_interval, verdict_prompt,
)

YES = '{"share": true, "reason": "Teams run incident reviews often."}'
SLACK_DM = {"platform": "slack", "chat_type": "dm", "chat_id": "D1", "user_id": "U1",
            "session_key": "slack:W1:D1:U1", "scope_id": "W1"}


class _Clock:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class _Fake:  # pylint: disable=too-many-instance-attributes
    """Every dependency of the controller, recorded."""

    def __init__(self, tmp_path):
        self.clock = _Clock()
        self.store = SuggestionStore(JsonState(tmp_path), tmp_path, clock=self.clock)
        self.answer = YES
        self.asked = []
        self.injected = []
        self.inject_result = True
        self.capability = True
        self.settings = {}
        self.interval = 3
        self.surface = {"source": "cli"}
        self.facts = {"incident": {"name": "incident", "description": "Review incident notes.",
                                   "uses": 4, "edits": 1, "content_hash": "sha-1",
                                   "shared": False, "top": False}}
        self.presented = []

    def side_question(self, token, prompt, *, timeout):
        self.asked.append((token, prompt, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def inject(self, content, role="user", **target):
        self.injected.append((content, role, target))
        return self.inject_result

    def present(self, route, session_id, suggestion):
        self.presented.append((route, session_id, suggestion))
        return {"state": "queued", "review_id": "r-1"}

    def controller(self, **changes):
        values = {
            "store": self.store, "side_question": self.side_question,
            "has_capability": lambda name: name == CAPABILITY and self.capability,
            "inject": self.inject, "setting": self.settings.get,
            "self_learning": lambda: self.interval, "candidate": self.facts.get,
            "session": lambda: self.surface, "native_available": lambda _platform: True,
            "present": self.present, "start": lambda work: work(), "clock": self.clock,
        }
        values.update(changes)
        return AutoSuggest(**values)


def _learn(controller, name="incident", *, action="created", provenance="agent_created",
           session_id="s1"):
    controller.on_skill_lifecycle(action=action, skill_name=name, provenance=provenance,
                                  session_id=session_id)


def _turn(controller, *, tools=3, token: str | None = "tok", platform="cli", session_id="s1"):
    for _ in range(tools):
        controller.on_tool_call(session_id=session_id, tool_name="terminal")
    controller.on_llm_call(session_id=session_id, side_question_token=token, platform=platform)


def _events(fake):
    return [row["event"] for row in fake.store.snapshot()["decisions"]]


def test_yes_injects_one_approval_turn_into_the_owners_session(tmp_path):
    fake = _Fake(tmp_path)
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    assert len(fake.asked) == 1 and fake.asked[0][0] == "tok"
    assert "incident" in fake.asked[0][1] and "Review incident notes." in fake.asked[0][1]
    assert fake.injected == [(approval_prompt("incident"), "user", {})]
    assert _events(fake) == ["candidate", "asked", "verdict", "injected"]


@pytest.mark.parametrize("gate", [
    "self_learning_off", "auto_suggest_false", "no_token", "no_capability",
    "interval_not_reached", "suppressed", "team_installed", "hub_skill", "group_chat",
    "already_shared", "no_lifecycle_fact",
])
def test_cadence_gates_ask_nothing(tmp_path, gate):  # pylint: disable=too-many-branches
    fake = _Fake(tmp_path)
    tools, token, platform = 3, "tok", "cli"
    lifecycle = {}
    if gate == "self_learning_off":
        fake.interval = 0
    elif gate == "auto_suggest_false":
        fake.settings["auto_suggest"] = False
    elif gate == "no_token":
        token = None
    elif gate == "no_capability":
        fake.capability = False
    elif gate == "interval_not_reached":
        tools = 2
    elif gate == "suppressed":
        fake.store.set_suppressed("incident", True, source="test")
    elif gate == "team_installed":
        lifecycle = {"name": "acme:incident"}
    elif gate == "hub_skill":
        lifecycle = {"provenance": "hub"}
    elif gate == "group_chat":
        platform = "slack"
        fake.surface = {**SLACK_DM, "chat_type": "channel", "chat_id": "C1"}
    elif gate == "already_shared":
        fake.facts["incident"]["shared"] = True
    controller = fake.controller()
    if gate != "no_lifecycle_fact":
        _learn(controller, **lifecycle)
    _turn(controller, tools=tools, token=token, platform=platform)
    assert not fake.asked and not fake.injected


def test_daily_cap_allows_one_suggestion_per_day(tmp_path):
    fake = _Fake(tmp_path)
    fake.facts["deploy"] = {**fake.facts["incident"], "name": "deploy", "content_hash": "sha-d"}
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    _learn(controller, "deploy")
    _turn(controller)
    assert len(fake.injected) == 1
    fake.clock.now += 86_400
    _turn(controller)
    assert len(fake.injected) == 2 and "deploy" in fake.injected[1][0]


def test_the_same_revision_is_never_asked_twice(tmp_path):
    fake = _Fake(tmp_path)
    fake.answer = '{"share": false, "reason": "personal"}'
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    _learn(controller, action="patched")
    _turn(controller)
    assert len(fake.asked) == 1
    fake.facts["incident"]["content_hash"] = "sha-2"  # A later edit may be suggested.
    _learn(controller, action="patched")
    _turn(controller)
    assert len(fake.asked) == 2


def test_used_skill_needs_the_top_skill_bar(tmp_path):
    fake = _Fake(tmp_path)
    controller = fake.controller()
    _learn(controller, action="loaded")
    _turn(controller)
    assert not fake.asked
    fake.facts["incident"]["top"] = True
    _learn(controller, action="loaded")
    _turn(controller)
    assert len(fake.asked) == 1


def test_denied_injection_dismisses_nothing_and_waits_a_day(tmp_path):
    fake = _Fake(tmp_path)
    fake.inject_result = False
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    assert not fake.store.seen("incident", "sha-1")
    assert fake.store.snapshot()["day"]["blocked"] is True
    _learn(controller, action="patched")
    _turn(controller)
    assert len(fake.asked) == 1, "no retry within the day"
    fake.clock.now += 86_400
    fake.inject_result = True
    _learn(controller, action="patched")
    _turn(controller)
    assert len(fake.injected) == 2 and fake.injected[1][2] == {}


def test_private_slack_route_targets_the_origin(tmp_path):
    fake = _Fake(tmp_path)
    fake.surface = dict(SLACK_DM)
    controller = fake.controller()
    _learn(controller)
    _turn(controller, platform="slack")
    assert len(fake.injected) == 1
    origin = fake.injected[0][2]["origin"]
    assert (origin["platform"], origin["chat_id"], origin["user_id"]) == ("slack", "D1", "U1")


@pytest.mark.parametrize(("answer", "share"), [
    (YES, True),
    ('Sure! {"share": true, "reason": "useful"} hope that helps', True),
    ('{"share": false, "reason": "personal"}', False),
    ('{"share": "yes"}', False),
    ('{"share": 1}', False),
    ("yes, share it", False),
    ("", False),
    (None, False),
    (TimeoutError("lease expired"), False),
])
def test_verdict_is_yes_only_for_boolean_true(tmp_path, answer, share):
    fake = _Fake(tmp_path)
    fake.answer = answer
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    assert bool(fake.injected) is share
    if not isinstance(answer, Exception):
        assert parse_verdict(answer)[0] is share


def test_verdict_prompt_carries_only_name_description_and_usage():
    prompt = verdict_prompt({"name": "incident", "description": "Review notes.", "uses": 4,
                             "edits": 1, "action": "patched", "content_hash": "secret-hash"})
    assert "incident" in prompt and "Review notes." in prompt and "4 uses, 1 edits" in prompt
    assert "secret-hash" not in prompt and '"share": true or false' in prompt


@pytest.mark.parametrize(("config", "interval"), [
    ({}, 10),
    ({"skills": {"creation_nudge_interval": 4}}, 4),
    ({"skills": {"creation_nudge_interval": 0}}, 0),
    ({"auxiliary": {"background_review": {"enabled": False}}}, 0),
    ({"skills": {"creation_nudge_interval": "bad"}}, 0),
])
def test_self_learning_follows_core_knobs(config, interval):
    assert self_learning_interval(lambda: config) == interval


def test_tool_shows_the_card_once_for_the_pending_suggestion(tmp_path):
    fake = _Fake(tmp_path)
    controller = fake.controller()
    _learn(controller)
    _turn(controller)
    shown = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
    assert shown["status"] == "shown" and shown["executed"] is False
    card = shown["card"]
    for line in ("Confirm share", "Don't share", "Never for this skill"):
        assert line in card
    latest = fake.store.latest("incident")
    assert latest is not None and card == local_card(latest, "default")
    with pytest.raises(PermissionError):
        controller.tool({"skill": "incident"}, session_id="s1")


def test_tool_refuses_without_a_suggestion_or_from_another_session(tmp_path):
    fake = _Fake(tmp_path)
    controller = fake.controller()
    with pytest.raises(PermissionError):
        controller.tool({"skill": "incident"}, session_id="s1")
    _learn(controller)
    _turn(controller)
    with pytest.raises(PermissionError):
        controller.tool({"skill": "incident"}, session_id="other")
    with pytest.raises(PermissionError):
        controller.tool({"skill": "deploy"}, session_id="s1")


def test_tool_queues_the_native_card_in_the_private_chat(tmp_path):
    fake = _Fake(tmp_path)
    fake.surface = dict(SLACK_DM)
    controller = fake.controller()
    _learn(controller)
    _turn(controller, platform="slack")
    result = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
    assert result["status"] == "queued" and len(fake.presented) == 1
    route, session_id, suggestion = fake.presented[0]
    assert route["chat_id"] == "D1" and session_id == "s1" and suggestion["skill"] == "incident"
