"""Native Hermes loading and lazy command-registration tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

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


def test_register_is_lazy_and_owns_both_command_handles(tmp_path, monkeypatch) -> None:
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

        context = SimpleNamespace(
            state=SimpleNamespace(data_dir=tmp_path / "plugin-data" / "wisdom"),
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
            "pre_gateway_dispatch", "pre_llm_call", "on_skill_lifecycle", "on_session_end"
        ]
        assert entries["slack"][0] == "slack"
        assert callable(entries["telegram"])
        assert set(entries["tools"]) == {
            "wisdom_inbox", "wisdom_inspect", "present_wisdom_consent",
        }
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
            "on_session_end", "on_skill_lifecycle",
            "pre_llm_call", "pre_gateway_dispatch",
            "hermes_collective_wisdom_refinement",
        ]
    finally:
        unload_native_plugin(module_name)
