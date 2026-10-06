"""Top skills: ranking, eligibility, paging, private routing and the CLI."""

# pylint: disable=missing-function-docstring,import-outside-toplevel

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from installed_test_support import JsonState

from hermes_wisdom_plugin import top_skills
from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_cli, run_slash
from hermes_wisdom_plugin.runtime import CommandRuntime
from hermes_wisdom_plugin.top_command import private_route, refusal
from hermes_wisdom_plugin.top_skills import TopSkill, collect_top_skills, rank_key


def _skill(root: Path, relative: str, name: str, description: str = "") -> Path:
    path = root / "skills" / relative
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name + ' helper'}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return path


def _api(usage, *, bundled=(), hub=(), archived=()):
    return SimpleNamespace(
        load_usage=lambda: usage,
        is_bundled=lambda name: name in bundled,
        is_hub_installed=lambda name: name in hub,
        list_archived_skill_names=lambda: list(archived),
    )


def _record(uses=0, edits=0, last=None, **extra):
    return {"use_count": uses, "patch_count": edits, "last_used_at": last, **extra}


def test_ranking_contract_orders_uses_edits_recency_then_name() -> None:
    rows = [
        TopSkill("delta", "", 5, 1, "2026-10-01T00:00:00+00:00", False),
        TopSkill("alpha", "", 5, 1, "2026-10-01T00:00:00+00:00", False),
        TopSkill("never", "", 5, 1, None, False),
        TopSkill("newer", "", 5, 1, "2026-10-03T00:00:00+00:00", False),
        TopSkill("edited", "", 5, 4, None, False),
        TopSkill("busiest", "", 9, 0, None, False),
    ]
    assert [row.name for row in sorted(rows, key=rank_key)] == [
        "busiest", "edited", "newer", "alpha", "delta", "never",
    ]


def test_eligibility_excludes_bundled_hub_team_archived_external_and_unused(tmp_path) -> None:
    home = tmp_path / "profile"
    mine = _skill(home, "mine", "mine")
    _skill(home, "bundled", "bundled")
    _skill(home, "hub", "hub")
    team = _skill(home, "team", "team")
    _skill(home, "archived", "archived")
    _skill(home, "retired", "retired")
    _skill(home, ".hub/hidden", "hidden")
    _skill(home, ".archive/old", "old")
    _skill(home, "unused", "unused")
    shared = _skill(home, "nested/shared", "shared")
    outside = tmp_path / "external" / "linked"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: linked\n---\n", encoding="utf-8")
    (home / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    usage = {name: _record(uses=3) for name in (
        "mine", "bundled", "hub", "team", "archived", "hidden", "old", "linked", "shared",
    )}
    usage["retired"] = _record(uses=2, state="archived")
    usage["unused"] = _record()
    installs = [
        {"state": "active", "slug": "team", "target_path": str(team)},
        {"state": "active", "slug": "shared-on-portal", "target_path": str(shared),
         "target_provenance": {"kind": "publisher_source"}},
    ]
    result = collect_top_skills(
        home, installations=installs, published_paths=[str(mine)],
        usage_api=lambda: _api(usage, bundled={"bundled"}, hub={"hub"}, archived={"archived"}),
    )
    assert result.state == "ok"
    assert {(row.name, row.shared) for row in result.items} == {("mine", True), ("shared", True)}


def test_missing_core_capability_is_an_unavailable_state(tmp_path, monkeypatch) -> None:
    _skill(tmp_path, "mine", "mine")
    assert collect_top_skills(tmp_path, usage_api=lambda: None).state == "unavailable"
    monkeypatch.setattr(top_skills, "import_module",
                        lambda _name: SimpleNamespace(load_usage=lambda: {}))
    assert top_skills.load_usage_api() is None

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(top_skills, "import_module", missing)
    assert top_skills.load_usage_api() is None
    assert collect_top_skills(tmp_path).state == "unavailable"


def test_no_recorded_usage_is_an_empty_state(tmp_path) -> None:
    _skill(tmp_path, "mine", "mine")
    result = collect_top_skills(tmp_path, usage_api=lambda: _api({"mine": _record()}))
    assert result.state == "empty" and not result.items


def test_pagination_bounds() -> None:
    items = tuple(TopSkill(f"s{index:02}", "", 100 - index, 0, None, False) for index in range(23))
    ranked = top_skills.TopSkills("ok", items)
    first = ranked.page(1, 10)
    assert (first["total"], first["pages"], len(first["items"])) == (23, 3, 10)
    assert [row["name"] for row in ranked.page(3, 10)["items"]] == ["s20", "s21", "s22"]
    assert ranked.page(4, 10)["items"] == []
    assert ranked.page(1, 50)["pages"] == 1
    for page, per_page in ((0, 10), (1, 0), (1, 51)):
        with pytest.raises(ValueError):
            ranked.page(page, per_page)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert parser.parse_args(["top"]).per_page == 10
    for bad in (["top", "--per-page", "51"], ["top", "--per-page", "0"], ["top", "--page", "0"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


class _Service:  # pylint: disable=too-few-public-methods
    def __init__(self, items, state="ok"):
        self.result = top_skills.TopSkills(state, tuple(items))

    def top_skills(self):
        return self.result


def _items(count):
    return [TopSkill(f"skill-{index}", "d", 10 - index, index, "2026-10-01T09:00:00+00:00",
                     index == 0) for index in range(count)]


def test_cli_top_json_contract(capsys) -> None:
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    args = parser.parse_args(["top", "--json", "--page", "2", "--per-page", "2"])
    assert run_cli(args, _Service(_items(5))) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"items", "page", "per_page", "total", "pages"} <= set(data)
    assert (data["page"], data["per_page"], data["total"], data["pages"]) == (2, 2, 5, 3)
    assert data["items"][0] == {"name": "skill-2", "uses": 8, "edits": 2,
                                "last_used": "2026-10-01T09:00:00+00:00", "shared": False}


def test_cli_top_human_rows_and_states(capsys) -> None:
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert run_cli(parser.parse_args(["top"]), _Service(_items(12))) == 0
    out = capsys.readouterr().out
    assert "1. skill-0 | 10 uses | 0 edits | last used 2026-10-01 | Shared" in out
    assert "Next: hermes wisdom top --page 2" in out
    run_cli(parser.parse_args(["top"]), _Service([], state="unavailable"))
    assert "unavailable on this Hermes version" in capsys.readouterr().out
    run_cli(parser.parse_args(["top"]), _Service([], state="empty"))
    assert "No recorded usage" in capsys.readouterr().out


def test_shared_safe_slash_view_refuses_without_names() -> None:
    assert run_slash(parse_slash("top"), _Service(_items(3))) == refusal("top")
    assert "/wisdom top [page]" in run_slash(parse_slash("help"), SimpleNamespace())


def _invocation(chat_type="private"):
    return SimpleNamespace(platform="telegram", chat_id="42", chat_type=chat_type,
                           thread_id="", actor_id="7", actor_name="victor", scope_id="")


def test_private_route_matrix() -> None:
    local = {"platform": "", "source": "cli", "chat_type": "", "chat_id": "", "session_key": ""}
    assert private_route(None, False, local) == {"kind": "local", "session_key": ""}
    tui = {**local, "source": "tui", "session_key": "sk-1"}
    assert private_route(None, False, tui) == {"kind": "local", "session_key": "sk-1"}
    assert private_route(_invocation(), False, local)["origin"] == {
        "platform": "telegram", "chat_id": "42", "chat_type": "private",
        "user_id": "7", "user_name": "victor",
    }
    assert private_route(None, True, local) is None
    group = {"platform": "discord", "source": "", "chat_type": "group", "chat_id": "9"}
    assert private_route(None, False, group) is None
    assert private_route(None, False, {**group, "chat_type": "dm"})["kind"] == "messaging"
    assert private_route(None, False, {**group, "chat_type": ""}) is None


def _runtime(tmp_path, monkeypatch, *, invocation=None, pending=False, surface=None):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: invocation)
    monkeypatch.setattr(runtime.native, "command_pending", lambda: pending)
    monkeypatch.setattr(top_skills, "session_surface", lambda: surface or {"source": "cli"})
    return runtime


def _seed(tmp_path, monkeypatch):
    _skill(tmp_path, "secret-skill", "secret-skill")
    monkeypatch.setattr(top_skills, "load_usage_api",
                        lambda: _api({"secret-skill": _record(uses=4, edits=1)}))


def _reply(result):
    return asyncio.run(result) if inspect.isawaitable(result) else result


@pytest.mark.parametrize("route", [
    {"pending": True},
    {"surface": {"platform": "telegram", "chat_type": "group", "chat_id": "-100"}},
    {"surface": {"platform": "slack", "chat_type": "channel", "chat_id": "C1"}},
])
def test_group_slash_refuses_without_skill_names(tmp_path, monkeypatch, route) -> None:
    _seed(tmp_path, monkeypatch)
    runtime = _runtime(tmp_path, monkeypatch, **route)
    try:
        reply = _reply(runtime.slash("top"))
        assert reply == refusal("top") and "secret-skill" not in reply
    finally:
        runtime.native.close()


@pytest.mark.parametrize("route", [
    {"invocation": _invocation()},
    {"surface": {"source": "cli"}},
    {"surface": {"source": "tui", "session_key": "sk"}},
])
def test_private_slash_lists_ranked_skills(tmp_path, monkeypatch, route) -> None:
    _seed(tmp_path, monkeypatch)
    runtime = _runtime(tmp_path, monkeypatch, **route)
    try:
        reply = _reply(runtime.slash("top"))
        assert "1. secret-skill | 4 uses | 1 edits | last used never" in reply
        assert _reply(runtime.slash("top 0")) == "Usage: /wisdom top [page]"
    finally:
        runtime.native.close()
