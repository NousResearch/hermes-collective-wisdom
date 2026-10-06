"""The manual-sharing release registers only user-initiated sharing surfaces."""

# pylint: disable=missing-function-docstring,import-outside-toplevel

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from installed_test_support import JsonState

from hermes_wisdom_plugin import release_scope
from hermes_wisdom_plugin.commands import build_parser, parse_slash, run_slash
from hermes_wisdom_plugin.desktop_api import build_router
from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.runtime import CommandRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HIDDEN_CLI = ("settings", "notifications", "mute", "inbox", "maintenance")
HIDDEN_SLASH = ("settings", "mute", "candidate", "suggest", "publish")


def _leaf_commands(parser: argparse.ArgumentParser) -> set[str]:
    leaves: set[str] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        actions = [action for action in current._actions  # pylint: disable=protected-access
                   if isinstance(action, argparse._SubParsersAction)]  # pylint: disable=protected-access
        if not actions:
            leaves.add(" ".join(prefix))
            return
        for name, child in actions[0].choices.items():
            walk(child, (*prefix, name))

    walk(parser, ())
    return leaves


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    return parser


def _load_root_plugin():
    module_name = "_hermes_release_scope_test.hermes_collective_wisdom"
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / "__init__.py",
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module_name, module


def _unload(module_name: str) -> None:
    for loaded in tuple(sys.modules):
        if loaded == module_name or loaded.startswith(module_name + "."):
            del sys.modules[loaded]


def test_active_release_is_manual_sharing() -> None:
    assert release_scope.active() is release_scope.MANUAL_SHARING


def test_registration_exposes_only_the_slash_dispatch_hook(tmp_path, monkeypatch) -> None:
    module_name, plugin = _load_root_plugin()
    runtime_module = importlib.import_module(f"{module_name}.hermes_wisdom_plugin.runtime")
    monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", tmp_path))
    entries: dict = {"hooks": [], "tools": [], "auxiliary": []}
    state = JsonState(tmp_path / "plugin-data")
    state.data_dir = tmp_path / "plugin-data"
    handle = SimpleNamespace(dispose=lambda: None)
    context = SimpleNamespace(
        state=state, llm=object(), spawn_task=lambda *_a, **_k: None,
        register_cli_command=lambda name, *_a: entries.update(cli=name) or handle,
        register_command=lambda name, _handler, **kwargs: entries.update(
            slash=name, args_hint=kwargs.get("args_hint")) or handle,
        register_tool=lambda **definition: entries["tools"].append(definition["name"]) or handle,
        register_hook=lambda name, _callback: entries["hooks"].append(name) or handle,
        register_auxiliary_task=lambda key, **_k: entries["auxiliary"].append(key) or handle,
        register_platform_handler=lambda name, _callback: entries.update(slack=name),
        register_telegram_handler=lambda _callback: entries.update(telegram=True),
        get_config=lambda _key, default=None: default,
        set_config=lambda *_a: None,
        on_unload=lambda callback: entries.update(unload=callback),
    )
    try:
        plugin.register(context)
        assert entries["hooks"] == ["pre_gateway_dispatch"]
        assert not entries["tools"]
        assert not entries["auxiliary"]
        assert entries["cli"] == entries["slash"] == "wisdom"
        assert entries["slack"] == "slack" and entries["telegram"] is True
        hint = entries["args_hint"]
        assert "share" in hint and not any(token in hint.split() for token in HIDDEN_SLASH)
        manifest = yaml.safe_load((PROJECT_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        assert manifest.get("provides_hooks") == list(release_scope.MANUAL_SHARING.hooks)
        assert manifest.get("provides_tools", []) == []
        entries["unload"]()
    finally:
        _unload(module_name)


def test_cli_registers_manual_sharing_commands_only() -> None:
    leaves = _leaf_commands(_parser())
    top = {leaf.split()[0] for leaf in leaves}
    assert top == set(release_scope.MANUAL_SHARING.cli_commands)
    assert "share" in top and "install" in top and "publication" in top
    assert not set(HIDDEN_CLI) & top
    assert "publication status" in leaves
    for command in HIDDEN_CLI:
        with pytest.raises(SystemExit):
            _parser().parse_args([command])


def test_cli_does_not_offer_model_written_editorial_copy() -> None:
    parser = _parser()
    for argv in (["suggest", "skill", "--allow-model-review"],
                 ["suggest", "skill", "--no-model-review"],
                 ["prepared", "review", "draft", "--allow-model-review"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    assert parser.parse_args(["suggest", "skill"]).allow_model_review is False


def test_cli_help_json_lists_only_registered_commands(capsys) -> None:
    from hermes_wisdom_plugin.commands import run_cli
    args = _parser().parse_args(["help", "--json"])
    run_cli(args, SimpleNamespace())
    listed = capsys.readouterr().out
    assert "share" in listed
    assert not any(f'"{command}"' in listed for command in HIDDEN_CLI)


@pytest.mark.parametrize("command", HIDDEN_SLASH)
def test_shared_chat_slash_rejects_hidden_commands(command) -> None:
    rendered = run_slash(parse_slash(command + " target"), SimpleNamespace())
    assert rendered.startswith("Unsupported Collective Wisdom command.")


def test_slash_help_lists_no_hidden_commands() -> None:
    rendered = run_slash(parse_slash("help"), SimpleNamespace())
    assert "/wisdom share <skill>" in rendered
    for command in HIDDEN_SLASH:
        assert f"/wisdom {command}" not in rendered


@pytest.mark.parametrize("command", HIDDEN_SLASH)
def test_admitted_private_slash_never_opens_hidden_reviews(tmp_path, monkeypatch, command):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_a, **_k: None,
    )
    opened = []
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: object())
    for name in ("begin_update", "begin_publication", "begin_suggest", "begin_catalog",
                 "begin_settings", "begin", "begin_candidate"):
        monkeypatch.setattr(runtime.native, name,
                            lambda *_a, name=name: opened.append(name) or {"state": "queued"})
    try:
        result = runtime.slash(command + " target")
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        assert not opened
        assert result.startswith("Unsupported Collective Wisdom command.")
    finally:
        runtime.native.close()


def test_admitted_private_share_still_opens_the_owner_review(tmp_path, monkeypatch):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: object())
    monkeypatch.setattr(runtime.native, "begin_suggest",
                        lambda _invocation, name: {"state": "queued", "review_id": name})
    try:
        assert runtime.slash("share my-skill") == ""
    finally:
        runtime.native.close()


class _DesktopService:
    def status(self, include_operations=False):
        del include_operations
        return {"configured": True, "pending_operations": []}

    def notification_config(self):
        raise AssertionError("notification settings are not part of this release")

    def inbox(self, **_kwargs):
        raise AssertionError("proactive activity is not part of this release")

    def list_installations(self):
        return []

    def close(self):
        return None


def test_desktop_routes_exclude_suggestions_notices_and_settings() -> None:
    router = build_router(_DesktopService)
    paths = {route.path for route in router.routes}
    assert "/publication/confirm" in paths and "/install/confirm" in paths
    assert not {path for path in paths
                if path.startswith(("/candidates", "/notices", "/settings"))}
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/bootstrap").json()
    assert body["surfaces"] == {"notification_settings": False, "share_suggestions": False}
    assert "settings" not in body and "activity" not in body


def test_native_review_cards_offer_no_recommendation_mute_controls() -> None:
    plan = {"operation": "update", "receipt": "r", "skill_name": "skill", "allowed": True}
    record = {"id": "rec", "operation": "update", "state": "pending", "plan": plan}
    view = project_view(record)
    offered = view.actions + view.navigation_actions + [
        action for item in view.items for action in item.actions
    ]
    assert offered
    assert not [action for action in offered
                if (action.action or "").startswith("settings")]
    assert "Mute" not in view.notice


def test_preparation_never_sends_content_to_the_model(tmp_path, monkeypatch) -> None:
    from hermes_wisdom_plugin.runtime import InvocationService
    calls = []
    service = InvocationService.__new__(InvocationService)
    service._get_config = lambda _key, default=None: default  # pylint: disable=protected-access
    monkeypatch.setattr(InvocationService, "_installation_owner", lambda self: {"id": "o"})
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    monkeypatch.setattr(InvocationService, "local_state", property(
        lambda self: SimpleNamespace(bind_owner=lambda _owner: None)))
    service._contribution_reviewer = lambda: SimpleNamespace(  # pylint: disable=protected-access
        prepare=lambda name, **kwargs: calls.append(kwargs["allow_model_review"]) or {
            "draft_id": "local:" + name})
    for requested in (None, True):
        try:
            service.suggest("skill", allow_model_review=requested)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    assert calls == [False, False]
    del tmp_path


def test_manual_preparation_uses_the_skills_own_name_and_description(tmp_path) -> None:
    import test_contribution_review as review
    value, _source, _binding = review.reviewer(tmp_path)
    result = value.prepare("reviewed-skill", allow_model_review=False)
    assert result["editorial_status"] == "complete"
    assert result["editorial_failure"] is None
    assert result["editorial_name"] == "reviewed-skill"
    assert result["editorial_description"] == "Review incident notes."
    assert not result["coworker_benefit"]


def test_manual_preparation_rejects_an_overlong_description(tmp_path) -> None:
    import test_contribution_review as review
    value, _source, _binding = review.reviewer(tmp_path)
    value.host.frontmatter = {"name": "reviewed-skill", "description": "x" * 321}
    (_source / "SKILL.md").write_text(
        "---\nname: reviewed-skill\ndescription: " + "x" * 321 + "\n---\n# Review\n",
        encoding="utf-8")
    assert value.prepare("reviewed-skill", allow_model_review=False)[
        "editorial_status"] == "incomplete"


def test_incomplete_private_share_explains_the_local_fix() -> None:
    from hermes_wisdom_plugin.native_review import _suggest_prepared_view
    view = _suggest_prepared_view({"skill_name": "reviewed-skill",
                                   "editorial_failure": "model_unavailable"})
    text = view.to_text()
    assert "SKILL.md" in text and "description" in text
    assert "model" not in text.lower()


def test_full_release_still_requires_complete_model_copy(tmp_path, full_release_scope) -> None:
    import test_contribution_review as review
    del full_release_scope
    value, _source, _binding = review.reviewer(tmp_path)
    result = value.prepare("reviewed-skill", allow_model_review=False)
    assert result["editorial_status"] == "incomplete"


def test_missing_description_asks_for_one_in_the_skill_file(tmp_path) -> None:
    import test_contribution_review as review
    from hermes_wisdom_plugin.package import PackagePolicyError
    value, source, _binding = review.reviewer(tmp_path)
    (source / "SKILL.md").write_text(
        "---\nname: reviewed-skill\n---\n# Incident review\n", encoding="utf-8")
    value.host.frontmatter = {"name": "reviewed-skill"}
    with pytest.raises(PackagePolicyError) as raised:
        value.prepare("reviewed-skill", allow_model_review=False)
    message = str(raised.value)
    assert "description" in message and "SKILL.md" in message
    assert "model" not in message


def test_private_share_review_is_ready_without_a_model_written_benefit() -> None:
    from hermes_wisdom_plugin.native_inspection import _sharing_lines
    from hermes_wisdom_plugin.native_review import _candidate_review_ready, _candidate_view
    plan = {
        "receipt": "r", "editorial_status": "complete", "editorial_name": "reviewed-skill",
        "editorial_description": "Review incident notes.", "coworker_benefit": "",
        "publication_mode": "open", "policy_version": 3,
        "authorization_expires_at": "2030-01-01T00:00:00Z", "hashes": {"content": "c"},
        "slug": "reviewed-skill",
    }
    assert _candidate_review_ready(plan)
    rendered = "\n".join(_sharing_lines(plan)) + _candidate_view(plan, "Team").summary
    assert "Review incident notes." in rendered
    assert "benefit" not in rendered.lower()
