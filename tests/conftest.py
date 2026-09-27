"""Minimal lifecycle contract for plugin tests that run without Hermes installed."""

import sys
from dataclasses import dataclass
from types import ModuleType

import pytest


@pytest.fixture
def private_lifecycle_contract(monkeypatch):
    """Model the public request shape; host integration is tested separately."""
    @dataclass(frozen=True)
    class SubagentLaunchRequest:
        """Only the request fields consumed by the plugin's classifier."""

        goal: str
        context: str | None = None
        role: str = "leaf"
        model: str | None = None
        allowed_toolsets: tuple[str, ...] | None = None
        parent_session_id: str | None = None
        private_default_route: bool = False

    parent = sys.modules.get("agent")
    if parent is None:
        parent = ModuleType("agent")
        parent.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", parent)
    module = ModuleType("agent.subagent_lifecycle")
    setattr(module, "SubagentLaunchRequest", SubagentLaunchRequest)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)
    monkeypatch.setattr(parent, "subagent_lifecycle", module, raising=False)
