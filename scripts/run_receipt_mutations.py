#!/usr/bin/env python3
"""Run the bounded receipt mutation target in a disposable source copy."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

MUTMUT_VERSION = "3.8.0"
TARGET = "hermes_wisdom_plugin.install_receipts.x*InstallReceipts*start__mutmut_*"
TEST = Path("tests/test_install_receipts.py")
_TERMINATION_GRACE_SECONDS = 2


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the launched process group, escalating after a short grace period."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()

    try:
        process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()


def _run(command: list[str], *, cwd: Path, env: dict[str, str], log: Path,
         timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run one logged command, cleaning up its process group on timeout."""
    started = time.monotonic()
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=os.name == "posix",
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        output, _ = process.communicate()
        output += f"\nlauncher: timed out after {timeout} seconds; process tree terminated\n"
        log.write_text(output, encoding="utf-8")
        raise RuntimeError(f"command timed out; inspect {log}") from None

    result = subprocess.CompletedProcess(command, process.returncode, output, None)
    log.write_text(output, encoding="utf-8")
    elapsed = time.monotonic() - started
    print(f"{log.name}: exit={result.returncode} elapsed={elapsed:.2f}s")
    if result.returncode:
        raise RuntimeError(f"command failed; inspect {log}")
    return result


def main() -> int:  # pylint: disable=too-many-locals
    """Prepare an isolated sandbox and delegate mutation work to mutmut."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-python", required=True, type=Path,
                        help="Python from a disposable venv containing mutmut==3.8.0")
    parser.add_argument("--artifact-dir", required=True, type=Path,
                        help="new output directory outside this repository")
    parser.add_argument("--budget-seconds", type=int, default=300)
    parser.add_argument("--max-children", type=int, default=4)
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    artifact = args.artifact_dir.expanduser().resolve()
    tool_python = args.tool_python.expanduser().absolute()
    if repository == artifact or repository in artifact.parents:
        parser.error("--artifact-dir must be outside the development repository")
    if artifact.exists():
        parser.error("--artifact-dir must not already exist")
    if not tool_python.is_file():
        parser.error("--tool-python is not a file")
    if args.budget_seconds < 30 or args.max_children < 1:
        parser.error("budget must be >=30 seconds and max children must be positive")

    version = subprocess.run(
        [str(tool_python), "-c", "import importlib.metadata as m; print(m.version('mutmut'))"],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    if version != MUTMUT_VERSION:
        parser.error(f"expected mutmut=={MUTMUT_VERSION}, got {version!r}")

    artifact.mkdir(parents=True)
    shutil.copytree(repository / "hermes_wisdom_plugin", artifact / "hermes_wisdom_plugin")
    (artifact / "tests").mkdir()
    shutil.copy2(repository / TEST, artifact / TEST)
    shutil.copy2(repository / "tests/installed_test_support.py", artifact / "tests")
    shutil.copy2(repository / "pyproject.toml", artifact / "pyproject.toml")
    (artifact / "setup.cfg").write_text(
        "[mutmut]\n"
        "source_paths=hermes_wisdom_plugin/\n"
        "only_mutate=hermes_wisdom_plugin/install_receipts.py\n"
        "pytest_add_cli_args=\n"
        "    -q\n"
        "    --confcutdir=tests\n"
        "pytest_add_cli_args_test_selection=\n"
        "    tests/test_install_receipts.py\n"
        "use_setproctitle=false\n",
        encoding="utf-8",
    )
    (artifact / "tests/conftest.py").write_text(
        '"""Campaign-only network denial; receipt tests require no transport."""\n'
        "import socket\n\n"
        "def _deny_network(*_args, **_kwargs):\n"
        "    raise AssertionError('network access is disabled in receipt mutation tests')\n\n"
        "socket.create_connection = _deny_network\n"
        "socket.socket.connect = _deny_network\n",
        encoding="utf-8",
    )

    home = artifact / "home"
    hermes_home = artifact / "hermes-home"
    home.mkdir()
    hermes_home.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    baseline_env = {**env, "PYTHONPATH": str(artifact)}
    verification = _run(
        [str(tool_python), "-c",
         "import hermes_wisdom_plugin.install_receipts as m; print(m.__file__)"],
        cwd=artifact, env=baseline_env, log=artifact / "import-verification.txt",
    )
    imported = Path(verification.stdout.strip()).resolve()
    if artifact not in imported.parents:
        raise RuntimeError(f"baseline imported outside sandbox: {imported}")
    _run(
        [str(tool_python), "-m", "pytest", "-q", "--confcutdir=tests", str(TEST)],
        cwd=artifact, env=baseline_env, log=artifact / "baseline.txt",
    )

    mutation_env = dict(env)
    mutation_env.pop("PYTHONPATH", None)
    _run(
        [str(tool_python), "-m", "mutmut", "run", "--max-children", str(args.max_children),
         TARGET],
        cwd=artifact, env=mutation_env, log=artifact / "mutation-run.txt",
        timeout=args.budget_seconds,
    )
    _run(
        [str(tool_python), "-m", "mutmut", "results"], cwd=artifact,
        env=mutation_env, log=artifact / "results.txt",
    )
    print(f"Inspect survivors/timeouts with mutmut results/show in {artifact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
