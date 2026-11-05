"""The automatic share suggestion as a card in the Desktop chat.

The tool returns the ``::wisdom-share{id="..."}`` directive only while the Desktop plugin has
checked in for this profile; the card routes read and answer one suggestion of the chat it was
made in, and reach the same store state as ``/wisdom suggestions``.
"""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from installed_test_support import JsonState

from hermes_wisdom_plugin import release_scope
from hermes_wisdom_plugin.auto_suggest import (
    CAPABILITY, DESKTOP_CHECK_IN_TTL, AutoSuggest, SuggestionStore, desktop_directive,
    local_card, run_suggestions,
)
from hermes_wisdom_plugin.desktop_api import build_router

SESSION = "desktop-session-1"
KEY = "stored-session-1"


class _Clock:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def _controller(store, clock):
    return AutoSuggest(
        store=store, side_question=lambda *_a, **_k: '{"share": true, "reason": "Teams need it."}',
        has_capability=lambda name: name == CAPABILITY, inject=lambda *_a, **_k: True,
        setting=lambda _key, default=None: default, self_learning=lambda: 1,
        candidate=lambda name: {"name": name, "description": "Review incident notes.",
                                "content_hash": "sha-1", "shared": False, "top": False},
        session=lambda: {"source": "desktop", "session_key": KEY},
        native_available=lambda _platform: False, present=lambda *_a: {},
        start=lambda work: work(), clock=clock,
    )


def _suggest(controller, platform="desktop"):
    controller.on_skill_lifecycle(action="created", skill_name="incident",
                                  provenance="agent_created", session_id=SESSION)
    controller.on_tool_call(session_id=SESSION, tool_name="terminal")
    controller.on_llm_call(session_id=SESSION, side_question_token="tok", platform=platform)
    return json.loads(controller.tool({"skill": "incident"}, session_id=SESSION))


def test_tool_returns_the_directive_after_a_fresh_desktop_check_in(tmp_path):
    clock = _Clock()
    store = SuggestionStore(JsonState(tmp_path), tmp_path, clock=clock)
    store.desktop_check_in()
    shown = _suggest(_controller(store, clock))
    row = next(iter(store.snapshot()["pending"].values()))
    assert shown["status"] == "shown" and shown["executed"] is False
    assert shown["card"] == desktop_directive(row["id"]) == f'::wisdom-share{{id="{row["id"]}"}}'
    assert "alone on its own line" in shown["instruction"]
    assert "one short sentence" in shown["instruction"]
    assert row["state"] == "presented" and row["expires_at"] > clock() + 60 * 60


@pytest.mark.parametrize("case", ["never_checked_in", "check_in_stale", "cli", "tui"])
def test_tool_keeps_the_text_card_without_a_desktop_renderer(tmp_path, case):
    clock = _Clock()
    store = SuggestionStore(JsonState(tmp_path), tmp_path, clock=clock)
    if case != "never_checked_in":
        store.desktop_check_in()
    if case == "check_in_stale":
        clock.now += DESKTOP_CHECK_IN_TTL + 1
    shown = _suggest(_controller(store, clock),
                     platform=case if case in {"cli", "tui"} else "desktop")
    row = next(iter(store.snapshot()["pending"].values()))
    assert shown["card"] == local_card(row, "default")
    assert "::wisdom-share" not in shown["card"]


class _Service:
    def __init__(self, root):
        self._state = JsonState(root)
        self.root = root
        self.profile = ("default", root)
        self.prepared: list[str] = []
        self.fail = False

    def native_owner(self):
        return {"account_id": "a", "installation_id": "i"}

    def suggest(self, skill_name, *, allow_model_review=None, may_apply=None):
        del allow_model_review
        assert may_apply is not None and may_apply() is True
        if self.fail:
            raise ValueError("preparation failed")
        self.prepared.append(skill_name)
        return {"state": "prepared", "draft_id": "local:draft-1", "skill_id": "incident"}

    def close(self):
        return None


def _app(service):
    app = FastAPI()
    app.include_router(build_router(lambda: service))
    return TestClient(app)


def _pending(service, *, sid="sugg-0001", state="presented", platform="desktop",  # pylint: disable=too-many-arguments
             expires=None, session_id=SESSION):
    store = SuggestionStore(service._state, service.root)
    store.add_pending({
        "id": sid, "skill": "incident", "description": "Review incident notes.",
        "reason": "Teams run incident reviews often.", "content_hash": "sha-1",
        "session_id": session_id, "state": state, "created_at": time.time(),
        "route": {"kind": "local", "native": False, "platform": platform, "session_key": KEY},
        "expires_at": time.time() + 600 if expires is None else expires,
    })
    return store


def _view(http):
    return http.post("/views").json()["view_id"]


def _card(http, sid="sugg-0001", session=SESSION):
    return http.get(f"/suggestions/{sid}", params={"view_id": _view(http), "session_id": session})


def _choose(http, choice, sid="sugg-0001", session=SESSION):
    return http.post(f"/suggestions/{sid}/choice",
                     json={"view_id": _view(http), "session_id": session, "choice": choice})


def test_release_registers_the_card_routes_without_the_candidates_tab(tmp_path):
    assert "suggestions" in release_scope.active().desktop_route_groups
    assert "candidates" not in release_scope.active().desktop_route_groups
    paths = {route.path for route in build_router(lambda: None).routes}
    assert {"/suggestions/desktop-card", "/suggestions/{suggestion_id}",
            "/suggestions/{suggestion_id}/choice"} <= paths
    assert not {path for path in paths if path.startswith("/candidates")}
    service = _Service(tmp_path)
    store = SuggestionStore(service._state, service.root)
    assert store.desktop_card_ready() is False
    assert _app(service).post("/suggestions/desktop-card").json() == {"ready": True}
    assert store.desktop_card_ready() is True


def test_pending_card_carries_the_messaging_card_copy(tmp_path):
    service = _Service(tmp_path)
    _pending(service)
    card = _card(_app(service)).json()
    assert card["state"] == "pending" and card["title"] == "📤 Share this skill?"
    assert card["summary"] == "Hermes thinks incident could help your team."
    assert card["items"] == [{"title": "incident", "detail": (
        "Review incident notes.\nHermes's reason: Teams run incident reviews often.")}]
    assert [(row["label"], row["choice"]) for row in card["actions"]] == [
        ("Confirm share", "share"), ("Don't share", "dismiss"), ("Never for this skill", "never")]
    assert card["notice"] == "Nothing is shared until you approve the share review."


@pytest.mark.parametrize("case", ["unknown", "other_session", "messaging", "no_view"])
def test_card_routes_refuse_other_chats_unknown_ids_and_missing_views(tmp_path, case):
    service = _Service(tmp_path)
    _pending(service, platform="slack" if case == "messaging" else "desktop")
    http = _app(service)
    sid = "sugg-9999" if case == "unknown" else "sugg-0001"
    session = "another-chat" if case == "other_session" else SESSION
    if case == "no_view":
        read = http.get(f"/suggestions/{sid}", params={"view_id": "wiv_missing_view",
                                                       "session_id": session})
        answer = http.post(f"/suggestions/{sid}/choice", json={
            "view_id": "wiv_missing_view", "session_id": session, "choice": "never"})
        assert read.status_code == answer.status_code == 401
    else:
        assert _card(http, sid, session).status_code == 404
        assert _choose(http, "never", sid, session).status_code == 404
    store = SuggestionStore(service._state, service.root)
    assert not store.suppressed("incident") and store.get("sugg-0001")["state"] == "presented"


def test_card_answered_through_the_stored_session_key(tmp_path):
    service = _Service(tmp_path)
    _pending(service)
    assert _card(_app(service), session=KEY).json()["state"] == "pending"


def _command_state(tmp_path, action):
    service = _Service(tmp_path / "command")
    (tmp_path / "command").mkdir()
    store = _pending(service)
    run_suggestions(action, "incident", store, current_hash=lambda _name: None, source="slash")
    return store.snapshot()


@pytest.mark.parametrize(("choice", "action", "title", "summary"), [
    ("dismiss", "dismiss", "Not shared", "Hermes won't suggest this version of incident again."),
    ("never", "never", "Not shared",
     "Hermes won't suggest incident again. To undo: /wisdom suggestions allow incident"),
])
def test_dont_share_and_never_reach_the_commands_store_state(  # pylint: disable=too-many-arguments
        tmp_path, choice, action, title, summary):
    service = _Service(tmp_path)
    store = _pending(service)
    http = _app(service)
    answered = _choose(http, choice).json()
    assert answered["state"] == "settled" and answered["actions"] == []
    assert (answered["title"], answered["summary"]) == (title, summary)
    commanded = _command_state(tmp_path, action)
    mine = store.snapshot()
    assert mine["suppressed"].keys() == commanded["suppressed"].keys()
    assert ({k: {h: v["outcome"] for h, v in rows.items()} for k, rows in mine["seen"].items()}
            == {k: {h: v["outcome"] for h, v in rows.items()}
                for k, rows in commanded["seen"].items()})
    last = mine["decisions"][-1]
    assert last["event"] == "choice" and last["choice"] == choice and last["source"] == "desktop"
    assert _choose(http, choice).status_code == 409  # Applied once.
    assert _card(http).json()["state"] == "settled"
    assert not service.prepared


def test_confirm_share_prepares_the_private_draft_once(tmp_path):
    service = _Service(tmp_path)
    store = _pending(service)
    http = _app(service)
    answered = _choose(http, "share").json()
    assert service.prepared == ["incident"]
    assert answered["state"] == "confirmed" and answered["draft_id"] == "local:draft-1"
    assert answered["skill"] == "incident" and answered["actions"] == []
    assert answered["notice"] == "Nothing is shared until you approve the share review."
    row = store.get("sugg-0001")
    assert row["state"] == "confirmed" and row["draft_id"] == "local:draft-1"
    assert store.snapshot()["decisions"][-1] == {
        **store.snapshot()["decisions"][-1], "event": "choice", "choice": "share",
        "source": "desktop"}
    assert _choose(http, "share").status_code == 409 and service.prepared == ["incident"]
    assert _card(http).json()["state"] == "confirmed"


def test_failed_preparation_leaves_the_card_open(tmp_path):
    service = _Service(tmp_path)
    store = _pending(service)
    service.fail = True
    assert _choose(_app(service), "share").status_code == 422
    assert store.get("sugg-0001")["state"] == "presented"
    service.fail = False
    assert _choose(_app(service), "share").json()["state"] == "confirmed"


@pytest.mark.parametrize("case", ["expired", "never_elsewhere", "dismissed_elsewhere", "closed"])
def test_settled_cards_show_the_messaging_closing_lines(tmp_path, case):
    service = _Service(tmp_path)
    store = _pending(service, expires=time.time() - 1 if case == "expired" else None,
                     state="undelivered" if case == "closed" else "presented")
    if case == "never_elsewhere":
        store.set_suppressed("incident", True, source="slash")
    if case == "dismissed_elsewhere":
        store.dismiss("incident", "sha-1", source="slash")
    http = _app(service)
    card = _card(http).json()
    assert card["state"] == "settled" and card["actions"] == []
    expected = {
        "expired": "Suggestion for incident closed.",
        "closed": "Suggestion for incident closed.",
        "never_elsewhere": ("Hermes won't suggest incident again. To undo: "
                            "/wisdom suggestions allow incident"),
        "dismissed_elsewhere": "Hermes won't suggest this version of incident again.",
    }[case]
    assert card["summary"] == expected
    assert _choose(http, "share").status_code == 409 and not service.prepared
