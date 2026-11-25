"""Generic confirmation after a real writer death inside core's first hold, before any journal."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name,unused-import,too-many-locals,duplicate-code
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from catalog_recovery_support import RecoveryPermits
from test_catalog_reviews import _confirmation, reviews
from test_skill_catalog import request as message
from test_update_execution import executor as update_executor

from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.package_application import tree_hashes

OUTCOMES = {"install": "installed", "update": "updated", "uninstall": "uninstalled"}


def confirmation_case(action, fixtures, tmp_path):
    from hermes_wisdom_plugin.catalog_provider import CatalogProvider
    from test_uninstall_review import _installed

    if action == "install":
        adapter, service, _reviewer, host, client, _clock = fixtures.getfixturevalue("reviews")
        client.record_install = lambda **_values: SimpleNamespace(effective_update_mode="MANUAL")
        client.installations = lambda _installation_id: []
        version = "1"
    else:
        if action == "update":
            executor, store, client, receipts, plan, _target, owner = fixtures.getfixturevalue(
                "update_executor")
            reviewer = SimpleNamespace(receipts=receipts, plan=lambda *_a, **_kw: dict(plan))
        else:
            executor, store, client, owner, _target = _installed(tmp_path)
            reviewer = None
        host = executor.host
        service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                                  _installation_services=lambda: (reviewer, executor),
                                  _update_services=lambda: (reviewer, executor))

        @contextmanager
        def services():
            yield service

        adapter = CatalogProvider(services, "collective-wisdom", host)
        version = None
    response = adapter.review(message(action=action, id="skill-1", version=version))
    assert response["ok"], response
    review = response["data"]
    target = host.skills_root / review["target_relative_path"]
    adapter.mutations = RecoveryPermits(target, host.skills_root, action)
    confirmation = _confirmation(review)
    confirmation["admission"]["expected_installation_revision"] = review["installation_revision"]
    return adapter, service, client, target, review, confirmation


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_generic_confirm_after_death_before_journal_resumes_under_recovery_hold(
    action, request, tmp_path, monkeypatch,
):
    adapter, service, _client, target, _review, confirmation = confirmation_case(
        action, request, tmp_path)
    api = adapter.mutations
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
            adapter.confirm(confirmation)
        finally:
            os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    pending, = service.installed.pending_operations("skill-1")
    # The durable operation exists, but no filesystem intent was ever persisted.
    assert pending["payload"].get("catalog_files") is None
    assert not pending["payload"].get("catalog_local_committed")

    def refuse_ordinary_commit(*_args):
        # Core binds a re-admitted pending operation as recovery and rejects acquire().
        raise PackagePolicyError("Pending recovery requires the journal recovery hold.")

    monkeypatch.setattr(api, "commit", refuse_ordinary_commit)
    expected = pending["payload"].get("baseline")
    response = adapter.confirm(confirmation)
    assert response["ok"], response
    operation = response["data"]
    assert operation["state"] == "completed" and operation["outcome"] == OUTCOMES[action]
    assert operation["operation_id"] == pending["id"]
    assert ("recover", pending["id"]) in api.calls
    assert api.decisions == [{"kind": "retry_effect"}]
    assert tree_hashes(target) == (None if action == "uninstall" else expected)
    assert not service.installed.pending_operations("skill-1")
    completed = service.installed.operation(pending["id"])
    assert completed["status"] == "done" and completed["payload"]["catalog_local_committed"]
    assert completed["payload"]["catalog_files"]["phase"] == "files_committed"
    assert len(service.installed._read()["operations"]) == 1


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_generic_first_confirmation_commits_under_the_ordinary_hold(action, request, tmp_path):
    adapter, service, _client, _target, _review, confirmation = confirmation_case(
        action, request, tmp_path)
    response = adapter.confirm(confirmation)
    assert response["ok"], response
    operation = response["data"]
    assert operation["state"] == "completed" and operation["outcome"] == OUTCOMES[action]
    api = adapter.mutations
    assert ("enter", operation["operation_id"]) in api.calls
    assert not [call for call in api.calls if call[0] == "recover"] and not api.decisions
    assert service.installed.operation(operation["operation_id"])["status"] == "done"
