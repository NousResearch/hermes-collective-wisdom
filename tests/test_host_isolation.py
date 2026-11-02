"""Collective Wisdom and plugin isolation (``plugins.isolation: host``).

The isolated plugin host gives ``register(ctx)`` a proxy context. For the ctx methods core
lists in ``HOST_UNSUPPORTED_CTX_METHODS`` (``register_platform_handler``,
``register_telegram_handler`` and, on hosts with session forks, ``fork_session``) the proxy
returns a callable that raises ``PluginHostUnsupported`` (a ``RuntimeError``). A host
without session forks has no ``fork_session`` at all.

Collective Wisdom's native Slack and Telegram cards need the Hermes process, so the host
refuses the plugin; it does so before any suggestion hook exists. Independently, a
``fork_session`` that raises is a refused fork: suggestions fall back to the side question
and manual sharing registers exactly as with a working fork.
"""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from test_auto_suggest import DUE, YES
from test_release_scope import _context, _load_root_plugin, _unload


class PluginHostUnsupported(RuntimeError):
    """Core's ``hermes_cli.plugin_host_wire.PluginHostUnsupported``."""


def _unsupported(name):
    def call(*_args, **_kwargs):
        raise PluginHostUnsupported(f"ctx.{name}() cannot run in the plugin host")
    return call


def _plugin(tmp_path, monkeypatch):
    module_name, plugin = _load_root_plugin()
    runtime_module = importlib.import_module(f"{module_name}.hermes_wisdom_plugin.runtime")
    monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", tmp_path))
    return module_name, plugin


def _ctx(tmp_path, calls, *, fork_session):
    """A context with both suggestion methods, recording what the plugin registers."""
    def ask(token, prompt, *, timeout):
        calls["asked"].append((token, prompt, timeout))
        return YES

    entries, context = _context(tmp_path, plugin_id="collective-wisdom",
                                has_capability=lambda _name: True, side_question=ask,
                                fork_session=fork_session)
    handle = SimpleNamespace(dispose=lambda: None)
    context.register_tool = lambda **definition: (
        calls["handlers"].append(definition["handler"])
        or entries["tools"].append(definition["name"]) or handle)
    context.register_auxiliary_task = lambda key, **_k: entries["auxiliary"].append(key) or handle
    return entries, context


def _calls():
    return {"forks": [], "asked": [], "injected": [], "handlers": []}


@pytest.mark.parametrize("forks", [True, False], ids=["fork-core", "stock-core"])
def test_the_isolated_host_refuses_the_plugin_before_any_suggestion_hook(
        tmp_path, monkeypatch, forks):
    module_name, plugin = _plugin(tmp_path, monkeypatch)
    calls = _calls()
    entries, context = _ctx(tmp_path, calls,
                            fork_session=_unsupported("fork_session") if forks else None)
    if not forks:
        del context.fork_session
    context.register_platform_handler = _unsupported("register_platform_handler")
    context.register_telegram_handler = _unsupported("register_telegram_handler")
    try:
        with pytest.raises(PluginHostUnsupported):
            plugin.register(context)
        assert entries["hooks"] == ["pre_gateway_dispatch"]  # Core unwinds this one.
        assert not entries["tools"] and not entries["auxiliary"] and not calls["forks"]
    finally:
        _unload(module_name)


def _drive_one_due_turn(controller, calls):
    """A created skill, then a due attended CLI turn (dependencies outside the host faked)."""
    controller.candidate = lambda name: {
        "name": name, "description": "Review incident notes.", "uses": 4, "edits": 1,
        "content_hash": "sha-1", "shared": False, "top": False}
    controller.session = lambda: {"source": "cli"}
    controller.profile_current = lambda: True
    controller.start = lambda work: work()
    controller.inject = lambda content, role="user", **target: (
        calls["injected"].append((content, role, target)) or True)
    controller.on_skill_lifecycle(action="created", skill_name="incident",
                                  provenance="agent_created", session_id="s1")
    controller.on_llm_call(session_id="s1", side_question_token="tok", platform="cli",
                           review_signals=dict(DUE))


def test_a_fork_session_that_raises_degrades_to_the_side_question(tmp_path, monkeypatch):
    module_name, plugin = _plugin(tmp_path, monkeypatch)
    calls = _calls()

    def fork_session(*args, **kwargs):
        calls["forks"].append((args, kwargs))
        raise PluginHostUnsupported("ctx.fork_session() cannot run in the plugin host")

    entries, context = _ctx(tmp_path, calls, fork_session=fork_session)
    try:
        plugin.register(context)
        controller = calls["handlers"][0].__self__
        assert controller.mode() == "fork"
        _drive_one_due_turn(controller, calls)
        assert len(calls["forks"]) == 1
        assert [token for token, _prompt, _timeout in calls["asked"]] == ["tok"]
        assert len(calls["injected"]) == 1 and "incident" in calls["injected"][0][0]
        assert controller.mode() == "side_question" and not controller._busy
        shown = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
        assert shown["status"] == "shown" and shown["executed"] is False
        entries["unload"]()
    finally:
        _unload(module_name)


def test_manual_sharing_registers_the_same_with_a_raising_fork_session(tmp_path, monkeypatch):
    seen = []
    for fork_session in (lambda *_a, **_k: None, _unsupported("fork_session")):
        module_name, plugin = _plugin(tmp_path, monkeypatch)
        entries, context = _ctx(tmp_path, _calls(), fork_session=fork_session)
        try:
            plugin.register(context)
            entries["unload"]()
            seen.append({key: value for key, value in entries.items() if key != "unload"})
        finally:
            _unload(module_name)
    assert seen[0] == seen[1]
    assert seen[0]["cli"] == seen[0]["slash"] == "wisdom" and "share" in seen[0]["args_hint"]
    assert seen[0]["slack"] == "slack" and seen[0]["telegram"] is True
