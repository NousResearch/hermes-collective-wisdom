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
from hermes_wisdom_plugin.native_context import InvocationCapture
from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_cli, run_slash
from hermes_wisdom_plugin.runtime import CommandRuntime
from hermes_wisdom_plugin.top_command import private_route, refusal
from hermes_wisdom_plugin.top_skills import (
    TopSkill, clean_text, collect_top_skills, is_messaging, published_source_paths, rank_key,
)

BOUND: list = []


@pytest.fixture(autouse=True)
def _host_home_override(monkeypatch):
    """Stand in for hermes_constants' context-local home override."""
    BOUND.clear()
    monkeypatch.setattr(top_skills, "_home_override", lambda: (
        lambda home: BOUND.append(("set", home)) or len(BOUND),
        lambda token: BOUND.append(("reset", token)),
    ))


def _skill(root: Path, relative: str, name: str, description: str = "") -> Path:
    path = root / "skills" / relative
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name + ' helper'}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return path


def _front_name(skill_md: Path) -> str:
    for line in skill_md.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return skill_md.parent.name


def _api(usage, *, bundled=(), hub=(), archived=(), suppressed=()):
    """Host-shaped skill_usage: dot/dependency dirs skipped, resolution by frontmatter name."""

    def iter_mds(base, *, local_only):
        assert local_only
        for skill_md in sorted(base.rglob("SKILL.md")):
            parts = skill_md.relative_to(base).parts[:-1]
            if not any(part.startswith(".") or part == "node_modules" for part in parts):
                yield _front_name(skill_md), skill_md

    def find_skill_dir(name):
        home = Path(BOUND[-1][1]) if BOUND and BOUND[-1][0] == "set" else None
        assert home is not None, "resolver used outside the profile override"
        return next((md.parent for found, md in iter_mds(home / "skills", local_only=True)
                     if found == name), None)

    return SimpleNamespace(
        load_usage=lambda: usage,
        is_bundled=lambda name: name in bundled,
        is_hub_installed=lambda name: name in hub,
        list_archived_skill_names=lambda: list(archived),
        read_suppressed_names=lambda: set(suppressed),
        _iter_skill_mds=iter_mds,
        _find_skill_dir=find_skill_dir,
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
    _skill(home, "suppressed", "suppressed")
    _skill(home, "pkg/node_modules/dep", "dep")
    _skill(home, "dir-name", "front-name")
    _skill(home, "_wisdom/mirror", "mirror")
    slugged = _skill(home, "elsewhere", "team-slug")
    shared = _skill(home, "nested/shared", "shared")
    outside = tmp_path / "external" / "linked"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\nname: linked\n---\n", encoding="utf-8")
    (home / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    usage = {name: _record(uses=3) for name in (
        "mine", "bundled", "hub", "team", "archived", "hidden", "old", "linked", "shared",
        "suppressed", "dep", "front-name", "dir-name", "mirror", "team-slug",
    )}
    usage["retired"] = _record(uses=2, state="archived")
    usage["unused"] = _record()
    installs = [
        {"state": "active", "slug": "team", "target_path": str(team)},
        {"state": "active", "slug": "team-slug", "target_path": str(slugged.parent / "gone")},
        {"state": "active", "slug": "shared-on-portal", "target_path": str(shared),
         "target_provenance": {"kind": "publisher_source"}},
    ]
    result = collect_top_skills(
        home, installations=installs, published_paths=[str(mine)],
        usage_api=lambda: _api(usage, bundled={"bundled"}, hub={"hub"}, archived={"archived"},
                               suppressed={"suppressed"}),
    )
    assert result.state == "ok"
    assert {(row.name, row.shared, row.description) for row in result.items} == {
        ("mine", True, "mine helper"), ("shared", True, "shared helper"),
        ("front-name", False, "front-name helper"),
    }
    assert BOUND[0] == ("set", str(home)) and BOUND[-1][0] == "reset"


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
    assert collect_top_skills(tmp_path, usage_api=lambda: SimpleNamespace(
        **{**vars(_api({})), "load_usage": lambda: ["not", "a", "map"]})).state == "unavailable"
    assert collect_top_skills(tmp_path, usage_api=lambda: _api({})).state == "empty"
    monkeypatch.setattr(top_skills, "_home_override", lambda: None)
    assert collect_top_skills(tmp_path, usage_api=lambda: _api({})).state == "unavailable"


def test_unexpected_failure_is_a_logged_error_state(tmp_path, caplog) -> None:
    _skill(tmp_path, "mine", "mine")

    def broken():
        raise KeyError("bug")

    api = SimpleNamespace(**{**vars(_api({"mine": _record(uses=1)})), "load_usage": broken})
    assert collect_top_skills(tmp_path, usage_api=lambda: api).state == "error"
    assert "KeyError" in caplog.text
    assert BOUND[-1][0] == "reset"


def test_shared_label_needs_a_completed_publication() -> None:
    drafts = {"d1": {"source_path": "/s/one"}, "d2": {"source_path": "/s/two"},
              "d3": {"source_path": "/s/three"}}
    publications = {
        "p1": {"status": "done", "publication_state": "published", "local_draft_id": "d1"},
        "p2": {"status": "pending", "publication_state": "published", "local_draft_id": "d2"},
        "p3": {"status": "done", "publication_state": "rejected", "local_draft_id": "d3"},
    }
    assert published_source_paths({"drafts": drafts, "publications": publications}) == ["/s/one"]


def test_descriptions_are_single_line_printable_and_bounded() -> None:
    assert clean_text("line\x1b[31m one\n two\u202e") == "line[31m one two"
    long = clean_text("x" * 500)
    assert len(long) == 200 and long.endswith("...")


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
    lines = out.splitlines()
    assert lines[2].split() == ["#", "Skill", "Uses", "Edits", "Last", "used", "Shared"]
    assert lines[4].split() == ["1", "skill-0", "10", "0", "2026-10-01", "Yes"]
    assert "|" not in lines[4]
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
                           thread_id="", actor_id="7", actor_name="alex", scope_id="")


def test_private_route_matrix() -> None:
    local = {"platform": "", "source": "cli", "chat_type": "", "chat_id": "", "session_key": ""}
    assert private_route(None, False, local) == {
        "kind": "local", "native": False, "session_key": "",
    }
    tui = {**local, "source": "tui", "session_key": "sk-1"}
    assert private_route(None, False, tui)["session_key"] == "sk-1"
    assert private_route(_invocation(), False, local)["origin"] == {
        "platform": "telegram", "chat_id": "42", "chat_type": "private",
        "user_id": "7", "user_name": "alex",
    }
    assert private_route(None, True, local) is None
    group = {"platform": "discord", "source": "", "chat_type": "group", "chat_id": "9"}
    assert private_route(None, False, group) is None
    assert private_route(None, False, {**group, "chat_type": "dm"})["kind"] == "messaging"
    assert private_route(None, False, {**group, "chat_type": ""}) is None
    assert private_route(None, False, {**group, "platform": "Discord", "chat_type": "dm"})[
        "origin"]["platform"] == "discord"
    for shared_route in ("webhook", "msgraph_webhook", "api_server", "kanban", "gateway", "tool"):
        assert private_route(None, False, {"source": shared_route, "session_key": "k"}) is None
    assert is_messaging({"platform": "", "source": "telegram"})
    assert private_route(_invocation(), False, local)["native"] is True
    assert private_route(None, False, tui)["native"] is False


def test_bound_session_platform_outranks_process_platform(monkeypatch) -> None:
    bound = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_TYPE": "group"}
    fake = SimpleNamespace(get_session_env=lambda name, default="": bound.get(name, default))
    monkeypatch.setattr(top_skills, "import_module", lambda _name: fake)
    monkeypatch.setenv("HERMES_PLATFORM", "cli")
    surface = top_skills.session_surface()
    assert surface["platform"] == "discord"
    assert private_route(None, False, surface) is None


def test_private_chat_types_match_native_admission() -> None:
    for chat_type in ("dm", "private", "im"):
        assert top_skills.is_private_chat(chat_type)
    for chat_type in ("direct", "group", "channel", ""):
        assert not top_skills.is_private_chat(chat_type)


def test_capability_probe_requires_the_share_resolver(monkeypatch) -> None:
    full = _api({})
    monkeypatch.setattr(top_skills, "import_module", lambda _name: full)
    assert top_skills.load_usage_api() is full
    partial = SimpleNamespace(**{key: value for key, value in vars(full).items()
                                 if key != "_find_skill_dir"})
    monkeypatch.setattr(top_skills, "import_module", lambda _name: partial)
    assert top_skills.load_usage_api() is None


def test_real_capture_reports_a_pending_gateway_command() -> None:
    capture = InvocationCapture()
    assert not capture.command_pending()
    capture._value.set(SimpleNamespace(command="wisdom"))  # pylint: disable=protected-access
    assert capture.command_pending()
    capture._value.set(SimpleNamespace(command=""))  # pylint: disable=protected-access
    assert not capture.command_pending()


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
        assert "| # | Skill | Uses | Edits | Last used | Shared |" in reply
        assert "| 1 | `secret-skill` | 4 | 1 | never |  |" in reply
        assert _reply(runtime.slash("top 0")) == "Usage: /wisdom top [page]"
    finally:
        runtime.native.close()


def test_pending_command_is_read_before_admission_consumes_it(tmp_path, monkeypatch) -> None:
    _seed(tmp_path, monkeypatch)
    runtime = _runtime(tmp_path, monkeypatch)
    state = {"pending": True}

    def consume(_raw):
        state["pending"] = False

    monkeypatch.setattr(runtime.native, "consume_request", consume)
    monkeypatch.setattr(runtime.native, "command_pending", lambda: state["pending"])
    try:
        assert _reply(runtime.slash("top")) == refusal("top")
    finally:
        runtime.native.close()


def test_chat_top_is_a_markdown_table_with_code_hints() -> None:
    from hermes_wisdom_plugin.top_command import render_page

    items = [{"name": "alpha", "uses": 42, "edits": 3, "last_used": "2026-10-20T10:00:00",
              "shared": True},
             {"name": "odd|name`x", "uses": 2, "edits": 0, "last_used": None, "shared": False}]
    data = {"state": "ok", "page": 2, "per_page": 2, "pages": 3, "total": 6, "items": items}
    text = render_page(data, next_command="/wisdom top {page}",
                       previous_command="/wisdom top {page}", markdown=True)
    lines = text.splitlines()
    assert lines[0] == "**Your top skills (page 2 of 3, 6 skills)**"
    assert lines[2] == "| # | Skill | Uses | Edits | Last used | Shared |"
    assert lines[3] == "|---:|:---|---:|---:|:---|:---|"
    assert lines[4] == "| 3 | `alpha` | 42 | 3 | 2026-10-20 | ✓ |"
    # A pipe in a skill name cannot split the row; a backtick cannot end the code span.
    assert lines[5] == "| 4 | `odd\\|name'x` | 2 | 0 | never |  |"
    assert lines[-1] == "Previous: `/wisdom top 1` · Next: `/wisdom top 3`"
