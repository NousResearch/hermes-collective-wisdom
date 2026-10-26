"""``hermes wisdom suggestions`` and the private-only ``/wisdom suggestions``."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import argparse
import asyncio
import inspect
from types import SimpleNamespace

import pytest
from installed_test_support import JsonState

from hermes_wisdom_plugin import top_skills
from hermes_wisdom_plugin.auto_suggest import SUGGESTIONS_USAGE, SuggestionStore
from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_cli, run_slash
from hermes_wisdom_plugin.runtime import CommandRuntime


class _Service:  # pylint: disable=too-few-public-methods
    def __init__(self, root, hashes=None):
        self._state = JsonState(root)
        self.root = root
        self.profile = ("default", root)
        self.plugin_id = "collective-wisdom"
        self.hashes = {"deploy-notes": "sha-1"} if hashes is None else hashes

    def share_candidate(self, name):
        return {"name": name, "content_hash": self.hashes[name]} if name in self.hashes else None

    @staticmethod
    def setup_pending():
        return False


def _cli(argv, service, capsys):
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    code = run_cli(parser.parse_args(argv), service)
    return code, capsys.readouterr().out


def test_cli_never_and_allow_round_trip(tmp_path, capsys) -> None:
    service = _Service(tmp_path)
    store = SuggestionStore(service._state, tmp_path)  # pylint: disable=protected-access
    code, out = _cli(["suggestions", "never", "deploy-notes"], service, capsys)
    assert code == 0 and "won't suggest sharing deploy-notes again" in out
    assert store.suppressed("deploy-notes")
    code, out = _cli(["suggestions"], service, capsys)
    assert code == 0 and "Never suggested: deploy-notes" in out
    assert "/wisdom suggestions allow <skill>" in out
    assert ("hermes config set plugins.entries.collective-wisdom.settings.auto_suggest false"
            in out)
    code, out = _cli(["suggestions", "allow", "deploy-notes"], service, capsys)
    assert code == 0 and "may suggest sharing deploy-notes again" in out
    assert not store.suppressed("deploy-notes")


def test_cli_dismiss_records_the_current_revision_only(tmp_path, capsys) -> None:
    service = _Service(tmp_path)
    store = SuggestionStore(service._state, tmp_path)  # pylint: disable=protected-access
    code, out = _cli(["suggestions", "dismiss", "deploy-notes"], service, capsys)
    assert code == 0 and "won't suggest this version of deploy-notes again" in out
    assert store.seen("deploy-notes", "sha-1") and not store.seen("deploy-notes", "sha-2")
    code, out = _cli(["suggestions", "dismiss", "missing"], service, capsys)
    assert code == 0 and "Couldn't find your local skill missing" in out


def test_cli_rejects_an_unknown_action() -> None:
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["suggestions", "share", "deploy-notes"])


def _runtime(tmp_path, monkeypatch, *, surface, invocation=None, pending=False):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_a, **_k: None,
    )
    service = _Service(tmp_path)
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: invocation)
    monkeypatch.setattr(runtime.native, "command_pending", lambda: pending)
    monkeypatch.setattr(top_skills, "session_surface", lambda: surface)
    monkeypatch.setattr(runtime, "_service", lambda: service)
    monkeypatch.setattr(runtime, "_close_service", lambda _service: None)
    return runtime, service


def _reply(result):
    return asyncio.run(result) if inspect.isawaitable(result) else result


def test_private_slash_records_never(tmp_path, monkeypatch) -> None:
    runtime, service = _runtime(tmp_path, monkeypatch, surface={"source": "cli"})
    try:
        reply = _reply(runtime.slash("suggestions never deploy-notes"))
        listed = _reply(runtime.slash("suggestions"))
        bad = _reply(runtime.slash("suggestions share deploy-notes"))
    finally:
        runtime.native.close()
    assert "won't suggest sharing deploy-notes again" in reply
    assert "Never suggested: deploy-notes" in listed
    assert bad == SUGGESTIONS_USAGE
    assert SuggestionStore(service._state, tmp_path).suppressed(  # pylint: disable=protected-access
        "deploy-notes")


@pytest.mark.parametrize(("surface", "pending"), [
    ({"platform": "slack", "chat_type": "channel", "chat_id": "C1"}, False),
    ({"platform": "telegram", "chat_type": "group", "chat_id": "-5"}, False),
    ({"source": "cli"}, True),  # a captured but unadmitted gateway command
])
def test_group_chats_get_no_suggestions_and_no_change(tmp_path, monkeypatch, surface, pending):
    runtime, service = _runtime(tmp_path, monkeypatch, surface=surface, pending=pending)
    try:
        reply = _reply(runtime.slash("suggestions never deploy-notes"))
    finally:
        runtime.native.close()
    assert reply.startswith("Your Collective Wisdom suggestions are private.")
    assert "deploy-notes" not in reply
    assert not SuggestionStore(service._state, tmp_path).suppressed(  # pylint: disable=protected-access
        "deploy-notes")


def test_shared_safe_view_refuses_suggestions() -> None:
    reply = run_slash(parse_slash("suggestions"), SimpleNamespace())
    assert reply.startswith("Your Collective Wisdom suggestions are private.")


def test_help_lists_the_suggestions_command() -> None:
    assert "/wisdom suggestions" in run_slash(parse_slash("help"), SimpleNamespace())
