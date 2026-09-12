"""Focused execution and crash-recovery tests for manual installation."""

# Test doubles intentionally expose the executor's protocol-shaped methods.
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=redefined-outer-name,too-few-public-methods,protected-access

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomNotFound
from hermes_wisdom_plugin import install_execution
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.store import WisdomStore


OWNER = {
    "profile_name": "default",
    "profile_home": "/profiles/default",
    "account_id": "account-1",
    "organization_id": "org/one",
    "base_url": "https://gateway.example",
    "installation_id": "install-1",
}


def _hash(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


FILES = (("SKILL.md", "file", b"# Installed\n"),)
BASELINE = {"SKILL.md": _hash(FILES[0][2])}
PLAN = {
    "receipt": "receipt-1",
    "skill_id": "skill-1",
    "slug": "managed-skill",
    "version": 2,
    "content_hash": "sha256:content",
    "takedown_generation": 3,
    "update_mode": "AUTO_WITH_NOTICE",
    "compatibility": {"outcome": "compatible"},
    "local_scan": {
        "allowed": True,
        "verdict": "allow",
        "summary": "clean",
        "findings": [],
    },
}


class Receipts:
    def __init__(self):
        self.started = {}
        self.completed = {}

    def load(self, receipt, allow_started=False):
        assert receipt == PLAN["receipt"]
        assert allow_started is True
        return dict(PLAN)

    def start(self, receipt, operation_id):
        self.started[receipt] = operation_id

    def outcome(self, receipt):
        return self.completed.get(receipt)

    def complete(self, receipt, result):
        self.completed[receipt] = result


class Store:
    def __init__(self):
        self.operations = {}
        self.records = {}
        self.counter = 0
        self.lock = None
        self.metadata = {}

    def acquire_operation_lock(self, entity_id):
        if self.lock:
            return None
        self.lock = f"lock:{entity_id}"
        return self.lock

    def assert_operation_lock(self, entity_id, token):
        assert token == self.lock == f"lock:{entity_id}"

    def release_operation_lock(self, _entity_id, token):
        if token == self.lock:
            self.lock = None

    def journal(self, kind, entity_id, phase, payload):
        self.counter += 1
        operation_id = f"operation-{self.counter}"
        self.operations[operation_id] = {
            "id": operation_id, "kind": kind, "entity_id": entity_id,
            "phase": phase, "state": "pending", "payload": dict(payload),
        }
        return operation_id

    def operation(self, operation_id):
        return self.operations.get(operation_id)

    def replace_operation_payload(self, operation_id, payload):
        self.operations[operation_id]["payload"] = dict(payload)

    def advance(self, operation_id, phase, *, done=False):
        self.operations[operation_id]["phase"] = phase
        self.operations[operation_id]["state"] = "done" if done else "pending"

    def pending_operations(self):
        return [item for item in self.operations.values() if item["state"] == "pending"]

    def record_install(self, values):
        self.records[values["skill_id"]] = dict(values)

    def installation(self, skill_id):
        return self.records.get(skill_id)

    def installations(self, owner=None):
        return [
            value
            for value in self.records.values()
            if owner is None or value["owner"] == owner
        ]

    def deactivate_install(self, skill_id):
        self.records[skill_id]["state"] = "inactive"

    def get_metadata(self, key):
        return self.metadata.get(key)

    def set_metadata(self, key, value):
        self.metadata[key] = dict(value)


class Client:
    def __init__(self):
        self.record_error = None
        self.deactivate_error = None
        self.record_calls = []
        self.deactivate_calls = []

    def installations(self, _installation_id):
        return [{
            "skill_id": "skill-1",
            "installed_version": 2,
            "latest_version": 3,
            "update_mode": "REQUIRED",
            "skill_state": "active",
            "takedown_generation": 3,
        }]

    def record_install(self, **values):
        self.record_calls.append(values)
        if self.record_error:
            raise self.record_error
        return SimpleNamespace(effective_update_mode="REQUIRED")

    def deactivate_install(self, installation_id, skill_id):
        self.deactivate_calls.append((installation_id, skill_id))
        if self.deactivate_error:
            raise self.deactivate_error
        return SimpleNamespace(state="inactive")


class Host:
    def __init__(self, root):
        self.skills_root = root

    def validate_target(self, path):
        assert Path(path).is_relative_to(self.skills_root)

    def scan(self, directory):
        assert Path(directory).is_relative_to(self.skills_root)
        return {"allowed": True, "verdict": "allow", "summary": "clean", "findings": []}


class Reviewer:
    def verify(self, plan):
        return SimpleNamespace(plan=dict(plan), files=FILES, baseline=BASELINE)


@pytest.fixture
def executor(tmp_path):
    store = Store()
    client = Client()
    receipts = Receipts()
    current = {"value": dict(OWNER)}
    value = InstallExecutor(
        client,
        store,
        receipts,
        Reviewer(),
        Host(tmp_path / "skills"),
        lambda: dict(current["value"]),
    )
    return value, store, client, receipts, current


def test_apply_commits_exact_tree_and_preserves_server_mode(executor):
    value, store, client, receipts, _current = executor
    result = value.apply("receipt-1")

    target = Path(result["path"])
    assert (target / "SKILL.md").read_bytes() == FILES[0][2]
    assert result["effective_update_mode"] == "REQUIRED"
    assert result["setup_required"] is False
    assert store.records["skill-1"]["owner"] == OWNER
    assert receipts.completed["receipt-1"] == result
    assert client.record_calls[0]["installation_id"] == "install-1"


def test_retry_after_rename_wins_verifies_target_and_finishes(executor, monkeypatch):
    value, store, _client, _receipts, _current = executor
    real_advance = store.advance
    failed = False

    def fail_once(operation_id, phase, *, done=False):
        nonlocal failed
        if phase == "files_committed" and not failed:
            failed = True
            raise OSError("journal unavailable")
        real_advance(operation_id, phase, done=done)

    monkeypatch.setattr(store, "advance", fail_once)
    with pytest.raises(OSError, match="journal"):
        value.apply("receipt-1")
    monkeypatch.setattr(store, "advance", real_advance)

    assert value.apply("receipt-1")["installed"] is True


def test_temporary_gateway_error_returns_pending_then_retries_only_gateway(executor):
    value, store, client, _receipts, _current = executor
    client.record_error = RuntimeError("gateway unavailable")
    pending = value.apply("receipt-1")
    target = Path(store.records["skill-1"]["target_path"])

    assert pending["pending"] is True
    assert pending["phase"] == "local_ledger_committed"
    assert target.is_dir()
    client.record_error = None
    result = value.apply("receipt-1")
    assert result["installed"] is True
    assert len(client.record_calls) == 2


def test_permanent_gateway_rejection_quarantines_active_bytes(executor):
    value, store, client, _receipts, _current = executor
    client.record_error = WisdomConflict("taken down", status=409)

    with pytest.raises(WisdomConflict, match="taken down"):
        value.apply("receipt-1")

    record = store.records["skill-1"]
    assert record["state"] == "inactive"
    assert not Path(record["target_path"]).exists()
    payload = next(iter(store.operations.values()))["payload"]
    assert (Path(payload["trash_path"]) / "SKILL.md").read_bytes() == FILES[0][2]
    assert value.apply("receipt-1") == payload["result"]
    assert len(client.record_calls) == 1


def test_permanent_rejection_archives_locally_modified_tree(executor):
    value, store, client, _receipts, _current = executor
    client.record_error = RuntimeError("offline")
    assert value.apply("receipt-1")["pending"] is True
    target = Path(store.records["skill-1"]["target_path"])
    (target / "SKILL.md").write_text("# locally modified\n", encoding="utf-8")
    client.record_error = WisdomConflict("taken down", status=409)

    with pytest.raises(WisdomConflict, match="taken down"):
        value.apply("receipt-1")

    payload = next(iter(store.operations.values()))["payload"]
    trash = Path(payload["trash_path"])
    assert not target.exists()
    assert (trash / "SKILL.md").read_text() == "# locally modified\n"


def test_rejected_receipt_completion_crash_recovers_without_reinstall(executor, monkeypatch):
    value, store, client, receipts, _current = executor
    client.record_error = WisdomConflict("taken down", status=409)
    real_complete = receipts.complete
    failed = False

    def fail_once(receipt, result):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("receipt completion interrupted")
        real_complete(receipt, result)

    monkeypatch.setattr(receipts, "complete", fail_once)
    with pytest.raises(OSError, match="receipt completion"):
        value.apply("receipt-1")
    operation = store.pending_operations()[0]
    assert operation["phase"] == "gateway_rejected"
    trash = Path(operation["payload"]["trash_path"])

    result = value.apply("receipt-1")

    assert result["rejected"] is True
    assert trash.is_dir()
    assert not Path(store.records["skill-1"]["target_path"]).exists()
    assert len(client.record_calls) == 1


def test_owner_change_before_effect_rejects_without_installing(executor):
    value, store, _client, _receipts, current = executor

    class SwitchingReviewer(Reviewer):
        def verify(self, plan):
            result = super().verify(plan)
            current["value"] = {**OWNER, "account_id": "account-2"}
            return result

    value.reviewer = SwitchingReviewer()
    with pytest.raises(PackagePolicyError, match="owner changed"):
        value.apply("receipt-1")
    assert store.records == {}


def test_foreign_owner_primary_key_collision_is_never_adopted(executor):
    value, store, _client, _receipts, _current = executor
    store.records["skill-1"] = {
        "skill_id": "skill-1",
        "state": "inactive",
        "owner": {**OWNER, "account_id": "other"},
    }
    with pytest.raises(PackagePolicyError, match="another installation owner"):
        value.apply("receipt-1")


def test_uninstall_preserves_modified_tree_and_retry_does_not_delete_recreated_target(executor):
    value, store, client, _receipts, _current = executor
    installed = value.apply("receipt-1")
    target = Path(installed["path"])
    (target / "SKILL.md").write_text("# locally edited\n", encoding="utf-8")
    client.deactivate_error = RuntimeError("offline")

    pending = value.uninstall("skill-1")
    operation = next(
        item for item in store.operations.values() if item["kind"] == "uninstall"
    )
    trash = Path(operation["payload"]["trash_path"])
    assert pending["pending"] is True
    assert (trash / "SKILL.md").read_text() == "# locally edited\n"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# recreated\n", encoding="utf-8")
    client.deactivate_error = None

    assert value.uninstall("skill-1")["uninstalled"] is True
    assert (target / "SKILL.md").read_text() == "# recreated\n"


def test_uninstall_treats_server_not_found_as_already_inactive(executor):
    value, store, client, _receipts, _current = executor
    value.apply("receipt-1")
    client.deactivate_error = WisdomNotFound("already absent", status=404)

    result = value.uninstall("skill-1")

    assert result["uninstalled"] is True
    assert result["state"] == "inactive"
    assert store.records["skill-1"]["state"] == "inactive"


def test_duplicate_completed_uninstall_replays_recorded_outcome(executor):
    value, store, client, _receipts, _current = executor
    value.apply("receipt-1")

    first = value.uninstall("skill-1")
    second = value.uninstall("skill-1")

    assert second == first
    assert len(client.deactivate_calls) == 1
    assert len([item for item in store.operations.values() if item["kind"] == "uninstall"]) == 1
    assert value.installed() == []


def test_installed_includes_owner_pending_before_local_ledger(executor, monkeypatch):
    value, _store, _client, _receipts, _current = executor
    monkeypatch.setattr(
        install_execution.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("promotion interrupted")),
    )
    with pytest.raises(OSError, match="promotion"):
        value.apply("receipt-1")

    installed = value.installed()

    assert len(installed) == 1
    assert installed[0]["state"] == "pending"
    assert installed[0]["pending_operation"] == {"kind": "install", "phase": "staged"}


def test_partial_install_requires_explicit_acceptance_and_claims_no_readiness(executor):
    value, _store, _client, _receipts, _current = executor
    original = dict(PLAN["compatibility"])
    PLAN["compatibility"] = {"outcome": "compatible_after_setup"}
    try:
        with pytest.raises(PackagePolicyError, match="accept-partial"):
            value.apply("receipt-1")
        result = value.apply("receipt-1", accept_partial=True)
        assert result["setup_required"] is True
        assert "ready" not in result
    finally:
        PLAN["compatibility"] = original


def test_completed_receipt_replays_outcome_without_starting_again(executor):
    value, store, client, receipts, _current = executor
    first = value.apply("receipt-1")
    started = dict(receipts.started)

    assert value.apply("receipt-1") == first
    assert receipts.started == started
    assert len(client.record_calls) == 1
    assert len(store.operations) == 1


def test_unknown_scan_requires_acknowledgement_in_reviewed_plan(executor):
    value, store, _client, _receipts, _current = executor
    value.host.scan = lambda *_args, **_kwargs: {
        "allowed": None, "verdict": "unknown", "summary": "scanner unavailable", "findings": []
    }
    with pytest.raises(PackagePolicyError, match="changed after review"):
        value.apply("receipt-1", accept_partial=True)
    assert store.records == {}


def test_unknown_scan_with_review_ack_can_install(executor):
    value, _store, _client, _receipts, _current = executor
    value.host.scan = lambda *_args, **_kwargs: {
        "allowed": None, "verdict": "unknown", "summary": "scanner unavailable", "findings": []
    }

    original = PLAN["local_scan"]
    PLAN["local_scan"] = {
        "allowed": None,
        "verdict": "unknown",
        "summary": "scanner unavailable",
        "findings": [],
    }
    try:
        assert value.apply("receipt-1", accept_partial=True)["installed"] is True
    finally:
        PLAN["local_scan"] = original


def test_real_store_and_receipt_complete_one_owner_bound_install(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    receipts = InstallReceipts(store, lambda: dict(owner))
    plan = receipts.create({key: value for key, value in PLAN.items() if key != "receipt"})
    client = Client()
    value = InstallExecutor(
        client,
        store,
        receipts,
        Reviewer(),
        Host(tmp_path / "skills"),
        lambda: dict(owner),
    )

    result = value.apply(plan["receipt"])

    assert receipts.outcome(plan["receipt"]) == result
    installed = value.installed()
    assert len(installed) == 1
    assert installed[0]["skill_id"] == "skill-1"
    assert installed[0]["latest_version"] == 3
    assert installed[0]["skill_state"] == "active"
    assert installed[0]["effective_update_mode"] == "REQUIRED"
    assert installed[0]["modified"] is False
    assert installed[0]["pending_operation"] is None
    assert installed[0]["setup_execution_authorized"] is False
    assert installed[0]["ready_to_use"] is None
    assert store.pending_operations() == []
    assert store.operation(receipts.store.get_metadata(
        "install_receipt:" + plan["receipt"]
    )["operation_id"])["phase"] == "gateway_recorded"


def test_pending_install_must_match_exact_receipt(executor):
    value, _store, client, receipts, _current = executor
    client.record_error = RuntimeError("offline")
    assert value.apply("receipt-1")["pending"] is True
    second = {**PLAN, "receipt": "receipt-2"}
    receipts.load = lambda receipt, allow_started=False: dict(
        second if receipt == "receipt-2" else PLAN
    )

    with pytest.raises(PackagePolicyError, match="another install receipt"):
        value.apply("receipt-2")


def test_pending_install_blocks_uninstall_for_same_skill(executor):
    value, _store, client, _receipts, _current = executor
    client.record_error = RuntimeError("offline")
    assert value.apply("receipt-1")["pending"] is True

    with pytest.raises(PackagePolicyError, match="another managed operation"):
        value.uninstall("skill-1")


def test_exact_staging_path_is_bound_to_operation_id(executor):
    value, store, client, _receipts, _current = executor
    client.record_error = RuntimeError("offline")
    assert value.apply("receipt-1")["pending"] is True
    operation = store.pending_operations()[0]
    operation["phase"] = "staged"
    operation["payload"]["staging_path"] = str(
        Path(value.host.skills_root) / ".hub" / "collective-wisdom" / "other-operation"
    )

    with pytest.raises(PackagePolicyError, match="staging path"):
        value.apply("receipt-1")


def test_staged_retry_rechecks_new_partial_compatibility(executor, monkeypatch):
    value, _store, _client, _receipts, _current = executor
    real_replace = install_execution.os.replace
    monkeypatch.setattr(
        install_execution.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("promotion interrupted")),
    )
    with pytest.raises(OSError, match="promotion"):
        value.apply("receipt-1")
    monkeypatch.setattr(install_execution.os, "replace", real_replace)

    class NewlyPartial(Reviewer):
        def verify(self, plan):
            result = super().verify(plan)
            result.plan["compatibility"] = {"outcome": "partial"}
            return result

    value.reviewer = NewlyPartial()
    with pytest.raises(PackagePolicyError, match="accept-partial"):
        value.apply("receipt-1")


def test_staging_write_interruption_is_reconstructed_on_retry(executor, monkeypatch):
    value, _store, _client, _receipts, _current = executor
    real_write = install_execution._write_staging
    failed = False

    def fail_once(staging, files):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("write interrupted")
        real_write(staging, files)

    monkeypatch.setattr(install_execution, "_write_staging", fail_once)
    with pytest.raises(OSError, match="write interrupted"):
        value.apply("receipt-1")

    assert value.apply("receipt-1")["installed"] is True


def test_gateway_recorded_crash_replays_payload_without_second_post(executor, monkeypatch):
    value, store, client, receipts, _current = executor
    real_complete = receipts.complete
    failed = False

    def fail_once(receipt, result):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("receipt commit interrupted")
        real_complete(receipt, result)

    monkeypatch.setattr(receipts, "complete", fail_once)
    with pytest.raises(OSError, match="receipt commit"):
        value.apply("receipt-1")
    assert store.pending_operations()[0]["phase"] == "gateway_recorded"

    assert value.apply("receipt-1")["installed"] is True
    assert len(client.record_calls) == 1


def test_auth_failure_is_never_returned_as_pending(executor):
    value, _store, client, _receipts, _current = executor
    client.record_error = WisdomAuthError("reauthentication required")

    with pytest.raises(WisdomAuthError, match="reauthentication"):
        value.apply("receipt-1")


def test_pending_error_redacts_unexpected_exception(executor):
    value, _store, client, _receipts, _current = executor
    client.record_error = RuntimeError("secret transport detail")

    result = value.apply("receipt-1")

    assert result["pending"] is True
    assert "secret" not in result["error"]
    assert result["files_installed"] is True
    assert result["local_state"] == "active"


def test_staging_parent_symlink_is_rejected(executor, tmp_path):
    value, _store, _client, _receipts, _current = executor
    outside = tmp_path / "outside"
    outside.mkdir()
    hub = Path(value.host.skills_root) / ".hub"
    hub.parent.mkdir(parents=True)
    hub.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackagePolicyError, match="symlink"):
        value.apply("receipt-1")


def test_installed_rechecks_owner_after_gateway_read(executor):
    value, _store, client, _receipts, current = executor
    value.apply("receipt-1")
    real_installations = client.installations

    def switch_owner(installation_id):
        result = real_installations(installation_id)
        current["value"] = {**OWNER, "account_id": "account-2"}
        return result

    client.installations = switch_owner
    with pytest.raises(PackagePolicyError, match="owner changed"):
        value.installed()
