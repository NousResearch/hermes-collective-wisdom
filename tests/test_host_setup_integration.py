"""Real pinned-host boundary probes for one-shot Wisdom setup execution."""

# pylint: disable=duplicate-code

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"
FIRST_PROBE = r'''
import json
import os
import time
from pathlib import Path

from hermes_wisdom_plugin.setup_host import SetupHost
from tools.terminal_tool import set_approval_callback
from tools import terminal_tool as terminal_module

host_root = Path(os.environ["WISDOM_AGENT_DIR"]).resolve()
assert Path(terminal_module.__file__).resolve().is_relative_to(host_root)
workdir = Path(os.environ["WISDOM_MARKER_DIR"]).resolve()
handle_path = workdir / "handle.json"
marker = workdir / "approved-marker"
command = f"rm -rf '{marker}' && printf approved > '{marker}'"
task_id = "wisdom-setup:test-approved"
seen = []

def deny(value, _description, *, allow_permanent=True):
    seen.append((value, allow_permanent, "deny"))
    return "deny"

set_approval_callback(deny)
denied = SetupHost().execute(command, workdir=workdir, task_id="wisdom-setup:test-denied")
assert denied["state"] == "blocked", denied
assert not marker.exists()
assert seen == [(command, True, "deny")]

def approve_once(value, _description, *, allow_permanent=True):
    seen.append((value, allow_permanent, "once"))
    return "once"

set_approval_callback(approve_once)
def record(handle):
    handle_path.write_text(json.dumps(handle), encoding="utf-8")
passed = SetupHost().execute(command, workdir=workdir, task_id=task_id, on_started=record)
assert passed["state"] == "passed", passed
assert marker.read_text(encoding="utf-8") == "approved"
assert passed["handle"] == json.loads(handle_path.read_text(encoding="utf-8"))
assert command not in json.dumps(passed)

# Inject acknowledgement loss after the real guarded background spawn returns.
# The adapter must make only one terminal call and must not replay the command.
attempts = workdir / "attempts"
fault_command = f"printf x >> '{attempts}'"
real_terminal = terminal_module.terminal_tool
terminal_calls = 0
def lose_ack(**kwargs):
    global terminal_calls
    terminal_calls += 1
    real_terminal(**kwargs)
    raise RuntimeError("injected post-spawn acknowledgement loss")
terminal_module.terminal_tool = lose_ack
lost = SetupHost().execute(
    fault_command, workdir=workdir, task_id="wisdom-setup:test-lost-ack"
)
assert lost == {"state": "unknown", "reason": "launch_acknowledgement_lost"}
assert terminal_calls == 1
deadline = time.monotonic() + 5
while not attempts.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert attempts.read_text(encoding="utf-8") == "x"
set_approval_callback(None)
print("clean-host Wisdom background setup boundary passed")
'''

SECOND_PROBE = r'''
import json
import os
import time
from pathlib import Path
from hermes_wisdom_plugin.setup_host import SetupHost

workdir = Path(os.environ["WISDOM_MARKER_DIR"]).resolve()
handle = json.loads((workdir / "handle.json").read_text(encoding="utf-8"))
command = f"rm -rf '{workdir / 'approved-marker'}' && printf approved > '{workdir / 'approved-marker'}'"
result = SetupHost().status(
    handle, command=command, workdir=workdir, task_id="wisdom-setup:test-approved"
)
# A fresh CLI process may recover an owner-matched durable result. If the host
# cannot expose it to this CLI owner, uncertainty is explicit and never replayed.
assert result["state"] in {"passed", "unknown"}, result
assert (workdir / "approved-marker").read_text(encoding="utf-8") == "approved"
print("fresh-process status=" + result["state"])
'''


VERTICAL_PROBE = r'''
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.contract import canonical_json_bytes
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.install_review import InstallReviewer
from hermes_wisdom_plugin.package import verify_content_files
from hermes_wisdom_plugin.setup_document import PREFIX, SETUP_PATH, SUFFIX
from hermes_wisdom_plugin.setup_execution import SetupController
from hermes_wisdom_plugin.setup_host import SetupHost
from hermes_wisdom_plugin.setup_receipts import SetupReceipts
from hermes_wisdom_plugin.store import WisdomStore
from tools.terminal_tool import set_approval_callback

profile = Path(os.environ["HERMES_HOME"]).resolve()
state = WisdomStore(Path(os.environ["WISDOM_STATE_DIR"]))
marker_dir = Path(os.environ["WISDOM_MARKER_DIR"]).resolve()
owner = {
    "profile_name": "default", "profile_home": str(profile),
    "account_id": "synthetic-member", "organization_id": "synthetic-org",
    "base_url": "https://gateway.invalid", "installation_id": "hwi_synthetic",
}
manifest = canonical_json_bytes({
    "schema_version": 1, "name": "managed",
    "requirements": {
        "hermes": {"minimum_version": "0.1.0"}, "platforms": [],
        "architectures": [], "model": {}, "tools": [], "plugins": [],
        "credentials": [], "connections": [], "filesystem": {}, "network": {},
        "runtime": {}, "hardware": [], "known_limitations": [],
    },
})
guidance = canonical_json_bytes({
    "schema_version": 1, "execution_requires_user_approval": True,
    "requirements": [{"kind": "account", "name": "example", "purpose": "Confirm account access."}],
    "setup_instructions": ["Create a harmless external marker."],
    "credential_handoff": [], "compatibility_limits": [],
    "verification_step": "Verify that the harmless marker exists.",
    "removed_or_generalized": [], "related_skills": [],
})
files = [
    ("SKILL.md", "file", b"# Managed\\n"),
    ("skill.manifest.json", "file", manifest),
    (SETUP_PATH, "file", PREFIX.encode() + guidance + SUFFIX.encode()),
]
_records, content_hash = verify_content_files(files)
system_spec = json.loads(manifest)["requirements"]

class Client:
    def skill(self, skill_id):
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            versions=[{"version": 1, "state": "published"}],
        )

    def version(self, skill_id, version):
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            version={"version": version, "content_hash": content_hash, "commit": "synthetic-commit", "system_spec": system_spec},
        )

    def content(self, *_args, **_kwargs):
        return SimpleNamespace(content_hash=content_hash, commit="synthetic-commit"), files

    def record_install(self, **_kwargs):
        return {"effective_update_mode": "MANUAL"}

client = Client()
host = InstallationHost(profile)
install_receipts = InstallReceipts(state, lambda: dict(owner))
reviewer = InstallReviewer(client, state, install_receipts, host, lambda: dict(owner))
installer = InstallExecutor(client, state, install_receipts, reviewer, host, lambda: dict(owner))
controller = SetupController(
    state, SetupReceipts(state, lambda: dict(owner)), host,
    SetupHost(), lambda: dict(owner),
)

class Service:
    def install_plan(self, reference, *, update_mode=None):
        return reviewer.plan(reference, update_mode=update_mode)

    def install_apply(self, receipt, *, accept_partial=False):
        return installer.apply(receipt, accept_partial=accept_partial)

    def installed_setup_inspect(self, skill_id, *, version=None):
        return controller.inspect(skill_id, version=version)

    def installed_setup_plan(self, skill_id, **values):
        return controller.plan(skill_id, **values)

    def installed_setup_apply(self, receipt):
        return controller.apply(receipt)

    def installed_setup_status(self, skill_id, *, version=None):
        return controller.status(skill_id, version=version)

service = Service()

def cli(arguments):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert run_cli(parser.parse_args(arguments), service) == 0
    return json.loads(output.getvalue())

marker = marker_dir / "vertical-marker"
seen = []
set_approval_callback(lambda value, _description, **_kwargs: seen.append(value) or "once")
install = cli(["install", "skill-vertical@v1", "--plan", "--json"])
target = profile / "skills" / "_wisdom" / "synthetic-org" / "managed"
assert not target.exists()
installed = cli(["install", "--apply-receipt", install["receipt"], "--json"])
assert installed["installed"] is True
assert target.is_dir()
assert not marker.exists()
assert seen == []

prerequisite = cli([
    "installed-setup", "plan", "skill-vertical", "--version", "1",
    "--phase", "prerequisite", "--index", "0", "--json",
])
assert cli(["installed-setup", "apply", prerequisite["receipt"], "--json"])["state"] == "passed"
setup = cli([
    "installed-setup", "plan", "skill-vertical", "--version", "1", "--phase", "setup",
    "--command", f"printf configured > '{marker}'", "--json",
])
assert cli(["installed-setup", "apply", setup["receipt"], "--json"])["state"] == "passed"
verify = cli([
    "installed-setup", "plan", "skill-vertical", "--version", "1", "--phase", "verify",
    "--command", f"test -f '{marker}'", "--json",
])
assert cli(["installed-setup", "apply", verify["receipt"], "--json"])["state"] == "passed"
assert cli(["installed-setup", "status", "skill-vertical", "--version", "1", "--json"])["ready_to_use"] is True
set_approval_callback(None)
(marker_dir / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
print("CLI install-through-setup journey passed")
'''

REOPEN_PROBE = r'''
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.setup_execution import SetupController
from hermes_wisdom_plugin.setup_host import SetupHost
from hermes_wisdom_plugin.setup_receipts import SetupReceipts
from hermes_wisdom_plugin.store import WisdomStore

profile = Path(os.environ["HERMES_HOME"]).resolve()
marker_dir = Path(os.environ["WISDOM_MARKER_DIR"]).resolve()
owner = json.loads((marker_dir / "owner.json").read_text(encoding="utf-8"))
state = WisdomStore(Path(os.environ["WISDOM_STATE_DIR"]))
controller = SetupController(
    state, SetupReceipts(state, lambda: dict(owner)), InstallationHost(profile),
    SetupHost(), lambda: dict(owner),
)

class Service:
    def installed_setup_status(self, skill_id, *, version=None):
        return controller.status(skill_id, version=version)

parser = argparse.ArgumentParser()
build_parser(parser)
output = io.StringIO()
with contextlib.redirect_stdout(output):
    code = run_cli(parser.parse_args([
        "installed-setup", "status", "skill-vertical", "--version", "1", "--json"
    ]), Service())
assert code == 0
status = json.loads(output.getvalue())
assert status["state"] == "verified", status
assert status["ready_to_use"] is True
assert (marker_dir / "vertical-marker").read_text(encoding="utf-8") == "configured"
print("reopened CLI setup state passed")
'''


def _git(agent_path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=agent_path, capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _environment(tmp_path: Path, agent_path: Path, marker_dir: Path) -> dict[str, str]:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "terminal:\n  backend: local\napprovals:\n  mode: manual\n",
        encoding="utf-8",
    )
    paths = [str(PROJECT_ROOT), str(agent_path)]
    if dependencies := os.environ.get("WISDOM_HOST_DEPENDENCIES"):
        paths.append(dependencies)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(profile),
        "HERMES_INTERACTIVE": "1",
        "HERMES_SESSION_ID": "synthetic-wisdom-cli-session",
        "WISDOM_AGENT_DIR": str(agent_path),
        "WISDOM_MARKER_DIR": str(marker_dir),
        "WISDOM_STATE_DIR": str(tmp_path / "wisdom-state"),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONNOUSERSITE": "1",
    }


def test_clean_host_background_setup_is_single_spawn_and_restart_safe(tmp_path: Path) -> None:
    """Exercise real guards, registry ownership, ack loss, and a fresh process."""

    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to the unmodified Agent checkout")
    agent_path = Path(agent_dir).resolve()
    assert _git(agent_path, "rev-parse", "HEAD") == EXPECTED_HOST_REVISION
    assert _git(agent_path, "status", "--porcelain") == ""

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    environment = _environment(tmp_path, agent_path, marker_dir)
    interpreter = os.environ.get("WISDOM_HOST_PYTHON", sys.executable)
    first = subprocess.run(
        [interpreter, "-c", FIRST_PROBE], cwd=marker_dir, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert first.returncode == 0, f"{first.stderr}\nstdout:\n{first.stdout}"
    assert first.stdout.strip() == "clean-host Wisdom background setup boundary passed"

    second = subprocess.run(
        [interpreter, "-c", SECOND_PROBE], cwd=marker_dir, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert second.returncode == 0, f"{second.stderr}\nstdout:\n{second.stdout}"
    assert second.stdout.strip().startswith("fresh-process status=")
    assert (marker_dir / "attempts").read_text(encoding="utf-8") == "x"

    vertical = subprocess.run(
        [interpreter, "-c", VERTICAL_PROBE], cwd=marker_dir, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert vertical.returncode == 0, f"{vertical.stderr}\nstdout:\n{vertical.stdout}"
    assert vertical.stdout.strip() == "CLI install-through-setup journey passed"
    reopened = subprocess.run(
        [interpreter, "-c", REOPEN_PROBE], cwd=marker_dir, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert reopened.returncode == 0, f"{reopened.stderr}\nstdout:\n{reopened.stdout}"
    assert reopened.stdout.strip() == "reopened CLI setup state passed"
    assert _git(agent_path, "status", "--porcelain") == ""
