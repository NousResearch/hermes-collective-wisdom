"""Native Hermes loading and lazy command-registration tests."""
# pylint: disable=missing-class-docstring,missing-function-docstring

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from test_notices import Page, Service as FeedService, event

from installed_test_support import JsonState
from hermes_wisdom_plugin.commands import read_unavailable
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomUnavailable
from hermes_wisdom_plugin.runtime import CommandRuntime
from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.notices import EndTurnNotices


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_native_plugin() -> tuple[str, ModuleType]:
    """Load the root entry point the way Hermes loads a native plugin."""
    module_name = "_hermes_native_test.hermes_collective_wisdom"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PROJECT_ROOT / "__init__.py",
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module_name, module


def unload_native_plugin(module_name: str) -> None:
    """Remove the synthetic plugin package and its children."""
    for loaded_name in tuple(sys.modules):
        if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
            del sys.modules[loaded_name]


def test_root_loader_uses_synthetic_namespace_relative_import() -> None:
    """The native loader resolves its implementation below a synthetic package."""
    before = set(sys.modules)
    module_name, plugin = load_native_plugin()
    try:
        assert plugin.register.__module__ == f"{module_name}.hermes_wisdom_plugin"
        newly_loaded = set(sys.modules) - before
        assert not newly_loaded.intersection({"agent", "hermes_cli", "tools"})
    finally:
        unload_native_plugin(module_name)


def test_register_is_lazy_and_owns_both_command_handles(  # pylint: disable=too-many-locals
    tmp_path, monkeypatch
) -> None:
    """Signed-out registration creates no local state or network clients."""
    module_name, plugin = load_native_plugin()
    try:
        runtime_module = __import__(
            f"{module_name}.hermes_wisdom_plugin.runtime", fromlist=["CommandRuntime"]
        )
        monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", tmp_path))
        monkeypatch.setattr(
            runtime_module.HermesAccountAdapter, "current",
            lambda: (_ for _ in ()).throw(AssertionError("registration read credentials")),
        )
        entries: dict[str, Any] = {"hooks": [], "tools": {}, "config": {}}
        disposed = []

        def register_cli(name, _help, setup, handler):
            entries["cli"] = (name, setup, handler)
            return SimpleNamespace(dispose=lambda: disposed.append("cli"))

        def register_slash(name, handler, **_kwargs):
            entries["slash"] = (name, handler)
            return SimpleNamespace(dispose=lambda: disposed.append("slash"))

        def register_tool(**definition):
            entries["tools"][definition["name"]] = definition
            return SimpleNamespace(dispose=lambda: disposed.append(definition["name"]))

        def set_config(key, value):
            entries["config"][key] = value

        state = JsonState(tmp_path / "plugin-data" / "wisdom")
        state.data_dir = tmp_path / "plugin-data" / "wisdom"
        context = SimpleNamespace(
            state=state,
            register_cli_command=register_cli,
            register_command=register_slash,
            register_tool=register_tool,
            register_hook=lambda name, callback: (
                entries["hooks"].append((name, callback))
                or SimpleNamespace(dispose=lambda: disposed.append(name))
            ),
            register_auxiliary_task=lambda key, **details: (
                entries.update(auxiliary=(key, details))
                or SimpleNamespace(dispose=lambda: disposed.append(key))
            ),
            register_platform_handler=lambda name, callback: entries.update(
                slack=(name, callback)
            ),
            register_telegram_handler=lambda callback: entries.update(telegram=callback),
            get_config=lambda key, default=None: (
                True if key == "qualification.allow_model_review"
                else entries["config"].get(key, default)
            ),
            set_config=set_config,
            llm=object(),
            spawn_task=lambda *_args, **_kwargs: None,
            on_unload=lambda callback: entries.update(unload=callback),
        )
        assert plugin.register(context) is None
        assert entries["auxiliary"][0] == "hermes_collective_wisdom_refinement"
        assert entries["cli"][0] == entries["slash"][0] == "wisdom"
        assert [name for name, _callback in entries["hooks"]] == [
            "pre_gateway_dispatch", "pre_llm_call",
            "on_skill_lifecycle", "transform_llm_output", "on_session_end"
        ]
        assert entries["slack"][0] == "slack"
        assert callable(entries["telegram"])
        assert set(entries["tools"]) == {
            "wisdom_inbox", "wisdom_inspect", "present_wisdom_consent",
        }
        manifest = yaml.safe_load((PROJECT_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        assert set(manifest["provides_hooks"]) == {
            name for name, _callback in entries["hooks"]
        }
        assert set(manifest["provides_tools"]) == set(entries["tools"])
        assert all(
            definition["toolset"] == "skills"
            and callable(definition["handler"])
            and callable(definition["check_fn"])
            for definition in entries["tools"].values()
        )
        owner = entries["slash"][1].__self__
        service = owner._service()  # pylint: disable=protected-access
        monkeypatch.setattr(service, "native_owner", lambda: {"organization_id": "org"})
        assert service.notification_mode() == "agent"
        service.set_notification_mode("fixed")
        assert entries["config"] == {"notifications.delivery_mode": "fixed"}
        assert service.notification_mode() == "fixed"
        service.close()
        assert not list(tmp_path.iterdir())
        entries["unload"]()
        entries["unload"]()
        assert disposed == [
            "slash", "cli", "present_wisdom_consent", "wisdom_inspect", "wisdom_inbox",
            "on_session_end", "transform_llm_output", "on_skill_lifecycle",
            "pre_llm_call", "pre_gateway_dispatch",
            "hermes_collective_wisdom_refinement",
        ]
    finally:
        unload_native_plugin(module_name)


@pytest.mark.parametrize("platform", ["cli", "tui"])
# pylint: disable=too-many-locals,too-many-statements
def test_registered_end_turn_awareness_is_private_and_owner_bound(
    tmp_path, monkeypatch, platform,
):
    """Registered hook → real notice projection, including restart and owner switch."""
    module_name, plugin = load_native_plugin()
    try:
        runtime_module = __import__(
            f"{module_name}.hermes_wisdom_plugin.runtime", fromlist=["current_profile"]
        )
        monkeypatch.setattr(runtime_module, "current_profile", lambda: ("default", tmp_path))
        stored = JsonState(tmp_path / "plugin-data" / "wisdom")
        stored.data_dir = tmp_path / "plugin-data" / "wisdom"
        owner = {"profile_name": "default", "account_id": "alice",
                 "organization_id": "team", "installation_id": "installed"}
        state = DirectState(stored, stored.data_dir)
        state.set_identity({"owner": owner})
        feed = FeedService([Page([event()], "after", False), Page([], "later", False)])
        hooks = {}
        context = SimpleNamespace(
            state=stored, llm=None, spawn_task=lambda *_a, **_k: None,
            register_hook=lambda name, callback: hooks.update({name: callback}) or None,
            register_auxiliary_task=lambda *_a, **_k: None,
            register_platform_handler=lambda *_a: None,
            register_telegram_handler=lambda *_a: None,
            register_tool=lambda **_k: None,
            register_cli_command=lambda *_a: None,
            register_command=lambda *_a, **_k: None,
            on_unload=lambda _callback: None,
        )
        plugin.register(context)
        command = next(cell.cell_contents for cell in hooks["on_session_end"].__closure__
                       if type(cell.cell_contents).__name__ == "CommandRuntime")
        calls = []
        nested = []

        class LocalService:
            def native_owner(self):
                return dict(owner)

            def end_turn_notices(self, renderer):
                calls.append(renderer)
                if nested:
                    second = nested.pop()
                    hooks["pre_llm_call"](**second)
                    hooks["on_session_end"](**{
                        **second, "completed": True, "failed": False, "interrupted": False,
                    })
                return EndTurnNotices(state, lambda: feed).run(renderer=renderer)

            def close(self):
                return None

        monkeypatch.setattr(command, "_service", LocalService)
        facts = {"platform": platform, "session_id": "s", "task_id": "t", "turn_id": "turn"}
        completed = {**facts, "completed": True, "failed": False, "interrupted": False}
        hooks["on_session_end"](**completed)  # no admission
        nested.append({**facts, "task_id": "simultaneous"})
        hooks["pre_llm_call"](**facts)
        hooks["on_session_end"](**completed)
        assert calls == [None]
        assert state.notice_state()["awareness_cursor"] == "after"
        assert state.notice_state()["cursor"] is None
        assert state.known_publications()[0]["inspect_reference"] == "skill-1@v2"
        assert (DirectState(stored, stored.data_dir).known_publications()
                == state.known_publications())
        hooks["on_session_end"](**completed)  # duplicate end
        async def loop_turn():
            hooks["pre_llm_call"](**{**facts, "task_id": "event-loop"})
            hooks["on_session_end"](**{**completed, "task_id": "event-loop"})

        asyncio.run(loop_turn())
        assert calls == [None]  # no synchronous feed/network on an active loop
        hooks["pre_llm_call"](**{**facts, "parent_session_id": "parent"})
        hooks["on_session_end"](**completed)
        assert calls == [None]
        hooks["pre_llm_call"](**facts)
        owner["account_id"] = "bob"
        hooks["on_session_end"](**completed)
        assert calls == [None]
        state.set_identity({"owner": owner})
        assert not state.known_publications()
        hooks["pre_llm_call"](**facts)
        hooks["on_session_end"](**completed)
        assert calls == [None, None]
    finally:
        unload_native_plugin(module_name)


@pytest.mark.parametrize("raw, expected", [
    ("update skill", ("begin_update", ("skill",))),
    ("publish draft", ("begin_publication", ("draft",))),
    ("suggest local-skill", ("begin_suggest", ("local-skill",))),
    ("browse query", ("begin_catalog", ("browse", "query"))),
    ('browse "incident handoff"', ("begin_catalog", ("browse", "incident handoff"))),
    ("settings", ("begin_settings", ())),
    ("status", ("read", ("status", []))),
    ("help", ("read", ("help", []))),
    ("setup", ("read", ("setup", []))),
    ("installed-setup skill", ("read", ("installed-setup", ["skill"]))),
    ("", ("read", ("help", []))),
    ("\"", "Invalid Wisdom command. Use /wisdom help."),
])
def test_admitted_slash_routes_only_explicit_native_actions(tmp_path, monkeypatch, raw, expected):
    """Read/enrollment commands cannot accidentally become install effects."""
    runtime = CommandRuntime(
        tmp_path, ('default', tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runtime.native, 'consume_request', lambda _raw: object())
    for name in (
        'begin_update', 'begin_publication',
        'begin_suggest', 'begin_catalog', 'begin_settings', 'begin',
    ):
        monkeypatch.setattr(
            runtime.native, name, lambda _invocation, *args, name=name: (name, args),
        )

    async def read_view(value):
        return ('read', value)

    monkeypatch.setattr(runtime, '_slash', read_view)
    try:
        result = runtime.slash(raw)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        assert result == expected
    finally:
        runtime.native.close()


@pytest.mark.parametrize("raw, method", [
    ("browse incident", "begin_catalog"), ("settings", "begin_settings"),
])
def test_admitted_native_card_does_not_echo_internal_review_id(tmp_path, monkeypatch, raw, method):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: object())
    monkeypatch.setattr(
        runtime.native, method,
        lambda *_args: {"state": "queued", "review_id": "internal-only"},
    )
    try:
        assert runtime.slash(raw) == ""
    finally:
        runtime.native.close()


@pytest.mark.parametrize("raw", ["", "help", "--help", "-h", "--help extra", "-h extra"])
def test_unadmitted_slash_help_aliases_render_help(tmp_path, monkeypatch, raw):
    """Empty input and documented aliases share the local help view."""
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    service = type("Service", (), {"close": lambda self: None})()
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: None)
    monkeypatch.setattr(runtime, "_service", lambda: service)
    try:
        pending = runtime.slash(raw)
        assert inspect.isawaitable(pending)
        output = asyncio.run(pending)
        assert output.startswith("Collective Wisdom commands")
    finally:
        runtime.native.close()


def test_slash_worker_receives_parsed_command_and_captured_profile(tmp_path, monkeypatch):
    """The bounded child receives words and profile scope, never reparses raw text."""
    runtime = CommandRuntime(
        tmp_path, ("fixture", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    captured = {}

    class Child:
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            if payload is not None:
                captured["request"] = json.loads(payload)
                captured["timeout"] = timeout
                return json.dumps("rendered"), ""
            return "", ""

        @staticmethod
        def poll():
            return 0

    def popen(*_args, **kwargs):
        captured["environment"] = kwargs["env"]
        return Child()

    monkeypatch.setattr("hermes_wisdom_plugin.runtime.subprocess.Popen", popen)
    try:
        result = runtime._run_worker(  # pylint: disable=protected-access
            ("browse", ["incident handoff"]), {"account_id": "owner"}, threading.Event()
        )
        assert result == "rendered"
        assert captured["request"]["command"] == ["browse", ["incident handoff"]]
        assert captured["environment"]["HERMES_HOME"] == str(tmp_path)
        assert 0 < captured["timeout"] <= 0.1
    finally:
        runtime.native.close()


@pytest.mark.parametrize("error, expected", [
    (WisdomUnavailable(), "Run /wisdom browse 'incident handoff' again to retry."),
    (WisdomAuthError("owner changed"), "Complete setup in your terminal."),
])
def test_slash_runtime_preserves_transient_vs_owner_errors(tmp_path, monkeypatch, error, expected):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    class Service:
        def binding(self):
            return {"account_id": "owner"}

        def check_owner(self):
            return None

        def close(self):
            return None

    service = Service()
    monkeypatch.setattr(runtime, "_service", lambda: service)
    monkeypatch.setattr(runtime, "_run_worker", lambda *_args: (_ for _ in ()).throw(error))
    try:
        result = asyncio.run(runtime._slash(("browse", ["incident handoff"])))  # pylint: disable=protected-access
        assert expected in result
        assert ("again to retry" in result) is isinstance(error, WisdomUnavailable)
    finally:
        runtime.native.close()


def test_slash_worker_deadline_offers_manual_retry_without_replay(tmp_path, monkeypatch):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    calls = []

    class Child:
        returncode = None

        def communicate(self, _payload=None, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise __import__("subprocess").TimeoutExpired("worker", timeout)
            return "", ""

        def poll(self):
            return None

        def kill(self):
            calls.append("killed")

    monkeypatch.setattr("hermes_wisdom_plugin.runtime.subprocess.Popen", lambda *_a, **_k: Child())
    monkeypatch.setattr("hermes_wisdom_plugin.runtime.SLASH_BUDGET_SECONDS", 0)
    try:
        result = runtime._run_worker(("list", []), {}, threading.Event())  # pylint: disable=protected-access
        assert result == (
            "Collective Wisdom is temporarily unavailable.\n"
            "Run /wisdom list again to retry."
        )
        assert calls == ["killed", None]
    finally:
        runtime.native.close()


def test_slash_retry_affordance_excludes_effects_and_terminal_handoffs():
    for command in ("install", "update", "publish", "setup", "installed-setup"):
        assert read_unavailable((command, ["skill-1"])) is None


def test_slash_unavailable_effect_handoff_does_not_offer_replay(tmp_path, monkeypatch):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path),
        llm=None, spawn_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runtime, "_service", lambda: (_ for _ in ()).throw(
        WisdomUnavailable()
    ))
    try:
        assert asyncio.run(runtime._slash(("install", ["skill-1"]))) == (  # pylint: disable=protected-access
            "Collective Wisdom is temporarily unavailable."
        )
    finally:
        runtime.native.close()
