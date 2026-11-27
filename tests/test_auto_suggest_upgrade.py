"""Pending suggestions written by the previous plugin revision settle through the new runtime.

Before ``reserved_day`` existed, ``_deliver`` counted a suggestion against the day and stored its
row in one step, so a durable row from that writer carries ``created_at`` but no ``reserved_day``.
The plugin reloads with such rows still waiting for the model's tool call. These tests write rows
exactly as that revision did (and, with ``WISDOM_PREVIOUS_PLUGIN_DIR`` pointing at a checkout of
it, with that revision's own code in another process), then read them with the current runtime.
Nothing rewrites a stored row before it is read back.
"""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from restoration_support import SlackHarness, TelegramHarness
from test_auto_suggest_card import _suggestion
from test_auto_suggest_cards import _build, _decisions, _rows, _suggest
from test_restored_native_flow import Service

from hermes_wisdom_plugin.auto_suggest import PENDING_TTL_SECONDS

PREVIOUS_REVISION = "22cfd32fdf79c880d16ec542288e5a3f9a99ba73"
PREVIOUS_ROW_KEYS = frozenset({
    "id", "skill", "description", "reason", "content_hash", "session_id", "route", "state",
    "created_at", "expires_at",
})
NOW = 1_800_000_000.0  # The fake clock of the controller hosts.
WRITERS = ["current", "previous_schema", "previous_revision"]
LIMIT = 5


def _day_of(stamp):
    """The local calendar day the store counts on, as ``SuggestionStore`` spells it."""
    return datetime.fromtimestamp(stamp).date().isoformat()

_PREVIOUS_WRITER = """
import json, sys
from pathlib import Path
from test_auto_suggest_cards import _build, _suggest
spec = json.loads(sys.argv[1])
host, _cards, controller = _build(Path(spec["root"]), surface=spec["route"], api=False)
host.clock.now = spec["at"]
_suggest(controller, platform=spec["platform"])
controller.close()
"""


def _previous_checkout() -> Path:
    raw = os.environ.get("WISDOM_PREVIOUS_PLUGIN_DIR")
    if not raw:
        pytest.skip("set WISDOM_PREVIOUS_PLUGIN_DIR to a checkout of plugin revision "
                    f"{PREVIOUS_REVISION[:7]} to write rows with that revision's code")
    path = Path(raw).resolve()
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert head == PREVIOUS_REVISION, f"{path} is at {head}, not the previous revision"
    return path


def _write_with_previous_revision(tmp_path, route, platform, at):
    """The previous revision's own controller, in its own process, creates the pending row."""
    previous = _previous_checkout()
    spec = json.dumps({"root": str(tmp_path), "route": route, "platform": platform, "at": at})
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(previous), str(previous / "tests")])}
    result = subprocess.run([sys.executable, "-c", _PREVIOUS_WRITER, spec], cwd=previous, env=env,
                            capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"


def _write_with_previous_schema(host, route, at):
    """The previous ``_deliver``, step for step: count today, then store a row without
    ``reserved_day``; the host had injected the approval turn the tool answers."""
    host.clock.now = at
    host.store.mark_seen("incident", "sha-i", "asked")
    assert host.store.reserve_today(LIMIT) == _day_of(at)
    host.store.add_pending({
        "id": "previous-1", "skill": "incident", "description": "Review incident notes.",
        "reason": "Teams review often.", "content_hash": "sha-i", "session_id": "s1",
        "route": dict(route), "state": "pending", "created_at": at,
        "expires_at": at + PENDING_TTL_SECONDS,
    })
    host.store.record("injected", "incident", surface=route["platform"])


def _write(writer, host, controller, ui, route, at):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    if writer == "current":
        host.clock.now = at
        _suggest(controller, platform=ui.caller.platform)
    elif writer == "previous_schema":
        _write_with_previous_schema(host, route, at)
    else:
        _write_with_previous_revision(host.store.root, route, ui.caller.platform, at)
    [row] = _rows(host)
    assert row["state"] == "pending" and row["created_at"] == at
    if writer == "current":
        assert row["reserved_day"] == _day_of(at)
    else:
        assert set(row) == PREVIOUS_ROW_KEYS, "the previous writer's exact row shape"
    assert host.store.snapshot()["day"] == {"date": _day_of(at), "shown": 1}, "counted once"
    return row


def _fail_sends(monkeypatch, ui, surface):
    async def failed(**_payload):
        raise OSError("synthetic transport failure")
    monkeypatch.setattr(ui, "chat_postMessage" if surface is SlackHarness else "send_message",
                        failed)


def _no_callback_error(caplog):
    assert "exception calling callback" not in caplog.text
    assert "KeyError" not in caplog.text


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("writer", WRITERS)
def test_a_pending_row_from_an_older_writer_settles_after_the_send(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    tmp_path, monkeypatch, caplog, surface, delivered, writer,
):
    async def scenario():
        ui = surface(tmp_path, Service())
        route, _ = _suggestion(ui)
        host, cards, controller = _build(
            tmp_path, surface=route, present=ui.runtime.present_auto_suggest, api=False,
        )
        row = _write(writer, host, controller, ui, route, NOW)
        if not delivered:
            _fail_sends(monkeypatch, ui, surface)
        reply = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
        assert reply["status"] == "queued" and _rows(host)[0]["state"] == "delivering"
        assert not any(event == "shown" for event, _, _ in _decisions(host))
        await ui.flush()
        snapshot = host.store.snapshot()
        shown = [event for event in _decisions(host) if event[0] == "shown"]
        if delivered:
            assert len(ui.posts) == 1 and not cards.posted
            assert _rows(host) == [{**row, "state": "presented"}], "the stored row is unchanged"
            assert shown == [("shown", ui.caller.platform, None)]
            assert snapshot["day"] == {"date": _day_of(NOW), "shown": 1}, "its count stands"
            assert host.store.seen("incident", "sha-i")
        else:
            assert not ui.posts and not cards.posted and not shown
            assert not _rows(host), "a failed send drops the row"
            assert snapshot["day"] == {"date": _day_of(NOW), "shown": 0, "blocked": True}, (
                "the failed send returns the day's allowance and keeps the no-retry fence")
            assert not host.store.day_open(LIMIT)
            assert not host.store.seen("incident", "sha-i"), "asked about again another day"
            assert _decisions(host)[-1] == ("not_shown", None, None)
        _no_callback_error(caplog)
        controller.close()
        ui.runtime.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())


def _around_midnight():
    """Ten minutes before and after the first local midnight after ``NOW``."""
    midnight = (datetime.fromtimestamp(NOW) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    return midnight - 600, midnight + 600


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("writer", WRITERS)
def test_a_row_counted_yesterday_does_not_refund_today(tmp_path, monkeypatch, caplog, surface,
                                                      writer):
    async def scenario():
        ui = surface(tmp_path, Service())
        route, _ = _suggestion(ui)
        host, cards, controller = _build(
            tmp_path, surface=route, present=ui.runtime.present_auto_suggest, api=False,
        )
        yesterday, today = _around_midnight()
        _write(writer, host, controller, ui, route, yesterday)
        host.clock.now = today  # The tool call arrives after midnight, inside the pending TTL.
        assert host.store.reserve_today(LIMIT) == _day_of(today), "another suggestion today"
        _fail_sends(monkeypatch, ui, surface)
        controller.tool({"skill": "incident"}, session_id="s1")
        await ui.flush()
        snapshot = host.store.snapshot()
        assert not ui.posts and not cards.posted and not _rows(host)
        assert snapshot["day"] == {"date": _day_of(today), "shown": 1, "blocked": True}, (
            "yesterday's count is not refunded against today's quota; today is still fenced")
        assert _decisions(host)[-1] == ("not_shown", None, None)
        assert not host.store.seen("incident", "sha-i")
        _no_callback_error(caplog)
        controller.close()
        ui.runtime.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())


def test_a_row_with_no_known_day_is_dropped_without_a_refund(tmp_path):
    host, _cards, controller = _build(tmp_path, surface={"source": "cli"})
    assert host.store.reserve_today(LIMIT) == _day_of(NOW)
    host.store.add_pending({"id": "unknown-1", "skill": "incident", "content_hash": "sha-i",
                            "route": {"platform": "slack"}, "state": "delivering",
                            "expires_at": NOW + PENDING_TTL_SECONDS})
    row = host.store.get("unknown-1")
    assert row is not None
    controller._native_delivered(row, "failed", by_card=False)
    assert not _rows(host)
    assert host.store.snapshot()["day"] == {"date": _day_of(NOW), "shown": 1, "blocked": True}
    assert _decisions(host) == [("not_shown", None, None)]


def test_the_counted_day_prefers_the_stored_reservation():
    from hermes_wisdom_plugin.auto_suggest import _counted_day  # pylint: disable=import-outside-toplevel
    assert _counted_day({"reserved_day": "2027-01-15", "created_at": NOW + 86_400}) == "2027-01-15"
    assert _counted_day({"created_at": NOW}) == _day_of(NOW)
    assert _counted_day({"created_at": int(NOW)}) == _day_of(NOW)
    assert _counted_day({"reserved_day": "", "created_at": True}) is None
    assert _counted_day({}) is None
