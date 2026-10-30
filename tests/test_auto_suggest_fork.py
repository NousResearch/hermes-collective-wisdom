"""Automatic share suggestions on a host with plugin session forks (ctx.fork_session).

The fake host follows core's API: ``fork_session(token, prompt, *, tools, max_iterations,
input_budget, task, timeout)`` returns a handle (``done``, ``outcome``, ``result``,
``actions``, ``wait``, ``cancel``) or None, and tool handlers inside the fork receive
``turn_origin="plugin_fork:<plugin id>"``.
"""
# pylint: disable=missing-function-docstring,protected-access,too-many-instance-attributes

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_auto_suggest import DUE, SLACK_DM, YES, _Fake

from hermes_wisdom_plugin.auto_suggest import (
    CAPABILITY, FORK_CAPABILITY, FORK_TASK, TOOL, AutoSuggest,
    approval_prompt, host_forks, host_supports, local_card, review_due,
)

ORIGIN = "plugin_fork:collective-wisdom"


class _Handle:
    """Core's PluginForkHandle surface."""

    def __init__(self, outcome: str, actions: tuple[str, ...]):
        self.outcome = outcome
        self.actions = actions
        self.result = "Done."
        self.done = True
        self.cancelled_by_plugin = False

    def wait(self, timeout=None):  # pylint: disable=unused-argument
        return self.done

    def cancel(self):
        self.cancelled_by_plugin = True


class _Host(_Fake):
    """The side-question host of test_auto_suggest plus a scripted session fork."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.granted = {FORK_CAPABILITY, CAPABILITY}
        self.facts["incident"]["content_hash"] = "sha-i"
        self.facts["deploy"] = {"name": "deploy", "description": "Ship a release.", "uses": 9,
                                "edits": 2, "content_hash": "sha-d", "shared": False,
                                "top": False}
        self.forks: list[dict] = []
        self.fork_script: list[dict] = [{"skill": "incident", "reason": "Teams review often."}]
        self.fork_results: list = []
        self.fork_outcome = "ok"
        self.fork_returns_none = False
        self.handles: list[_Handle] = []
        self.handle_done = True
        self.origin = ORIGIN  # What core passes the plugin's tool inside the fork.
        self.live: AutoSuggest | None = None
        self.injected_during_fork = -1  # Injections seen while the fork was still running.

    def fork_session(  # pylint: disable=too-many-arguments
        self, token, prompt, *, tools=(), max_iterations=8, input_budget=None, task=None,
        timeout=None,
    ):
        self.forks.append({"token": token, "prompt": prompt, "tools": tuple(tools),
                           "max_iterations": max_iterations, "input_budget": input_budget,
                           "task": task, "timeout": timeout})
        if self.fork_returns_none:
            return None
        assert self.live is not None
        actions = []
        for args in self.fork_script:  # The fork's model calls the whitelisted tool.
            actions.append(TOOL)
            try:
                self.fork_results.append(json.loads(self.live.tool(
                    dict(args), turn_origin=self.origin, session_id="s1", task_id="t")))
            except PermissionError as error:
                self.fork_results.append(error)
        self.injected_during_fork = len(self.injected)
        handle = _Handle(self.fork_outcome, tuple(actions))
        handle.done = self.handle_done
        self.handles.append(handle)
        return handle

    def build(self, *, fork=True, side_question=True, **changes):
        self.live = self.controller(
            side_question=self.side_question if side_question else None,
            fork_session=self.fork_session if fork else None, fork_task=FORK_TASK,
            has_capability=lambda name: name in self.granted, **changes,
        )
        return self.live


def _learn(controller, name="incident", *, action="created", session_id="s1"):
    controller.on_skill_lifecycle(action=action, skill_name=name, provenance="agent_created",
                                  session_id=session_id)


def _turn(controller, *, signals=None, tools=0, platform="cli", token="tok"):
    for _ in range(tools):
        controller.on_tool_call(session_id="s1", tool_name="terminal")
    facts = {"session_id": "s1", "side_question_token": token, "platform": platform}
    if signals is not None:
        facts["review_signals"] = signals
    controller.on_llm_call(**facts)


def _events(host):
    return [(row["event"], row["skill"]) for row in host.store.snapshot()["decisions"]]


def test_fork_is_chosen_over_side_question_with_only_the_wisdom_tool(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _learn(controller, "deploy", action="patched")
    _turn(controller, signals=DUE)
    assert not host.asked, "the side question is only the fallback"
    assert len(host.forks) == 1
    fork = host.forks[0]
    assert fork["token"] == "tok" and fork["tools"] == (TOOL,)
    assert fork["task"] == FORK_TASK and 1 <= fork["max_iterations"] <= 4
    assert fork["timeout"] and fork["timeout"] <= 1800
    assert "incident" in fork["prompt"] and "deploy" in fork["prompt"]
    assert "Review incident notes." in fork["prompt"] and "sha-i" not in fork["prompt"]
    assert host.fork_results[0]["status"] == "suggested"
    assert host.injected == [(approval_prompt("incident"), "user", {})]


def test_the_suggestion_is_delivered_after_the_fork_ends(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert host.fork_results[0]["status"] == "suggested"
    assert host.injected_during_fork == 0 and len(host.injected) == 1


@pytest.mark.parametrize("outcome", ["cancelled", "timeout"])
def test_a_suggestion_made_before_the_fork_was_interrupted_is_still_delivered(tmp_path, outcome):
    host = _Host(tmp_path)
    host.fork_outcome = outcome
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert len(host.injected) == 1 and host.store.seen("incident", "sha-i")


def test_fork_suggestion_shows_the_same_card_as_before(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    shown = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
    assert shown["status"] == "shown" and shown["executed"] is False
    latest = host.store.latest("incident")
    assert latest is not None and shown["card"] == local_card(latest, "default")
    assert "Hermes's reason: Teams review often." in shown["card"]
    assert _events(host) == [
        ("candidate", "incident"), ("asked", "incident"), ("verdict", "incident"),
        ("injected", "incident"), ("shown", "incident"),
    ]


def test_fork_suggestion_in_a_slack_dm_queues_the_native_card(tmp_path):
    host = _Host(tmp_path)
    host.surface = dict(SLACK_DM)
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE, platform="slack")
    origin = host.injected[0][2]["origin"]
    assert (origin["platform"], origin["chat_id"], origin["user_id"]) == ("slack", "D1", "U1")
    result = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
    assert result["status"] == "queued" and len(host.presented) == 1
    route, session_id, suggestion = host.presented[0]
    assert route["chat_id"] == "D1" and session_id == "s1" and suggestion["skill"] == "incident"


def test_live_agent_and_other_plugins_cannot_suggest(tmp_path):
    host = _Host(tmp_path)
    host.fork_script = []
    controller = host.build()
    with pytest.raises(PermissionError):  # Live agent: nothing pending, nothing proposed.
        controller.tool({"skill": "incident", "reason": "x"}, session_id="s1")
    with pytest.raises(PermissionError):  # A fork, but no check is running.
        controller.tool({"skill": "incident"}, turn_origin=ORIGIN, session_id="s1")
    host.fork_script = [{"skill": "incident"}]
    host.origin = "plugin_fork:other-plugin"
    _learn(controller)
    _turn(controller, signals=DUE)
    assert isinstance(host.fork_results[0], PermissionError)
    assert not host.injected and host.store.latest("incident") is None


def test_a_fork_suggests_at_most_one_skill(tmp_path):
    host = _Host(tmp_path)
    host.fork_script = [{"skill": "incident"}, {"skill": "deploy"}, {"skill": "incident"}]
    controller = host.build()
    _learn(controller)
    _learn(controller, "deploy")
    _turn(controller, signals=DUE)
    assert host.fork_results[0]["status"] == "suggested"
    assert all(isinstance(item, PermissionError) for item in host.fork_results[1:])
    assert len(host.injected) == 1 and "incident" in host.injected[0][0]


def test_fork_can_only_suggest_a_skill_it_was_asked_about(tmp_path):
    host = _Host(tmp_path)
    host.fork_script = [{"skill": "unrelated"}, {"skill": "incident"}]
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert isinstance(host.fork_results[0], PermissionError)
    assert host.fork_results[1]["status"] == "suggested"


def test_fork_that_suggests_nothing_records_no_for_every_skill(tmp_path):
    host = _Host(tmp_path)
    host.fork_script = []
    controller = host.build()
    _learn(controller)
    _learn(controller, "deploy")
    _turn(controller, signals=DUE)
    assert not host.injected
    assert host.store.seen("incident", "sha-i") and host.store.seen("deploy", "sha-d")
    verdicts = [row for row in host.store.snapshot()["decisions"] if row["event"] == "verdict"]
    assert {row["skill"] for row in verdicts} == {"incident", "deploy"}
    assert not any(row["share"] for row in verdicts)
    _learn(controller, action="patched")
    _turn(controller, signals=DUE)
    assert len(host.forks) == 1, "the same revision is never asked about twice"


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "budget_exhausted", "refused:x"])
def test_interrupted_fork_judges_nothing_and_asks_again_later(tmp_path, outcome):
    host = _Host(tmp_path)
    host.fork_script = []
    host.fork_outcome = outcome
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert not host.store.seen("incident", "sha-i")
    assert ("not_asked", "incident") in _events(host)
    host.fork_outcome = "ok"
    _turn(controller, signals=DUE)
    assert len(host.forks) == 2


def test_a_fork_still_deferred_after_the_wait_is_cancelled(tmp_path):
    host = _Host(tmp_path)
    host.fork_script = []
    host.fork_outcome = "deferred"
    host.handle_done = False
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert host.handles[0].cancelled_by_plugin
    assert ("not_asked", "incident") in _events(host) and not controller._busy


def test_fork_refused_by_the_host_falls_back_to_the_side_question(tmp_path):
    host = _Host(tmp_path)
    host.fork_returns_none = True  # An older or differently configured core.
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert len(host.forks) == 1 and len(host.asked) == 1  # Same turn, same token.
    assert host.asked[0][0] == "tok"
    assert host.injected == [(approval_prompt("incident"), "user", {})]
    assert controller.mode() == "side_question"


def test_after_a_refused_fork_later_turns_use_the_side_question(tmp_path):
    host = _Host(tmp_path)
    host.fork_returns_none = True
    host.answer = None  # The refused fork spent the token: no answer this turn.
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert len(host.forks) == 1 and len(host.asked) == 1 and not host.injected
    assert not host.store.seen("incident", "sha-i")  # Still pending, not a "no".
    assert ("not_asked", "incident") in _events(host)
    host.answer = YES
    host.fork_returns_none = False  # Not asked again in this process.
    _turn(controller, signals=DUE, token="tok2")
    assert len(host.forks) == 1 and len(host.asked) == 2
    assert host.injected == [(approval_prompt("incident"), "user", {})]


def test_a_new_process_tries_the_fork_again(tmp_path):
    host = _Host(tmp_path)
    host.fork_returns_none = True
    first = host.build()
    _learn(first)
    _turn(first, signals=DUE)
    host.fork_returns_none = False
    assert host.build().mode() == "fork"


def test_refused_fork_without_the_side_question_grant_asks_again_later(tmp_path):
    host = _Host(tmp_path)
    host.granted = {FORK_CAPABILITY}
    host.fork_returns_none = True
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert not host.injected and not host.asked
    host.fork_returns_none = False
    _turn(controller, signals=DUE)
    assert len(host.forks) == 2 and len(host.injected) == 1


@pytest.mark.parametrize(("signals", "fires"), [
    (DUE, True),
    ({**DUE, "skill_review_due": False}, False),
    ({**DUE, "background_review_enabled": False}, False),
    ({**DUE, "skill_nudge_interval": 0}, False),
    ({**DUE, "skill_review_due": "yes"}, False),
    ({}, False),
])
def test_review_signals_decide_the_cadence(tmp_path, signals, fires):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=signals, tools=50)  # The plugin's own counter is not used.
    assert bool(host.forks) is fires
    assert review_due(signals) is fires


def test_review_signals_replace_the_plugin_counter_and_config_read(tmp_path):
    host = _Host(tmp_path)
    host.interval = 0  # Would turn the counter path off; the signals win.
    calls = []
    controller = host.build(self_learning=lambda: calls.append(1) or 0)
    _learn(controller)
    _turn(controller, signals=DUE, tools=0)
    assert len(host.forks) == 1 and not calls


def test_without_review_signals_the_counter_still_applies(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _turn(controller, tools=2)
    assert not host.forks
    _turn(controller, tools=1)
    assert len(host.forks) == 1


def test_falls_back_to_side_question_without_the_fork_capability(tmp_path):
    host = _Host(tmp_path)
    host.granted = {CAPABILITY}
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert not host.forks and len(host.asked) == 1
    assert host.injected == [(approval_prompt("incident"), "user", {})]


def test_side_question_host_without_forks_follows_the_review_signals(tmp_path):
    host = _Host(tmp_path)
    controller = host.build(fork=False)
    _learn(controller)
    _turn(controller, signals={**DUE, "skill_review_due": False}, tools=50)
    assert not host.asked
    _turn(controller, signals=DUE)
    assert len(host.asked) == 1


@pytest.mark.parametrize(("granted", "fork", "side_question"), [
    (set(), True, True),
    ({FORK_CAPABILITY}, False, True),
    ({CAPABILITY}, True, False),
])
def test_off_when_no_granted_method_exists(tmp_path, granted, fork, side_question):
    host = _Host(tmp_path)
    host.granted = granted
    controller = host.build(fork=fork, side_question=side_question)
    _learn(controller)
    _turn(controller, signals=DUE)
    assert controller.mode() is None
    assert not host.forks and not host.asked and not host.injected


def test_auto_suggest_false_and_daily_limit_still_gate_the_fork(tmp_path):
    host = _Host(tmp_path)
    host.settings["auto_suggest"] = False
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    assert not host.forks
    host.settings.clear()
    host.settings["auto_suggest_daily_limit"] = 0
    _turn(controller, signals=DUE)
    assert not host.forks


def test_closed_controller_refuses_the_fork_tool(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    controller._offer = {"session_id": "s1", "route": {}, "suggested": None, "closed": False,
                         "skills": {"incident": host.facts["incident"]},
                         "expires_at": host.clock() + 60}
    controller.close()
    with pytest.raises(PermissionError):
        controller.tool({"skill": "incident"}, turn_origin=ORIGIN, session_id="s1")


def test_host_detection():
    def probe(_name):
        return True

    def method(*_args, **_kwargs):
        return None

    plain = SimpleNamespace(has_capability=probe)
    assert not host_supports(plain) and not host_forks(plain)
    side = SimpleNamespace(has_capability=probe, side_question=method)
    assert host_supports(side) and not host_forks(side)
    forking = SimpleNamespace(has_capability=probe, fork_session=method)
    assert host_supports(forking) and host_forks(forking)
    assert not host_supports(SimpleNamespace(fork_session=method, side_question=method))
