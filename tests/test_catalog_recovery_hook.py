"""D11 exact journal recovery uses the current host hold, never fresh commit."""
# pylint: disable=missing-function-docstring,unused-import,duplicate-code
# pylint: disable=too-many-locals,redefined-outer-name,too-many-arguments
# pylint: disable=too-many-positional-arguments,import-outside-toplevel
import os

import pytest
from test_catalog_crash_recovery import scenario, run_action
from test_install_execution import invocation_executor
from test_update_execution import executor as update_executor
from catalog_recovery_support import RecoveryPermits
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.package_application import tree_hashes


@pytest.mark.parametrize("action,phase,kind", [
    ("install", "prepared", "retry_effect"),
    ("install", "files_committed", "reconciled"),
    ("update", "prepared", "retry_effect"),
    ("update", "old_preserved", "retry_effect"),
    ("update", "files_committed", "reconciled"),
    ("uninstall", "prepared", "retry_effect"),
    ("uninstall", "files_committed", "reconciled"),
])
def test_journal_recovery_selects_bound_host_hook(
    action, phase, kind, request, tmp_path, monkeypatch,
):
    executor, store, client, receipts, plan, target, _api, _admission = scenario(
        action, request, tmp_path)
    api = RecoveryPermits(target, executor.host.skills_root, action)
    executor.catalog.api = api
    checkpoint = store.checkpoint

    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_files", {}).get("phase") == phase:
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("journal death")
        return checkpoint(operation_id, payload, stage=stage)

    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="journal death"):
        run_action(executor, receipts, plan, action)
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    calls = len(api.calls)
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert any(name == "recover" for name, _value in api.calls[calls:])
    assert not any(name == "enter" for name, _value in api.calls[calls:])
    assert api.decisions[-1]["kind"] == kind
    assert not store.pending_operations("skill-1")
    assert client is not None


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
@pytest.mark.parametrize("marker_saved", [False, True])
def test_death_after_core_pending_settlement_retains_one_effect(
    action, marker_saved, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, target, api, _admission = scenario(
        action, request, tmp_path)
    checkpoint = store.checkpoint

    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_local_committed"):
            if marker_saved:
                checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("marker death")
        return checkpoint(operation_id, payload, stage=stage)

    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="marker death"):
        run_action(executor, receipts, plan, action)
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    before = tree_hashes(target)
    assert any(name == "local" for name, _value in api.calls)
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert tree_hashes(target) == before
    assert not store.pending_operations("skill-1")


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_observed_digest_mismatch_is_terminal_and_preserves_local_bytes(
    action, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, target, api, _admission = scenario(
        action, request, tmp_path)
    checkpoint = store.checkpoint

    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_files", {}).get("phase") == "files_committed":
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("journal death")
        return checkpoint(operation_id, payload, stage=stage)

    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="journal death"):
        run_action(executor, receipts, plan, action)
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    before = tree_hashes(target)
    api.observation = lambda hold: setattr(hold, "observed_revision", "different-digest")
    with pytest.raises(PackagePolicyError, match="digest changed"):
        run_action(executor, receipts, plan, action)
    assert tree_hashes(target) == before
    assert api.decisions[-1]["kind"] == "abandon"
    assert not store.pending_operations("skill-1")


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_journal_mismatch_abandons_without_touching_replacement(
    action, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, target, _api, _admission = scenario(
        action, request, tmp_path)
    api = RecoveryPermits(target, executor.host.skills_root, action)
    executor.catalog.api = api
    checkpoint = store.checkpoint

    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_files", {}).get("phase") == "files_committed":
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("journal death")
        return checkpoint(operation_id, payload, stage=stage)

    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="journal death"):
        run_action(executor, receipts, plan, action)
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    before = []

    def replace(_hold):
        if not target.exists():
            target.mkdir()
        (target / "SKILL.md").write_text("replacement not owned by the operation")
        before.append(tree_hashes(target))

    api.observation = replace
    with pytest.raises(PackagePolicyError, match="changed|evidence|unrecognized"):
        run_action(executor, receipts, plan, action)
    assert tree_hashes(target) == before[0]
    assert api.decisions[-1]["kind"] == "abandon"
    assert not store.pending_operations("skill-1")
    result = run_action(executor, receipts, plan, action)
    assert result["rejected"]
    assert tree_hashes(target) == before[0]
