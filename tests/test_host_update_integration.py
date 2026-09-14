"""Pinned clean-host CLI update journey and fresh-process setup-state probe."""

# pylint: disable=duplicate-code

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"

UPDATE_PROBE = r'''
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

from agent.skill_utils import iter_skill_index_files
from hermes_wisdom_plugin import update_execution as update_module
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
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import UpdateReviewer
from tools.terminal_tool import set_approval_callback

profile = Path(os.environ["HERMES_HOME"]).resolve()
state = WisdomStore(Path(os.environ["WISDOM_STATE_DIR"]))
owner = {
    "profile_name": "default", "profile_home": str(profile),
    "account_id": "synthetic-member", "organization_id": "synthetic-org",
    "base_url": "https://gateway.invalid", "installation_id": "hwi_update",
}
spec = {
    "hermes": {"minimum_version": "0.0.0"}, "platforms": [],
    "architectures": [], "model": {}, "tools": [], "plugins": [],
    "credentials": [], "connections": [], "filesystem": {}, "network": {},
    "runtime": {}, "hardware": [], "known_limitations": [],
}
manifest = canonical_json_bytes({"schema_version": 1, "name": "managed", "requirements": spec})
guidance = canonical_json_bytes({
    "schema_version": 1, "execution_requires_user_approval": True,
    "requirements": [], "setup_instructions": [], "credential_handoff": [],
    "compatibility_limits": [], "verification_step": "Run a harmless true command.",
    "removed_or_generalized": [], "related_skills": [],
})
setup = PREFIX.encode() + guidance + SUFFIX.encode()

def package(version):
    files = [
        ("SKILL.md", "file", f"# Managed v{version}\\n".encode()),
        ("skill.manifest.json", "file", manifest),
        (SETUP_PATH, "file", setup),
    ]
    records, content_hash = verify_content_files(files)
    return files, content_hash, {row.path: row.hash for row in records}

packages = {version: package(version) for version in (1, 2)}

class Client:
    latest = 1
    remote_version = None
    posts = []

    def skill(self, skill_id):
        versions = [
            {"version": value, "state": "published"}
            for value in range(self.latest, 0, -1)
        ]
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            versions=versions,
        )

    def version(self, skill_id, version):
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            version={"version": version, "content_hash": packages[version][1],
                     "commit": f"synthetic-{version}", "system_spec": spec},
        )

    def content(self, _skill_id, version, **_kwargs):
        files, content_hash, _baseline = packages[version]
        return SimpleNamespace(content_hash=content_hash, commit=f"synthetic-{version}"), files

    def installations(self, _installation_id):
        if self.remote_version is None:
            return []
        return [{
            "skill_id": "skill-update", "installed_version": self.remote_version,
            "latest_version": self.latest, "update_mode": "MANUAL",
            "skill_state": "active", "takedown_generation": 0,
        }]

    def record_install(self, **values):
        self.posts.append(dict(values))
        self.remote_version = values["version"]
        return {"effective_update_mode": "MANUAL"}

client = Client()
host = InstallationHost(profile)
install_receipts = InstallReceipts(state, lambda: dict(owner))
install_reviewer = InstallReviewer(client, state, install_receipts, host, lambda: dict(owner))
installer = InstallExecutor(client, state, install_receipts, install_reviewer, host, lambda: dict(owner))
setup_controller = SetupController(
    state, SetupReceipts(state, lambda: dict(owner)), host, SetupHost(), lambda: dict(owner)
)
update_receipts = UpdateReceipts(state, lambda: dict(owner))
update_reviewer = UpdateReviewer(client, state, update_receipts, host, lambda: dict(owner))
updater = UpdateExecutor(client, state, update_receipts, update_reviewer, host, lambda: dict(owner))

class Service:
    def install_plan(self, reference, *, update_mode=None):
        return install_reviewer.plan(reference, update_mode=update_mode)
    def install_apply(self, receipt, *, accept_partial=False):
        return installer.apply(receipt, accept_partial=accept_partial)
    def installed_setup_inspect(self, skill_id, *, version=None):
        return setup_controller.inspect(skill_id, version=version)
    def installed_setup_plan(self, skill_id, **values):
        return setup_controller.plan(skill_id, **values)
    def installed_setup_apply(self, receipt):
        return setup_controller.apply(receipt)
    def installed_setup_status(self, skill_id, *, version=None):
        return setup_controller.status(skill_id, version=version)
    def check_update(self, skill_id):
        return update_reviewer.check(skill_id)
    def update_plan(self, skill_id, **values):
        return update_reviewer.plan(skill_id, **values)
    def update_apply(self, receipt):
        return updater.apply(receipt)

service = Service()
def cli(arguments, expected_code=0):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = run_cli(parser.parse_args(arguments), service)
    assert code == expected_code, (code, output.getvalue())
    return json.loads(output.getvalue())

install = cli(["install", "skill-update@v1", "--plan", "--json"])
installed = cli(["install", "--apply-receipt", install["receipt"], "--json"])
assert installed["version"] == 1
client.latest = 2
assert cli(["check", "skill-update", "--json"])["state"] == "update_available"
set_approval_callback(lambda *_args, **_kwargs: "once")
verify = cli([
    "installed-setup", "plan", "skill-update", "--version", "1", "--phase", "verify",
    "--command", "true", "--json",
])
assert cli(["installed-setup", "apply", verify["receipt"], "--json"])["state"] == "passed"
assert cli(["installed-setup", "status", "skill-update", "--version", "1", "--json"])["ready_to_use"] is True
stale_v1_verify = cli([
    "installed-setup", "plan", "skill-update", "--version", "1", "--phase", "verify",
    "--command", "true", "--json",
])
set_approval_callback(None)

target = Path(installed["path"])
local_bytes = b"# locally edited exact bytes\\n"
(target / "SKILL.md").write_bytes(local_bytes)
plan = cli([
    "update", "skill-update", "--plan", "--preserve-modified", "--json"
])
assert plan["modified"] is True
assert plan["preservation"]["action"] == "preserve_unmanaged_copy"
assert client.posts == [{
    "skill_id": "skill-update", "installation_id": "hwi_update", "version": 1,
    "takedown_generation": 0, "update_mode": None,
}], client.posts
durable_replace = update_module._durable_replace
preservation_interrupted = False
preservation_destination = Path(plan["preservation"]["destination"])
def interrupt_preservation(source, destination):
    global preservation_interrupted
    if Path(destination) == preservation_destination and not preservation_interrupted:
        preservation_interrupted = True
        assert Path(source).is_relative_to(profile / "skills" / ".hub")
        assert list(iter_skill_index_files(profile / "skills", "SKILL.md")) == []
        raise OSError("synthetic interruption before edited-copy promotion")
    durable_replace(source, destination)
update_module._durable_replace = interrupt_preservation
preservation_failure = cli(["update", "--apply-receipt", plan["receipt"], "--json"], 8)
assert preservation_failure["ok"] is False
assert not target.exists() and not preservation_destination.exists()
update_module._durable_replace = durable_replace
advance = state.advance
interrupted = False
def interrupt_after_promotion(operation_id, phase, *, done=False):
    global interrupted
    if phase == "files_committed" and not interrupted:
        interrupted = True
        raise OSError("synthetic interruption after target promotion")
    advance(operation_id, phase, done=done)
state.advance = interrupt_after_promotion
failed = cli(["update", "--apply-receipt", plan["receipt"], "--json"], 8)
assert failed["ok"] is False
assert (target / "SKILL.md").read_bytes() == b"# Managed v2\\n"
state.advance = advance
result = cli(["update", "--apply-receipt", plan["receipt"], "--json"])
assert result["updated"] is True and result["version"] == 2
assert (target / "SKILL.md").read_bytes() == b"# Managed v2\\n"
preserved = Path(result["preserved_copy"])
assert preserved.parent == profile / "skills"
assert (preserved / "SKILL.md").read_bytes() == local_bytes
assert Path(result["recovery_path"]).is_dir()
assert cli(["installed-setup", "status", "skill-update", "--version", "2", "--json"])["ready_to_use"] is None
stale = cli(["installed-setup", "apply", stale_v1_verify["receipt"], "--json"], 6)
assert stale["ok"] is False and stale["category"] == 6
set_approval_callback(lambda *_args, **_kwargs: "once")
verify_v2 = cli([
    "installed-setup", "plan", "skill-update", "--version", "2", "--phase", "verify",
    "--command", "true", "--json",
])
assert cli(["installed-setup", "apply", verify_v2["receipt"], "--json"])["state"] == "passed"
assert cli(["installed-setup", "status", "skill-update", "--version", "2", "--json"])["ready_to_use"] is True
set_approval_callback(None)
assert cli(["check", "skill-update", "--json"])["state"] == "current"
assert len(client.posts) == 2
assert cli(["update", "--apply-receipt", plan["receipt"], "--json"]) == result
assert len(client.posts) == 2
indexed = list(iter_skill_index_files(profile / "skills", "SKILL.md"))
assert target / "SKILL.md" in indexed and preserved / "SKILL.md" in indexed
(profile / "update-result.json").write_text(json.dumps(result), encoding="utf-8")
print("pinned-host CLI update journey passed")
'''

REOPEN_PROBE = r'''
import json
import os
from pathlib import Path
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.installed_setup import inspect_installed_setup
from hermes_wisdom_plugin.store import WisdomStore
profile = Path(os.environ["HERMES_HOME"]).resolve()
state = WisdomStore(Path(os.environ["WISDOM_STATE_DIR"]))
owner = {
    "profile_name": "default", "profile_home": str(profile),
    "account_id": "synthetic-member", "organization_id": "synthetic-org",
    "base_url": "https://gateway.invalid", "installation_id": "hwi_update",
}
result = json.loads((profile / "update-result.json").read_text(encoding="utf-8"))
inspection = inspect_installed_setup(
    state, InstallationHost(profile), owner, "skill-update", version=2
)
assert len(inspection["setup_progress"]) == 1
assert inspection["setup_progress"][0]["phase"] == "verify"
assert inspection["ready_to_use"] is True
assert Path(result["preserved_copy"]).joinpath("SKILL.md").read_bytes() == b"# locally edited exact bytes\\n"
assert Path(result["recovery_path"]).is_dir()
print("fresh-process updated setup state passed")
'''


def _git(agent_path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=agent_path, capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_pinned_host_manual_update_preserves_edits_and_reopens_safely(tmp_path: Path):
    """Exercise U1 through the pinned host without touching a live profile."""
    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to the unmodified Agent checkout")
    agent_path = Path(agent_dir).resolve()
    assert _git(agent_path, "rev-parse", "HEAD") == EXPECTED_HOST_REVISION
    assert _git(agent_path, "status", "--porcelain") == ""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "terminal:\n  backend: local\napprovals:\n  mode: manual\n", encoding="utf-8"
    )
    dependencies = os.environ.get("WISDOM_HOST_DEPENDENCIES")
    paths = [str(PROJECT_ROOT), str(agent_path)]
    if dependencies:
        paths.append(dependencies)
    environment = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
        "HERMES_HOME": str(profile), "HERMES_INTERACTIVE": "1",
        "HERMES_SESSION_ID": "wisdom-update-integration",
        "WISDOM_AGENT_DIR": str(agent_path),
        "WISDOM_STATE_DIR": str(tmp_path / "state"),
        "PYTHONPATH": os.pathsep.join(paths), "PYTHONNOUSERSITE": "1",
    }
    interpreter = os.environ.get("WISDOM_HOST_PYTHON", sys.executable)
    first = subprocess.run(
        [interpreter, "-c", UPDATE_PROBE], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert first.returncode == 0, f"{first.stderr}\nstdout:\n{first.stdout}"
    assert first.stdout.strip() == "pinned-host CLI update journey passed"
    reopened = subprocess.run(
        [interpreter, "-c", REOPEN_PROBE], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert reopened.returncode == 0, f"{reopened.stderr}\nstdout:\n{reopened.stdout}"
    assert reopened.stdout.strip() == "fresh-process updated setup state passed"
    assert _git(agent_path, "status", "--porcelain") == ""
