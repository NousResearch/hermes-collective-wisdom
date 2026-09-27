"""Exercise the candidate host's actual lifecycle route with an offline child response."""
# pylint: disable=missing-function-docstring,protected-access
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.context_relevance import relevant_skill


def test_candidate_host_private_route_with_fake_model_response(tmp_path, monkeypatch):
    secret_scope = pytest.importorskip("agent.secret_scope")
    lifecycle_module = pytest.importorskip("agent.subagent_lifecycle")
    constants = pytest.importorskip("hermes_constants")
    agent_class = pytest.importorskip("run_agent").AIAgent
    lifecycle_class = lifecycle_module.SubagentLifecycleService
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: deepseek\n  default: test/default\n")
    (home / ".env").write_text("DEEPSEEK_API_KEY=fixture-only\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("run_agent._hermes_home", home)
    parent = SimpleNamespace(session_id="session-1", model="other/model", provider="openrouter",
                             api_key="parent-fixture", base_url="https://openrouter.ai/api/v1",
                             enabled_toolsets=["terminal"], disabled_toolsets=[],
                             _fallback_chain=[], _delegate_depth=0)
    seen = []

    def reply(child, *, user_message, **_kwargs):
        seen.append((child.provider, child.model, child.tools, child._persist_disabled,
                     child._session_db, user_message))
        return {"final_response": "0"}

    monkeypatch.setattr(agent_class, "run_conversation", reply)
    token = constants.set_hermes_home_override(str(home))
    scope = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(home))
    try:
        service = lifecycle_class(lambda: parent)
        row = {"slug": "incident", "author_description": "Triage incidents",
               "skill_id": "private-id", "version": 2}
        assert relevant_skill(service, "Help triage an incident", [row], "session-1") is row
    finally:
        secret_scope.reset_secret_scope(scope)
        constants.reset_hermes_home_override(token)
    assert len(seen) == 1
    assert seen[0][:2] == ("deepseek", "test/default")
    assert seen[0][2] == [] and seen[0][3] and seen[0][4] is None
    assert "private-id" not in seen[0][5]


def test_candidate_host_refuses_missing_or_changed_parent():
    lifecycle_module = pytest.importorskip("agent.subagent_lifecycle")
    service = lifecycle_module.SubagentLifecycleService(lambda: None)
    row = {"slug": "incident", "author_description": "Triage incidents"}
    assert relevant_skill(service, "incident", [row], "session-1") is None
    service = lifecycle_module.SubagentLifecycleService(
        lambda: SimpleNamespace(session_id="different-session"),
    )
    assert relevant_skill(service, "incident", [row], "session-1") is None
