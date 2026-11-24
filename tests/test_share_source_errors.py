"""Missing share sources are permanent, actionable errors on every surface."""
# pylint: disable=missing-function-docstring,protected-access
import argparse
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from installed_test_support import JsonState
from test_commands import _native_invocation
from test_contribution_review import reviewer
from test_share_command import ShareCli

from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.desktop_api import build_router
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.runtime import CommandRuntime


def _expected(name, roots):
    return f"`{name}` isn't a local skill on this profile. Hermes searches: {', '.join(map(str, roots))}."


def test_missing_source_reports_all_search_roots(tmp_path, monkeypatch):
    value, _source, _owner = reviewer(tmp_path)
    external = tmp_path / "shared"
    monkeypatch.setattr(value.host, "skill_search_roots", lambda: [
        (1, value.host.skills_root), (3, external)], raising=False)
    with pytest.raises(WisdomNotFound) as missing:
        value.source("unknown-skill")
    assert str(missing.value) == _expected("unknown-skill", [value.host.skills_root, external])


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_unknown_source_never_says_try_again(tmp_path, monkeypatch):
    value, _source, _owner = reviewer(tmp_path)
    runtime = CommandRuntime(tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
                             llm=None, spawn_task=lambda coroutine, **_kwargs: coroutine.close())
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())
    monkeypatch.setattr(runtime.native, "begin_suggest", lambda _caller, name: value.prepare(name))
    try:
        reply = runtime.slash("share unknown-skill")
    finally:
        runtime.close()
    assert reply == _expected("unknown-skill", [value.host.skills_root])
    assert "try again" not in reply.lower()


def test_direct_cli_unknown_source_keeps_actionable_message(tmp_path, capsys):
    service = ShareCli(tmp_path)
    parser = argparse.ArgumentParser()
    build_parser(parser)
    args = parser.parse_args(["share", "unknown-skill", "--yes", "--json"])
    assert run_cli(args, service) == 1
    assert _expected("unknown-skill", [service.reviewer.host.skills_root]) in capsys.readouterr().out


def test_desktop_unknown_source_returns_specific_404(tmp_path):
    value, _source, binding = reviewer(tmp_path)

    class Service:
        """Minimal owner-bound Desktop adapter over the real source resolver."""
        def __init__(self):
            self.root = tmp_path
            self.profile = ("default", tmp_path)
            self._state = JsonState(tmp_path)

        def native_owner(self):
            return dict(binding)

        def suggest(self, name, **_kwargs):
            return value.prepare(name)

        def close(self):
            return None

    app = FastAPI()
    app.include_router(build_router(Service))
    with TestClient(app) as http:
        view = http.post("/views").json()["view_id"]
        response = http.post("/drafts/prepare", json={"view_id": view, "skill_name": "unknown-skill"})
    assert response.status_code == 404, response.text
    assert json.loads(response.text)["detail"] == _expected("unknown-skill", [value.host.skills_root])
