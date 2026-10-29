"""Share suggestions: injection path, fallback, private routing and the CLI pointer."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from installed_test_support import JsonState

from hermes_wisdom_plugin import commands, release_scope, top_skills
from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_cli, run_slash
from hermes_wisdom_plugin.runtime import CommandRuntime
from hermes_wisdom_plugin.share_suggest import accepted_text, build_prompt, chat_pointer
from hermes_wisdom_plugin.top_command import refusal
from hermes_wisdom_plugin.top_skills import TopSkill

CONFIG_HINT = "hermes config set plugins.entries.collective-wisdom.allow_gateway_injection true"


def _items(count):
    return tuple(TopSkill(f"skill-{index:02}", f"does thing {index}", 100 - index, index % 3,
                          "2026-10-01T09:00:00+00:00", index == 1) for index in range(count))


class _Service:  # pylint: disable=too-few-public-methods
    def __init__(self, items, state="ok"):
        self.result = top_skills.TopSkills(state, tuple(items))

    def top_skills(self):
        return self.result


def _runtime(  # pylint: disable=too-many-arguments
    tmp_path, monkeypatch, *, inject, invocation=None, pending=False, surface=None,
    items=_items(35), profile="default",
):
    runtime = CommandRuntime(
        tmp_path, (profile, tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_a, **_k: None, inject_message=inject,
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: invocation)
    monkeypatch.setattr(runtime.native, "command_pending", lambda: pending)
    monkeypatch.setattr(top_skills, "session_surface", lambda: surface or {"source": "cli"})
    monkeypatch.setattr(runtime, "_service",
                        lambda: _Service(items, state="ok" if items else "empty"))
    monkeypatch.setattr(runtime, "_close_service", lambda _service: None)
    return runtime


def _reply(result):
    return asyncio.run(result) if inspect.isawaitable(result) else result


class _Inject:  # pylint: disable=too-few-public-methods
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, content, role="user", **target):
        self.calls.append((content, role, target))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_prompt_lists_thirty_ranked_skills_and_keeps_sharing_with_the_user() -> None:
    prompt = build_prompt(_items(35))
    assert "1. skill-00: does thing 0 (uses 100, edits 0, last used 2026-10-01)" in prompt
    assert "30. skill-29" in prompt and "skill-30" not in prompt
    assert "already shared" in prompt.split("2. skill-01")[1].split("\n")[0]
    assert "up to 10" in prompt and "`hermes wisdom share my-skill`" in prompt
    assert "/wisdom share " not in prompt
    assert "may read the skill files" in prompt
    assert "Do not run any share or publish command" in prompt
    assert "cannot share" not in prompt
    assert "`/wisdom share my-skill`" in build_prompt(_items(3), native=True)


_TELEGRAM_DM = SimpleNamespace(platform="telegram", chat_id="42", chat_type="private",
                               thread_id="", actor_id="7", actor_name="", scope_id="")


@pytest.mark.parametrize(("route", "target", "native"), [
    ({"surface": {"source": "cli"}}, {}, False),
    ({"surface": {"source": "tui", "session_key": "agent:main:tui:1"}},
     {"session_key": "agent:main:tui:1"}, False),
    ({"surface": {"platform": "discord", "chat_type": "dm", "chat_id": "9"}},
     {"origin": {"platform": "discord", "chat_type": "dm", "chat_id": "9"}}, False),
    ({"invocation": _TELEGRAM_DM},
     {"origin": {"platform": "telegram", "chat_id": "42", "chat_type": "private",
                 "user_id": "7"}}, True),
])
def test_private_share_suggest_starts_a_normal_turn(  # pylint: disable=too-many-arguments
    tmp_path, monkeypatch, route, target, native,
):
    inject = _Inject(True)
    runtime = _runtime(tmp_path, monkeypatch, inject=inject, **route)
    try:
        reply = _reply(runtime.slash("share-suggest"))
    finally:
        runtime.native.close()
    assert reply == accepted_text(_items(35), native=native)
    assert "If no reply arrives, send Hermes this message:" in reply
    assert len(inject.calls) == 1
    content, role, sent = inject.calls[0]
    assert role == "user" and sent == target
    assert content == build_prompt(_items(35), native=native)


def test_recommended_command_reaches_the_share_flow_on_its_own_surface(tmp_path, monkeypatch):
    """The suggested command must work where it was suggested."""
    local = build_prompt(_items(1))
    assert "`hermes wisdom share my-skill`" in local
    shared = []
    monkeypatch.setattr(commands, "run_share",
                        lambda args, _service: shared.append(args.skill) or 0)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert run_cli(parser.parse_args("wisdom share skill-00".split()[1:]), object()) == 0
    assert shared == ["skill-00"]

    assert "`/wisdom share my-skill`" in build_prompt(_items(1), native=True)
    runtime = _runtime(tmp_path, monkeypatch, inject=None, invocation=_TELEGRAM_DM)
    opened = []
    monkeypatch.setattr(runtime.native, "begin_suggest",
                        lambda _invocation, name: opened.append(name) or {"state": "queued"})
    try:
        assert _reply(runtime.slash("share skill-00")) == ""
    finally:
        runtime.native.close()
    assert opened == ["skill-00"]


@pytest.mark.parametrize("result", [False, None, RuntimeError("host")])
def test_refused_injection_falls_back_to_list_prompt_and_operator_command(
    tmp_path, monkeypatch, result,
):
    inject = None if result is None else _Inject(result)
    runtime = _runtime(tmp_path, monkeypatch, inject=inject,
                       surface={"source": "desktop", "session_key": "sk"})
    try:
        reply = _reply(runtime.slash("share-suggest"))
    finally:
        runtime.native.close()
    assert reply.startswith("Hermes could not start the suggestion turn here.")
    assert "hermes wisdom share <skill> command" in reply
    assert "1. skill-00 | 100 uses | 0 edits | last used 2026-10-01" in reply
    assert "10. skill-09" in reply and "11. skill-10" not in reply
    assert "Send this message to Hermes" in reply and "skill-29" in reply
    assert reply.rstrip().endswith(CONFIG_HINT)


@pytest.mark.parametrize("route", [
    {"pending": True},
    {"surface": {"platform": "telegram", "chat_type": "group", "chat_id": "-100"}},
    {"surface": {"source": "webhook", "session_key": "agent:main:webhook:github"}},
])
def test_group_share_suggest_refuses_without_names_or_a_turn(tmp_path, monkeypatch, route):
    inject = _Inject(True)
    runtime = _runtime(tmp_path, monkeypatch, inject=inject, **route)
    try:
        reply = _reply(runtime.slash("share-suggest"))
    finally:
        runtime.native.close()
    assert reply == refusal("share-suggest") and "skill-" not in reply
    assert not inject.calls
    assert run_slash(parse_slash("share-suggest"), _Service(_items(3))) == refusal("share-suggest")


def test_hosts_without_origin_continue_the_messaging_session(tmp_path, monkeypatch):
    calls = []

    def legacy_inject(content, role="user", *, session_key=None):
        calls.append((role, session_key, content))
        return True

    invocation = SimpleNamespace(platform="slack", chat_id="D1", chat_type="dm", thread_id="",
                                 actor_id="U1", actor_name="", scope_id="T1",
                                 session_key="agent:main:slack:dm:D1")
    runtime = _runtime(tmp_path, monkeypatch, inject=legacy_inject, invocation=invocation)
    try:
        assert _reply(runtime.slash("share-suggest")) == accepted_text(_items(35), native=True)
    finally:
        runtime.native.close()
    assert [(role, key) for role, key, _content in calls] == [("user", "agent:main:slack:dm:D1")]


def test_states_and_usage_never_start_a_turn(tmp_path, monkeypatch):
    inject = _Inject(True)
    runtime = _runtime(tmp_path, monkeypatch, inject=inject, items=())
    try:
        assert "No recorded usage" in _reply(runtime.slash("share-suggest"))
        assert _reply(runtime.slash("share-suggest now")) == "Usage: /wisdom share-suggest"
    finally:
        runtime.native.close()
    assert not inject.calls


def test_cli_share_suggest_prints_the_list_and_chat_pointer(capsys):
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert run_cli(parser.parse_args(["share-suggest"]), _Service(_items(12))) == 0
    out = capsys.readouterr().out
    assert "Your top skills (page 1 of 2, 12 skills)" in out
    assert ["2", "skill-01", "99", "1", "2026-10-01", "Yes"] in [
        line.split() for line in out.splitlines()]
    assert chat_pointer() in out
    assert run_cli(parser.parse_args(["share-suggest", "--json"]), _Service(_items(3))) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next"] == "/wisdom share-suggest" and data["total"] == 3


def test_native_fallback_and_compact_prompt_keep_the_native_command(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, inject=_Inject(False), invocation=_TELEGRAM_DM)
    try:
        reply = _reply(runtime.slash("share-suggest"))
    finally:
        runtime.native.close()
    assert reply.startswith("Hermes could not start the suggestion turn here.")
    assert "/wisdom share <skill> command" in reply and "hermes wisdom share" not in reply
    native_accepted = accepted_text(_items(2), native=True)
    assert "you share a skill with `/wisdom share <skill>` and its review" in native_accepted
    assert "the exact /wisdom share <skill> command" in native_accepted


def test_named_profile_commands_select_that_profile(tmp_path, monkeypatch):
    inject = _Inject(True)
    runtime = _runtime(tmp_path, monkeypatch, inject=inject, profile="work")
    try:
        reply = _reply(runtime.slash("share-suggest"))
    finally:
        runtime.native.close()
    assert "`hermes -p work wisdom share my-skill`" in inject.calls[0][0]
    assert "`hermes -p work wisdom share <skill>`" in reply
    assert "hermes -p work wisdom share <skill> command" in reply
    fallback_runtime = _runtime(tmp_path, monkeypatch, inject=None, profile="work")
    try:
        fallback = _reply(fallback_runtime.slash("share-suggest"))
    finally:
        fallback_runtime.native.close()
    assert fallback.rstrip().endswith(
        "hermes -p work config set plugins.entries.collective-wisdom.allow_gateway_injection true")
    assert "hermes -p work wisdom share <skill>" in chat_pointer("work")
    assert "`hermes wisdom share <skill>`" in chat_pointer("default")


def test_share_suggest_ships_in_every_scope_and_help():
    for scope in (release_scope.FULL, release_scope.MANUAL_SHARING):
        assert scope.allows_cli("share-suggest") and scope.allows_slash("share-suggest")
    assert "/wisdom share-suggest" in run_slash(parse_slash("help"), SimpleNamespace())
