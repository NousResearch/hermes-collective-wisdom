"""Focused safety checks for the receipt mutation launcher."""

# pylint: disable=missing-function-docstring

import os
from pathlib import Path
import runpy
import signal
import sys
import time

import pytest

_launcher = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "run_receipt_mutations.py")
)
_run = _launcher["_run"]


def test_sandbox_runs_real_receipt_baseline_before_mutation(tmp_path, monkeypatch):
    """The selected tests and their local support must work outside the checkout."""
    artifact = tmp_path / "campaign"
    main = _launcher["main"]
    globals_ = main.__globals__
    real_run = globals_["subprocess"].run

    def version_or_run(command, **kwargs):
        if command[1:3] == ["-c", "import importlib.metadata as m; print(m.version('mutmut'))"]:
            return globals_["subprocess"].CompletedProcess(command, 0, "3.8.0\n", "")
        return real_run(command, **kwargs)

    def run_until_mutation(command, **kwargs):
        if command[1:4] == ["-m", "mutmut", "run"]:
            raise RuntimeError("reached mutation after real baseline")
        return _run(command, **kwargs)

    monkeypatch.setattr(globals_["subprocess"], "run", version_or_run)
    monkeypatch.setitem(globals_, "_run", run_until_mutation)
    monkeypatch.setattr(sys, "argv", [
        "run_receipt_mutations.py", "--tool-python", sys.executable,
        "--artifact-dir", str(artifact),
    ])
    with pytest.raises(RuntimeError, match="reached mutation after real baseline"):
        main()
    assert "passed" in (artifact / "baseline.txt").read_text(encoding="utf-8")
    assert str(artifact) in (artifact / "import-verification.txt").read_text(encoding="utf-8")


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_terminates_descendants_and_preserves_output(tmp_path):
    log = tmp_path / "timed-out.txt"
    pid_file = tmp_path / "descendant.pid"
    descendant_pid = None
    parent = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "print(f'descendant={child.pid}', flush=True); time.sleep(60)"
    )

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            _run([sys.executable, "-c", parent, str(pid_file)],
                 cwd=tmp_path, env=os.environ.copy(),
                 log=log, timeout=2)

        output = log.read_text(encoding="utf-8")
        descendant_pid = int(output.splitlines()[0].removeprefix("descendant="))
        deadline = time.monotonic() + 2
        while _process_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_exists(descendant_pid)
        assert "descendant=" in output
        assert "timed out after 2 seconds" in output
    finally:
        if descendant_pid is None and pid_file.exists():
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        if descendant_pid is not None and _process_exists(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)
