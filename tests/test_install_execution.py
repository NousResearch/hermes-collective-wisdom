"""Focused execution and crash-recovery tests for manual installation."""

# Test doubles intentionally expose the executor's protocol-shaped methods.
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=redefined-outer-name,too-few-public-methods,protected-access
# pylint: disable=import-outside-toplevel,wrong-import-order,too-many-lines,duplicate-code

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from review_test_support import _content

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomNotFound
from hermes_wisdom_plugin import install_execution
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.install_review import InstallReviewer
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.store import WisdomStore
from test_install_review import Client as ReviewClient, Host as ReviewHost


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


FILES = (
    ("SKILL.md", "file", b"# Installed\n"),
    ("scripts/sentinel.sh", "exec", b"#!/bin/sh\ntouch SHOULD_NOT_EXIST\n"),
)
BASELINE = {
    path: (f"exec:{_hash(body)}" if mode == "exec" else _hash(body))
    for path, mode, body in FILES
}
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
        "available": True,
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
    def __init__(self, root):
        self.root = root
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


def test_apply_rejects_pending_setup_even_if_database_lease_is_free(executor):
    value, store, _client, _receipts, _current = executor
    store.journal("wisdom_setup", "skill-1", "running", {"owner": OWNER})

    with pytest.raises(PackagePolicyError, match="setup operation"):
        value.apply("receipt-1")


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
        self.scan_result = {
            "available": True,
            "allowed": True,
            "verdict": "allow",
            "summary": "clean",
            "findings": [],
        }

    def validate_target(self, path):
        assert Path(path).is_relative_to(self.skills_root)

    def scan(self, directory):
        assert Path(directory).is_relative_to(self.skills_root)
        return dict(self.scan_result)


class Reviewer:
    def verify(self, plan):
        return SimpleNamespace(plan=dict(plan), files=FILES, baseline=BASELINE)


@pytest.fixture
def executor(tmp_path):
    store = Store(tmp_path / "state")
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
    script = target / "scripts" / "sentinel.sh"
    assert script.read_bytes() == FILES[1][2]
    assert script.stat().st_mode & 0o777 == 0o700
    assert not (target / "SHOULD_NOT_EXIST").exists()
    assert result["effective_update_mode"] == "REQUIRED"
    assert result["setup_required"] is False
    assert store.records["skill-1"]["owner"] == OWNER
    assert receipts.completed["receipt-1"] == result
    assert client.record_calls[0]["installation_id"] == "install-1"


def test_apply_rejects_new_affirmative_scan_finding_at_activation(executor):
    value, store, client, _receipts, _current = executor
    calls = 0

    def changed_at_final_boundary(_directory):
        nonlocal calls
        calls += 1
        scan = dict(value.host.scan_result)
        if calls > 1:
            scan["findings"] = [{"kind": "new-warning", "file": "SKILL.md"}]
        return scan

    value.host.scan = changed_at_final_boundary

    with pytest.raises(PackagePolicyError, match="scan changed"):
        value.apply("receipt-1")

    assert store.records == {}
    assert client.record_calls == []


def test_install_recovery_rejects_changed_affirmative_scan_before_promotion(
    executor, monkeypatch
):
    value, store, client, _receipts, _current = executor
    original_same_filesystem = install_execution._same_filesystem
    failed = False

    def interrupt_once(source, destination_parent):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("promotion interrupted")
        return original_same_filesystem(source, destination_parent)

    monkeypatch.setattr(install_execution, "_same_filesystem", interrupt_once)
    with pytest.raises(OSError, match="promotion interrupted"):
        value.apply("receipt-1")
    assert store.pending_operations()[0]["phase"] == "staged"

    value.host.scan_result["summary"] = "new affirmative summary"
    with pytest.raises(PackagePolicyError, match="scan changed"):
        value.apply("receipt-1")

    assert store.records == {}
    assert client.record_calls == []


def test_install_respects_nonexpiring_setup_starter_lock(executor):
    from hermes_wisdom_plugin.setup_lock import managed_operation_guard

    value, store, _client, _receipts, _current = executor
    with managed_operation_guard(Path(store.root), "skill-1"):
        with pytest.raises(PackagePolicyError, match="starting or running"):
            value.apply("receipt-1")


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


def test_uninstall_revocation_during_filesystem_io_stops_before_removal(
    executor, monkeypatch
):
    value, store, client, _receipts, _current = executor
    installed = value.apply("receipt-1")
    target = Path(installed["path"])
    current = {"value": True}
    original_same_filesystem = install_execution._same_filesystem

    def inspect_then_revoke(source, destination_parent):
        original_same_filesystem(source, destination_parent)
        current["value"] = False

    monkeypatch.setattr(install_execution, "_same_filesystem", inspect_then_revoke)

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.uninstall("skill-1", may_apply=lambda: current["value"])

    operation = next(
        item for item in store.operations.values() if item["kind"] == "uninstall"
    )
    assert operation["phase"] == "staged"
    assert target.is_dir()
    assert store.records["skill-1"]["state"] == "active"
    assert client.deactivate_calls == []


def test_uninstall_recovery_revocation_during_owner_reread_stops_gateway_effect(
    executor,
):
    value, store, client, _receipts, current_owner = executor
    value.apply("receipt-1")
    client.deactivate_error = RuntimeError("offline")
    assert value.uninstall("skill-1")["pending"] is True
    client.deactivate_error = None
    current = {"value": True}
    owner_reads = {"count": 0}
    original_owner = value.owner

    def owner_and_revoke():
        owner_reads["count"] += 1
        result = original_owner()
        if owner_reads["count"] == 3:
            current["value"] = False
        return result

    value.owner = owner_and_revoke

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.uninstall("skill-1", may_apply=lambda: current["value"])

    operation = next(
        item for item in store.operations.values() if item["kind"] == "uninstall"
    )
    assert operation["phase"] == "local_ledger_committed"
    assert store.records["skill-1"]["state"] == "inactive"
    assert len(client.deactivate_calls) == 1
    assert current_owner["value"] == OWNER


def test_uninstall_revocation_during_gateway_io_records_outcome_without_replay(
    executor,
):
    value, store, client, _receipts, _current = executor
    value.apply("receipt-1")
    current = {"value": True}
    original_deactivate = client.deactivate_install

    def deactivate_and_revoke(installation_id, skill_id):
        result = original_deactivate(installation_id, skill_id)
        current["value"] = False
        return result

    client.deactivate_install = deactivate_and_revoke

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.uninstall("skill-1", may_apply=lambda: current["value"])

    operation = next(
        item for item in store.operations.values() if item["kind"] == "uninstall"
    )
    assert operation["state"] == "done"
    assert operation["phase"] == "gateway_recorded"
    assert len(client.deactivate_calls) == 1

    current["value"] = True
    result = value.uninstall("skill-1", may_apply=lambda: current["value"])

    assert result["uninstalled"] is True
    assert len(client.deactivate_calls) == 1


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
    with pytest.raises(PackagePolicyError, match="scan is unavailable"):
        value.apply("receipt-1", accept_partial=True)
    assert store.records == {}


@pytest.mark.parametrize("invalid_allowed", [1, 0, "true", [], {}])
def test_scan_allowed_requires_strict_bool_or_none(invalid_allowed):
    scan = {
        "available": True,
        "allowed": invalid_allowed,
        "verdict": "invalid",
        "summary": "invalid allowed type",
        "findings": [],
    }

    with pytest.raises(PackagePolicyError, match="blocked|changed"):
        install_execution._require_scan(scan, scan, accepted_warning=True)


def test_unavailable_scan_cannot_be_approved():
    scan = {
        "available": False,
        "allowed": None,
        "verdict": "unavailable",
        "summary": "scanner unavailable",
        "findings": [],
    }

    with pytest.raises(PackagePolicyError, match="unavailable"):
        install_execution._require_scan(scan, scan, accepted_warning=True)


def test_ask_scan_with_exact_warning_ack_can_install(executor):
    value, _store, _client, _receipts, _current = executor
    value.host.scan = lambda *_args, **_kwargs: {
        "available": True,
        "allowed": None,
        "verdict": "ask",
        "summary": "review requested",
        "findings": [],
    }

    original = PLAN["local_scan"]
    PLAN["local_scan"] = {
        "available": True,
        "allowed": None,
        "verdict": "ask",
        "summary": "review requested",
        "findings": [],
    }
    original_policy = PLAN.get("policy")
    PLAN["policy"] = {"warning_acceptance": {
        "accepted": True,
        "operation": "install",
        "content_hash": PLAN["content_hash"],
        "scan_fingerprint": "sha256:fixture",
    }}
    try:
        assert value.apply("receipt-1")["installed"] is True
    finally:
        PLAN["local_scan"] = original
        if original_policy is None:
            PLAN.pop("policy", None)
        else:
            PLAN["policy"] = original_policy


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


class InvocationClient(ReviewClient):
    """Real-review transport with the install acknowledgement endpoint."""

    def __init__(self):
        super().__init__()
        self.record_calls = []

    def record_install(self, **values):
        self.record_calls.append(values)
        return SimpleNamespace(effective_update_mode="REQUIRED")


@pytest.fixture
def invocation_executor(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    authority = {"current": True, "owner_hook": None}

    def current_owner():
        hook = authority["owner_hook"]
        if hook is not None:
            hook()
        return dict(owner)

    store = WisdomStore(tmp_path / "state")
    host = ReviewHost(tmp_path / "profile")
    client = InvocationClient()
    receipts = InstallReceipts(store, current_owner)
    reviewer = InstallReviewer(client, store, receipts, host, current_owner)
    plan = reviewer.plan("skill-1@v2", update_mode="AUTO_WITH_NOTICE")
    value = InstallExecutor(client, store, receipts, reviewer, host, current_owner)
    target = (
        host.skills_root
        / "_wisdom"
        / install_execution.org_directory_name(owner["organization_id"])
        / "managed-skill"
    )
    return value, store, client, receipts, plan, target, authority


@pytest.mark.parametrize("failure", [False, RuntimeError("review callback failed")])
def test_invocation_authority_fails_closed_before_install_effects(
    invocation_executor, failure
):
    value, store, client, _receipts, plan, target, _authority = invocation_executor

    def may_apply():
        if isinstance(failure, Exception):
            raise failure
        return failure

    expected = RuntimeError if isinstance(failure, Exception) else WisdomAuthError
    with pytest.raises(expected):
        value.apply(plan["receipt"], may_apply=may_apply)

    assert store.pending_operations() == []
    assert store.installations() == []
    assert client.record_calls == []
    assert not target.exists()


def test_invocation_revocation_during_owner_reread_prevents_install(
    invocation_executor,
):
    value, store, client, _receipts, plan, target, authority = invocation_executor

    def revoke():
        authority["current"] = False

    authority["owner_hook"] = revoke
    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.apply(plan["receipt"], may_apply=lambda: authority["current"])

    assert store.pending_operations() == []
    assert store.installations() == []
    assert client.record_calls == []
    assert not target.exists()


@pytest.mark.parametrize("boundary", ["source", "preparation"])
def test_invocation_revocation_during_verification_reads_prevents_install(
    invocation_executor, boundary
):
    value, store, client, _receipts, plan, target, authority = invocation_executor
    if boundary == "source":
        original = client.content

        def revoke_after_content(*args, **kwargs):
            result = original(*args, **kwargs)
            authority["current"] = False
            return result

        client.content = revoke_after_content
    else:
        original = value.host.scan

        def revoke_after_scan(path):
            result = original(path)
            authority["current"] = False
            return result

        value.host.scan = revoke_after_scan

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.apply(plan["receipt"], may_apply=lambda: authority["current"])

    assert store.pending_operations() == []
    assert store.installations() == []
    assert client.record_calls == []
    assert not target.exists()


def test_late_invocation_revocation_keeps_recoverable_filesystem_effect(
    invocation_executor,
):
    value, store, client, receipts, plan, target, _authority = invocation_executor

    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.apply(plan["receipt"], may_apply=lambda: not target.exists())

    operation = store.pending_operations()[0]
    assert operation["kind"] == "install"
    assert operation["phase"] == "staged"
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed\n"
    assert store.installations() == []
    assert client.record_calls == []
    inode = target.stat().st_ino

    result = value.apply(plan["receipt"], may_apply=lambda: True)

    assert result["installed"] is True
    assert target.stat().st_ino == inode
    assert receipts.outcome(plan["receipt"]) == result
    assert store.pending_operations() == []
    assert len(client.record_calls) == 1


def test_current_invocation_completes_exact_receipt_install(invocation_executor):
    value, store, client, receipts, plan, target, _authority = invocation_executor
    checks = []

    result = value.apply(
        plan["receipt"], may_apply=lambda: checks.append(target.exists()) or True
    )

    assert result["installed"] is True
    assert result["version"] == plan["version"]
    assert result["content_hash"] == plan["content_hash"]
    assert result["effective_update_mode"] == "REQUIRED"
    assert checks and False in checks and True in checks
    assert receipts.outcome(plan["receipt"]) == result
    assert store.installation("skill-1")["target_path"] == str(target)
    assert len(client.record_calls) == 1


def test_real_warning_review_receipt_and_executor_install_without_running_script(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    host = ReviewHost(tmp_path / "profile", scan_allowed=False)
    host.findings = [{"kind": "suspicious", "file": "scripts/sentinel.sh"}]
    client = InvocationClient()
    marker = tmp_path / "SHOULD_NOT_EXIST"
    script = f"#!/bin/sh\ntouch {marker}\n".encode()
    client.files.append(("scripts/sentinel.sh", "exec", script))
    content = _content(client.files)
    client.version_value.update(content_hash=content.content_hash, commit=content.commit)
    receipts = InstallReceipts(store, lambda: dict(owner))
    reviewer = InstallReviewer(client, store, receipts, host, lambda: dict(owner))

    blocked = reviewer.plan("skill-1@v2")
    assert blocked["allowed"] is False
    accepted = reviewer.plan("skill-1@v2", accept_warnings=True)
    assert accepted["policy"]["warning_acceptance"]["operation"] == "install"
    executor = InstallExecutor(client, store, receipts, reviewer, host, lambda: dict(owner))
    result = executor.apply(accepted["receipt"])

    assert result["installed"] is True
    assert not marker.exists()
    installed_script = Path(result["path"]) / "scripts" / "sentinel.sh"
    assert installed_script.read_bytes() == script
    assert installed_script.stat().st_mode & 0o111
    assert receipts.outcome(accepted["receipt"]) == result


def test_real_retry_repairs_unbound_install_journal_start(tmp_path, monkeypatch):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    receipts = InstallReceipts(store, lambda: dict(owner))
    plan = receipts.create({key: value for key, value in PLAN.items() if key != "receipt"})
    value = InstallExecutor(
        Client(), store, receipts, Reviewer(), Host(tmp_path / "skills"), lambda: dict(owner)
    )
    real_replace = store.replace_operation_payload
    failed = False

    def fail_once(operation_id, payload):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("payload commit interrupted")
        real_replace(operation_id, payload)

    monkeypatch.setattr(store, "replace_operation_payload", fail_once)
    with pytest.raises(OSError, match="payload commit"):
        value.apply(plan["receipt"])
    pending = store.pending_operations()[0]
    assert pending["phase"] == "staged"
    assert pending["payload_json"].find('"staging_path": null') >= 0
    assert receipts.store.get_metadata("install_receipt:" + plan["receipt"])[
        "operation_id"
    ] is None

    result = value.apply(plan["receipt"])

    assert result["installed"] is True
    assert receipts.outcome(plan["receipt"]) == result


def test_real_retry_repairs_uninstall_journal_without_trash_path(tmp_path, monkeypatch):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    receipts = InstallReceipts(store, lambda: dict(owner))
    plan = receipts.create({key: value for key, value in PLAN.items() if key != "receipt"})
    value = InstallExecutor(
        Client(), store, receipts, Reviewer(), Host(tmp_path / "skills"), lambda: dict(owner)
    )
    value.apply(plan["receipt"])
    real_replace = store.replace_operation_payload
    failed = False

    def fail_uninstall_start(operation_id, payload):
        nonlocal failed
        operation = store.operation(operation_id)
        if operation["kind"] == "uninstall" and not failed:
            failed = True
            raise OSError("trash payload interrupted")
        real_replace(operation_id, payload)

    monkeypatch.setattr(store, "replace_operation_payload", fail_uninstall_start)
    with pytest.raises(OSError, match="trash payload"):
        value.uninstall("skill-1")
    pending = store.pending_operations()[0]
    assert pending["phase"] == "staged"
    assert pending["payload_json"].find('"trash_path": null') >= 0

    result = value.uninstall("skill-1")

    assert result["uninstalled"] is True
    assert Path(result["recoverable_path"]).is_dir()


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
