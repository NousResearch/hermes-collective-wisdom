"""Atomic manual update replacement, preservation, and recovery tests."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name
# pylint: disable=too-few-public-methods,wrong-import-order,protected-access
# pylint: disable=import-outside-toplevel,duplicate-code

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.contract import org_directory_name
from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import (
    UpdateAuthorityRejected, UpdateReviewer, VerifiedUpdate,
)
from test_install_execution import Host as InstallHost
from test_install_review import Host as ReviewHost
from test_update_review import Client as ReviewClient


OWNER = {
    "profile_name": "default", "profile_home": "/profiles/default",
    "account_id": "account-1", "organization_id": "org-1",
    "base_url": "https://gateway.example", "installation_id": "install-1",
}
V1 = b"# version one\n"
V2 = b"# version two\n"


def _hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


class Host:
    def __init__(self, root):
        self.skills_root = root

    def validate_target(self, path):
        assert Path(path).is_relative_to(self.skills_root)

    def scan(self, path):
        assert Path(path).is_relative_to(self.skills_root)
        return {"allowed": True, "verdict": "allow", "summary": "clean", "findings": []}


class Reviewer:
    reject = False

    def verify(self, plan, *, recovery_phase=None):
        del recovery_phase
        if self.reject:
            raise UpdateAuthorityRejected("authority removed")
        return VerifiedUpdate(
            plan=dict(plan), files=[("SKILL.md", "file", V2)],
            baseline={"SKILL.md": _hash(V2)},
            previous_snapshot=dict(plan["local_snapshot"]),
        )


class Client:
    def __init__(self):
        self.remote_version = 1
        self.record_calls = []
        self.error = None

    def installations(self, _installation_id):
        return [{
            "skill_id": "skill-1", "installed_version": self.remote_version,
            "latest_version": 2, "update_mode": "MANUAL",
            "skill_state": "active", "takedown_generation": 7,
        }]

    def record_install(self, **values):
        self.record_calls.append(values)
        if self.error:
            raise self.error
        self.remote_version = values["version"]
        return SimpleNamespace(effective_update_mode="MANUAL")


@pytest.fixture
def executor(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    state = tmp_path / "state"
    store = WisdomStore(state)
    skills = tmp_path / "profile" / "skills"
    target = skills / "_wisdom" / org_directory_name(owner["organization_id"]) / "managed-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(V1)
    previous = {
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "managed-skill", "version": 1, "content_hash": "sha256:old",
        "baseline": {"SKILL.md": _hash(V1)}, "target_path": str(target),
        "update_mode": "MANUAL", "state": "active", "owner": owner,
    }
    store.record_install(previous)
    receipts = UpdateReceipts(store, lambda: dict(owner))
    plan = receipts.create({
        "skill_id": "skill-1", "slug": "managed-skill", "from_version": 1,
        "version": 2, "content_hash": "sha256:new",
        "baseline": {"SKILL.md": _hash(V2)},
        "previous_content_hash": "sha256:old",
        "previous_baseline": {"SKILL.md": _hash(V1)},
        "previous_installation": previous, "target_path": str(target),
        "local_snapshot": {"SKILL.md": _hash(V1)},
        "takedown_generation": 7, "update_mode": "MANUAL",
        "authority": {
            "installed_version": 1, "latest_version": 2,
            "takedown_generation": 7, "update_mode": "MANUAL",
            "skill_state": "active",
        },
        "preservation": {"action": "none", "destination": None},
        "compatibility": {"outcome": "compatible"},
        "local_scan": {"allowed": True}, "allowed": True,
        "permitted_actions": ["confirm_update", "recheck"],
    })
    client = Client()
    value = UpdateExecutor(
        client, store, receipts, Reviewer(), Host(skills), lambda: dict(owner)
    )
    return value, store, client, receipts, plan, target, owner


@pytest.mark.parametrize("storage_name", [".hub", ".archive"])
def test_update_rejects_redirected_storage_before_mutation(executor, tmp_path, storage_name):
    value, store, client, _receipts, plan, target, _owner = executor
    outside = tmp_path / "outside-storage"
    outside.mkdir()
    (value.host.skills_root / storage_name).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackagePolicyError, match="symlink"):
        value.apply(plan["receipt"])

    assert not list(outside.iterdir())
    assert (target / "SKILL.md").read_bytes() == V1
    assert store.installation("skill-1")["version"] == 1
    assert client.record_calls == []


def test_clean_update_commits_exact_bytes_and_retains_recovery_tree(executor):
    value, store, client, receipts, plan, target, _owner = executor

    result = value.apply(plan["receipt"])

    assert result["updated"] is True and result["version"] == 2
    assert (target / "SKILL.md").read_bytes() == V2
    assert Path(result["recovery_path"]).joinpath("SKILL.md").read_bytes() == V1
    assert result["preserved_copy"] is None
    assert store.installation("skill-1")["version"] == 2
    assert receipts.outcome(plan["receipt"]) == result
    assert len(client.record_calls) == 1


def test_modified_update_preserves_reviewed_actual_bytes_before_swap(executor):
    value, _store, _client, receipts, plan, target, _owner = executor
    edited = b"# precious raccoon edits\n"
    (target / "SKILL.md").write_bytes(edited)
    destination = value.host.skills_root / "managed-skill-local-before-v2-fixed"
    plan = receipts.create({
        **{key: val for key, val in plan.items()
           if key not in {"receipt", "operation", "owner", "created_at", "expires_at"}},
        "local_snapshot": {"SKILL.md": _hash(edited)}, "modified": True,
        "preservation": {
            "action": "preserve_unmanaged_copy", "destination": str(destination)
        },
    })

    first = value.apply(plan["receipt"])
    second = value.apply(plan["receipt"])

    assert destination.joinpath("SKILL.md").read_bytes() == edited
    assert Path(first["recovery_path"]).joinpath("SKILL.md").read_bytes() == edited
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert second == first
    assert len(list(value.host.skills_root.glob("managed-skill-local-before-v2-*"))) == 1


def test_crash_after_old_move_recovers_from_hash_evidence(executor, monkeypatch):
    value, store, _client, _receipts, plan, target, _owner = executor
    advance = store.advance
    failed = False

    def fail_once(operation_id, phase, *, done=False):
        nonlocal failed
        if phase == "old_preserved" and not failed:
            failed = True
            raise OSError("journal interrupted after old move")
        advance(operation_id, phase, done=done)

    monkeypatch.setattr(store, "advance", fail_once)
    with pytest.raises(OSError, match="old move"):
        value.apply(plan["receipt"])
    assert not target.exists()
    monkeypatch.setattr(store, "advance", advance)

    assert value.apply(plan["receipt"])["updated"] is True
    assert target.joinpath("SKILL.md").read_bytes() == V2


def test_partial_staging_write_is_rebuilt_from_reverified_bytes(executor, monkeypatch):
    value, store, _client, _receipts, plan, target, _owner = executor
    from hermes_wisdom_plugin import update_execution
    write_staging = update_execution._write_staging

    def partial(path, _files):
        path.mkdir(parents=True)
        (path / "SKILL.md").write_bytes(b"partial")
        raise OSError("staging interrupted")

    monkeypatch.setattr(update_execution, "_write_staging", partial)
    with pytest.raises(OSError, match="staging interrupted"):
        value.apply(plan["receipt"])
    assert target.joinpath("SKILL.md").read_bytes() == V1
    assert store.pending_operations()[0]["phase"] == "reviewed"

    monkeypatch.setattr(update_execution, "_write_staging", write_staging)
    assert value.apply(plan["receipt"])["updated"] is True
    assert target.joinpath("SKILL.md").read_bytes() == V2


def test_crash_after_new_move_recovers_without_replacing_again(executor, monkeypatch):
    value, store, _client, _receipts, plan, target, _owner = executor
    advance = store.advance
    failed = False

    def fail_once(operation_id, phase, *, done=False):
        nonlocal failed
        if phase == "files_committed" and not failed:
            failed = True
            raise OSError("journal interrupted after new move")
        advance(operation_id, phase, done=done)

    monkeypatch.setattr(store, "advance", fail_once)
    with pytest.raises(OSError, match="new move"):
        value.apply(plan["receipt"])
    assert target.joinpath("SKILL.md").read_bytes() == V2
    monkeypatch.setattr(store, "advance", advance)

    assert value.apply(plan["receipt"])["updated"] is True


def test_transient_gateway_error_is_pending_and_retry_converges(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = RuntimeError("offline secret")

    pending = value.apply(plan["receipt"])

    assert pending["pending"] is True
    assert "secret" not in pending["error"]
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert store.pending_operations()[0]["phase"] == "local_ledger_committed"
    client.error = None
    assert value.apply(plan["receipt"])["updated"] is True
    assert len(client.record_calls) == 2


def test_lost_success_response_reconciles_remote_target_without_second_post(executor):
    value, _store, client, _receipts, plan, _target, _owner = executor

    def applied_then_lost(**values):
        client.record_calls.append(values)
        client.remote_version = 2
        raise RuntimeError("response lost")

    client.record_install = applied_then_lost
    assert value.apply(plan["receipt"])["pending"] is True
    client.record_install = lambda **_values: (_ for _ in ()).throw(
        AssertionError("must reconcile without repost")
    )

    assert value.apply(plan["receipt"])["updated"] is True
    assert len(client.record_calls) == 1


def test_terminal_authority_removal_during_recovery_quarantines_and_deactivates(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = RuntimeError("offline")
    assert value.apply(plan["receipt"])["pending"] is True
    value.reviewer.reject = True

    rejected = value.apply(plan["receipt"])

    assert rejected["rejected"] is True
    assert not target.exists()
    assert Path(rejected["quarantine_path"]).joinpath("SKILL.md").read_bytes() == V2
    assert Path(rejected["recovery_path"]).joinpath("SKILL.md").read_bytes() == V1
    assert store.installation("skill-1")["state"] == "inactive"
    assert store.installation("skill-1", owner=_owner)["state"] == "inactive"
    assert store.pending_operations() == []


@pytest.mark.parametrize(
    "code",
    ["skill_not_active", "skill_taken_down", "skill_archived", "skill_not_found",
     "version_not_found", "takedown_generation_changed"],
)
def test_terminal_gateway_rejection_quarantines_new_bytes_without_reactivation(
    executor, code
):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = WisdomConflict(code, status=409, code=code)

    with pytest.raises(WisdomConflict, match=code):
        value.apply(plan["receipt"])

    operation = store.operation(store.get_metadata(
        "update_receipt:" + plan["receipt"]
    )["operation_id"])
    assert operation["state"] == "done" and operation["phase"] == "gateway_rejected"
    assert not target.exists()
    assert Path(operation["payload"]["quarantine_path"]).joinpath("SKILL.md").read_bytes() == V2
    assert Path(operation["payload"]["recovery_path"]).joinpath("SKILL.md").read_bytes() == V1
    assert store.installation("skill-1")["state"] == "inactive"


@pytest.mark.parametrize("code", ["installation_changed", "policy_not_configured"])
def test_concurrent_or_policy_409_stays_pending_and_does_not_quarantine(executor, code):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = WisdomConflict(code, status=409, code=code)

    result = value.apply(plan["receipt"])

    assert result["pending"] is True
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert store.installation("skill-1")["state"] == "active"
    assert store.pending_operations()[0]["phase"] == "local_ledger_committed"


def test_pending_setup_blocks_update_even_when_database_lease_expired(executor):
    value, store, _client, _receipts, plan, _target, _owner = executor
    store.journal("wisdom_setup", "skill-1", "unknown", {"owner": OWNER})

    with pytest.raises(PackagePolicyError, match="setup operation"):
        value.apply(plan["receipt"])


def test_pending_update_blocks_install_and_uninstall_paths(executor):
    value, store, _client, _receipts, plan, _target, owner = executor
    assert value.apply(plan["receipt"])["updated"] is True
    # Re-open a representative update journal to prove the existing executor checks kind.
    store.journal("update", "skill-1", "local_ledger_committed", {"owner": owner})
    install = InstallExecutor(
        SimpleNamespace(), store, SimpleNamespace(), SimpleNamespace(),
        InstallHost(value.host.skills_root), lambda: dict(owner)
    )
    with pytest.raises(PackagePolicyError, match="managed operation"):
        install.uninstall("skill-1")


class RealClient(ReviewClient):
    """Package and authority transport for real-reviewer recovery coverage."""

    def __init__(self):
        super().__init__()
        self.record_calls = []
        self.record_error = None
        self.committed_mode = "MANUAL"

    def record_install(self, **values):
        self.record_calls.append(values)
        if self.record_error == "before":
            raise RuntimeError("temporarily offline")
        self.remote["installed_version"] = values["version"]
        self.remote["update_mode"] = self.committed_mode
        if self.record_error == "after":
            raise RuntimeError("successful response lost")
        return SimpleNamespace(effective_update_mode=self.committed_mode)


@pytest.fixture
def real_executor(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    host = ReviewHost(tmp_path / "profile")
    client = RealClient()
    target = (host.skills_root / "_wisdom"
              / org_directory_name(owner["organization_id"]) / "managed-skill")
    target.mkdir(parents=True)
    for name, _mode, body in client.files[1]:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    baseline = {name: _hash(body) for name, _mode, body in client.files[1]}
    store.record_install({
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "managed-skill", "version": 1,
        "content_hash": client._content(client.files[1]).content_hash,
        "baseline": baseline, "target_path": str(target),
        "update_mode": "MANUAL", "state": "active", "owner": owner,
    })
    receipts = UpdateReceipts(store, lambda: dict(owner))
    reviewer = UpdateReviewer(client, store, receipts, host, lambda: dict(owner))
    plan = reviewer.plan("skill-1")
    value = UpdateExecutor(client, store, receipts, reviewer, host, lambda: dict(owner))
    return value, store, client, receipts, plan, target


@pytest.mark.parametrize("interrupted_phase", ["old_preserved", "files_committed"])
def test_real_reviewer_recovers_pre_ledger_crash_windows(
    real_executor, monkeypatch, interrupted_phase
):
    value, store, _client, _receipts, plan, target = real_executor
    advance = store.advance
    failed = False

    def fail_once(operation_id, phase, *, done=False):
        nonlocal failed
        if phase == interrupted_phase and not failed:
            failed = True
            raise OSError(f"interrupted before {phase} acknowledgement")
        advance(operation_id, phase, done=done)

    monkeypatch.setattr(store, "advance", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        value.apply(plan["receipt"])
    monkeypatch.setattr(store, "advance", advance)

    assert value.apply(plan["receipt"])["updated"] is True
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"


def test_real_reviewer_terminal_authority_loss_during_pending_retry(real_executor):
    value, store, client, _receipts, plan, target = real_executor
    client.record_error = "before"
    assert value.apply(plan["receipt"])["pending"] is True
    client.remote = None

    result = value.apply(plan["receipt"])

    assert result["rejected"] is True
    assert not target.exists()
    assert store.installation("skill-1")["state"] == "inactive"


@pytest.mark.parametrize("latest_version", [2, 3])
def test_real_reviewer_accepts_policy_clamped_commit_after_lost_response(
    real_executor, latest_version
):
    value, store, client, _receipts, plan, _target = real_executor
    client.committed_mode = "REQUIRED"
    client.record_error = "after"
    assert value.apply(plan["receipt"])["pending"] is True
    client.record_error = None
    client.remote["latest_version"] = latest_version
    client.content = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a reconciled remote commit must not refetch package bytes")
    )

    result = value.apply(plan["receipt"])

    assert result["updated"] is True
    assert result["effective_update_mode"] == "REQUIRED"
    assert store.installation("skill-1")["update_mode"] == "REQUIRED"
    assert len(client.record_calls) == 1


def test_recovery_rejects_journal_redirected_to_unmanaged_skill(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = RuntimeError("offline")
    assert value.apply(plan["receipt"])["pending"] is True
    row = store.pending_operations()[0]
    unrelated = value.host.skills_root / "user-owned"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_bytes(V2)
    payload = json.loads(row["payload_json"])
    payload["target_path"] = str(unrelated)
    store.replace_operation_payload(row["id"], payload)
    client.error = None
    calls = len(client.record_calls)

    with pytest.raises(PackagePolicyError, match="managed.*target"):
        value.apply(plan["receipt"])

    assert (unrelated / "SKILL.md").read_bytes() == V2
    assert store.installation("skill-1")["target_path"] == str(target)
    assert len(client.record_calls) == calls


@pytest.mark.parametrize("persisted_before_loss", [False, True])
def test_final_completion_ack_loss_finishes_journal_without_reapplying(
    real_executor, monkeypatch, persisted_before_loss
):
    value, store, client, receipts, plan, _target = real_executor
    complete = receipts.complete
    failed = False

    def lose_once(receipt, result):
        nonlocal failed
        if not failed:
            failed = True
            if persisted_before_loss:
                complete(receipt, result)
            raise OSError("final completion acknowledgement lost")
        complete(receipt, result)

    monkeypatch.setattr(receipts, "complete", lose_once)
    with pytest.raises(OSError, match="acknowledgement lost"):
        value.apply(plan["receipt"])
    assert store.pending_operations()[0]["phase"] == "gateway_recorded"
    client.remote = None  # Final local acknowledgement must not re-run authority or mutation.

    result = value.apply(plan["receipt"])

    assert result["updated"] is True
    assert store.pending_operations() == []
    assert len(client.record_calls) == 1


def test_native_recovery_reconciles_real_executor_after_post_apply_inspection_loss(
    real_executor,
):
    value, store, client, _receipts, _plan, target = real_executor
    owner = store.installation("skill-1")["owner"]

    class Service:
        fail_inspection = True

        def native_owner(self):
            return dict(owner)

        def update_plan(self, skill_id, **choices):
            return value.reviewer.plan(skill_id, **choices)

        def update_apply(self, receipt):
            return value.apply(receipt)

        def installed_setup_inspect(self, skill_id, *, version=None):
            assert skill_id == "skill-1" and version == 2
            if self.fail_inspection:
                raise OSError("post-apply inspection response lost")
            return {
                "skill_id": skill_id, "version": version,
                "content_hash": client._content(client.files[2]).content_hash,
                "state": "setup_required", "ready_to_use": None,
            }

    service = Service()
    invocation = NativeInvocation(
        platform="slack", profile="default", chat_type="dm", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="T1", scope_id="W1",
        message_id="request-1", session_key="slack:W1:D1:U1", adapter=object(),
    )
    controller = NativeReviewController(store)
    record = controller.request(service, invocation, "skill-1")
    controller.bind_delivery(record["id"], invocation, "card-1")
    callback = replace(invocation, message_id="card-1")

    with pytest.raises(OSError, match="inspection response lost"):
        controller.act(service, record["id"], callback, "confirm")
    service.fail_inspection = False
    completed = NativeReviewController(WisdomStore(store.root)).act(
        service, record["id"], callback, "recover"
    )

    assert completed["state"] == "completed"
    assert len(client.record_calls) == 1
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"


def test_native_pending_gateway_reconciliation_survives_restart_without_replacing_or_reposting(  # pylint: disable=too-many-locals
    real_executor,
):
    value, store, client, _receipts, _plan, target = real_executor
    owner = store.installation("skill-1")["owner"]

    class Service:
        inspections = 0

        def __init__(self, executor):
            self.executor = executor

        def native_owner(self):
            return dict(owner)

        def update_plan(self, skill_id, **choices):
            return self.executor.reviewer.plan(skill_id, **choices)

        def update_apply(self, receipt):
            return self.executor.apply(receipt)

        def installed_setup_inspect(self, *_args, **_kwargs):
            self.inspections += 1
            return {"ready_to_use": True}

    invocation = NativeInvocation(
        platform="slack", profile="default", chat_type="dm", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="T1", scope_id="W1",
        message_id="request-1", session_key="slack:W1:D1:U1", adapter=object(),
    )
    first_service = Service(value)
    now = [100.0]
    first = NativeReviewController(store, clock=lambda: now[0], ttl_seconds=5)
    record = first.request(first_service, invocation, "skill-1")
    first.bind_delivery(record["id"], invocation, "card-1")
    callback = replace(invocation, message_id="card-1")
    original_bytes = target.joinpath("SKILL.md").read_bytes()
    client.record_error = "after"

    pending = first.act(first_service, record["id"], callback, "confirm")

    assert pending["state"] == "needs_review"
    assert pending["result"]["pending"] is True
    assert pending["result"]["receipt"] == record["plan"]["receipt"]
    assert first_service.inspections == 0
    assert first.claim_handoff(first_service, invocation) is None
    target_inode = target.stat().st_ino
    operation = store.pending_operations()[0]
    recovery_path = Path(json.loads(operation["payload_json"])["recovery_path"])
    assert len(client.record_calls) == 1

    restarted_store = WisdomStore(store.root)
    restarted_receipts = UpdateReceipts(restarted_store, lambda: dict(owner))
    restarted_reviewer = UpdateReviewer(
        client, restarted_store, restarted_receipts, value.host, lambda: dict(owner)
    )
    restarted_executor = UpdateExecutor(
        client, restarted_store, restarted_receipts, restarted_reviewer,
        value.host, lambda: dict(owner),
    )
    restarted_service = Service(restarted_executor)
    client.record_error = None
    now[0] = 106.0
    completed = NativeReviewController(restarted_store, clock=lambda: now[0], ttl_seconds=5).act(
        restarted_service, record["id"], callback, "recover"
    )

    assert completed["state"] == "completed"
    assert completed["plan"]["receipt"] == record["plan"]["receipt"]
    assert restarted_service.inspections == 1
    assert target.stat().st_ino == target_inode
    assert recovery_path.joinpath("SKILL.md").read_bytes() == original_bytes
    assert len(client.record_calls) == 1
