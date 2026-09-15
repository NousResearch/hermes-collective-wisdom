"""Real host-turn-to-renderer journeys for local Collective Wisdom advice."""
# pylint: disable=line-too-long,missing-function-docstring

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "hermes-collective-wisdom"


def _configured_host() -> tuple[Path, Path]:
    agent_dir = os.environ.get("WISDOM_PRESENTATION_AGENT_DIR")
    host_python = os.environ.get("WISDOM_PRESENTATION_HOST_PYTHON")
    if not agent_dir and not host_python:
        pytest.skip(
            "Set WISDOM_PRESENTATION_AGENT_DIR and "
            "WISDOM_PRESENTATION_HOST_PYTHON to run the optional host journey"
        )
    if not agent_dir or not host_python:
        pytest.fail("Both WISDOM_PRESENTATION_AGENT_DIR and WISDOM_PRESENTATION_HOST_PYTHON are required")
    agent_path = Path(agent_dir).expanduser().resolve()
    interpreter = Path(os.path.abspath(os.path.expanduser(host_python)))
    assert (agent_path / "hermes_cli" / "plugins_presentation.py").is_file()
    assert interpreter.is_file() and os.access(interpreter, os.X_OK)
    return agent_path, interpreter


PROBE_SETUP = 'from local_presentation_fixture import discovered_wisdom_fixture\nwith discovered_wisdom_fixture() as fixture:\n    globals().update(fixture)\n'


def _environment(tmp_path: Path, profile: Path, agent_path: Path, surface: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(profile),
        "WISDOM_PRESENTATION_AGENT_DIR": str(agent_path),
        "WISDOM_PRESENTATION_SURFACE_ID": surface,
        "PYTHONPATH": os.pathsep.join((str(PLUGIN_ROOT), str(PLUGIN_ROOT / "tests" / "fixtures"), str(agent_path))),
        "PYTHONNOUSERSITE": "1",
        "TERM": "xterm-256color",
    }


def _run_journey(tmp_path: Path, surface: str, body: str) -> subprocess.CompletedProcess[str]:
    agent_path, interpreter = _configured_host()
    profile = tmp_path / "hermes-home"
    plugin_dir = profile / "plugins" / PLUGIN_ID
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (profile / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n", encoding="utf-8"
    )
    return subprocess.run(
        [str(interpreter), "-c", PROBE_SETUP + body],
        cwd=tmp_path,
        env=_environment(tmp_path, profile, agent_path, surface),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_real_cli_turn_produces_assesses_renders_and_receipts_on_pty(tmp_path: Path) -> None:
    body = r'''
    from agent.turn_context import _collect_pre_llm_call_context
    from queue import Queue
    import importlib.util
    import sys

    plugins._plugin_manager = manager
    agent = SimpleNamespace(
        _persist_disabled=False, session_id="session-1", model="fixture-model", platform="cli",
        _parent_session_id="", _user_id="member",
    )
    runtime_token = set_runtime_main(
        "fixture-provider", "fixture-model", session_id="session-1"
    )
    try:
        assert _collect_pre_llm_call_context(
            agent, effective_task_id="session-1", turn_id="agent-turn-1",
            original_user_message="find a useful workflow",
            messages=[{"role": "user", "content": "find a useful workflow"}],
            conversation_history=[],
        ) == ""
    finally:
        reset_runtime_main(runtime_token)
    assert assessment._local_turns
    assert queue.assessments("org") == []

    harness_path = AGENT_ROOT / "tests" / "hermes_cli" / "test_cli_plugin_presentation.py"
    spec = importlib.util.spec_from_file_location("wisdom_cli_harness", harness_path)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)

    async def journey():
        with harness._pty_application() as (app, pipe, master_fd, slave):
            host = harness._host(app)
            host.session_id = "session-1"
            host._pending_input = Queue()
            previous_stdout = sys.stdout
            sys.stdout = slave
            try:
                app_task = await harness._start_app(app)
                from prompt_toolkit.application import run_in_terminal
                from prompt_toolkit.application.current import set_app
                with set_app(app):
                    await run_in_terminal(lambda: print("settled reply", file=slave, flush=True))
                host._offer_cli_presentation(harness._completed_turn(), "settled reply")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    rows = queue.assessments("org")
                    if rows and rows[0]["state"] == "delivered":
                        break
                    await asyncio.sleep(0.02)
                output = harness._read_pty(master_fd)
                assert "Agent-assessed advice" in output, (
                    output, rows, len(model_calls), len(service_opens),
                    assessment._local_turns, fixture_calls, store.feed_checkpoint(),
                )
                assert "wisdom_inspect" in output, output
                assert len(model_calls) == 1
                assert rows[0]["reference"]["event_id"] == "event-1"
                assert rows[0]["state"] == "delivered"
                await asyncio.to_thread(follow_inspection_handoff, "cli", "session-1")
                pipe.send_text("\x11")
                assert await asyncio.wait_for(app_task, 2) == "quit"
            finally:
                sys.stdout = previous_stdout

    asyncio.run(journey())
    print("real local turn journey passed: cli")
'''
    result = _run_journey(tmp_path, "cli", body)
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    assert result.stdout.strip() == "real local turn journey passed: cli"


@pytest.mark.parametrize("interrupt", [False, True], ids=["render", "new-input"])
def test_real_tui_prompt_turn_produces_assesses_renders_and_receipts_in_ink(
    tmp_path: Path, interrupt: bool,
) -> None:
    body = f"    interrupt = {interrupt!r}\n" + r'''
    from agent.turn_context import _collect_pre_llm_call_context
    import importlib.util
    import subprocess
    import sys
    import threading

    assessment_started = threading.Event()
    assessment_release = threading.Event()
    assessment_finished = threading.Event()
    worker_finished = threading.Event()
    worker_started = threading.Event()
    worker_errors = []
    original_run = assessment.run_local

    def recorded_worker(*args, **kwargs):
        first = not worker_started.is_set()
        worker_started.set()
        try:
            return original_run(*args, **kwargs)
        except Exception as error:
            worker_errors.append(error)
            raise
        finally:
            if first:
                worker_finished.set()

    assessment.run_local = recorded_worker
    original_complete = assessment.llm.complete_structured

    def recording_completion(**kwargs):
        try:
            if interrupt:
                assessment_started.set()
                assert assessment_release.wait(10)
            return original_complete(**kwargs)
        finally:
            assessment_finished.set()

    assessment.llm.complete_structured = recording_completion
    plugins._plugin_manager = manager
    os.environ["HERMES_TEST_ISOLATION"] = str(HOME)
    harness_path = AGENT_ROOT / "tests" / "tui_gateway" / "test_plugin_presentation_e2e.py"
    spec = importlib.util.spec_from_file_location("wisdom_tui_harness", harness_path)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    renderer = HOME / "renderer.mjs"
    build = subprocess.run(
        ["node", str(AGENT_ROOT / "tests" / "fixtures" / "plugin_presentation" /
                     "build-renderer.mjs"), str(renderer)],
        cwd=AGENT_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert build.returncode == 0, build.stderr
    renderer_env = harness._renderer_env(HOME, "positive")
    proc = subprocess.Popen(
        ["node", str(renderer)], cwd=AGENT_ROOT, env=renderer_env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    transport = harness.RendererTransport(proc)
    session_id = harness.SESSION_ID

    class RecordingAgent:
        session_id = session_id
        model = "fixture-model"
        provider = "fixture-provider"
        platform = "tui"
        _parent_session_id = ""
        _user_id = "member"
        base_url = "https://model-fixture.invalid"
        api_key = "fixture"
        _cached_system_prompt = ""
        interim_assistant_callback = None

        def run_conversation(
            self, prompt, *, conversation_history=None, stream_callback=None,
            task_id=None, **_kwargs,
        ):
            runtime_token = set_runtime_main(
                self.provider, self.model, session_id=self.session_id
            )
            try:
                assert _collect_pre_llm_call_context(
                    self, effective_task_id=str(task_id), turn_id="tui-agent-turn-1",
                    original_user_message=prompt,
                    messages=[*(conversation_history or []), {"role": "user", "content": prompt}],
                    conversation_history=conversation_history or [],
                ) == ""
            finally:
                reset_runtime_main(runtime_token)
            if stream_callback:
                stream_callback("settled reply")
            return {
                "final_response": "settled reply", "completed": True, "failed": False,
                "interrupted": False, "api_calls": 1,
                "messages": [*(conversation_history or []),
                             {"role": "user", "content": prompt},
                             {"role": "assistant", "content": "settled reply"}],
            }

    import tui_gateway.server as server
    session = {
        "agent": RecordingAgent(), "session_key": session_id, "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False,
        "attached_images": [], "image_counter": 0, "cols": 80,
        "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "pending_title": None, "profile_home": str(HOME), "transport": transport,
    }
    server._sessions[session_id] = session
    try:
        transport.wait_probe("renderer_ready")
        response = server.dispatch({
            "jsonrpc": "2.0", "id": "turn-1", "method": "prompt.submit",
            "params": {"session_id": session_id, "text": "find a useful workflow"},
        }, transport=transport)
        assert response and response.get("result", {}).get("status") == "streaming", response
        run_thread = session.get("_run_thread")
        assert run_thread is not None
        run_thread.join(timeout=10)
        assert not run_thread.is_alive()
        if interrupt:
            assert assessment_started.wait(3)
            response = server.dispatch({
                "jsonrpc": "2.0", "id": "turn-2", "method": "prompt.submit",
                "params": {"session_id": session_id, "text": "new input during assessment"},
            }, transport=transport)
            assert response and response.get("result", {}).get("status") == "streaming", response
            next_thread = session["_run_thread"]
            next_thread.join(timeout=5)
            assert not next_thread.is_alive(), (
                session.get("_presentation_generation"), session.get("queued_prompt"),
                next_thread is session.get("_run_thread"), session.get("history"),
            )
            assessment_release.set()
            assert assessment_finished.wait(3)
            # Observe the real worker's final authorization/state checks.
            assert worker_finished.wait(3)
            assert not worker_errors, worker_errors
            assert not any(
                frame.get("params", {}).get("type") == "presentation.notification"
                for frame in transport.frames
            ), transport.frames
            assert all(row["state"] != "delivered" for row in queue.assessments("org"))
            print("real local turn journey passed: tui interrupted")
            raise SystemExit(0)
        deadline = time.monotonic() + 10
        while "presentation.rendered" not in transport.rpc_methods and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "presentation.rendered" in transport.rpc_methods, transport.frames
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = queue.assessments("org")
            if rows and rows[0]["state"] == "delivered":
                break
            time.sleep(0.02)
        assert len(model_calls) == 1, (fixture_calls, rows)
        assert rows[0]["reference"]["event_id"] == "event-1"
        assert rows[0]["state"] == "delivered"
        assert "presentation.ready" in transport.rpc_methods
        assert "presentation.rendered" in transport.rpc_methods
        notification = next(
            frame for frame in transport.frames
            if frame.get("params", {}).get("type") == "presentation.notification"
        )
        assert "wisdom_inspect" in notification["params"]["payload"]["text"]
        follow_inspection_handoff("tui", session_id)
        print("real local turn journey passed: tui")
    finally:
        assessment_release.set()
        getattr(server, "_close_tui_presentations")(session)
        server._sessions.pop(session_id, None)
        transport.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        transport._reader.join(timeout=1)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        assert not transport.protocol_errors, transport.protocol_errors
'''
    result = _run_journey(tmp_path, "tui:session-1", body)
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
