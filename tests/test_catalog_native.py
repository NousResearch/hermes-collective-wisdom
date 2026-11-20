"""Native workflows share the catalog reservation path when it is live."""
# pylint: disable=missing-function-docstring,protected-access,too-few-public-methods
# pylint: disable=import-outside-toplevel,redefined-outer-name,too-many-locals
# pylint: disable=duplicate-code
from types import SimpleNamespace

import pytest
from test_catalog_reviews import reviews  # pylint: disable=unused-import
from test_catalog_mutations import Permits
from test_install_execution import invocation_executor  # pylint: disable=unused-import
from test_update_execution import executor as update_executor  # pylint: disable=unused-import


def test_uninstall_service_uses_the_durable_review_before_effect(tmp_path):
    from hermes_wisdom_plugin.runtime import InvocationService
    from test_uninstall_review import _installed

    executor, store, _client, owner, target = _installed(tmp_path)
    service = object.__new__(InvocationService)
    service._installation_services = lambda: (None, executor)
    service.native_owner = lambda: dict(owner)
    service.check_owner = lambda: None
    service._installed = store
    service._state = None
    plan = service.uninstall_plan("skill-1")
    (target / "SKILL.md").write_text("new local edit")
    from hermes_wisdom_plugin.package import PackagePolicyError
    with pytest.raises(PackagePolicyError, match="changed"):
        service.uninstall_apply(plan["receipt"])
    assert (target / "SKILL.md").read_text() == "new local edit"


def test_native_uninstall_admission_uses_reviewed_bytes(tmp_path):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer
    from test_uninstall_review import _installed

    executor, store, _client, owner, target = _installed(tmp_path)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    api.admit_native = lambda native, _choices: {
        "admission": {"registration_id": "registration_001", "generation": "generation_00001",
                      "review_id": native["review_id"], "reservation_id": "reservation_0001",
                      "expected_path_revision": 0, "expected_installation_revision": "old"},
        "operation_id": "core_uninstall_00001"}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    assert executor.uninstall_reviewed(plan["receipt"], receipts=receipts)["uninstalled"]
    assert store.operation("core_uninstall_00001")["status"] == "done" and not target.exists()


def test_native_update_admission_uses_one_core_operation(update_executor):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog

    executor, store, _client, _receipts, plan, target, _owner = update_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    api.admit_native = lambda native, _choices: {
        "admission": {"registration_id": "registration_001", "generation": "generation_00001",
                      "review_id": native["review_id"], "reservation_id": "reservation_0001",
                      "expected_path_revision": 0, "expected_installation_revision": "old"},
        "operation_id": "core_update_00000001"}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    assert executor.apply(plan["receipt"])["updated"]
    assert store.operation("core_update_00000001")["status"] == "done"


def test_native_admission_binds_the_returned_operation_id(invocation_executor):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog

    executor, store, _client, _receipts, plan, target, _authority = invocation_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    seen = []

    def admit(native_receipt, approved_choices):
        assert native_receipt["review_id"] != native_receipt["review_token"]
        assert native_receipt["scan"]["verdict"] in {"safe", "caution", "dangerous", "unknown"}
        assert set(approved_choices) <= {
            "acknowledgements", "accept_partial", "preserve_modified",
            "accept_sensitive", "accept_warnings",
        }
        assert approved_choices["acknowledgements"] == native_receipt["acknowledgements_required"]
        seen.append((native_receipt, approved_choices))
        return {"admission": {"registration_id": "registration_001",
                              "generation": "generation_00001",
                              "review_id": native_receipt["review_id"],
                              "reservation_id": "reservation_0001", "expected_path_revision": 0,
                              "expected_installation_revision": None},
                "operation_id": "core_operation_00001"}

    api.admit_native = admit
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    assert executor.apply(plan["receipt"])["installed"]
    assert store.operation("core_operation_00001")["status"] == "done"
    assert seen[0][0]["native_receipt_id"] == plan["receipt"]
    assert api.calls[0][1]["operation_id"] == "core_operation_00001"


def test_no_live_catalog_keeps_the_stock_execution(invocation_executor):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog

    executor, store, _client, _receipts, plan, _target, _authority = invocation_executor
    executor.admitter = NativeCatalog(SimpleNamespace(admit_native=lambda *_a: None),
                                      "collective-wisdom", executor.host)
    assert executor.apply(plan["receipt"])["installed"]
    assert len(store._read()["operations"]) == 1 and executor.catalog is None


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_restarted_pending_re_admits_same_operation_without_second_write(
    action, request, tmp_path, monkeypatch,
):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer
    from test_uninstall_review import _installed

    if action == "install":
        executor, store, client, receipts, plan, target, _owner = request.getfixturevalue(
            "invocation_executor")
        def method():
            return executor.apply(plan["receipt"])
    elif action == "update":
        executor, store, client, receipts, plan, target, _owner = request.getfixturevalue(
            "update_executor")
        def method():
            return executor.apply(plan["receipt"])
    else:
        executor, store, client, owner, target = _installed(tmp_path)
        receipts = UninstallReceipts(store, lambda: dict(owner))
        plan = UninstallReviewer(store, receipts, executor.host,
            lambda: dict(owner)).plan("skill-1")
        def method():
            return executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    seen = []

    def admit(native, choices):
        seen.append((native, choices))
        return {"admission": {"registration_id": "registration_001",
                              "generation": "generation_00001",
                              "review_id": native["review_id"],
                              "reservation_id": "reservation_0001", "expected_path_revision": 0,
                              "expected_installation_revision": None},
                "operation_id": "core_recovery_00001"}

    api.admit_native = admit
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    gateway_name = "deactivate_install" if action == "uninstall" else "record_install"
    original = getattr(client, gateway_name)

    def lost(*_args, **_kwargs):
        raise RuntimeError("lost response")

    monkeypatch.setattr(client, gateway_name, lost)
    assert method()["pending"]
    executor.catalog = None  # a new executor after restart has no process-local admission
    monkeypatch.setattr(client, gateway_name, original)
    monkeypatch.setattr(api, "commit", lambda *_a: pytest.fail("second file-write phase"))
    assert (method()
        [{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]])
    assert len(seen) == 2 and seen[0] == seen[1]
    assert store.operation("core_recovery_00001")["status"] == "done"
    assert len(store._read()["operations"]) == 1
    assert api.calls[-1][0] == "terminal"


def test_raw_uninstall_refuses_facade_bypass_before_effect(tmp_path):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog
    from hermes_wisdom_plugin.package import PackagePolicyError
    from test_uninstall_review import _installed

    executor, store, client, _owner, target = _installed(tmp_path)
    executor.admitter = NativeCatalog(SimpleNamespace(admit_native=lambda *_a: {"live": True}),
                                      "collective-wisdom", executor.host)
    with pytest.raises(PackagePolicyError, match="review"):
        executor.uninstall("skill-1")
    assert target.exists() and not store.pending_operations() and not client.deactivate_calls


def test_admission_error_never_falls_back_to_raw_writer(invocation_executor):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog

    executor, store, _client, _receipts, plan, target, _authority = invocation_executor

    def refuse(*_args):
        raise RuntimeError("facade unavailable")

    executor.admitter = NativeCatalog(SimpleNamespace(admit_native=refuse),
                                      "collective-wisdom", executor.host)
    with pytest.raises(RuntimeError, match="facade unavailable"):
        executor.apply(plan["receipt"])
    assert not target.exists() and not store.pending_operations()
