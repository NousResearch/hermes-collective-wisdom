"""Share suggestions as a posted conversation card (``ctx.post_card``), with no model turn.

The fake host follows the core contract: ``post_card(card, *, session_key=None, origin=None)``
returns ``{"ok": True, "card_id", "status": "queued" | "posted" | "duplicate"}`` or
``{"ok": False, "error": ...}`` and never raises; ``conversation.cards`` is its consent.
"""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from test_auto_suggest import DUE, SLACK_DM
from test_auto_suggest_fork import _Host, _learn, _turn
from test_auto_suggest_unload import _unload_at
from test_release_scope import _context, _load_root_plugin, _unload

from hermes_wisdom_plugin.auto_suggest import (
    CAPABILITY, CARD_TTL_SECONDS, DESKTOP_DIRECTIVE, FORK_CAPABILITY, PENDING_TTL_SECONDS,
    approval_prompt, chat_card_line, local_card,
)
from hermes_wisdom_plugin.auto_suggest_card import desktop_answer, desktop_card
from hermes_wisdom_plugin.card_delivery import (
    CARD_CAPABILITY, POSTED, REFUSED, UNCERTAIN, CardPoster,
)

DESKTOP = {"source": "desktop", "session_key": "desktop-key-1"}


class _Cards:  # pylint: disable=too-few-public-methods
    """The host's ``ctx.post_card``, recorded."""

    def __init__(self):
        self.posted: list[tuple[dict, dict]] = []
        self.result: dict | Exception = {"ok": True, "card_id": "", "status": "queued"}

    def __call__(self, card, *, session_key=None, origin=None):
        self.posted.append((card, {"session_key": session_key, "origin": origin}))
        if isinstance(self.result, Exception):
            raise self.result
        return {**self.result, "card_id": card.get("id")}


def _build(tmp_path, *, granted=True, api=True, surface=None, **changes):
    host = _Host(tmp_path)
    if granted:
        host.granted.add(CARD_CAPABILITY)
    host.surface = dict(surface or DESKTOP)
    cards = _Cards()
    poster = CardPoster(cards if api else None, lambda name: name in host.granted)
    controller = host.build(cards=poster, **changes)
    return host, cards, controller


def _suggest(controller, platform="desktop"):
    _learn(controller)
    _turn(controller, signals=DUE, platform=platform)


def _rows(host):
    return list(host.store.snapshot()["pending"].values())


def _decisions(host):
    return [(row["event"], row.get("surface"), row.get("via"))
            for row in host.store.snapshot()["decisions"]
            if row["event"] in {"shown", "injected", "not_shown"}]


def test_desktop_posts_one_directive_card_and_injects_no_turn(tmp_path):
    host, cards, controller = _build(tmp_path)
    _suggest(controller)
    assert not host.injected, "no model turn on the card path"
    assert len(cards.posted) == 1
    card, target = cards.posted[0]
    [row] = _rows(host)
    assert target == {"session_key": "desktop-key-1", "origin": None}
    assert card == {"id": row["id"], "text": local_card(row, "default"),
                    "directive": {"name": DESKTOP_DIRECTIVE, "attrs": {"id": row["id"]}}}
    assert row["state"] == "presented", "answerable straight away, no tool call"
    assert row["expires_at"] == host.clock() + CARD_TTL_SECONDS
    assert _decisions(host) == [("shown", "desktop", "card")]
    assert host.store.snapshot()["day"]["shown"] == 1
    with pytest.raises(PermissionError):  # The live model cannot show it a second time.
        controller.tool({"skill": "incident"}, session_id="s1")


def test_the_desktop_card_choices_work_on_the_posted_row(tmp_path):
    host, _cards, controller = _build(tmp_path)
    _suggest(controller)
    [row] = _rows(host)
    assert desktop_card(host.store, row)["state"] == "pending"
    answered = desktop_answer(host.store, row, "dismiss", prepare=lambda _name: None)
    assert answered["state"] == "settled"
    assert host.store.seen("incident", "sha-i")
    assert (host.store.get(row["id"]) or {}).get("state") == "dismissed"


def test_tui_posts_the_text_card_without_a_directive(tmp_path):
    host, cards, controller = _build(tmp_path, surface={"source": "tui", "session_key": "t-1"})
    _suggest(controller, platform="tui")
    assert len(cards.posted) == 1
    card, target = cards.posted[0]
    [row] = _rows(host)
    assert "directive" not in card and card["text"] == local_card(row, "default")
    assert target["session_key"] == "t-1" and not host.injected
    assert row["expires_at"] == host.clock() + PENDING_TTL_SECONDS
    assert _decisions(host) == [("shown", "tui", "card")]


def test_private_slack_chat_sends_the_native_card_and_records_one_line(tmp_path):
    host, cards, controller = _build(tmp_path, surface=SLACK_DM)
    _suggest(controller, platform="slack")
    assert not host.injected
    assert len(host.presented) == 1 and len(cards.posted) == 1
    route, session_id, suggestion = host.presented[0]
    assert route["chat_id"] == "D1" and session_id == "s1" and suggestion["skill"] == "incident"
    card, target = cards.posted[0]
    assert card == {"id": suggestion["id"], "text": chat_card_line(suggestion)}
    assert target["session_key"] == SLACK_DM["session_key"]
    assert _rows(host)[0]["state"] == "presented"
    assert _decisions(host) == [("shown", "slack", "card")]


def test_a_native_card_that_could_not_be_sent_records_nothing_and_waits_a_day(tmp_path):
    host, cards, controller = _build(tmp_path, surface=SLACK_DM)
    controller.present = lambda _route, _session_id, _suggestion: {"state": "unavailable"}
    _suggest(controller, platform="slack")
    assert not cards.posted and not host.injected
    assert _decisions(host) == [("not_shown", None, None)]
    assert host.store.snapshot()["decisions"][-1]["why"] == "card_unavailable"
    assert host.store.snapshot()["day"]["blocked"] is True
    assert not host.store.seen("incident", "sha-i")


@pytest.mark.parametrize("why", ["no_api", "not_granted"])
def test_hosts_without_cards_keep_the_injected_turn(tmp_path, why):
    host, cards, controller = _build(tmp_path, api=why != "no_api",
                                     granted=why != "not_granted")
    _suggest(controller)
    assert not cards.posted
    assert host.injected == [(approval_prompt("incident"), "user",
                              {"session_key": "desktop-key-1"})]
    assert _rows(host)[0]["state"] == "pending"
    assert _decisions(host) == [("injected", "desktop", None)]


def test_a_controller_without_a_card_poster_is_unchanged(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _suggest(controller, platform="cli")
    assert host.injected == [(approval_prompt("incident"), "user", {})]


def test_the_classic_cli_without_a_session_key_keeps_the_injected_turn(tmp_path):
    host, cards, controller = _build(tmp_path, surface={"source": "cli"})
    _suggest(controller, platform="cli")
    assert not cards.posted
    assert host.injected == [(approval_prompt("incident"), "user", {})]


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "consent_required"}, {"ok": False, "error": "no_conversation"},
    {"ok": False, "error": "gateway_unavailable"}, RuntimeError("boom"),
])
def test_a_refused_card_falls_back_to_the_injected_turn(tmp_path, result):
    host, cards, controller = _build(tmp_path)
    cards.result = result
    _suggest(controller)
    assert len(cards.posted) == 1 and len(host.injected) == 1
    [row] = _rows(host)
    assert row["state"] == "pending" and row["expires_at"] == host.clock() + PENDING_TTL_SECONDS
    shown = json.loads(controller.tool({"skill": "incident"}, session_id="s1"))
    assert shown["status"] == "shown"


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "timeout"},
    {"ok": False, "error": "cancelled"},
    {"ok": False, "error": "post_failed"},
    {"ok": False, "error": "future_error", "certain": False},
])
def test_an_uncertain_card_is_not_shown_a_second_way(tmp_path, result):
    host, cards, controller = _build(tmp_path)
    cards.result = result
    _suggest(controller)
    assert len(cards.posted) == 1 and not host.injected
    assert _rows(host)[0]["state"] == "presented"
    assert host.store.snapshot()["decisions"][-1]["uncertain"] is True


@pytest.mark.parametrize("gate", ["auto_suggest_false", "daily_limit", "no_grant_to_ask"])
def test_gates_post_nothing(tmp_path, gate):
    host, cards, controller = _build(tmp_path)
    if gate == "auto_suggest_false":
        host.settings["auto_suggest"] = False
    elif gate == "daily_limit":
        host.settings["auto_suggest_daily_limit"] = 0
    else:
        host.granted -= {FORK_CAPABILITY, CAPABILITY}
    _suggest(controller)
    assert not cards.posted and not host.injected and not host.presented


def test_the_daily_limit_holds_across_posted_cards(tmp_path):
    host, cards, controller = _build(tmp_path)
    host.facts["deploy"]["content_hash"] = "sha-d"
    host.fork_script = [{"skill": "incident", "reason": "Teams review often."}]
    _suggest(controller)
    host.fork_script = [{"skill": "deploy", "reason": "Releases."}]
    _learn(controller, "deploy", action="patched")
    _turn(controller, signals=DUE, platform="desktop")
    assert len(cards.posted) == 1


@pytest.mark.parametrize("boundary", ["after_closed_check", "after_reservation",
                                      "after_pending_write"])
def test_unload_during_delivery_posts_nothing(tmp_path, boundary):
    host, cards, controller = _build(tmp_path)
    _unload_at(host, controller, boundary)
    _suggest(controller)
    snapshot = host.store.snapshot()
    assert not cards.posted and not host.injected
    assert snapshot["pending"] == {} and int(snapshot["day"].get("shown") or 0) == 0


@pytest.mark.parametrize(("result", "outcome"), [
    ({"ok": True, "status": "queued"}, POSTED), ({"ok": True, "status": "posted"}, POSTED),
    ({"ok": True, "status": "duplicate"}, POSTED), ({"ok": True, "status": "odd"}, REFUSED),
    ({"ok": False, "error": "timeout"}, UNCERTAIN),
    ({"ok": False, "error": "cancelled"}, UNCERTAIN),
    ({"ok": False, "error": "post_failed"}, UNCERTAIN),
    ({"ok": False, "error": "future_error", "certain": False}, UNCERTAIN),
    ({"ok": False, "error": "timeout", "certain": True}, REFUSED),
    ({"ok": True, "status": "queued", "certain": False}, UNCERTAIN),
    ({"ok": False, "error": "card_conflict"}, REFUSED), (None, REFUSED), ("yes", REFUSED),
])
def test_card_poster_maps_core_results(result, outcome):
    poster = CardPoster(lambda _card, **_target: result, lambda _name: True)
    assert poster.post("abcdefgh1", "text", session_key="k") == outcome


def _raises(_name: str) -> bool:
    raise KeyError(_name)


def test_card_poster_needs_the_method_the_grant_and_a_session():
    def call(*_args, **_kwargs):
        return {"ok": True, "status": "queued"}

    assert CardPoster(None, lambda _name: True).ready() is False
    assert CardPoster(call, lambda name: name != CARD_CAPABILITY).ready() is False
    assert CardPoster(call, _raises).ready() is False
    assert CardPoster(call, lambda name: name == CARD_CAPABILITY).ready() is True
    assert CardPoster(call, lambda _name: True).post("abcdefgh1", "t", session_key="") == REFUSED


def test_register_wires_ctx_post_card_behind_the_grant(tmp_path, monkeypatch):
    module_name, plugin = _load_root_plugin()
    runtime_module = importlib.import_module(f"{module_name}.hermes_wisdom_plugin.runtime")
    monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", tmp_path))
    granted = {CAPABILITY}
    handlers = []
    _entries, context = _context(
        tmp_path, side_question=lambda *_a, **_k: None,
        has_capability=lambda name: name in granted,
        post_card=lambda *_a, **_k: {"ok": True, "status": "queued"},
    )
    context.register_tool = lambda **definition: (
        handlers.append(definition["handler"]) or SimpleNamespace(dispose=lambda: None))
    try:
        plugin.register(context)
        cards = handlers[0].__self__.cards  # The controller behind wisdom_share_suggestion.
        assert cards.ready() is False, "declared but not granted"
        granted.add(CARD_CAPABILITY)
        assert cards.ready() is True
    finally:
        _unload(module_name)
