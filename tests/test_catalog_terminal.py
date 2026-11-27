"""Terminal catalog data precedes every native completion boundary."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name,unused-import,too-many-locals,duplicate-code
import pytest
from test_catalog_mutations import Permits
from test_install_execution import invocation_executor
from test_update_execution import executor as update_executor


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_terminal_wire_is_saved_before_native_finish(action, request, tmp_path, monkeypatch):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer
    from test_uninstall_review import _installed

    if action == "install":
        executor, store, _client, receipts, plan, target, _owner = request.getfixturevalue(
            "invocation_executor")
    elif action == "update":
        executor, store, _client, receipts, plan, target, _owner = request.getfixturevalue(
            "update_executor")
    else:
        executor, store, _client, owner, target = _installed(tmp_path)
        receipts = UninstallReceipts(store, lambda: dict(owner))
        plan = UninstallReviewer(store, receipts, executor.host,
            lambda: dict(owner)).plan("skill-1")
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": None}
    executor.catalog = CatalogMutation(api, admission, action, "plugin.source.team:skill-1",
                                       plan, executor.host)
    finish = store.finish
    observed = []

    def checked(operation_id, result, *, stage):
        saved = store.review("catalog_operation:" + operation_id)
        assert saved is not None and saved["state"] == "completed"
        observed.append(saved)
        return finish(operation_id, result, stage=stage)

    monkeypatch.setattr(store, "finish", checked)
    if action == "uninstall":
        executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    else:
        executor.apply(plan["receipt"])
    assert len(observed) == 1 and api.calls[-1][1]["result"] == observed[0]
