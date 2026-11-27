"""Update authority loss never reaches legacy compensating writers for catalog work."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=unused-import,too-many-locals,duplicate-code
from contextlib import contextmanager

import pytest
from test_catalog_crash_recovery import scenario, run_action
from test_install_execution import invocation_executor
from test_update_execution import executor as update_executor

from hermes_wisdom_plugin.package_application import tree_hashes


@pytest.mark.parametrize("phase", ["prepared", "old_preserved", "files_committed"])
@pytest.mark.parametrize("admitted", [True, False])
def test_authority_refusal_retains_local_state_under_current_admission(
    phase, admitted, request, tmp_path, monkeypatch,
):
    executor, store, client, receipts, plan, target, api, _admission = scenario(
        "update", request, tmp_path)
    checkpoint = store.checkpoint
    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_files", {}).get("phase") == phase:
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("death at phase")
        return checkpoint(operation_id, payload, stage=stage)
    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="death at phase"):
        run_action(executor, receipts, plan, "update")
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    executor.reviewer.reject = True
    original = api.recover
    @contextmanager
    def permit(*args):
        if not admitted:
            raise RuntimeError("revoked or generic writer owns target")
        with original(*args) as hold:
            yield hold
    monkeypatch.setattr(api, "recover", permit)
    before = tree_hashes(target)
    record_before = store.installation("skill-1")
    if not admitted:
        with pytest.raises(RuntimeError, match="revoked or generic"):
            run_action(executor, receipts, plan, "update")
        assert tree_hashes(target) == before
        assert store.installation("skill-1") == record_before
        return
    result = run_action(executor, receipts, plan, "update")
    assert result["rejected"] and result["updated"] is False
    expected_version = 2 if phase == "files_committed" else 1
    assert store.installation("skill-1")["version"] == expected_version
    assert tree_hashes(target) == (plan["baseline"] if expected_version == 2
                                   else plan["local_snapshot"])
    assert not client.record_calls
    assert not store.pending_operations("skill-1")
    assert api.calls[-1][1]["result"]["state"] == "failed"
    assert api.calls[-1][1]["result"]["managed_state"] == "installed"


@pytest.mark.parametrize("phase", ["prepared", "old_preserved", "files_committed"])
def test_refusal_restarts_after_local_marker_without_reapplying_update(
    phase, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, target, api, _admission = scenario(
        "update", request, tmp_path)
    checkpoint = store.checkpoint
    def die_initial(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_files", {}).get("phase") == phase:
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("initial death")
        return checkpoint(operation_id, payload, stage=stage)
    monkeypatch.setattr(store, "checkpoint", die_initial)
    with pytest.raises(RuntimeError, match="initial death"):
        run_action(executor, receipts, plan, "update")
    executor.reviewer.reject = True
    def die_refusal(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_local_committed"):
            checkpoint(operation_id, payload, stage=stage)
            raise RuntimeError("refusal marker death")
        return checkpoint(operation_id, payload, stage=stage)
    monkeypatch.setattr(store, "checkpoint", die_refusal)
    with pytest.raises(RuntimeError, match="refusal marker death"):
        run_action(executor, receipts, plan, "update")
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    executor.reviewer.reject = False
    result = run_action(executor, receipts, plan, "update")
    assert result["rejected"] and result["updated"] is False
    assert tree_hashes(target) == (plan["baseline"] if phase == "files_committed"
                                   else plan["local_snapshot"])
    assert api.calls[-1][1]["result"]["state"] == "failed"
