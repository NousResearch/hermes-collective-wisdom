"""Generic update/removal replay, concurrent confirmation and lost settlement."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name,unused-import,too-many-locals,duplicate-code
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_catalog_mutations import Permits
from test_catalog_reviews import _confirmation
from test_skill_catalog import request
from test_update_execution import executor as update_executor


@pytest.fixture(params=["update", "uninstall"])
def operation_case(request, tmp_path):
    from hermes_wisdom_plugin.catalog_provider import CatalogProvider
    from test_uninstall_review import _installed

    action = request.param
    if action == "update":
        executor, store, client, receipts, plan, target, owner = request.getfixturevalue(
            "update_executor")
        reviewer = SimpleNamespace(receipts=receipts, plan=lambda *_a, **_kw: dict(plan))
    else:
        executor, store, client, owner, target = _installed(tmp_path)
        reviewer = None
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                              _installation_services=lambda: (reviewer, executor),
                              _update_services=lambda: (reviewer, executor))

    @contextmanager
    def services():
        yield service

    api = Permits(target.relative_to(executor.host.skills_root).as_posix())
    adapter = CatalogProvider(services, "collective-wisdom", executor.host, mutations=api)
    response = adapter.review(request_message(action=action))
    assert response["ok"], response
    review = response["data"]
    confirmation = _confirmation(review)
    confirmation["admission"]["expected_installation_revision"] = review["installation_revision"]
    return adapter, service, client, target, review, confirmation


def request_message(**values):
    return request(id="skill-1", version=None, **values)


def test_edited_update_refuses_with_guidance_without_a_second_target(operation_case):
    adapter, service, client, target, review, confirmation = operation_case
    if review["action"] != "update":
        return  # Removal of edited trees remains a separate reviewed action.
    before = service.installed.installation("skill-1")
    (target / "SKILL.md").write_text("my local edits")
    response = adapter.confirm(confirmation)
    assert response["ok"], response
    result = response["data"]
    assert result["state"] == "failed" and result["outcome"] == "refused"
    assert result["message"] == (
        f"Local edits present at {review['target_relative_path']}. "
        "Archive or rename it in Skills, then update."
    )
    assert (target / "SKILL.md").read_text() == "my local edits"
    assert service.installed.installation("skill-1") == before
    assert not client.record_calls
    assert not any(name == "enter" for name, _value in adapter.mutations.calls)
    assert adapter.confirm(confirmation) == response


def test_generic_completed_replay_survives_restart_and_target_reuse(operation_case):
    from hermes_wisdom_plugin.catalog_provider import CatalogProvider

    adapter, service, _client, target, review, confirmation = operation_case
    result = adapter.confirm(confirmation)
    assert result["ok"], result
    operation = result["data"]
    assert operation["state"] == "completed"
    if not target.exists():
        target.mkdir()
    (target / "SKILL.md").write_text("reused ordinary local bytes")
    restarted = CatalogProvider(adapter.services, adapter.plugin_key, adapter.host,
                                mutations=adapter.mutations, clock=lambda: 99999999999)
    assert restarted.confirm({**confirmation, "admission": None}) == result
    assert restarted.operation(request(operation_id=operation["operation_id"])) == result
    assert (restarted.reviewed(request(review_token=review["review_token"]))["data"]["kind"]
        == "completed")
    assert (target / "SKILL.md").read_text() == "reused ordinary local bytes"
    assert len(service.installed._read()["operations"]) == 1


def test_generic_concurrent_confirm_creates_one_operation(operation_case):
    adapter, service, _client, _target, _review, confirmation = operation_case
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(adapter.confirm, [confirmation, confirmation]))
    assert results[0]["ok"] and results[0] == results[1], results
    assert len(service.installed._read()["operations"]) == 1
    assert len([call for call in adapter.mutations.calls if call[0] == "enter"]) == 1


def test_generic_lost_terminal_settlement_preserves_read_only_recovery(operation_case, monkeypatch):
    adapter, service, _client, target, review, confirmation = operation_case

    def lost(_request):
        raise RuntimeError("lost settlement response")

    monkeypatch.setattr(adapter.mutations, "settle", lost)
    assert not adapter.confirm(confirmation)["ok"]
    row = service.installed._read()["operations"].popitem()[1]
    assert row["status"] == "done"
    retained = service.installed.review("catalog_operation:" + row["id"])
    assert retained["state"] == "completed"
    before = list(adapter.mutations.calls)
    if target.exists():
        (target / "SKILL.md").write_text("new local bytes")
    recovered = adapter.confirm({**confirmation, "admission": None})
    assert recovered == {"ok": True, "data": retained}
    assert adapter.mutations.calls == before
    assert (adapter.reviewed(request(review_token=review["review_token"]))["data"]["operation"]
        == retained)
    assert Path(adapter.host.skills_root).is_dir()
