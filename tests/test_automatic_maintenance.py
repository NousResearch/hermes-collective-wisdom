"""Policy-authorized automatic update and maintenance producer journeys."""
# pylint: disable=missing-function-docstring,too-few-public-methods,duplicate-code
# pylint: disable=wrong-import-order,protected-access,missing-class-docstring

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_wisdom_plugin.automatic_updates import AutomaticUpdateMaintenance
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.consumption import WisdomConsumption
from hermes_wisdom_plugin.contract import org_directory_name
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import UpdateReviewer
from test_update_execution import OWNER, RealClient, _hash
from test_update_review import Host


def _automatic(tmp_path, mode="AUTO_WITH_NOTICE"):
    owner = {**OWNER, "profile_home": str(tmp_path / "profile")}
    store = WisdomStore(tmp_path / "state")
    store.activate_installation_identity(owner["installation_id"], "org-1")
    host = Host(tmp_path / "profile")
    client = RealClient()
    client.remote["update_mode"] = mode
    client.committed_mode = mode
    target = (
        host.skills_root / "_wisdom" / org_directory_name(owner["organization_id"])
        / "managed-skill"
    )
    target.mkdir(parents=True)
    for name, _file_mode, body in client.files[1]:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    baseline = {name: _hash(body) for name, _file_mode, body in client.files[1]}
    store.record_install({
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "managed-skill", "version": 1,
        "content_hash": client._content(client.files[1]).content_hash,
        "baseline": baseline, "target_path": str(target),
        "update_mode": mode, "state": "active", "owner": owner,
    })
    store.bind_feed_owner(
        owner["organization_id"], owner["installation_id"],
        owner["account_id"], owner["base_url"],
    )
    receipts = UpdateReceipts(store, lambda: dict(owner))
    reviewer = UpdateReviewer(client, store, receipts, host, lambda: dict(owner))
    executor = UpdateExecutor(client, store, receipts, reviewer, host, lambda: dict(owner))
    return AutomaticUpdateMaintenance(
        store, reviewer, executor, lambda: dict(owner), limit=8
    ), reviewer, executor, store, target


def _local_notifications(store, owner):
    client = argparse.Namespace(
        display_org_id=owner["organization_id"],
        owner_user_id=owner["account_id"],
        base_url=owner["base_url"],
    )
    return WisdomConsumption(store, client).notifications()


def test_clean_auto_with_notice_uses_policy_provenance_and_applies_once(tmp_path):
    maintenance, _reviewer_value, executor, store, target = _automatic(tmp_path)

    first = maintenance.run()
    second = maintenance.run()

    assert first["results"][0]["state"] == "updated"
    result = first["results"][0]["result"]
    assert result["updated"] is True and result["effective_update_mode"] == "AUTO_WITH_NOTICE"
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"
    operation = store.operation(first["results"][0]["operation_id"])
    assert operation["payload"]["authorization"] == {
        "kind": "automatic_policy", "mode": "AUTO_WITH_NOTICE"
    }
    assert operation["payload"]["user_confirmation"] is False
    operation_owner = operation["payload"]["owner"]
    notices = _local_notifications(store, operation_owner)
    event_id = "automatic-update:" + first["results"][0]["operation_id"]
    assert notices["events"] == [{
        "event_id": event_id,
        "source_event_ids": [event_id],
        "category": "updated", "kind": "installation_updated",
        "skill_id": "skill-1", "skill_name": "managed-skill", "version": 2,
        "occurred_at": operation["updated_at"],
    }]
    assert second["results"][0]["state"] == "current"
    assert len(executor.client.record_calls) == 1


def test_repeated_bounded_passes_reach_ninth_installation(tmp_path):
    maintenance, reviewer, _executor, store, _target = _automatic(
        tmp_path, mode="MANUAL"
    )
    owner = reviewer.install_reviewer._owner_snapshot()
    original = store.installation("skill-1", owner=owner)
    assert original is not None
    for index in range(2, 10):
        store.record_install({
            **original,
            "skill_id": f"skill-{index}",
            "slug": f"managed-{index:02d}",
            "target_path": str(tmp_path / f"managed-{index:02d}"),
        })
    checked = []

    def current(skill_id):
        checked.append(skill_id)
        return {"state": "current"}

    reviewer.check = current

    first = maintenance.run()
    second = maintenance.run()

    assert first["processed"] == second["processed"] == 8
    assert first["more"] is second["more"] is True
    assert set(checked) == {f"skill-{index}" for index in range(1, 10)}
    assert "skill-1" not in checked[:8]
    assert "skill-1" in checked[8:]


def test_completed_update_repairs_local_notice_after_notice_write_failure(
    tmp_path, monkeypatch
):
    maintenance, _reviewer, executor, store, _target = _automatic(tmp_path)
    persist = store.persist_automatic_update_notice
    attempts = 0

    def fail_once(operation_id, owner):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic notice commit loss")
        return persist(operation_id, owner)

    monkeypatch.setattr(store, "persist_automatic_update_notice", fail_once)

    first = maintenance.run()["results"][0]
    assert first["state"] == "updated"
    assert first["notice_error"] == "OSError"
    assert _local_notifications(store, executor.owner())["events"] == []

    second = maintenance.run()

    events = _local_notifications(store, executor.owner())["events"]
    assert second["results"][0]["state"] == "current"
    assert [event["event_id"] for event in events] == [
        "automatic-update:" + first["operation_id"]
    ]
    assert len(executor.client.record_calls) == 1


def test_manual_mode_and_modified_auto_with_notice_never_mutate(tmp_path):
    maintenance, reviewer, _executor, store, target = _automatic(tmp_path, mode="MANUAL")
    before = target.joinpath("SKILL.md").read_bytes()
    manual = maintenance.run()
    assert manual["results"][0]["state"] == "needs_review"
    assert target.joinpath("SKILL.md").read_bytes() == before
    assert store.pending_operations() == []

    reviewer.client.remote["update_mode"] = "AUTO_WITH_NOTICE"
    target.joinpath("SKILL.md").write_bytes(b"# edited locally\n")
    modified = maintenance.run()
    assert modified["results"][0]["state"] == "needs_review"
    assert target.joinpath("SKILL.md").read_bytes() == b"# edited locally\n"
    assert store.pending_operations() == []


def test_modified_required_preserves_exact_bytes_before_promotion(tmp_path):
    maintenance, _reviewer_value, _executor, _store, target = _automatic(
        tmp_path, mode="REQUIRED"
    )
    edited = b"# exact precious edits\n"
    target.joinpath("SKILL.md").write_bytes(edited)

    result = maintenance.run()["results"][0]["result"]

    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"
    preserved = Path(result["preserved_copy"])
    assert preserved.joinpath("SKILL.md").read_bytes() == edited
    assert Path(result["recovery_path"]).joinpath("SKILL.md").read_bytes() == edited


def test_automatic_receipt_cannot_enter_manual_apply(tmp_path):
    maintenance, reviewer, executor, _store, _target = _automatic(tmp_path)
    plan = reviewer.plan_automatic("skill-1")
    with pytest.raises(PackagePolicyError, match="automatic policy"):
        executor.apply(plan["receipt"])
    assert maintenance.run()["results"][0]["state"] == "updated"


def test_stale_automatic_authority_never_mutates(tmp_path):
    _maintenance, reviewer, executor, _store, target = _automatic(tmp_path)
    before = target.joinpath("SKILL.md").read_bytes()
    plan = reviewer.plan_automatic("skill-1")
    reviewer.client.remote["update_mode"] = "MANUAL"
    reviewer.client.committed_mode = "MANUAL"

    with pytest.raises(PackagePolicyError, match="authority or policy changed"):
        executor.apply_automatic(plan["receipt"])

    assert target.joinpath("SKILL.md").read_bytes() == before
    assert executor.client.record_calls == []


def test_automatic_operation_reenters_after_post_promotion_crash(tmp_path):
    maintenance, _reviewer_value, executor, store, target = _automatic(tmp_path)
    advance = store.advance
    interrupted = False

    def interrupt(operation_id, phase, *, done=False):
        nonlocal interrupted
        if phase == "files_committed" and not interrupted:
            interrupted = True
            raise OSError("synthetic post-promotion interruption")
        return advance(operation_id, phase, done=done)

    store.advance = interrupt
    first = maintenance.run()["results"][0]
    assert first["state"] == "pending"
    assert target.joinpath("SKILL.md").read_bytes() == b"# Managed v2\n"

    store.advance = advance
    second = maintenance.run()["results"][0]
    assert second["state"] == "updated"
    assert len(executor.client.record_calls) == 1


def test_public_maintenance_command_requires_explicit_automatic_flag():
    parser = argparse.ArgumentParser()
    build_parser(parser)

    passive = parser.parse_args(["maintenance", "--json"])
    active = parser.parse_args(["maintenance", "--apply-automatic", "--json"])

    assert passive.apply_automatic is False
    assert active.apply_automatic is True


def test_public_maintenance_command_reaches_real_producer_switch(capsys):
    parser = argparse.ArgumentParser()
    build_parser(parser)

    class Service:
        called = None

        def maintenance(self, **values):
            self.called = values
            return {"automatic_updates": {"processed": 1}, "mediation": {"queued": 1}}

    service = Service()
    code = run_cli(
        parser.parse_args(["maintenance", "--apply-automatic", "--json"]), service
    )

    assert code == 0
    assert service.called == {"refresh_policy": False, "apply_automatic": True}
    assert '"processed": 1' in capsys.readouterr().out
