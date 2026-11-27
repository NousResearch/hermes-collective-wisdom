"""Catalog updates require the same compatibility consent as native updates."""
# pylint: disable=missing-function-docstring,import-outside-toplevel,protected-access
# pylint: disable=duplicate-code,redefined-outer-name
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from installed_test_support import installed_store
from review_test_support import _spec
from test_catalog_mutations import Permits
from test_catalog_reviews import _confirmation
from test_skill_catalog import request
from test_update_review import Client, _reviewer

from hermes_wisdom_plugin.catalog_provider import CatalogProvider
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts


@pytest.fixture
def update(tmp_path, monkeypatch):
    client = Client()
    reviewer, fixture_store, target = _reviewer(tmp_path, client=client)
    store = installed_store(tmp_path / "state")
    store.record_install(fixture_store.record)
    receipts = UpdateReceipts(store, reviewer.owner)
    reviewer.store, reviewer.receipts = store, receipts
    reviewer.install_reviewer.store = store

    def record_install(**values):
        client.remote["installed_version"] = values["version"]
        return SimpleNamespace(effective_update_mode="MANUAL")

    monkeypatch.setattr(client, "record_install", record_install, raising=False)
    executor = UpdateExecutor(client, store, receipts, reviewer, reviewer.host, reviewer.owner)

    @contextmanager
    def services():
        yield SimpleNamespace(installed=store, native_owner=reviewer.owner,
                              _update_services=lambda: (reviewer, executor))

    api = Permits(target.relative_to(reviewer.host.skills_root).as_posix())
    adapter = CatalogProvider(services, "collective-wisdom", reviewer.host, mutations=api)
    return adapter, reviewer, executor, store, target, client, api


def _review(adapter):
    response = adapter.review(request(action="update", id="skill-1", version="2"))
    assert response["ok"], response
    review = response["data"]
    confirm = _confirmation(review)
    confirm["admission"]["expected_installation_revision"] = review["installation_revision"]
    return review, confirm


@pytest.mark.parametrize("needs_setup", [False, True])
def test_update_review_discloses_setup_and_requires_acknowledgement(update, needs_setup):
    adapter, reviewer, executor, store, target, client, api = update
    if needs_setup:
        client.files[2] = client._version_two(_spec(tools=[{"name": "review-probe"}]))
        native = reviewer.plan("skill-1")
        assert native["compatibility"]["outcome"] == "compatible_after_setup"
        assert native["compatibility"]["limitations"] == []
        assert native["sensitive_expansion"] == []
        assert not native["allowed"]
        with pytest.raises(PackagePolicyError, match="requires a new explicit action"):
            executor.apply(native["receipt"])
    review, confirm = _review(adapter)
    if needs_setup:
        before = (target / "SKILL.md").read_bytes()
        result = adapter.confirm(confirm)
        assert not result["ok"], result
        assert result["error"]["code"] == "acknowledgement_required"
        assert not api.calls and (target / "SKILL.md").read_bytes() == before
        assert store.installation("skill-1")["version"] == 1
        assert client.remote["installed_version"] == 1
        assert not store.pending_operations()
        finding, = review["scan"]["findings"]
        assert finding["category"] == "compatibility" and finding["acknowledgement_required"]
        assert "enable tool review-probe" in finding["description"].splitlines()
        assert review["acknowledgements_required"] == [finding["id"]]
        confirm["acknowledgements"] = review["acknowledgements_required"]
    else:
        assert review["acknowledgements_required"] == []
    result = adapter.confirm(confirm)
    assert result["ok"], result
    assert result["data"]["outcome"] == "updated"
    assert store.installation("skill-1")["version"] == 2
    assert client.remote["installed_version"] == 2
    assert (target / "SKILL.md").read_bytes() == b"# Managed v2\n"


@pytest.mark.parametrize("outcome", ["partial", "compatible_after_setup"])
def test_update_without_projected_consent_cannot_use_preaccepted_native_plan(update, outcome):
    adapter, _reviewer_value, _executor, store, target, client, api = update
    client.files[2] = client._version_two(_spec(**(
        {"tools": [{"name": "review-probe"}]} if outcome == "compatible_after_setup"
        else {"known_limitations": ["limited output"]})))
    review, confirm = _review(adapter)
    # Retained pre-fix generic reviews did not disclose compatibility. Their
    # native preview receipts preaccepted partial updates, not human consent.
    state = store._read()
    record = state["reviews"]["catalog_review:" + review["review_token"]]
    record["review"]["scan"]["findings"] = []
    record["review"]["acknowledgements_required"] = []
    store.state.set("installed-core", state)
    before = (target / "SKILL.md").read_bytes()
    result = adapter.confirm(confirm)
    assert not result["ok"], result
    assert result["error"]["code"] == "acknowledgement_required"
    assert not api.calls and not store.pending_operations()
    assert store.installation("skill-1")["version"] == 1
    assert client.remote["installed_version"] == 1
    assert (target / "SKILL.md").read_bytes() == before
