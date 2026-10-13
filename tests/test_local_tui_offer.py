"""Exact-turn text offer admission through the registered plugin hooks."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-many-locals,duplicate-code
from types import SimpleNamespace

import pytest

from test_native_plugin import load_native_plugin, unload_native_plugin, JsonState


@pytest.mark.parametrize("platform", ["cli", "tui"])
@pytest.mark.parametrize("guard", ["none", "mute", "suppressed", "policy",
                                   "owner", "child", "profile"])
def test_registered_local_transform(full_release_scope, tmp_path, monkeypatch, platform, guard):
    module_name, plugin = load_native_plugin()
    full_release_scope(f"{module_name}.hermes_wisdom_plugin")
    try:
        runtime_module = __import__(
            f"{module_name}.hermes_wisdom_plugin.runtime", fromlist=["current_profile"]
        )
        active_profile = ["default"]
        monkeypatch.setattr(runtime_module, "current_profile",
                            lambda: (active_profile[0], tmp_path))
        stored = JsonState(tmp_path / "plugin-data" / "wisdom")
        stored.data_dir = tmp_path / "plugin-data" / "wisdom"
        owner = {"profile_name": "default", "account_id": "alice",
                 "organization_id": "team", "installation_id": "installed"}
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
        lifecycle = next(cell.cell_contents for cell in hooks["on_skill_lifecycle"].__closure__
                         if type(cell.cell_contents).__name__ == "LifecycleBridge")
        monkeypatch.setattr(lifecycle, "on_skill_lifecycle", lambda **facts:
                            "event" if facts.get("action") == "loaded" else None)
        calls = []
        muted = guard == "mute"

        class LocalService:
            qualification_state = SimpleNamespace(local_event=lambda _id: {
                "payload": {"preparation": {"draft_id": "local:skill:draft123"}},
            })

            def native_owner(self):
                return dict(owner)

            def notice_candidates(self):
                return [{"event_id": "event", "category": "skill_ready_to_share",
                         "local_skill_id": "local", "content_hash": "hash"}]

            def notice_policy(self):
                return {"skill_ready_to_share": guard != "policy"}

            def notice_preferences(self, _references):
                return {"muted": muted, "suppressed": {
                    "candidate:local:hash": True} if guard == "suppressed" else {}}

            def prepare_candidate_offer(self, event_id):
                calls.append(("prepare", event_id))
                return {"kind": "candidate", "facts": {
                    "name": "Original", "editorial_status": "complete",
                                  "editorial": {"editorial_name": "Useful title",
                                                "editorial_description": "Safe description",
                                                "coworker_benefit": "Saves teammates time"}}}

            def end_turn_notices(self, renderer, *, candidate_event_id=None):
                calls.append(("notice", candidate_event_id))
                renderer([{"event_id": candidate_event_id}])

            def close(self):
                return None

        monkeypatch.setattr(command, "_service", LocalService)
        pre = {"platform": platform, "session_id": "s", "task_id": "task", "turn_id": "turn"}
        if guard == "child":
            pre["parent_session_id"] = "parent"
        transform = {"platform": platform, "session_id": "s", "turn_id": "turn",
                     "response_text": "Unchanged answer."}
        assert hooks["transform_llm_output"](**transform) is None
        hooks["pre_llm_call"](**pre)
        assert hooks["transform_llm_output"](**transform) is None
        hooks["on_skill_lifecycle"](action="loaded", task_id="other", session_id="s")
        assert hooks["transform_llm_output"](**transform) is None
        hooks["on_skill_lifecycle"](action="loaded", task_id="task", session_id="s")
        assert hooks["transform_llm_output"](**{**transform, "turn_id": "other"}) is None
        if guard == "owner":
            owner["account_id"] = "bob"
        if guard == "profile":
            active_profile[0] = "other"
        result = hooks["transform_llm_output"](**transform)
        if guard != "none":
            assert result is None
            assert not calls
            return
        assert result.startswith("Unchanged answer.\n\nCollective Wisdom")
        assert all(text in result for text in (
            "Useful title", "Safe description", "Saves teammates time", "team",
            "Gateway security: pending until submission",
            "hermes wisdom prepared review local:skill:draft123",
            "Nothing has been uploaded or published",
        ))
        assert calls == [("prepare", "event"), ("notice", "event")]
        assert hooks["transform_llm_output"](**transform) is None
        hooks["on_session_end"](**{**pre, "completed": False, "failed": True,
                                    "interrupted": False})
        assert hooks["transform_llm_output"](**transform) is None
    finally:
        unload_native_plugin(module_name)
