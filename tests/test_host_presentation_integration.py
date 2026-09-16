"""Isolated real-host discovery gate for local ready-advice presentation."""
# The isolated host subprocess deliberately carries its own Gateway fixture.
# pylint: disable=duplicate-code,too-many-lines

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "hermes-collective-wisdom"
PROBE = r'''
from local_presentation_fixture import discovered_wisdom_fixture
with discovered_wisdom_fixture() as fixture:
    globals().update(fixture)
    # The real host pre-LLM hook captures this context-local active route but does
    # only reads the local owner binding; no queue claim, model or delivery work.
    runtime_token = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-1"
    )
    try:
        pre_llm(
            platform="cli",
            parent_session_id="",
            session_id="session-1",
            task_id="task-1",
            turn_id="agent-turn-1",
            model="fixture-model",
            conversation_history=[{"role": "user", "content": "real host context"}],
        )
        assert assessment._local_turns, (
            bridge_module._active_main_runtime(),
            mediation_module.delivery_context_allowed(),
        )
    finally:
        reset_runtime_main(runtime_token)

    # Capture reads local enrollment; the opportunity owns proactive work.
    assert len(service_opens) == 2
    assert not model_calls
    active_registrations = [
        entry
        for entry in manager._ownership_ledger[PLUGIN_ID]
        if entry.active
    ]
    assert sum(entry.kind == "presentation_handler" for entry in active_registrations) == 1

    async def scenario():
        tasks = await manager.present_local_opportunity(
            profile_name="default",
            session_id="session-1",
            turn_id="turn-1",
            surface_id=SURFACE_ID,
            surface_generation=1,
            render=lambda notification_id, text, _current: (
                rendered.append((notification_id, text))
                or presentation.PresentationOutcome.RENDERED
            ),
        )
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    # The assessment thread owns a separate connection lifetime from delivery.
    assert len(service_opens) == 4
    rows = queue.assessments("org")
    assert len(model_calls) == 1, [
        (row["state"], row["owner_session"], row["last_error"]) for row in rows
    ]
    assert model_calls[0]["task"] is None
    assert len(rendered) == 1, rendered
    assert "Agent-assessed advice" in rendered[0][1], rendered
    assert rows[0]["state"] == "delivered"

    # Follow the notice through the actual registered model-tool handler on the
    # next owning local turn. This is the exact version handoff, not `/wisdom show`
    # (which intentionally resolves the current catalog version).
    runtime_token = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-1"
    )
    try:
        pre_llm(
            platform="cli" if SURFACE_ID == "cli" else "tui",
            parent_session_id="",
            session_id="session-1",
            task_id="inspect-task",
            turn_id="inspect-turn",
            model="fixture-model",
            conversation_history=[{"role": "user", "content": "inspect that exact suggestion"}],
        )
        from tools.registry import registry
        entry = registry.get_entry("wisdom_inspect", scope=manager.scope_key)
        assert entry is not None
        inspected = json.loads(entry.handler(
            {"kind": "skill", "identity": "skill-1", "version": 1},
            task_id="inspect-task", session_id="session-1",
        ))
    finally:
        reset_runtime_main(runtime_token)
    assert inspected["skill_id"] == "skill-1"
    assert inspected["version"] == 1
    assert inspected["facts"]["version"]["version"] == 1
    print(f"isolated host presentation passed: {SURFACE_ID}")
'''


def _configured_host() -> tuple[Path, Path]:
    """Resolve the explicitly configured optional host, failing on partial/bad config."""

    agent_dir = os.environ.get("WISDOM_PRESENTATION_AGENT_DIR")
    host_python = os.environ.get("WISDOM_PRESENTATION_HOST_PYTHON")
    if not agent_dir and not host_python:
        pytest.skip(
            "Set WISDOM_PRESENTATION_AGENT_DIR and "
            "WISDOM_PRESENTATION_HOST_PYTHON to run the optional host probe"
        )
    if not agent_dir:
        pytest.fail(
            "WISDOM_PRESENTATION_AGENT_DIR is required when "
            "WISDOM_PRESENTATION_HOST_PYTHON is configured"
        )
    if not host_python:
        pytest.fail(
            "WISDOM_PRESENTATION_HOST_PYTHON is required when "
            "WISDOM_PRESENTATION_AGENT_DIR is configured"
        )

    agent_path = Path(agent_dir).expanduser().resolve()
    # Preserve a virtualenv interpreter symlink: resolving it bypasses pyvenv.cfg.
    interpreter = Path(os.path.abspath(os.path.expanduser(host_python)))
    assert (agent_path / "hermes_cli" / "plugins.py").is_file(), (
        f"invalid WISDOM_PRESENTATION_AGENT_DIR: {agent_path}"
    )
    assert (agent_path / "hermes_cli" / "plugins_presentation.py").is_file(), (
        f"configured host lacks presentation support: {agent_path}"
    )
    assert interpreter.is_file() and os.access(interpreter, os.X_OK), (
        f"invalid WISDOM_PRESENTATION_HOST_PYTHON: {interpreter}"
    )
    return agent_path, interpreter


def _isolated_environment(
    tmp_path: Path, profile: Path, agent_path: Path, surface_id: str,
) -> dict[str, str]:
    """Build an allowlisted subprocess environment with no ambient credentials/profile."""

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(profile),
        "WISDOM_PRESENTATION_AGENT_DIR": str(agent_path),
        "WISDOM_PRESENTATION_SURFACE_ID": surface_id,
        "PYTHONPATH": os.pathsep.join((
            str(PLUGIN_ROOT), str(PLUGIN_ROOT / "tests" / "fixtures"), str(agent_path),
        )),
        "PYTHONNOUSERSITE": "1",
    }


@pytest.mark.parametrize("surface_id", ["cli", "tui:session-1"])
def test_discovered_wisdom_plugin_is_inert_then_produces_and_delivers_advice(
    tmp_path: Path, surface_id: str,
) -> None:
    """Discover the directory plugin and cross a real host opportunity boundary."""

    agent_path, interpreter = _configured_host()
    profile = tmp_path / "hermes-home"
    plugin_dir = profile / "plugins" / PLUGIN_ID
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (profile / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(interpreter), "-c", PROBE],
        cwd=tmp_path,
        env=_isolated_environment(tmp_path, profile, agent_path, surface_id),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    assert result.stdout.strip() == f"isolated host presentation passed: {surface_id}"
