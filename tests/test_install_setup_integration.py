"""An isolated Install applies only the reviewed files and declared setup."""

import subprocess

from installed_test_support import JsonState
from review_test_support import _content
from test_install_execution import invocation_executor  # pylint: disable=unused-import
from test_installed_setup import Host as InspectionHost

from hermes_wisdom_plugin.contract import canonical_json_bytes
from hermes_wisdom_plugin.direct_services import DirectState, SetupService
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.setup_document import PREFIX, SETUP_PATH, SUFFIX


# This fixture owns an isolated filesystem, local state, and fake Gateway.
# pylint: disable=missing-function-docstring,redefined-outer-name,too-many-locals

def test_install_runs_exact_declared_setup_once_without_second_prompt(
    invocation_executor, monkeypatch,
):
    executor, store, client, _receipts, _original_plan, target, _authority = (
        invocation_executor
    )
    command = "printf done > ../setup-ran"
    guidance = {
        "schema_version": 1, "execution_requires_user_approval": True,
        "requirements": [], "setup_instructions": [], "setup_commands": [command],
        "credential_handoff": [], "compatibility_limits": [],
        "verification_step": "Check the marker.", "removed_or_generalized": [],
        "related_skills": [],
    }
    body = PREFIX.encode() + canonical_json_bytes(guidance) + SUFFIX.encode()
    client.files.append((SETUP_PATH, "file", body))
    content = _content(client.files)
    client.version_value["content_hash"] = content.content_hash
    client.version_value["commit"] = content.commit
    plan = executor.reviewer.plan("skill-1@v2")
    assert plan["setup_commands"] == [command]
    assert not target.exists()

    class DisposableHost:  # pylint: disable=too-few-public-methods
        """Actually execute only in the temporary fixture's managed directory."""

        def __init__(self):
            self.calls = []

        def execute(self, exact, *, workdir, task_id, on_started):
            assert workdir == target
            self.calls.append(exact)
            on_started({"session_id": "fixture", "task_id": task_id,
                        "workdir": str(workdir)})
            run = subprocess.run(exact, shell=True, cwd=workdir, check=False,
                                 capture_output=True, text=True, timeout=10)
            return {"state": "passed" if run.returncode == 0 else "failed",
                    "exit_code": run.returncode, "output": run.stdout + run.stderr}

    host = DisposableHost()
    progress = DirectState(JsonState(store.root), store.root)
    setup = SetupService(progress, store, InspectionHost(executor.host.skills_root),
                         host, executor._owner)  # pylint: disable=protected-access
    monkeypatch.setattr(
        InvocationService, "_installation_services",
        lambda self: (executor.reviewer, executor),
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    first = service.install_apply(plan["receipt"])
    assert first["installed"] is True
    assert first["setup_required"] is True
    assert first["ready_to_use"] is None
    assert first["setup"]["state"] == "passed"
    assert first["setup_execution_authorized"] is True
    assert (target.parent / "setup-ran").read_text() == "done"
    assert host.calls == [command]
    assert len(client.record_calls) == 1

    # Replaying the same Install receipt cannot launch setup or install twice.
    second = service.install_apply(plan["receipt"])
    assert second == first
    assert host.calls == [command]
    assert len(client.record_calls) == 1
