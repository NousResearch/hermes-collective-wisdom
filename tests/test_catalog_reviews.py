"""Exact generic review authority wrapping Wisdom's real native receipts."""
# pylint: disable=missing-function-docstring,import-outside-toplevel,protected-access
# pylint: disable=duplicate-code,redefined-outer-name,too-few-public-methods

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from installed_test_support import installed_store
from test_install_review import Client, Host
from test_skill_catalog import request

from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.install_review import InstallReviewer


@pytest.fixture
def reviews(tmp_path):
    from hermes_wisdom_plugin.catalog_provider import CatalogProvider
    from hermes_wisdom_plugin.install_execution import InstallExecutor
    from test_catalog_mutations import Permits

    owner = {"profile_name": "default", "profile_home": str(tmp_path), "account_id": "account-1",
             "organization_id": "org-1", "base_url": "https://gateway.example",
             "installation_id": "install-1"}
    store = installed_store(tmp_path / "state")
    client, host = Client(), Host(tmp_path)
    receipt = InstallReceipts(store, lambda: dict(owner))
    reviewer = InstallReviewer(client, store, receipt, host, lambda: dict(owner))
    executor = InstallExecutor(client, store, receipt, reviewer, host, lambda: dict(owner))
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                              _installation_services=lambda: (reviewer, executor))

    @contextmanager
    def services():
        yield service

    clock = [1000.0]
    adapter = CatalogProvider(services, "collective-wisdom", host, clock=lambda: clock[0],
                              mutations=Permits("_wisdom/org-1/safe-skill"))
    return adapter, service, reviewer, host, client, clock


def test_exact_findings_are_stable_and_bound_to_saved_native_plan(reviews):
    adapter, service, reviewer, host, _client, _clock = reviews
    host.scan_allowed = False
    host.findings = [{"severity": "high", "category": "network", "file": "SKILL.md",
                      "line": 4, "description": "Uses a credential to call the requested API"}]
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    assert review["confirmable"] and review["scan"]["policy"] == "acknowledge"
    assert len(review["acknowledgements_required"]) == 1
    finding = review["scan"]["findings"][0]
    assert finding["description"] == host.findings[0]["description"]
    assert finding["id"] == review["acknowledgements_required"][0]
    record = adapter.record(service, review["review_token"])
    assert record["plan_digest"]
    assert (reviewer.receipts.load(record["native_receipt_id"])
        ["policy"]["warning_acceptance"]["accepted"])
    assert not service.installed.pending_operations()
    for acknowledgements in ([], [finding["id"], finding["id"]], ["wrong"]):
        result = adapter.authorize(service, record, acknowledgements)
        assert result["error"]["code"] == "acknowledgement_required"
    assert adapter.authorize(service, record, [finding["id"]]) is None


def test_generic_review_has_fifteen_minute_expiry_and_owner_binding(reviews):
    adapter, service, _reviewer, _host, _client, clock = reviews
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    assert review["expires_at_ms"] == 1900000
    clock[0] = 1900
    result = adapter.reviewed(request(review_token=review["review_token"]))
    assert result["error"]["code"] == "review_expired"
    original = service.native_owner()
    service.native_owner = lambda: {**original, "account_id": "another-user"}
    result = adapter.reviewed(request(review_token=review["review_token"]))
    assert result["error"]["code"] == "sign_in_required"


def test_changed_native_receipt_cannot_reuse_generic_acknowledgement(reviews):
    adapter, service, _reviewer, _host, _client, _clock = reviews
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    record = adapter.record(service, review["review_token"])
    state = service.installed._read()
    key = "install_receipt:" + record["native_receipt_id"]
    state["reviews"][key]["plan"]["files"][0]["hash"] = "changed"
    service.installed.state.set("installed-core", state)
    result = adapter.authorize(service, record, [])
    assert result["error"]["code"] == "review_changed"


def test_findings_and_file_plan_match_frozen_wire_shape(reviews):
    import json
    from pathlib import Path

    adapter, _service, _reviewer, _host, _client, _clock = reviews
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    payload = Path(__file__).parent / "fixtures/catalog-review-install.json"
    frozen = json.loads(payload.read_text(encoding="utf-8"))
    assert set(review) == set(frozen)
    assert all(set(row) == set(frozen["files"][0]) for row in review["files"])
    assert set(review["scan"]) == set(frozen["scan"])
    assert review["target_relative_path"].startswith("_wisdom/")


def _confirmation(review, *, acknowledgements=None, admission=True):
    return request(action=review["action"], id="skill-1", review_token=review["review_token"],
                   acknowledgements=acknowledgements or [], admission={
                       "registration_id": "registration_001", "generation": "generation_fixture_01",
                       "review_id": review["review_id"], "reservation_id": "reservation_0001",
                       "expected_path_revision": 0, "expected_installation_revision": None,
                   } if admission else None)


def test_confirm_install_and_expired_replay_use_one_operation(reviews):
    adapter, service, _reviewer, _host, client, clock = reviews
    client.record_install = lambda **_values: SimpleNamespace(effective_update_mode="MANUAL")
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    adapter.mutations.target = review["target_relative_path"]
    result = adapter.confirm(_confirmation(review))
    assert result["ok"], result
    operation = result["data"]
    assert operation["state"] == "completed" and operation["outcome"] == "installed"
    assert (adapter.reviewed(request(review_token=review["review_token"]))["data"]["operation"]
        == operation)
    assert adapter.operation(request(operation_id=operation["operation_id"]))["data"] == operation
    calls = list(adapter.mutations.calls)
    clock[0] += 10000
    assert adapter.confirm(_confirmation(review, admission=False))["data"] == operation
    assert adapter.mutations.calls == calls
    assert len(service.installed._read()["operations"]) == 1


def test_confirm_rejects_acknowledgement_drift_before_mutation(reviews):
    adapter, service, _reviewer, host, _client, _clock = reviews
    host.scan_allowed = False
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    result = adapter.confirm(_confirmation(review))
    assert result["error"]["code"] == "acknowledgement_required"
    assert not service.installed.pending_operations() and not adapter.mutations.calls


@pytest.mark.parametrize("values", [
    {"action": None}, {"acknowledgements": None}, {"acknowledgements": [True]},
    {"admission": {"expected_path_revision": True}},
    {"admission": {"generation": 17}}, {"admission": {"surprise": "unknown"}},
])
def test_confirm_shape_rejected_before_opening_services(reviews, values):
    adapter, _service, _reviewer, _host, _client, _clock = reviews
    review = adapter.review(request(action="install", id="skill-1", version="1"))["data"]
    message = _confirmation(review)
    if isinstance(values.get("admission"), dict):
        values = {"admission": {**message["admission"], **values["admission"]}}
    message.update(values)

    def forbidden():
        pytest.fail("invalid request opened scoped service")

    adapter.services = forbidden
    assert adapter.confirm(message)["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("version", ["1" * 65, True, "0", "01", "", 1])
def test_review_version_rejected_before_opening_services(reviews, version):
    adapter, _service, _reviewer, _host, _client, _clock = reviews

    def forbidden():
        pytest.fail("invalid version opened scoped service")

    adapter.services = forbidden
    assert adapter.review(request(action="install", id="skill-1", version=version))[
        "error"]["code"] == "invalid_request"


def test_changed_managed_bytes_map_to_safe_review_changed(reviews):
    from hermes_wisdom_plugin.package import PackagePolicyError
    from hermes_wisdom_plugin.errors import WisdomConflict

    adapter, _service, reviewer, _host, _client, _clock = reviews
    for error in (PackagePolicyError("sensitive private path"), WisdomConflict("private state")):
        def changed(*_a, failure=error, **_kw):
            raise failure
        reviewer.plan = changed
        response = adapter.review(request(action="install", id="skill-1", version="1"))
        assert response["error"]["code"] == "review_changed"
        assert "private" not in response["error"]["message"]


def test_projection_normalizes_host_allow_verdict_to_catalog_safe():
    from pathlib import Path
    from hermes_wisdom_plugin.catalog_reviews import project_review

    review = project_review({
        "owner": {"organization_id": "org"}, "skill_id": "skill", "slug": "skill",
        "version": 1, "content_hash": "hash", "local_scan": {"verdict": "allow"},
    }, "install", "source", SimpleNamespace(skills_root=Path("/skills")),
        review_id="review", token="token", now=1)
    assert review["scan"]["verdict"] == "safe"


def test_sensitive_expansion_is_an_exact_required_finding():
    from hermes_wisdom_plugin.catalog_reviews import findings

    rows = findings({"sensitive_expansion": ["network: api.example"]})
    assert len(rows) == 1 and rows[0]["acknowledgement_required"]
    assert rows[0]["description"] == "network: api.example"
