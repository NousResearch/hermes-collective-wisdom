"""Vertical setup review, execution, status, and recovery tests."""

# pylint: disable=import-outside-toplevel,missing-function-docstring,redefined-outer-name

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.setup_receipts import SetupReceipts
from test_installed_setup import OWNER, installed  # pylint: disable=unused-import,wrong-import-order


class ExecutionHost:
    """Protocol double; filesystem/package inspection remains real."""

    def __init__(self):
        self.calls: list[tuple[str, Path, str]] = []
        self.next_result = {
            "state": "passed", "exit_code": 0, "output": "ok",
            "output_truncated": False,
        }
        self.status_result = {"state": "unknown", "reason": "unavailable"}
        self.status_calls: list[dict] = []

    def execute(self, command, *, workdir, task_id, on_started, timeout=600):
        del timeout
        self.calls.append((command, workdir, task_id))
        handle = {
            "session_id": "proc_test", "task_id": task_id,
            "session_key": task_id, "workdir": str(workdir),
        }
        on_started(handle)
        return {**self.next_result, "handle": handle}

    def status(self, handle, *, command, workdir, task_id):
        assert handle["task_id"] == task_id
        assert Path(handle["workdir"]) == workdir
        assert command
        self.status_calls.append(dict(handle))
        return dict(self.status_result)


@pytest.fixture
def controller(installed):
    from hermes_wisdom_plugin.setup_execution import SetupController

    store, inspection_host, target = installed
    execution_host = ExecutionHost()
    receipts = SetupReceipts(store, lambda: dict(OWNER))
    value = SetupController(
        store, receipts, inspection_host, execution_host, lambda: dict(OWNER)
    )
    return value, store, inspection_host, execution_host, target


def test_managed_operation_guard_rejects_redirected_lock_directory(tmp_path):
    from hermes_wisdom_plugin.setup_lock import managed_operation_guard

    root = tmp_path / "state"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "operation-locks").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackagePolicyError, match="unavailable or unsafe"):
        with managed_operation_guard(root, "skill-1"):
            pass
    assert not list(outside.iterdir())


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "fifo"])
def test_managed_operation_guard_rejects_unsafe_lock_file(tmp_path, unsafe_kind):
    """A lock path must resolve to one ordinary, singly linked file."""

    from hermes_wisdom_plugin.setup_lock import managed_operation_guard

    root = tmp_path / "state"
    locks = root / "operation-locks"
    locks.mkdir(parents=True)
    lock_path = locks / (hashlib.sha256(b"skill-1").hexdigest() + ".lock")
    if unsafe_kind == "hardlink":
        lock_path.write_bytes(b"")
        os.link(lock_path, tmp_path / "duplicate-lock")
    else:
        os.mkfifo(lock_path)

    with pytest.raises(PackagePolicyError, match="unavailable or unsafe"):
        with managed_operation_guard(root, "skill-1"):
            pass


def test_setup_step_preserves_exact_command_and_rejects_controls_or_secrets():
    from hermes_wisdom_plugin.setup_execution import SetupStep

    command = "printf 'trailing '  "
    assert SetupStep(phase="setup", index=0, command=command).command == command
    for unsafe in ("printf x\u0085whoami", "printf x\u202ewhoami", "token=sk-abcdefghijklmnop"):
        with pytest.raises(ValidationError):
            SetupStep(phase="setup", index=0, command=unsafe)


@pytest.mark.parametrize("stage", ["planning", "readiness"])
def test_missing_detectable_prerequisite_invalidates_prior_acknowledgement(controller, stage):
    """A historical acknowledgement cannot override a currently missing local fact."""
    value, _store, inspection_host, _execution_host, _target = controller
    inspection_host.environment.add("EXAMPLE_TOKEN")
    for index in (0, 2):
        receipt = value.plan("skill-1", version=1, phase="prerequisite", index=index)
        value.apply(receipt["receipt"])
    setup = value.plan("skill-1", version=1, phase="setup", command="printf configured")
    value.apply(setup["receipt"])
    if stage == "readiness":
        verify = value.plan("skill-1", version=1, phase="verify", command="printf verified")
        value.apply(verify["receipt"])
        assert value.inspect("skill-1")["ready_to_use"] is True
    inspection_host.environment.clear()
    if stage == "planning":
        with pytest.raises(PackagePolicyError, match="prerequisites"):
            value.plan("skill-1", version=1, phase="verify", command="printf verified")
    else:
        assert value.inspect("skill-1")["ready_to_use"] is not True


def test_plan_is_kind_separated_nonexecuting_and_apply_is_idempotent(controller):
    value, store, _inspection_host, execution_host, target = controller
    command = "printf configured > ../configured-marker  "

    plan = value.plan("skill-1", version=1, phase="setup", index=0, command=command)
    assert execution_host.calls == []
    assert plan["operation"] == "installed_setup"
    assert plan["step"]["command"] == command
    assert plan["target_path"] == str(target)

    result = value.apply(plan["receipt"])
    assert result["state"] == "passed"
    assert execution_host.calls == [(command, target, execution_host.calls[0][2])]
    assert value.apply(plan["receipt"]) == result
    assert len(execution_host.calls) == 1
    assert store.get_metadata("install_receipt:" + plan["receipt"]) is None
    assert store.get_metadata("setup_receipt:" + plan["receipt"])["result"] == result


def test_unknown_launch_is_never_replayed_and_requires_explicit_recovery(controller):
    value, _store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {
        "state": "unknown", "reason": "launch_acknowledgement_lost"
    }
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf maybe"
    )

    first = value.apply(plan["receipt"])
    second = value.apply(plan["receipt"])
    assert first["state"] == second["state"] == "unknown"
    assert len(execution_host.calls) == 1
    operation_id = first["operation_id"]
    assert value.recover("skill-1", operation_id)["recovery_review"] is True
    abandoned = value.recover(
        "skill-1", operation_id, acknowledged_effects_checked_and_stopped=True
    )
    assert abandoned["state"] == "abandoned"
    assert len(execution_host.calls) == 1
    assert value.apply(plan["receipt"])["state"] == "abandoned"


@pytest.mark.parametrize(
    "observable_host_result",
    [
        {"state": "running"},
        {"state": "passed", "exit_code": 0, "output": "finished"},
    ],
)
def test_recovery_status_returns_durable_running_snapshot_without_poll_or_write(
    controller, observable_host_result,
):
    """Passive review reports the journal even if polling would now be terminal."""

    value, store, _inspection_host, execution_host, target = controller
    execution_host.next_result = {"state": "running"}
    execution_host.status_result = observable_host_result
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf maybe"
    )
    started = value.apply(plan["receipt"])
    before = store.operation(started["operation_id"])

    status = value.recovery_status("skill-1", started["operation_id"])

    assert status == {
        "kind": "wisdom_setup",
        "skill_id": "skill-1",
        "operation_id": started["operation_id"],
        "receipt": plan["receipt"],
        "journal_phase": "running",
        "journal_state": "pending",
        "version": 1,
        "content_hash": plan["content_hash"],
        "target_path": str(target),
        "setup_key": plan["setup_key"],
        "step": plan["step"],
        "task_id": "wisdom-setup:" + plan["receipt"],
        "handle": before["payload"]["handle"],
        "result": None,
        "can_recover": True,
    }
    assert execution_host.status_calls == []
    assert store.operation(started["operation_id"]) == before


def test_recovery_status_returns_durable_terminal_snapshot_without_poll_or_write(controller):
    value, store, _inspection_host, execution_host, target = controller
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf complete"
    )
    result = value.apply(plan["receipt"])
    operation_id = store.get_metadata("setup_receipt:" + plan["receipt"])["operation_id"]
    before = store.operation(operation_id)

    status = value.recovery_status("skill-1", operation_id)

    assert status == {
        "kind": "wisdom_setup",
        "skill_id": "skill-1",
        "operation_id": operation_id,
        "receipt": plan["receipt"],
        "journal_phase": "passed",
        "journal_state": "done",
        "version": 1,
        "content_hash": plan["content_hash"],
        "target_path": str(target),
        "setup_key": plan["setup_key"],
        "step": plan["step"],
        "task_id": "wisdom-setup:" + plan["receipt"],
        "handle": before["payload"]["handle"],
        "result": result,
        "can_recover": False,
    }
    assert execution_host.status_calls == []
    assert store.operation(operation_id) == before


def test_reviewed_setup_recovery_revalidates_command_before_poll_or_abandon(controller):
    value, store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {"state": "running"}
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf reviewed"
    )
    started = value.apply(plan["receipt"])
    reviewed = value.recovery_status("skill-1", started["operation_id"])
    row = store.operation(started["operation_id"])
    payload = dict(row["payload"])
    payload["step"] = {**payload["step"], "command": "printf different"}
    store.replace_operation_payload(started["operation_id"], payload)

    with pytest.raises(WisdomConflict, match="reviewed|receipt|content"):
        value.recover(
            "skill-1",
            started["operation_id"],
            acknowledged_effects_checked_and_stopped=True,
            expected_recovery=reviewed,
            may_apply=lambda: True,
        )

    assert execution_host.status_calls == []
    assert store.operation(started["operation_id"])["state"] == "pending"


def test_reviewed_setup_recovery_never_abandons_a_still_running_process(controller):
    value, store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {"state": "running"}
    execution_host.status_result = {"state": "running"}
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf running"
    )
    started = value.apply(plan["receipt"])
    reviewed = value.recovery_status("skill-1", started["operation_id"])

    result = value.recover(
        "skill-1",
        started["operation_id"],
        acknowledged_effects_checked_and_stopped=True,
        expected_recovery=reviewed,
        may_apply=lambda: True,
    )

    assert result["state"] == "running"
    assert len(execution_host.status_calls) == 1
    assert store.operation(started["operation_id"])["state"] == "pending"


def test_revoked_review_has_no_setup_recovery_poll_or_write(controller):
    value, store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {"state": "running"}
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf revoked"
    )
    started = value.apply(plan["receipt"])
    reviewed = value.recovery_status("skill-1", started["operation_id"])
    before = store.operation(started["operation_id"])

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.recover(
            "skill-1",
            started["operation_id"],
            acknowledged_effects_checked_and_stopped=True,
            expected_recovery=reviewed,
            may_apply=lambda: False,
        )

    assert execution_host.status_calls == []
    assert store.operation(started["operation_id"]) == before


# pylint: disable-next=too-many-locals
def test_native_unknown_recovery_clear_uses_c3_journal_and_preserves_filesystem_effect(
    installed,
):
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from hermes_wisdom_plugin.native_review import NativeReviewController
    from hermes_wisdom_plugin.setup_execution import SetupController

    store, inspection_host, target = installed

    class UnknownAfterEffectHost:
        """Run a harmless command, then simulate losing its completion acknowledgement."""

        calls = 0

        def execute(self, command, *, workdir, task_id, on_started, timeout=600):
            del timeout
            self.calls += 1
            handle = {
                "session_id": "proc_native", "task_id": task_id,
                "session_key": task_id, "workdir": str(workdir),
            }
            on_started(handle)
            subprocess.run(command, cwd=workdir, shell=True, check=True, timeout=5)
            return {"state": "unknown", "reason": "completion_acknowledgement_lost"}

        def status(self, handle, *, command, workdir, task_id):
            del handle, command, workdir, task_id
            return {"state": "unknown", "reason": "process_record_unavailable"}

    host = UnknownAfterEffectHost()
    c3 = SetupController(
        store, SetupReceipts(store, lambda: dict(OWNER)), inspection_host, host,
        lambda: dict(OWNER),
    )
    info = c3.inspect("skill-1", version=1)

    class Service:
        """Expose the real C3 controller through the native workflow service seam."""

        def native_owner(self):
            return dict(OWNER)

        def update_plan(self, skill_id, **_choices):
            return {
                "operation": "update", "receipt": "update-parent", "skill_id": skill_id,
                "slug": "managed", "from_version": 0, "version": 1,
                "content_hash": info["content_hash"], "manifest_hash": "c" * 64,
                "allowed": True, "modified": False, "sensitive_expansion": [],
                "compatibility": {"outcome": "compatible"},
                "local_scan": {"status": "pass"}, "server_checks": {"status": "pass"},
                "policy": {}, "owner": dict(OWNER),
            }

        def update_apply(self, _receipt, *, may_apply):
            assert may_apply() is True
            return {"updated": True, "version": 1}

        def installed_setup_inspect(self, skill_id, *, version=None):
            return c3.inspect(skill_id, version=version)

        def installed_setup_plan(self, skill_id, **values):
            return c3.plan(skill_id, **values)

        def installed_setup_apply(self, receipt, *, may_apply):
            return c3.apply(receipt, may_apply=may_apply)

        def installed_setup_recovery_inspect(self, skill_id, operation_id):
            return c3.recovery_status(skill_id, operation_id)

        def installed_setup_recover(
            self, skill_id, operation_id, *, acknowledged=False,
            expected_recovery=None, may_apply=None,
        ):
            return c3.recover(
                skill_id, operation_id,
                acknowledged_effects_checked_and_stopped=acknowledged,
                expected_recovery=expected_recovery, may_apply=may_apply,
            )

    service = Service()
    invocation = NativeInvocation(
        platform="slack", profile="default", chat_type="dm", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="T1", scope_id="W1",
        message_id="request-1", session_key="slack:W1:D1:U1", adapter=object(),
    )
    native = NativeReviewController(store)
    parent = native.request(service, invocation, "skill-1")
    native.bind_delivery(parent["id"], invocation, "card-1")
    callback = replace(invocation, message_id="card-1")
    native.act(service, parent["id"], callback, "confirm")
    claim = native.claim_handoff(service, invocation)
    assert claim is not None
    setup = native.create_setup(
        service, claim, invocation, phase="setup", index=0,
        command="touch ../native-side-effect", explanation="Create the test marker.",
    )
    native.bind_delivery(setup["id"], invocation, "setup-card")
    setup_callback = replace(callback, message_id="setup-card")

    unknown = native.act(service, setup["id"], setup_callback, "confirm")
    marker = target.parent / "native-side-effect"
    assert unknown["state"] == "applying" and marker.is_file()
    reviewed = native.act(service, setup["id"], setup_callback, "setup.recover")
    assert reviewed["result"]["setup"]["recovery_review"] is True
    cleared = native.act(service, setup["id"], setup_callback, "setup.clear")

    assert cleared["state"] == "needs_review"
    assert cleared["result"]["setup"]["state"] == "abandoned"
    assert marker.is_file()
    assert host.calls == 1


def test_status_projects_unrecoverable_running_handle_as_unknown(controller):
    """A failed handle lookup must not remain projected as running forever."""

    value, _store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {"state": "running"}
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf maybe"
    )
    assert value.apply(plan["receipt"])["state"] == "running"

    status = value.status("skill-1", version=1)

    assert status["setup_progress"][-1]["state"] == "unknown"
    assert len(execution_host.calls) == 1
    assert len(execution_host.status_calls) == 1


def test_status_respects_shared_nonexpiring_operation_guard(controller):
    """Status cannot race a guarded apply or recovery transition."""

    from hermes_wisdom_plugin.setup_lock import managed_operation_guard

    value, store, _inspection_host, _execution_host, _target = controller
    with managed_operation_guard(Path(store.root), "skill-1"):
        with pytest.raises(PackagePolicyError, match="starting or running"):
            value.status("skill-1", version=1)


@pytest.mark.parametrize("stale_field", ["owner", "content_hash"])
def test_status_ignores_stale_owner_or_content_pending_rows(controller, stale_field):
    """Status reconciles only the current installation and enrolment scope."""

    value, store, _inspection_host, execution_host, target = controller
    info = value.inspect("skill-1", version=1)
    payload = {
        "owner": dict(OWNER),
        "receipt": "wip_" + "a" * 32,
        "version": 1,
        "content_hash": info["content_hash"],
        "target_path": str(target),
        "setup_key": "stale",
        "step": {"phase": "setup", "index": 0, "command": "printf stale"},
        "task_id": "wisdom-setup:stale",
        "handle": {
            "session_id": "stale", "task_id": "wisdom-setup:stale",
            "session_key": "stale", "workdir": str(target),
        },
    }
    if stale_field == "owner":
        payload["owner"] = {**OWNER, "account_id": "old-member"}
    else:
        payload["content_hash"] = "0" * 64
    store.journal("wisdom_setup", "skill-1", "running", payload)

    value.status("skill-1", version=1)

    assert execution_host.status_calls == []


def test_stale_reconciler_cannot_overwrite_recovery_terminal_state(controller):
    """A stale process result loses to a newer recovery decision."""

    value, store, _inspection_host, execution_host, _target = controller
    execution_host.next_result = {"state": "unknown", "reason": "wait_unavailable"}
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf maybe"
    )
    started = value.apply(plan["receipt"])
    stale_row = store.operation(started["operation_id"])
    stale_payload = dict(stale_row["payload"])
    recovered = value.recover(
        "skill-1",
        started["operation_id"],
        acknowledged_effects_checked_and_stopped=True,
    )

    reconciled = value._finish(  # pylint: disable=protected-access
        stale_row, stale_payload, {"state": "passed", "exit_code": 0}
    )

    assert recovered["state"] == "abandoned"
    assert reconciled == recovered
    assert store.operation(started["operation_id"])["phase"] == "abandoned"


@pytest.mark.parametrize(
    ("phase", "command"), [("setup", "printf complete"), ("prerequisite", "")]
)
def test_completion_acknowledgement_interruption_keeps_atomic_outcome(  # pylint: disable=too-many-locals
    controller, monkeypatch, phase, command
):
    """A crash after durable completion leaves no pending journal to recover."""

    value, store, _inspection_host, _execution_host, _target = controller
    receipts = value.receipts
    plan = value.plan(
        "skill-1", version=1, phase=phase,
        index=2 if phase == "prerequisite" else 0, command=command,
    )
    finalize = getattr(receipts, "finalize", None)
    if finalize is not None:
        def finish_then_interrupt(*args, **kwargs):
            finalize(*args, **kwargs)
            raise OSError("completion acknowledgement interrupted")

        monkeypatch.setattr(receipts, "finalize", finish_then_interrupt)
    else:
        complete = receipts.complete

        def complete_then_interrupt(receipt, result):
            complete(receipt, result)
            raise OSError("completion acknowledgement interrupted")

        monkeypatch.setattr(receipts, "complete", complete_then_interrupt)

    with pytest.raises(OSError, match="completion acknowledgement"):
        value.apply(plan["receipt"])

    outcome = value.apply(plan["receipt"])

    assert outcome["state"] == "passed"
    assert store.pending_operations() == []
    operation_id = store.get_metadata("setup_receipt:" + plan["receipt"])["operation_id"]
    assert store.operation(operation_id)["state"] == "done"


def test_prerequisite_setup_and_separate_verification_derive_readiness(controller):
    value, _store, inspection_host, execution_host, _target = controller
    with pytest.raises(PackagePolicyError, match="prerequisites and setup"):
        value.plan(
            "skill-1", version=1, phase="verify", index=0,
            command="test -f ../configured-marker",
        )

    acknowledgement = value.plan(
        "skill-1", version=1, phase="prerequisite", index=2, command=""
    )
    assert acknowledgement["allowed"] is True
    assert value.apply(acknowledgement["receipt"])["state"] == "passed"

    setup = value.plan(
        "skill-1", version=1, phase="setup", index=0,
        command="printf configured > ../configured-marker",
    )
    assert value.apply(setup["receipt"])["state"] == "passed"
    inspection_host.environment.add("EXAMPLE_TOKEN")

    verification = value.plan(
        "skill-1", version=1, phase="verify", index=0,
        command="test -f ../configured-marker",
    )
    assert verification["receipt"] != setup["receipt"]
    assert value.apply(verification["receipt"])["state"] == "passed"
    status = value.status("skill-1", version=1)
    assert status["ready_to_use"] is True
    assert status["state"] == "verified"
    assert len(execution_host.calls) == 2


def test_changed_owner_or_installed_content_invalidates_setup_receipt(controller):
    value, _store, _inspection_host, _execution_host, target = controller
    plan = value.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf safe"
    )
    target.joinpath("SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(PackagePolicyError):
        value.apply(plan["receipt"])
