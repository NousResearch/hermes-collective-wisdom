"""Configured-host probe for the real invocation-scoped TUI review API."""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_host_presentation_integration import _configured_host, _isolated_environment


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "hermes-collective-wisdom"
PROBE = r'''
import json
from pathlib import Path
import threading

from hermes_cli import plugins
from tui_gateway import server, server_requests
from tui_review_rpc import TuiReviewRpcDriver

PLUGIN_ID = "hermes-collective-wisdom"
HOME = Path(__import__("os").environ["HERMES_HOME"])
manager = plugins.PluginManager()
manager.discover_and_load()
entry = manager._plugin_commands["wisdom"]
runtime = entry["handler"].__self__
assert runtime.request_review.__self__.plugin_id == PLUGIN_ID
assert runtime.can_request_review.__self__.plugin_id == PLUGIN_ID
assert runtime.review_action_type is plugins.ReviewAction
assert runtime.review_outcome_type is plugins.ReviewOutcome

owner = {
    "profile_name": "default", "profile_home": str(HOME),
    "account_id": "member", "organization_id": "org",
    "base_url": "https://fixture.invalid", "installation_id": "install",
}
calls = []
reviews = []
invalidate_next_owner_check = False
update_plan_generation = 0


def plan(operation):
    value = {
        "operation": operation, "receipt": "receipt-" + operation,
        "owner": owner, "skill_id": "skill-1", "slug": "fixture", "version": 2,
        "content_hash": "sha256:" + "a" * 64,
        "manifest_hash": "sha256:" + "b" * 64,
        "manifest": {"schema_version": 1, "name": "fixture", "requirements": {}},
        "files": [{"path": "SKILL.md", "mode": "file",
                   "hash": "sha256:" + "c" * 64, "size": 12000}],
        "compatibility": {"outcome": "compatible", "satisfied": [],
                          "setup_actions": [], "limitations": [], "blocked": []},
        "server_checks": {"security_check": {"state": "passed"}},
        "local_scan": {"allowed": True, "findings": []},
        "policy": {"requires_explicit_confirmation": True},
        "acknowledgements": {"reviewed": True},
        "hashes": {"content": "sha256:" + "a" * 64,
                   "manifest": "sha256:" + "b" * 64},
        "permitted_actions": ["confirm_" + operation, "recheck"],
        "allowed": True,
        "source_body_fixture": "BEGIN-SOURCE\n" + "x" * 12000 + "\nEND-SOURCE",
    }
    if operation == "installed_setup":
        value.update({
            "step": {"phase": "setup", "index": 0, "command": "printf configured"},
            "setup_instruction": "Configure fixture", "setup_key": "d" * 64,
            "target_path": str(HOME / "skills" / "fixture"),
        })
    return value


class Service:
    closed = False

    def __init__(self):
        global invalidate_next_owner_check
        self.invalidate_review_on_owner_check = invalidate_next_owner_check
        invalidate_next_owner_check = False

    def install_plan(self, reference, *, update_mode=None):
        calls.append(("install_plan", reference, update_mode))
        return plan("install")

    def install_apply(self, receipt, *, accept_partial=False):
        assert not self.closed
        calls.append(("install_apply", receipt, accept_partial))
        return {"installed": True, "skill_id": "skill-1", "version": 2}

    def native_install_inspect(self, receipt):
        calls.append(("native_install_inspect", receipt))
        return [{"path": "SKILL.md", "content": "# exact install" + chr(10),
                 "content_start": 0, "content_end": 16,
                 "hash": "sha256:" + "c" * 64}]

    def update_plan(self, skill_id, **choices):
        global update_plan_generation
        update_plan_generation += 1
        calls.append(("update_plan", skill_id, choices))
        value = plan("update")
        value["receipt"] = "receipt-update-" + str(update_plan_generation)
        return value

    def update_apply(self, receipt, *, may_apply=None):
        assert not self.closed
        assert may_apply is None or may_apply() is True
        calls.append(("update_apply", receipt))
        return {"updated": True, "skill_id": "skill-1", "version": 2}

    def prepare_update_deferral(self, value):
        calls.append(("prepare_update_deferral", value["receipt"]))
        return {
            "owner": owner,
            "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
            "not_now_suppression_days": 7,
        }

    def defer_update(self, value, *, may_apply):
        assert may_apply() is True
        calls.append(("defer_update", value["reference"], value["not_now_suppression_days"]))
        (HOME / "tui-update-deferral.json").write_text(
            json.dumps(value, sort_keys=True), encoding="utf-8"
        )
        return {
            "deferred": True, "skill_id": "skill-1", "version": 2,
            "not_now_suppression_days": 7, "preference_sync": "pending",
        }

    def native_update_inspect(self, receipt):
        calls.append(("native_update_inspect", receipt))
        return [{"path": "SKILL.md", "content": "# exact update" + chr(10),
                 "content_start": 0, "content_end": 15,
                 "hash": "sha256:" + "c" * 64}]

    def installed_setup_inspect(self, skill_id, *, version=None):
        calls.append(("installed_setup_inspect", skill_id, version))
        return {"skill_id": skill_id, "version": version,
                "owner": owner, "guidance": {"setup_instructions": ["Configure fixture"]},
                "content_hash": "sha256:" + "a" * 64,
                "compatibility": {"outcome": "compatible"},
                "files": ["SKILL.md", "refs/wisdom-setup.md"]}

    def installed_setup_plan(self, skill_id, *, version, phase, index, command):
        calls.append(("installed_setup_plan", skill_id, version, phase, index, command))
        return plan("installed_setup")

    def installed_setup_apply(self, receipt, *, may_apply=None):
        assert not self.closed
        assert may_apply is None or may_apply() is True
        calls.append(("installed_setup_apply", receipt))
        return {"state": "passed", "skill_id": "skill-1", "version": 2}

    def check_owner(self):
        if self.invalidate_review_on_owner_check:
            server._begin_tui_review_input(sid, session)

    def close(self):
        self.closed = True


services = []
def service_factory():
    value = Service()
    services.append(value)
    return value
runtime._service = service_factory

class Transport:
    supports_local_presentation = True

    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def write(self, frame):
        with self.lock:
            self.frames.append(frame)
        return True

    def close(self):
        pass


transport = Transport()
sid = "wisdom-real-command-review"
session = {
    "history_lock": threading.RLock(), "profile_home": str(HOME),
    "running": False, "session_key": sid, "transport": transport,
}
server._sessions[sid] = session
server_requests.reset_for_tests()
server_requests.bind_sinks(transport.write, lambda *_args: None)

# The public availability check must fail closed outside actual TUI command admission.
before_services = len(services)
outside = plugins.resolve_plugin_command_result(entry["handler"]("install skill-1@v2"))
assert "terminal" in outside.lower()
assert len(services) == before_services


rpc = TuiReviewRpcDriver(
    lambda raw: server._run_tui_plugin_command(entry, raw, sid, session, transport),
    lambda response: server_requests.resolve_response(response, transport=transport),
    transport,
    timeout=5,
)


def invoke(raw, action_id):
    output, params = rpc.invoke(raw, action_id)
    if params is not None:
        reviews.append((
            params["title"], params["body"],
            tuple((item["id"], item["label"]) for item in params["actions"]),
        ))
    return output


install = invoke("install skill-1@v2", "install")
install_json = install.split("\n\n", 1)[0]
assert install_json.startswith("{"), repr((
    install, reviews, calls, entry.get("review_owner"),
))
assert json.loads(install_json)["installed"] is True
assert "/wisdom installed-setup inspect skill-1 --version 2" in install
assert "not run automatically" in install
assert reviews and reviews[0][0] == "Review Collective Wisdom install"
reviewed_install = json.loads(reviews[0][1])
for field in (
    "owner", "hashes", "files", "manifest", "compatibility", "server_checks",
    "local_scan", "policy", "acknowledgements",
):
    assert reviewed_install["plan"][field]
assert reviewed_install["plan"]["owner"] == owner
assert reviewed_install["package_files"][0]["content"] == "# exact install" + chr(10)
assert reviewed_install["apply_acknowledgements"] == {"accept_partial": False}
assert reviews[0][2] == (("install", "Install exact reviewed package"),)

inspect = invoke("installed-setup inspect skill-1 --version 2", None)
assert json.loads(inspect)["guidance"]["setup_instructions"] == ["Configure fixture"]
review_count = len(reviews)
setup = invoke(
    "installed-setup plan skill-1 --version 2 --phase setup --command 'printf configured'",
    "run_setup",
)
assert json.loads(setup)["state"] == "passed"
assert len(reviews) == review_count + 1

update = invoke("update skill-1", "update")
assert json.loads(update)["updated"] is True

defer_review = len(reviews)
deferred = invoke("update skill-1", "defer")
assert json.loads(deferred.split("\n\n", 1)[0])["deferred"] is True
assert "Manual reopen: /wisdom update skill-1" in deferred
assert (HOME / "tui-update-deferral.json").is_file()
deferred_receipt = json.loads(reviews[defer_review][1])["plan"]["receipt"]
assert reviews[defer_review][2] == (
    ("update", "Update exact reviewed package"), ("defer", "Not Now")
)

reopen_review = len(reviews)
reopened = invoke("update skill-1", "update")
assert json.loads(reopened)["updated"] is True
reopened_receipt = json.loads(reviews[reopen_review][1])["plan"]["receipt"]
assert reopened_receipt != deferred_receipt
assert ("update_apply", reopened_receipt) in calls

before = list(calls)
unknown = invoke("update skill-1", "forged")
assert "Nothing was changed" in unknown
assert calls[:-3] == before
assert [item[0] for item in calls[-3:]] == [
    "update_plan", "native_update_inspect", "prepare_update_deferral",
]

before = list(calls)
bypass = invoke("install --apply-receipt receipt-install", None)
assert "cannot apply a receipt" in bypass
assert calls == before

# A selected UI response is not durable host authority. If a new session input
# invalidates this invocation while Wisdom rereads its owner, mutation must stop.
before = list(calls)
invalidate_next_owner_check = True
stale_after_owner_read = invoke("update skill-1", "update")
assert "Nothing was changed" in stale_after_owner_read
assert calls[:-3] == before
assert [item[0] for item in calls[-3:]] == [
    "update_plan", "native_update_inspect", "prepare_update_deferral",
]

runtime.close()
assert all(service.closed for service in services)
server._sessions.pop(sid, None)
(HOME / "tui-review-probe-passed").write_text("passed", encoding="utf-8")
'''


def test_discovered_plugin_uses_real_host_review_types_and_context(tmp_path: Path) -> None:
    """Cross actual registration and request_review without live services or profiles."""

    agent_path, interpreter = _configured_host()
    assert (agent_path / "hermes_cli" / "plugins_review.py").is_file(), (
        f"configured host lacks review/action support: {agent_path}"
    )
    profile = tmp_path / "hermes-home"
    plugin_dir = profile / "plugins" / PLUGIN_ID
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (profile / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n",
        encoding="utf-8",
    )
    environment = _isolated_environment(tmp_path, profile, agent_path, "tui:review")
    result = subprocess.run(
        [str(interpreter), "-c", PROBE], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    assert (profile / "tui-review-probe-passed").read_text(encoding="utf-8") == "passed"


def test_real_kernels_cross_registered_host_command_and_review_rpc(tmp_path: Path) -> None:
    """Exercise real install, setup, and update kernels through host review RPC authority."""

    agent_path, interpreter = _configured_host()
    profile = tmp_path / "hermes-home"
    plugin_dir = profile / "plugins" / PLUGIN_ID
    plugin_dir.parent.mkdir(parents=True)
    plugin_dir.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    (profile / "config.yaml").write_text(
        "terminal:\n  backend: local\napprovals:\n  mode: manual\n"
        f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n",
        encoding="utf-8",
    )
    environment = _isolated_environment(tmp_path, profile, agent_path, "tui:real-kernel")
    probe = PLUGIN_ROOT / "tests" / "fixtures" / "wisdom_tui_real_kernel_probe.py"
    result = subprocess.run(
        [str(interpreter), str(probe)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    assert (profile / "real-kernel-probe-passed").read_text(encoding="utf-8") == "passed"
