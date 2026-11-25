"""Compatibility gaps require exact consent independently of scanner approval."""
# pylint: disable=missing-function-docstring,duplicate-code,protected-access
# pylint: disable=redefined-outer-name,unused-import
from types import SimpleNamespace

import pytest
from review_test_support import _content, _files, _spec
from test_catalog_reviews import _confirmation, reviews
from test_skill_catalog import request

from hermes_wisdom_plugin.catalog_reviews import findings
from hermes_wisdom_plugin.contract import sha256_address


@pytest.mark.parametrize("outcome", ["partial", "compatible_after_setup"])
@pytest.mark.parametrize("details", [False, True])
def test_every_compatibility_gap_has_a_required_bound_row(outcome, details):
    compatibility = {"outcome": outcome, "setup_actions": ["enable tool ffmpeg"] if details else [],
                     "limitations": ["limited output"] if details else []}
    rows = findings({"compatibility": compatibility})
    assert rows and all(row["acknowledgement_required"] for row in rows)
    assert all(row["category"] == "compatibility" for row in rows)
    if details:
        assert "enable tool ffmpeg" in str(rows) and "limited output" in str(rows)
    changed = findings({"compatibility": {**compatibility, "setup_actions": ["enable tool other"]}})
    assert {row["id"] for row in rows} != {row["id"] for row in changed}


def _gap_review(case, outcome):
    adapter, _service, _reviewer, _host, client, _clock = case
    client.spec = _spec(**({"tools": [{"name": "ffmpeg"}]} if outcome == "compatible_after_setup"
                          else {"known_limitations": ["limited output"]}))
    client.files = _files(client.spec)
    content = _content(client.files)
    manifest = next(body for path, _, body in client.files if path == "skill.manifest.json")
    client.version_value.update(system_spec=client.spec.model_dump(mode="json"),
                                content_hash=content.content_hash, commit=content.commit,
                                package_manifest_hash=sha256_address(manifest))
    client.record_install = lambda **_kw: SimpleNamespace(effective_update_mode="MANUAL")
    response = adapter.review(request(action="install", id="skill-1", version="1"))
    assert response["ok"], response
    review = response["data"]
    adapter.mutations.target = review["target_relative_path"]
    return review


@pytest.mark.parametrize("outcome", ["partial", "compatible_after_setup"])
def test_catalog_gap_confirm_refuses_without_acknowledgement_then_installs(reviews, outcome):
    adapter, service, _reviewer, host, _client, _clock = reviews
    review = _gap_review(reviews, outcome)
    refused = adapter.confirm(_confirmation(review))
    assert refused["error"]["code"] == "acknowledgement_required"
    assert not service.installed.pending_operations() and not adapter.mutations.calls
    assert not (host.skills_root / review["target_relative_path"]).exists()
    accepted = adapter.confirm(_confirmation(
        review, acknowledgements=review["acknowledgements_required"]))
    assert accepted["ok"], accepted
    assert accepted["data"]["outcome"] == "installed"


@pytest.mark.parametrize("outcome", ["partial", "compatible_after_setup"])
def test_catalog_gap_without_projected_consent_cannot_bypass_native_gate(reviews, outcome):
    adapter, service, _reviewer, host, _client, _clock = reviews
    review = _gap_review(reviews, outcome)
    # A retained pre-fix review has no compatibility acknowledgement. It must not
    # acquire accept_partial merely because scanner/other acknowledgements passed.
    record = adapter.record(service, review["review_token"])
    record["review"]["scan"]["findings"] = []
    record["review"]["acknowledgements_required"] = []
    state = service.installed._read()
    state["reviews"]["catalog_review:" + review["review_token"]] = record
    service.installed.state.set("installed-core", state)
    refused = adapter.confirm(_confirmation(review))
    assert refused["ok"] is False, refused
    assert not service.installed.pending_operations() and not adapter.mutations.calls
    assert not (host.skills_root / review["target_relative_path"]).exists()
