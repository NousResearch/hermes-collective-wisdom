"""Team update discovery delegates to the authoritative existing checker, not reviews."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name,unused-import,duplicate-code,missing-class-docstring
from contextlib import contextmanager

import pytest
from test_update_execution import executor as update_executor
from test_skill_catalog import request
from hermes_wisdom_plugin.catalog_provider import CatalogProvider
from hermes_wisdom_plugin.update_review import UpdateReviewer
from hermes_wisdom_plugin.errors import WisdomUnavailable


@pytest.mark.parametrize("state,expected", [("current", "current"),
    ("update_available", "update_available"), ("removed", "removed"),
    ("unverifiable", "unavailable"), ("taken_down", "blocked"), ("archived", "blocked")])
def test_check_maps_authoritative_status_without_preparing_a_review(
    update_executor, state, expected,
):
    executor, store, _client, _receipts, _plan, _target, owner = update_executor
    calls = []
    class Service:
        installed = store
        def native_owner(self):
            return dict(owner)
        def check_update(self, skill_id):
            calls.append(skill_id)
            return {"state": state, "installed_version": 1, "latest_version": 2}
    @contextmanager
    def services():
        yield Service()
    provider = CatalogProvider(services, "collective-wisdom", executor.host)
    result = provider.check(request(ids=["skill-1"], cursor=None))
    assert result["ok"], result
    assert result["data"] == {"items": [{"identifier": provider.source + ":skill-1",
        "installed_version": "1", "latest_version": "2", "state": expected,
        "installation_revision": provider.installation(store.installation("skill-1"), owner)[
            "installation_revision"]}], "next_cursor": None}
    assert calls == ["skill-1"]
    assert not store.pending_operations()


def test_check_uses_real_existing_checker(update_executor):
    from types import SimpleNamespace
    executor, store, client, receipts, _plan, _target, owner = update_executor
    checker = UpdateReviewer(client, store, receipts, executor.host, lambda: dict(owner))
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                              check_update=checker.check)
    @contextmanager
    def services():
        yield service
    provider = CatalogProvider(services, "collective-wisdom", executor.host)
    result = provider.check(request(ids=["skill-1"]))
    assert result["ok"] and result["data"]["items"][0]["state"] == "update_available"
    assert result["data"]["items"][0]["latest_version"] == "2"
    assert not client.record_calls and not store.pending_operations()


@pytest.mark.parametrize("ids", [["unowned-id"], ["skill-1", "skill-1"], [None], "skill-1"])
def test_check_rejects_unowned_or_malformed_ids_without_remote_read(update_executor, ids):
    from types import SimpleNamespace
    executor, store, _client, _receipts, _plan, _target, owner = update_executor
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                              check_update=lambda *_a: pytest.fail("remote read"))
    @contextmanager
    def services():
        yield service
    result = CatalogProvider(services, "collective-wisdom", executor.host).check(request(ids=ids))
    assert result["ok"] is False
