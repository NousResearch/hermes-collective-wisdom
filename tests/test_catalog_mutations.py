"""Two-phase catalog permit at the actual filesystem boundary, not around network work."""
# pylint: disable=too-many-locals,missing-class-docstring,attribute-defined-outside-init
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=duplicate-code,redefined-outer-name,too-few-public-methods

from contextlib import contextmanager

import pytest
from test_uninstall_review import _installed
from test_install_execution import invocation_executor  # pylint: disable=unused-import
from test_update_execution import executor as update_executor  # pylint: disable=unused-import


def test_install_local_commit_is_guarded_and_gateway_has_no_hold(invocation_executor):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation

    executor, store, client, _receipts, plan, target, _authority = invocation_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": None}
    executor.catalog = CatalogMutation(api, admission, "install", "plugin.source.team:skill-1",
                                       plan, executor.host)
    original = client.record_install

    def gateway(**values):
        assert not api.held
        assert store.pending_operations("skill-1")[0]["payload"]["catalog_local_committed"]
        return original(**values)

    client.record_install = gateway
    assert executor.apply(plan["receipt"])["installed"]
    assert [name for name, _value in api.calls] == [
        "bind", "enter", "local", "release", "terminal"]


def test_update_local_commit_is_guarded_and_gateway_has_no_hold(update_executor):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation

    executor, store, client, _receipts, plan, target, _owner = update_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": "old"}
    executor.catalog = CatalogMutation(api, admission, "update", "plugin.source.team:skill-1",
                                       plan, executor.host)
    original = client.record_install

    def gateway(**values):
        assert not api.held
        assert store.pending_operations("skill-1")[0]["payload"]["catalog_local_committed"]
        return original(**values)

    client.record_install = gateway
    assert executor.apply(plan["receipt"])["updated"]
    assert [name for name, _value in api.calls] == [
        "bind", "enter", "local", "release", "terminal"]


def test_catalog_gateway_refusal_preserves_the_committed_install(invocation_executor):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin.errors import WisdomValidationError

    executor, store, client, _receipts, plan, target, _authority = invocation_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": None}
    executor.catalog = CatalogMutation(api, admission, "install", "plugin.source.team:skill-1",
                                       plan, executor.host)

    def refuse(**_values):
        raise WisdomValidationError("rejected")

    client.record_install = refuse
    result = executor.apply(plan["receipt"])
    assert result["rejected"]
    assert (target / "SKILL.md").is_file()
    terminal = api.calls[-1][1]["result"]
    assert terminal["state"] == "failed" and terminal["managed_state"] == "installed"
    assert store.operation(terminal["operation_id"])["status"] == "done"


def test_catalog_gateway_refusal_preserves_the_committed_update(update_executor):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin.errors import WisdomNotFound
    from test_update_execution import V2

    executor, _store, client, _receipts, plan, target, _owner = update_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": "old"}
    executor.catalog = CatalogMutation(api, admission, "update", "plugin.source.team:skill-1",
                                       plan, executor.host)
    client.error = WisdomNotFound("retired")
    result = executor.apply(plan["receipt"])
    assert result["rejected"] and (target / "SKILL.md").read_bytes() == V2
    assert api.calls[-1][1]["result"]["state"] == "failed"


def test_pending_catalog_install_retries_network_only(invocation_executor, monkeypatch):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin import install_execution

    executor, _store, client, _receipts, plan, target, _authority = invocation_executor
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": None}
    executor.catalog = CatalogMutation(api, admission, "install", "plugin.source.team:skill-1",
                                       plan, executor.host)
    original = client.record_install

    def lost(**_values):
        raise RuntimeError("response lost")

    client.record_install = lost
    assert executor.apply(plan["receipt"])["pending"]
    calls = list(api.calls)
    client.record_install = original
    monkeypatch.setattr(install_execution, "_install_package",
                        lambda *_a: pytest.fail("recovery repeated file effect"))
    assert executor.apply(plan["receipt"])["installed"]
    assert api.calls[:-1] == calls and api.calls[-1][0] == "terminal"


def test_catalog_uninstall_permanent_refusal_keeps_removed_state_and_replays(tmp_path):
    from hermes_wisdom_plugin.catalog_native import NativeCatalog
    from hermes_wisdom_plugin.errors import WisdomValidationError
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer

    executor, store, client, owner, target = _installed(tmp_path)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": "old"}
    api.admit_native = lambda *_a: {"admission": admission, "operation_id": "core_refused_00001"}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)

    def refuse(*_args):
        raise WisdomValidationError("Gateway refuses removal")

    client.deactivate_install = refuse
    result = executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert result["rejected"] and not target.exists()
    assert store.installation("skill-1")["state"] == "inactive"
    assert store.operation("core_refused_00001")["status"] == "done"
    assert api.calls[-1][1]["result"]["state"] == "failed"
    assert api.calls[-1][1]["result"]["managed_state"] == "removed"
    target.mkdir()
    (target / "SKILL.md").write_text("unmanaged replacement")
    assert executor.uninstall_reviewed(plan["receipt"], receipts=receipts) == result
    assert (target / "SKILL.md").read_text() == "unmanaged replacement"


class Permits:
    def __init__(self, target):
        self.target = target
        self.held = False
        self.calls = []

    def bind(self, request):
        self.calls.append(("bind", request))
        return {"ok": True}

    @contextmanager
    def commit(self, admission, operation_id):
        assert not self.held
        self.held = True
        self.calls.append(("enter", operation_id))
        api = self

        class Commit:
            target_relative_path = api.target
            expected_installation_revision = admission["expected_installation_revision"]

            def settle(self, result):
                assert api.held
                api.calls.append(("local", result))

        try:
            yield Commit()
        finally:
            self.held = False
            self.calls.append(("release", operation_id))

    def settle(self, request):
        assert not self.held
        self.calls.append(("terminal", request))
        return {"ok": True}


def test_uninstall_file_commit_and_local_journal_are_inside_permit_gateway_outside(tmp_path):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin.skill_catalog import digest
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer

    executor, store, client, owner, target = _installed(tmp_path)
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0,
                 "expected_installation_revision": digest(plan["installation"])}
    executor.catalog = CatalogMutation(api, admission, "uninstall", "plugin.source.team:skill-1",
                                       plan, executor.host)
    original = client.deactivate_install

    def gateway(*args):
        assert not api.held
        assert not target.exists()
        row = store.pending_operations("skill-1")[0]
        assert row["payload"]["catalog_local_committed"] is True
        return original(*args)

    client.deactivate_install = gateway
    result = executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert result["uninstalled"]
    phases = [name for name, _value in api.calls]
    assert phases == ["bind", "enter", "local", "release", "terminal"]
    local = next(value for name, value in api.calls if name == "local")
    assert local["outcome"] is None
    assert local["installation_revision"]
    assert local["state"] == "pending" and local["managed_state"] == "removed"
    terminal = next(value for name, value in api.calls if name == "terminal")
    assert terminal["result"]["state"] == "completed" and "permit_id" not in terminal


def test_catalog_prestate_recheck_inside_permit_blocks_target_swap(tmp_path):
    from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
    from hermes_wisdom_plugin.package import PackagePolicyError
    from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer

    executor, store, client, owner, target = _installed(tmp_path)
    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": "old"}
    original = api.commit

    @contextmanager
    def swapped(*args):
        with original(*args) as commit:
            (target / "SKILL.md").write_text("changed at final admission")
            yield commit

    api.commit = swapped
    executor.catalog = CatalogMutation(api, admission, "uninstall", "plugin.source.team:skill-1",
                                       plan, executor.host)
    with pytest.raises(PackagePolicyError, match="changed"):
        executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert (target / "SKILL.md").read_text() == "changed at final admission"
    assert not client.deactivate_calls
    assert not api.held
