"""Stock-host regression for cached Gateway session binding."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "416a8177c25d87aa9929dfcf31f7964137d7fcdd"
PROBE = r'''
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path[:0] = [os.environ["WISDOM_STOCK_AGENT_DIR"], os.environ["WISDOM_PLUGIN_ROOT"]]

from agent.auxiliary_client import reset_runtime_main, set_runtime_main
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import clear_session_vars, get_session_env, set_current_session_id
from hermes_cli.plugins_state import PluginState
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.runtime import current_session_context


class Adapter:
    _owner_profile = "codex"


class Transport:
    def __init__(self, adapter):
        self.adapter = adapter

    def _is_user_authorized_for_source(self, source):
        return source.user_id == "fixture-human"

    def _delivery_adapter_for(self, _source):
        return self.adapter

    def _session_key_for_source(self, _source):
        return "telegram:fixture-chat:fixture-human"


def source(message_id, *, user_id="fixture-human", profile="codex"):
    return SessionSource(
        platform=Platform.TELEGRAM, profile=profile, chat_type="dm",
        chat_id="fixture-chat", user_id=user_id, message_id=message_id,
    )


def event(route, *, internal=False):
    return SimpleNamespace(
        source=route, message_id=route.message_id, internal=internal,
        get_command=lambda: "", get_command_args=lambda: "",
    )


async def main():
    adapter = Adapter()
    gateway = Transport(adapter)
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    native = NativeSurfaceRuntime(
        Path.home() / "wisdom-state", "codex",
        lambda: SimpleNamespace(
            native_owner=lambda: {"profile_name": "codex", "account_id": "portal-fixture"},
            close=lambda: None,
        ), None, None,
        installed_state=PluginState("wisdom-binding-fixture"),
        session_context=current_session_context,
    )
    native.wire_telegram(SimpleNamespace(add_handler=lambda *_a, **_kw: None), adapter)

    async def lifecycle(
        label, *, durable="session-A", context_id="", active="session-A",
        parent="", internal=False, user_id="fixture-human", profile="codex",
    ):
        route = source(label, user_id=user_id, profile=profile)
        context = SessionContext(
            source=route, connected_platforms=[], home_channels={},
            session_key="telegram:fixture-chat:fixture-human", session_id=durable,
        )
        tokens = GatewayRunner._set_session_env(runner, context)
        runtime_token = None
        try:
            assert get_session_env("HERMES_SESSION_ID") == ""
            if context_id:
                set_current_session_id(context_id)
            if active:
                runtime_token = set_runtime_main(
                    "fixture-provider", "fixture-model", session_id=active,
                )
            native.capture.hook(event=event(route, internal=internal), gateway=gateway)
            await asyncio.to_thread(
                native.lifecycle_hook,
                model="fixture-model", session_id=durable, task_id=f"task-{label}",
                turn_id=f"turn-{label}", platform="telegram", sender_id=user_id,
                parent_session_id=parent,
            )
            return native.tool_invocation(task_id=f"task-{label}", session_id=durable) is not None
        finally:
            if runtime_token is not None:
                reset_runtime_main(runtime_token)
            clear_session_vars(tokens)

    assert await lifecycle("cached") is True
    tokens = GatewayRunner._set_session_env(
        runner,
        SessionContext(
            source=source("cached"), connected_platforms=[], home_channels={},
            session_key="telegram:fixture-chat:fixture-human", session_id="session-A",
        ),
    )
    runtime_token = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-A",
    )
    try:
        admitted = await asyncio.to_thread(
            native.tool_invocation, task_id="task-cached", session_id="session-A",
        )
        assert admitted.host_session_id == "session-A"
        for task_id, session_id in (("other-task", "session-A"), ("task-cached", "session-B")):
            assert await asyncio.to_thread(
                native.tool_invocation, task_id=task_id, session_id=session_id,
            ) is None, "different task/session reused cached admission"
    finally:
        reset_runtime_main(runtime_token)
        clear_session_vars(tokens)

    assert await lifecycle("nonempty", context_id="session-A") is True
    assert await lifecycle("conflict", context_id="session-B") is False
    assert await lifecycle("different-runtime", active="session-B") is False
    assert await lifecycle("missing-runtime", active="") is False
    assert await lifecycle("subagent", parent="parent-session") is False
    assert await lifecycle("internal", internal=True) is False
    assert await lifecycle("bot", user_id="fixture-bot") is False
    assert await lifecycle("cross-profile", profile="other") is False

    assert await lifecycle("owner-change") is True
    changed = source("owner-change", user_id="other-human")
    changed_tokens = GatewayRunner._set_session_env(
        runner,
        SessionContext(
            source=changed, connected_platforms=[], home_channels={},
            session_key="telegram:fixture-chat:fixture-human", session_id="session-A",
        ),
    )
    changed_runtime = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-A",
    )
    try:
        assert await asyncio.to_thread(
            native.tool_invocation, task_id="task-owner-change", session_id="session-A",
        ) is None, "changed owner reused cached admission"
    finally:
        reset_runtime_main(changed_runtime)
        clear_session_vars(changed_tokens)

    assert await lifecycle("replay", durable="session-A", active="session-A") is True
    replay_tokens = GatewayRunner._set_session_env(
        runner,
        SessionContext(
            source=source("replay"), connected_platforms=[], home_channels={},
            session_key="telegram:fixture-chat:fixture-human", session_id="session-B",
        ),
    )
    replay_runtime = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-B",
    )
    try:
        assert await asyncio.to_thread(
            native.tool_invocation, task_id="task-replay", session_id="session-A",
        ) is None, "another active session replayed cached admission"
    finally:
        reset_runtime_main(replay_runtime)
        clear_session_vars(replay_tokens)

    native.close()
    print("stock cached-turn binding PASS")


asyncio.run(main())
'''


def _git(host: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=host, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_stock_host_cached_gateway_binding(tmp_path: Path):
    """Exercise the real stock Gateway binder and plugin admission gate."""

    host_value = os.environ.get("WISDOM_STOCK_AGENT_DIR")
    python_value = os.environ.get("WISDOM_STOCK_HOST_PYTHON")
    if not host_value or not python_value:
        pytest.skip("Set WISDOM_STOCK_AGENT_DIR and WISDOM_STOCK_HOST_PYTHON")
    host = Path(host_value).resolve()
    assert _git(host, "rev-parse", "HEAD") == EXPECTED_HOST_REVISION
    assert _git(host, "status", "--porcelain") == ""
    environment = {
        "HOME": str(tmp_path / "home"),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "WISDOM_STOCK_AGENT_DIR": str(host),
        "WISDOM_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    result = subprocess.run(
        [python_value, "-I", "-c", PROBE], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    assert result.stdout.strip() == "stock cached-turn binding PASS"
    assert _git(host, "status", "--porcelain") == ""
