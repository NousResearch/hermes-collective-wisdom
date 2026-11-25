"""Real death at every catalog rename boundary retains exactly one local effect."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name,unused-import,too-many-locals,duplicate-code
# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-statements
import os
from contextlib import contextmanager

import pytest
from test_install_execution import invocation_executor
from test_update_execution import executor as update_executor
from catalog_recovery_support import RecoveryPermits
from test_uninstall_review import _installed

from hermes_wisdom_plugin.catalog_mutations import CatalogMutation
from hermes_wisdom_plugin.catalog_native import NativeCatalog
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer


def scenario(action, request, tmp_path):
    if action == "install":
        executor, store, client, receipts, plan, target, owner = request.getfixturevalue(
            "invocation_executor")
    elif action == "update":
        executor, store, client, receipts, plan, target, owner = request.getfixturevalue(
            "update_executor")
    else:
        executor, store, client, owner, target = _installed(tmp_path)
        receipts = UninstallReceipts(store, lambda: dict(owner))
        plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan(
            "skill-1")
    api = RecoveryPermits(target, executor.host.skills_root, action)
    admission = {"registration_id": "registration_001", "generation": "generation_00001",
                 "review_id": "review_000000001", "reservation_id": "reservation_0001",
                 "expected_path_revision": 0, "expected_installation_revision": None}
    executor.catalog = CatalogMutation(api, admission, action, "plugin.source.team:skill-1",
                                       plan, executor.host)
    return executor, store, client, receipts, plan, target, api, admission


def run_action(executor, receipts, plan, action):
    if action == "uninstall":
        return executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    return executor.apply(plan["receipt"])


@pytest.mark.parametrize("action,index", [("install", 1), ("update", 1), ("update", 2),
                                          ("uninstall", 1)])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("replacement", [False, True])
def test_death_at_rename_re_registers_without_repeating_effect(
    action, index, when, replacement, request, tmp_path, monkeypatch,
):
    from hermes_wisdom_plugin import package_application
    executor, store, _client, receipts, plan, target, api, admission = scenario(
        action, request, tmp_path)
    original = package_application.os.replace
    pid = os.fork()
    if pid == 0:
        count = 0
        def die(source, destination):
            nonlocal count
            # State aggregate replaces are not managed tree effects.
            if os.path.isdir(source) and str(source).startswith(str(executor.host.skills_root)):
                count += 1
                if count == index and when == "before":
                    os._exit(73)
                original(source, destination)
                if count == index and when == "after":
                    os._exit(73)
            else:
                original(source, destination)
        package_application.os.replace = die
        try:
            run_action(executor, receipts, plan, action)
        finally:
            os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    pending = store.pending_operations("skill-1")[0]
    executor.catalog = None
    api.admit_native = lambda *_a: {"admission": admission, "operation_id": pending["id"]}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    if replacement:
        # Even byte-identical replacement directories are not this operation's evidence.
        import shutil
        if not target.exists():
            target.mkdir()
            (target / "SKILL.md").write_text("foreign replacement")
        else:
            backup = target.with_name("unmanaged-backup")
            target.rename(backup)
            shutil.copytree(backup, target)
        before = (target / "SKILL.md").read_bytes()
        with pytest.raises(PackagePolicyError, match="changed|evidence|unrecognized"):
            run_action(executor, receipts, plan, action)
        assert (target / "SKILL.md").read_bytes() == before
        return
    renames = []
    def counted(source, destination):
        if os.path.isdir(source) and str(source).startswith(str(executor.host.skills_root)):
            assert api.held
            renames.append((str(source), str(destination)))
        original(source, destination)
    monkeypatch.setattr(package_application.os, "replace", counted)
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    expected_total = 2 if action == "update" else 1
    completed = index if when == "after" else index - 1
    assert len(renames) == expected_total - completed
    assert not store.pending_operations("skill-1")
    assert store.operation(pending["id"])["status"] == "done"


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_death_inside_first_hold_before_journal_resumes_under_recovery_hold(
    action, request, tmp_path, monkeypatch,
):
    from hermes_wisdom_plugin.package_application import tree_hashes

    executor, store, _client, receipts, plan, target, api, admission = scenario(
        action, request, tmp_path)
    pid = os.fork()
    if pid == 0:
        original = api.commit

        @contextmanager
        def die_inside_hold(*args):
            with original(*args):
                os._exit(73)
                yield  # pylint: disable=unreachable

        api.commit = die_inside_hold
        try:
            run_action(executor, receipts, plan, action)
        finally:
            os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    pending, = store.pending_operations("skill-1")
    # The durable operation exists, but no filesystem intent was ever persisted.
    assert pending["payload"].get("catalog_files") is None
    assert not pending["payload"].get("catalog_local_committed")
    executor.catalog = None
    api.admit_native = lambda *_a: {"admission": admission, "operation_id": pending["id"]}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)

    def refuse_ordinary_commit(*_args):
        # Core binds a re-admitted pending operation as recovery and rejects acquire().
        raise PackagePolicyError("Pending recovery requires the journal recovery hold.")

    monkeypatch.setattr(api, "commit", refuse_ordinary_commit)
    expected = pending["payload"].get("baseline")
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert ("recover", pending["id"]) in api.calls
    assert api.decisions == [{"kind": "retry_effect"}]
    assert tree_hashes(target) == (None if action == "uninstall" else expected)
    assert not store.pending_operations("skill-1")
    completed = store.operation(pending["id"])
    assert completed["status"] == "done" and completed["payload"]["catalog_local_committed"]
    assert completed["payload"]["catalog_files"]["phase"] == "files_committed"


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_first_admission_commits_under_the_ordinary_hold(action, request, tmp_path):
    executor, store, _client, receipts, plan, _target, api, admission = scenario(
        action, request, tmp_path)
    executor.catalog = None
    api.admit_native = lambda *_a: {"admission": admission, "operation_id": "core_first_00001"}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert ("enter", "core_first_00001") in api.calls
    assert not [call for call in api.calls if call[0] == "recover"] and not api.decisions
    assert store.operation("core_first_00001")["status"] == "done"


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_local_marker_never_precedes_core_pending_settlement(
    action, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, _target, api, _admission = scenario(
        action, request, tmp_path)
    checkpoint = store.checkpoint
    checked = []

    def marker(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_local_committed"):
            assert any(name == "local" for name, _value in api.calls), (
                "plugin marker made recovery network-only before core knew local state")
            checked.append(True)
        return checkpoint(operation_id, payload, stage=stage)

    monkeypatch.setattr(store, "checkpoint", marker)
    result = run_action(executor, receipts, plan, action)
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert checked


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_recovery_revalidates_evidence_inside_current_writer_hold(
    action, request, tmp_path, monkeypatch,
):
    executor, store, _client, receipts, plan, target, api, _admission = scenario(
        action, request, tmp_path)
    checkpoint = store.checkpoint
    def die(operation_id, payload=None, *, stage=None):
        if (payload or {}).get("catalog_local_committed") or stage == "local_committed":
            raise RuntimeError("death")
        return checkpoint(operation_id, payload, stage=stage)
    monkeypatch.setattr(store, "checkpoint", die)
    with pytest.raises(RuntimeError, match="death"):
        run_action(executor, receipts, plan, action)
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    original = api.recover
    @contextmanager
    def swapped(*args):
        with original(*args) as hold:
            if not target.exists():
                target.mkdir()
            (target / "SKILL.md").write_text("writer won before recovery")
            yield hold
    monkeypatch.setattr(api, "recover", swapped)
    with pytest.raises(PackagePolicyError, match="changed|evidence"):
        run_action(executor, receipts, plan, action)
    assert (target / "SKILL.md").read_text() == "writer won before recovery"


def test_committed_update_recovery_scans_target_with_acknowledged_findings(
    request, tmp_path, monkeypatch,
):
    from hermes_wisdom_plugin import package_application
    from test_update_execution import V2

    executor, store, _client, receipts, original, target, api, _admission = scenario(
        "update", request, tmp_path)
    warning = {"available": True, "allowed": False, "verdict": "caution",
               "summary": "reviewed warning", "findings": [{"description": "network access"}]}
    plan = receipts.create({**original, "local_scan": warning, "policy": {
        "warning_acceptance": {"accepted": True, "operation": "update",
                               "content_hash": original["content_hash"]}}})
    executor.catalog.plan = plan
    scans = []

    def scan(path):
        scans.append(path)
        # The real host reports a missing directory as a clean, empty scan.
        return dict(warning) if path.exists() else dict(executor.host.scan_result)

    monkeypatch.setattr(executor.host, "scan", scan)
    replace = package_application.os.replace

    def crash_after_promotion(source, destination):
        replace(source, destination)
        if destination == target:
            raise RuntimeError("crash after promotion")

    monkeypatch.setattr(package_application.os, "replace", crash_after_promotion)
    with pytest.raises(RuntimeError, match="crash after promotion"):
        run_action(executor, receipts, plan, "update")
    pending, = store.pending_operations("skill-1")
    assert not pending["payload"].get("catalog_local_committed")
    staging = executor._paths(pending["id"], pending["payload"])["staging_path"]
    assert not staging.exists() and target.joinpath("SKILL.md").read_bytes() == V2
    monkeypatch.setattr(package_application.os, "replace", replace)
    executor.catalog = None
    admission = _admission
    api.admit_native = lambda *_a: {"admission": admission, "operation_id": pending["id"]}
    executor.admitter = NativeCatalog(api, "collective-wisdom", executor.host)
    scans.clear()
    assert run_action(executor, receipts, plan, "update")["updated"]
    assert scans == [target]
    completed = store.operation(pending["id"])
    assert completed["status"] == "done" and completed["payload"]["catalog_local_committed"]
    assert not store.pending_operations("skill-1")
