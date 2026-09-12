"""Focused execution and crash-recovery tests for manual installation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.errors import WisdomConflict, WisdomNotFound
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


class Client:
    def __init__(self):
        self.record_error = None
        self.deactivate_error = None
        self.record_calls = []
        self.deactivate_calls = []

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

    def scan(self, directory, *, source, force):
        assert ".hub/collective-wisdom" in Path(directory).as_posix()
        assert source == "community"
        assert force is False
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
    with pytest.raises(PackagePolicyError, match="did not allow"):
        value.apply("receipt-1")
    assert store.records == {}


def test_unknown_scan_with_review_ack_can_install(executor):
    value, _store, _client, _receipts, _current = executor
    value.host.scan = lambda *_args, **_kwargs: {
        "allowed": None, "verdict": "unknown", "summary": "scanner unavailable", "findings": []
    }

    class AcknowledgedReviewer(Reviewer):
        def verify(self, plan):
            result = super().verify(plan)
            result.plan["scan_review_acknowledged"] = True
            return result

    value.reviewer = AcknowledgedReviewer()
    assert value.apply("receipt-1")["installed"] is True


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
    assert value.installed() == [store.installation("skill-1", owner=owner)]
    assert store.pending_operations() == []
    assert store.operation(receipts.store.get_metadata(
        "install_receipt:" + plan["receipt"]
    )["operation_id"])["phase"] == "gateway_recorded"
