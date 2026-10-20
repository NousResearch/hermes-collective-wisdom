"""A never-enrolled profile answers every Wisdom command with the setup step."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import pytest
from installed_test_support import JsonState

from hermes_wisdom_plugin.commands import (
    SETUP_PROMPT,
    build_parser,
    run_cli,
    setup_prompt_needed,
)
from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.runtime import CommandRuntime, InvocationService


class _Unenrolled:
    """Service double whose only answer is local enrollment state."""

    def __init__(self, pending=True):
        self.pending = pending
        self.calls = []

    def setup_pending(self):
        """Return the fixture's local enrollment answer."""
        return self.pending

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"configured": False, "state": "not_configured"}
        return record


def _args(*argv):
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    args = parser.parse_args(list(argv))
    args._wisdom_parser = parser  # pylint: disable=protected-access
    return args


def _native_invocation(platform):
    return NativeInvocation(
        platform=platform, profile="default", chat_type="private",
        chat_id="D1" if platform == "slack" else "123", actor_id="native-user",
        actor_name="Shannon", thread_id="", scope_id="W1" if platform == "slack" else "",
        message_id="command-1", session_key=f"{platform}:private", adapter=object(),
    )


def _runtime(tmp_path):
    return CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )


def _resolve(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_prompt_names_the_terminal_setup_command():
    """The prompt names the terminal command and changes nothing."""
    assert "hermes wisdom setup" in SETUP_PROMPT
    assert "enrolls this profile" in SETUP_PROMPT
    assert "Nothing has been changed" in SETUP_PROMPT


@pytest.mark.parametrize("argv", [
    ("share", "local-skill"), ("browse",), ("show", "skill"), ("install", "skill"),
    ("installed",), ("update", "skill"), ("publication", "status", "op"),
])
def test_cli_commands_on_unenrolled_profile_answer_with_setup(argv, capsys):
    """Terminal commands answer with setup before any service call."""
    service = _Unenrolled()

    assert run_cli(_args(*argv), service) == 6

    captured = capsys.readouterr()
    assert captured.err.strip() == SETUP_PROMPT
    assert not captured.out
    assert not service.calls


def test_cli_json_envelope_carries_setup_required_code(capsys):
    """Scripts get a stable setup_required envelope."""
    service = _Unenrolled()

    assert run_cli(_args("browse", "--json"), service) == 6

    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {
        "ok": False, "error": SETUP_PROMPT, "code": "setup_required", "category": 6,
    }
    assert not service.calls


@pytest.mark.parametrize("command", ["setup", "status", "help", "top", "share-suggest"])
def test_setup_status_help_and_local_rankings_are_never_redirected(command):
    """Setup, status, help and local rankings keep their own answers."""
    assert setup_prompt_needed(command, _Unenrolled()) is False


def test_enrolled_or_unknown_state_falls_through_to_command_checks():
    """Only a definite never-enrolled answer redirects."""
    assert setup_prompt_needed("browse", _Unenrolled(pending=False)) is False
    assert setup_prompt_needed("browse", object()) is False

    class Broken:  # pylint: disable=too-few-public-methods
        """Service whose local state cannot be read."""

        def setup_pending(self):
            """Fail the way an unreadable state file would."""
            raise RuntimeError("state unreadable")

    assert setup_prompt_needed("browse", Broken()) is False


def test_cli_status_still_reports_setup_required(capsys):
    """Status reports the profile state instead of redirecting."""
    service = _Unenrolled()

    assert run_cli(_args("status"), service) == 0

    assert "Setup: required" in capsys.readouterr().out
    assert service.calls[0][0] == "status"


def test_setup_pending_reads_local_enrollment_without_credentials(tmp_path, monkeypatch):
    """The check reads local state only and creates none."""
    service = InvocationService(tmp_path, ("default", tmp_path), state=JsonState(tmp_path))

    def no_account(*_args, **_kwargs):
        raise AssertionError("setup_pending must not touch the account")

    monkeypatch.setattr(InvocationService, "check_owner", no_account)
    assert service.setup_pending() is True
    assert not (tmp_path / "state.json").exists()

    DirectState(JsonState(tmp_path), tmp_path).set_identity({"state": "active"})
    assert service.setup_pending() is False


@pytest.mark.parametrize("raw", ["browse", "show skill", "install skill", "share local-skill"])
def test_cli_chat_and_tui_slash_answer_with_setup(tmp_path, monkeypatch, raw):
    """Unadmitted slash (classic CLI chat, TUI) never reaches the read worker."""
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: None)

    def no_worker(_parsed):
        raise AssertionError("the read worker must not start")

    monkeypatch.setattr(runtime, "_slash", no_worker)
    try:
        assert _resolve(runtime.slash(raw)) == SETUP_PROMPT
    finally:
        runtime.close()


@pytest.mark.parametrize("platform", ["telegram", "slack"])
@pytest.mark.parametrize("raw, method", [
    ("share local-skill", "begin_suggest"), ("browse", "begin_catalog"),
    ("install skill", "install_direct"),
])
def test_gateway_private_chat_answers_with_terminal_setup_not_a_card(
    tmp_path, monkeypatch, platform, raw, method,
):
    """No native review opens: enrollment is accepted only in the owner's terminal."""
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime.native, "consume_request", lambda _raw: _native_invocation(platform),
    )
    opened = []
    monkeypatch.setattr(
        runtime.native, method, lambda *args, **_kwargs: opened.append(args),
    )
    try:
        assert _resolve(runtime.slash(raw)) == SETUP_PROMPT
        assert not opened
    finally:
        runtime.close()


def test_gateway_setup_and_status_keep_their_own_answers(tmp_path, monkeypatch):
    """Native setup and status reach their own handlers, never the prompt."""
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime.native, "consume_request", lambda _raw: _native_invocation("telegram"),
    )
    reads = []

    async def read_view(parsed):
        reads.append(parsed)
        return "own view"

    monkeypatch.setattr(runtime, "_slash", read_view)
    try:
        assert _resolve(runtime.slash("setup")) == "own view"
        assert _resolve(runtime.slash("status")) == "own view"
        assert reads == [("setup", []), ("status", [])]
    finally:
        runtime.close()
