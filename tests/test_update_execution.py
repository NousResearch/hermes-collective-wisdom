"""Behavioral tests for direct reviewed update execution and recovery."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name
# pylint: disable=too-few-public-methods,wrong-import-order,protected-access
# pylint: disable=import-outside-toplevel,too-many-locals

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from installed_test_support import installed_store
from review_test_support import _content

from hermes_wisdom_plugin.contract import org_directory_name, sha256_address
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict
from hermes_wisdom_plugin import package_application
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import (
    UpdateAuthorityRejected, UpdateReviewer, VerifiedUpdate,
)
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
        self.scan_result = {
            "available": True, "allowed": True, "verdict": "allow",
            "summary": "clean", "findings": [],
        }

    def validate_target(self, path):
        assert Path(path).is_relative_to(self.skills_root)

    def scan(self, path):
        assert Path(path).is_relative_to(self.skills_root)
        return dict(self.scan_result)


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
    store = installed_store(tmp_path / "state")
    skills = tmp_path / "profile" / "skills"
    target = skills / "_wisdom" / org_directory_name(owner["organization_id"]) / "managed-skill"
    target.mkdir(parents=True)
    target.joinpath("SKILL.md").write_bytes(V1)
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
        "baseline": {"SKILL.md": _hash(V2)}, "previous_content_hash": "sha256:old",
        "previous_baseline": {"SKILL.md": _hash(V1)},
        "previous_installation": previous, "target_path": str(target),
        "local_snapshot": {"SKILL.md": _hash(V1)},
        "takedown_generation": 7, "update_mode": "MANUAL",
        "authority": {
            "installed_version": 1, "latest_version": 2,
            "takedown_generation": 7, "update_mode": "MANUAL", "skill_state": "active",
        },
        "preservation": {"action": "none", "destination": None},
        "compatibility": {"outcome": "compatible"},
        "local_scan": dict(Host(skills).scan_result), "allowed": True,
        "permitted_actions": ["confirm_update", "recheck"],
    })
    client = Client()
    value = UpdateExecutor(client, store, receipts, Reviewer(), Host(skills), lambda: dict(owner))
    return value, store, client, receipts, plan, target, owner


def test_manual_success_commits_exact_bytes_and_complete_result(executor):
    value, store, client, receipts, plan, target, _owner = executor

    result = value.apply(plan["receipt"])

    assert result["updated"] is True and result["version"] == 2
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert Path(result["recovery_path"]).joinpath("SKILL.md").read_bytes() == V1
    assert store.installation("skill-1")["version"] == 2
    assert receipts.outcome(plan["receipt"]) == result
    review = store.review("update_receipt:" + plan["receipt"])
    operation = store.operation(review["operation_id"])
    assert operation["status"] == "done" and operation["stage"] == "complete"
    assert operation["result"] == result
    assert len(client.record_calls) == 1


def test_changed_scan_before_swap_preserves_original(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    calls = 0

    def changed(_path):
        nonlocal calls
        calls += 1
        result = dict(value.host.scan_result)
        result["verdict"] = "allow-with-advisory"
        return result

    value.host.scan = changed
    with pytest.raises(PackagePolicyError, match="scan changed"):
        value.apply(plan["receipt"])

    assert calls == 1
    assert target.joinpath("SKILL.md").read_bytes() == V1
    assert store.installation("skill-1")["version"] == 1
    assert client.record_calls == []


def test_modified_update_preserves_exact_reviewed_bytes(executor):
    value, _store, _client, receipts, plan, target, _owner = executor
    edited = b"# precious raccoon edits\n"
    target.joinpath("SKILL.md").write_bytes(edited)
    destination = value.host.skills_root / "managed-skill-local-before-v2-fixed"
    payload = {
        key: val for key, val in plan.items()
        if key not in {"receipt", "operation", "owner", "created_at", "expires_at"}
    }
    reviewed = receipts.create({
        **payload, "local_snapshot": {"SKILL.md": _hash(edited)}, "modified": True,
        "preservation": {"action": "preserve_unmanaged_copy", "destination": str(destination)},
    })

    first = value.apply(reviewed["receipt"])
    second = value.apply(reviewed["receipt"])

    assert destination.joinpath("SKILL.md").read_bytes() == edited
    assert Path(first["recovery_path"]).joinpath("SKILL.md").read_bytes() == edited
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert second == first


@pytest.mark.parametrize("boundary", ["old", "new"])
def test_restart_recovers_when_rename_wins_before_checkpoint(executor, monkeypatch, boundary):
    value, _store, client, _receipts, plan, target, _owner = executor
    replace = package_application.durable_replace
    interrupted = False

    def interrupt(source, destination):
        nonlocal interrupted
        replace(source, destination)
        selected = destination.name == (
            "previous-managed" if boundary == "old" else "managed-skill"
        )
        if selected and not interrupted:
            interrupted = True
            raise OSError("checkpoint acknowledgement lost")

    monkeypatch.setattr(package_application, "durable_replace", interrupt)
    with pytest.raises(OSError, match="acknowledgement lost"):
        value.apply(plan["receipt"])
    monkeypatch.setattr(package_application, "durable_replace", replace)

    result = value.apply(plan["receipt"])
    assert result["updated"] is True
    assert target.joinpath("SKILL.md").read_bytes() == V2
    assert len(client.record_calls) == 1


def test_partial_staging_is_rebuilt_from_reverified_bytes(executor, monkeypatch):
    from hermes_wisdom_plugin import update_execution

    value, store, _client, _receipts, plan, target, _owner = executor
    write_staging = update_execution._write_staging

    def partial(path, _files, _baseline):
        path.mkdir(parents=True)
        path.joinpath("SKILL.md").write_bytes(b"partial")
        raise OSError("staging interrupted")

    monkeypatch.setattr(update_execution, "_write_staging", partial)
    with pytest.raises(OSError, match="staging interrupted"):
        value.apply(plan["receipt"])
    assert store.pending_operations()[0]["stage"] == "prepared"
    assert target.joinpath("SKILL.md").read_bytes() == V1

    monkeypatch.setattr(update_execution, "_write_staging", write_staging)
    assert value.apply(plan["receipt"])["updated"] is True


def test_transient_gateway_outcome_is_pending_then_reconciles(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = RuntimeError("offline secret")

    pending = value.apply(plan["receipt"])

    assert pending["pending"] is True and "secret" not in pending["error"]
    assert pending["phase"] == "local_committed"
    assert store.pending_operations()[0]["stage"] == "local_committed"
    assert target.joinpath("SKILL.md").read_bytes() == V2

    client.error = None
    assert value.apply(plan["receipt"])["updated"] is True
    assert len(client.record_calls) == 2


def test_lost_gateway_success_reconciles_without_second_post(executor):
    value, _store, client, _receipts, plan, _target, _owner = executor

    def applied_then_lost(**values):
        client.record_calls.append(values)
        client.remote_version = 2
        raise RuntimeError("response lost")

    client.record_install = applied_then_lost
    assert value.apply(plan["receipt"])["pending"] is True
    client.record_install = lambda **_values: (_ for _ in ()).throw(
        AssertionError("must not repost")
    )

    assert value.apply(plan["receipt"])["updated"] is True
    assert len(client.record_calls) == 1


def test_terminal_gateway_rejection_quarantines_and_deactivates(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    client.error = WisdomConflict(
        "skill_taken_down", status=409, code="skill_taken_down"
    )

    with pytest.raises(WisdomConflict, match="skill_taken_down"):
        value.apply(plan["receipt"])

    review = store.review("update_receipt:" + plan["receipt"])
    operation = store.operation(review["operation_id"])
    assert operation["status"] == "done" and operation["stage"] == "complete"
    assert operation["result"]["rejected"] is True
    assert not target.exists()
    assert Path(operation["result"]["quarantine_path"]).joinpath("SKILL.md").read_bytes() == V2
    assert store.installation("skill-1")["state"] == "inactive"


def test_owner_change_denies_before_update_effect(executor):
    value, store, client, _receipts, plan, target, _owner = executor
    original_owner = value.owner
    value.owner = lambda: {**original_owner(), "account_id": "other"}

    with pytest.raises(WisdomAuthError):
        value.apply(plan["receipt"])

    assert target.joinpath("SKILL.md").read_bytes() == V1
    assert store.pending_operations() == []
    assert client.record_calls == []


def test_completion_ack_loss_finishes_without_reapplying(executor, monkeypatch):
    value, store, client, receipts, plan, _target, _owner = executor
    complete = receipts.complete
    interrupted = False

    def lose_once(receipt, result):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("completion acknowledgement lost")
        return complete(receipt, result)

    monkeypatch.setattr(receipts, "complete", lose_once)
    with pytest.raises(OSError, match="acknowledgement lost"):
        value.apply(plan["receipt"])
    assert store.pending_operations()[0]["stage"] == "outcome_recorded"

    result = value.apply(plan["receipt"])
    assert result["updated"] is True
    assert store.pending_operations() == []
    assert len(client.record_calls) == 1


class RealClient(ReviewClient):
    """Package and authority transport for real reviewer paths."""

    def __init__(self):
        super().__init__()
        self.record_calls = []
        self.record_error: str | None = None
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
    store = installed_store(tmp_path / "state")
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
        "content_hash": _content(client.files[1]).content_hash,
        "baseline": baseline, "target_path": str(target),
        "update_mode": "MANUAL", "state": "active", "owner": owner,
    })
    receipts = UpdateReceipts(store, lambda: dict(owner))
    reviewer = UpdateReviewer(client, store, receipts, host, lambda: dict(owner))
    plan = reviewer.plan("skill-1")
    executor_value = UpdateExecutor(
        client, store, receipts, reviewer, host, lambda: dict(owner)
    )
    return executor_value, store, client, receipts, plan, target


def test_real_reviewer_denies_changed_local_bytes(real_executor):
    value, store, client, _receipts, plan, target = real_executor
    target.joinpath("SKILL.md").write_bytes(b"changed after review")

    with pytest.raises(PackagePolicyError, match="local bytes changed"):
        value.apply(plan["receipt"])

    assert store.pending_operations() == []
    assert client.record_calls == []


def test_real_reviewer_recovers_lost_remote_response_with_clamped_policy(real_executor):
    value, store, client, _receipts, plan, _target = real_executor
    client.committed_mode = "REQUIRED"
    client.record_error = "after"
    assert value.apply(plan["receipt"])["pending"] is True
    client.record_error = None
    client.content = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("committed update must not refetch bytes")
    )

    result = value.apply(plan["receipt"])

    assert result["updated"] is True
    assert result["effective_update_mode"] == "REQUIRED"
    assert store.installation("skill-1")["update_mode"] == "REQUIRED"
    assert len(client.record_calls) == 1


def test_real_reviewer_authority_loss_quarantines_pending_update(real_executor):
    value, store, client, _receipts, plan, target = real_executor
    client.record_error = "before"
    assert value.apply(plan["receipt"])["pending"] is True
    client.remote = None

    result = value.apply(plan["receipt"])

    assert result["rejected"] is True
    assert not target.exists()
    assert store.installation("skill-1")["state"] == "inactive"


def test_publisher_origin_survives_apply_and_authorizes_next_review(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = installed_store(tmp_path / "state")
    host = ReviewHost(tmp_path / "profile")
    client = RealClient()
    target = host.skills_root / "authored" / "managed-skill"
    target.mkdir(parents=True)
    for name, _mode, body in client.files[1]:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    baseline = {name: _hash(body) for name, _mode, body in client.files[1]}
    manifest = next(body for name, _mode, body in client.files[1]
                    if name == "skill.manifest.json")
    origin = {
        "kind": "publisher_source", "owner": owner, "source_path": str(target),
        "source_hash": "sha256:" + "a" * 64,
        "publication": {
            "operation_id": "publication-1", "remote_draft_id": "draft-1",
            "proposal_id": 1, "skill_id": "skill-1", "version": 1,
            "content_hash": _content(client.files[1]).content_hash,
            "commit": "sha256:" + "c" * 64,
            "manifest_hash": sha256_address(manifest),
        },
    }
    store.record_install({
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "managed-skill", "version": 1,
        "content_hash": _content(client.files[1]).content_hash,
        "manifest_hash": sha256_address(manifest), "baseline": baseline,
        "target_path": str(target), "target_provenance": origin,
        "update_mode": "MANUAL", "state": "active", "owner": owner,
    })
    receipts = UpdateReceipts(store, lambda: dict(owner))
    reviewer = UpdateReviewer(client, store, receipts, host, lambda: dict(owner))
    executor_value = UpdateExecutor(
        client, store, receipts, reviewer, host, lambda: dict(owner)
    )

    v2_plan = reviewer.plan("skill-1")
    client.record_error = "after"
    pending = executor_value.apply(v2_plan["receipt"])
    assert pending["pending"] is True
    client.record_error = None
    assert executor_value.apply(v2_plan["receipt"])["updated"] is True
    assert len(client.record_calls) == 1
    installed = store.installation("skill-1", owner=owner)
    assert installed is not None
    assert installed["target_path"] == str(target)
    assert installed["target_provenance"] == origin
    assert installed["target_provenance"]["publication"]["version"] == 1
    assert installed["version"] == 2

    client.files[3] = client._version_two()
    client.files[3][0] = ("SKILL.md", "file", b"# Managed v3\n")
    client.remote["latest_version"] = 3
    v3_plan = reviewer.plan("skill-1")
    assert v3_plan["from_version"] == 2 and v3_plan["version"] == 3
    assert v3_plan["target_path"] == str(target)
    assert v3_plan["target_provenance"] == origin


def test_retry_binds_existing_operation_before_files_or_gateway(
    real_executor, monkeypatch,
):
    value, store, client, receipts, plan, target = real_executor
    start = receipts.start

    def lose_binding(*_args, **_kwargs):
        raise OSError("binding interrupted")

    monkeypatch.setattr(receipts, "start", lose_binding)
    with pytest.raises(OSError, match="binding interrupted"):
        value.apply(plan["receipt"])
    operation = store.pending_operations()[0]
    operation_id = operation["id"]
    assert not {
        "staging_path", "recovery_path", "quarantine_path"
    }.intersection(operation["payload"])
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed\n"
    assert client.record_calls == []

    monkeypatch.setattr(receipts, "start", start)
    result = value.apply(plan["receipt"])

    review = store.review("update_receipt:" + plan["receipt"])
    assert review["operation_id"] == operation_id
    assert result["updated"] is True
    assert store.operation(operation_id)["result"] == result
    assert len(client.record_calls) == 1


def test_retry_ignores_untrusted_persisted_derived_path(
    real_executor, monkeypatch, tmp_path,
):
    value, store, client, receipts, plan, target = real_executor

    def lose_binding(*_args, **_kwargs):
        raise OSError("binding interrupted")

    monkeypatch.setattr(receipts, "start", lose_binding)
    with pytest.raises(OSError, match="binding interrupted"):
        value.apply(plan["receipt"])
    operation = store.pending_operations()[0]
    payload = dict(operation["payload"])
    payload["staging_path"] = str(tmp_path / "redirected")
    store.checkpoint(operation["id"], payload)
    monkeypatch.undo()

    result = value.apply(plan["receipt"])

    assert result["updated"] is True
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"
    assert not (tmp_path / "redirected").exists()
    assert len(client.record_calls) == 1


def test_retry_rescans_actual_staging_before_promotion(
    real_executor, monkeypatch,
):
    value, store, client, _receipts, plan, target = real_executor
    replace = package_application.durable_replace
    interrupted = False

    def lose_old_rename(source, destination):
        nonlocal interrupted
        replace(source, destination)
        if destination.name == "previous-managed" and not interrupted:
            interrupted = True
            raise OSError("old-tree rename interrupted")

    monkeypatch.setattr(package_application, "durable_replace", lose_old_rename)
    with pytest.raises(OSError, match="old-tree rename"):
        value.apply(plan["receipt"])
    operation = store.pending_operations()[0]
    paths = value._paths(operation["id"], operation["payload"])
    staging = paths["staging_path"]
    recovery = paths["recovery_path"]
    scans = []
    original_scan = value.host.scan

    def deny_actual_staging(path):
        if Path(path) == staging:
            scans.append(Path(path))
            return {
                "available": True, "allowed": False, "verdict": "block",
                "summary": "new denial", "findings": [],
            }
        return original_scan(path)

    monkeypatch.setattr(package_application, "durable_replace", replace)
    monkeypatch.setattr(value.host, "scan", deny_actual_staging)
    with pytest.raises(PackagePolicyError, match="scan changed"):
        value.apply(plan["receipt"])

    assert scans == [staging]
    assert not target.exists()
    assert recovery.joinpath("SKILL.md").read_bytes() == b"# Managed\n"
    assert client.record_calls == []
