"""Durable uninstall authority, exact target binding and completed replay."""
# pylint: disable=missing-function-docstring,protected-access,duplicate-code
# pylint: disable=import-outside-toplevel

from pathlib import Path

import pytest
from installed_test_support import installed_store
from test_install_execution import BASELINE, FILES, OWNER, Client, Host, Reviewer

from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.package import PackagePolicyError


def _installed(tmp_path):
    owner = {**OWNER, "profile_home": str(tmp_path)}
    store = installed_store(tmp_path / "state")
    receipts = InstallReceipts(store, lambda: dict(owner))
    host = Host(tmp_path / "skills")
    client = Client()
    executor = InstallExecutor(client, store, receipts, Reviewer(), host, lambda: dict(owner))
    target = executor._target(owner, "managed-skill")
    target.mkdir(parents=True)
    for name, mode, body in FILES:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o700 if mode == "exec" else 0o600)
    store.record_install({
        "skill_id": "skill-1", "slug": "managed-skill", "org_id": owner["organization_id"],
        "version": 2, "content_hash": "sha256:content", "baseline": BASELINE,
        "target_path": str(target), "owner": owner, "state": "active", "update_mode": "MANUAL",
    })
    return executor, store, client, owner, target


def test_durable_uninstall_review_survives_restart_and_replays_after_target_reuse(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, client, owner, target = _installed(tmp_path)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    reviewer = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner))
    plan = reviewer.plan("skill-1")
    assert target.is_dir() and not store.pending_operations()
    restarted = InstallExecutor(client, store, executor.receipts, executor.reviewer,
                                executor.host, lambda: dict(owner))
    result = restarted.uninstall_reviewed(plan["receipt"])
    assert result["uninstalled"] is True and not target.exists()
    target.mkdir()
    (target / "SKILL.md").write_text("replacement, not this provider's files")
    assert restarted.uninstall_reviewed(plan["receipt"]) == result
    assert (target / "SKILL.md").read_text() == "replacement, not this provider's files"
    assert len(client.deactivate_calls) == 1
    assert len(store._read()["operations"]) == 1


def test_uninstall_review_refuses_changed_tree_without_starting_operation(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, _client, owner, target = _installed(tmp_path)
    reviewer = UninstallReviewer(store, UninstallReceipts(store, lambda: dict(owner)),
                                 executor.host, lambda: dict(owner))
    plan = reviewer.plan("skill-1")
    (target / "SKILL.md").write_text("edited after review")
    with pytest.raises(PackagePolicyError, match="changed"):
        executor.uninstall_reviewed(plan["receipt"])
    assert (target / "SKILL.md").read_text() == "edited after review"
    assert not store.pending_operations()


def test_uninstall_review_binds_modified_tree_and_removed_outside_state(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, _client, owner, target = _installed(tmp_path)
    (target / "SKILL.md").write_text("edited before review")
    reviewer = UninstallReviewer(store, UninstallReceipts(store, lambda: dict(owner)),
                                 executor.host, lambda: dict(owner))
    plan = reviewer.plan("skill-1")
    assert plan["local_snapshot"] != BASELINE
    outcome = executor.uninstall_reviewed(plan["receipt"])
    archived = Path(outcome["recoverable_path"])
    assert (archived / "SKILL.md").read_text() == "edited before review"


def test_uninstall_review_refuses_inner_symlink_without_persisting_authority(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, _client, owner, target = _installed(tmp_path)
    (target / "SKILL.md").unlink()
    (target / "SKILL.md").symlink_to(target.parent / "outside")
    receipts = UninstallReceipts(store, lambda: dict(owner))
    with pytest.raises(PackagePolicyError, match="unsafe"):
        UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    assert not store._read()["reviews"]
    assert target.is_dir()


def test_uninstall_recovers_terminal_journal_when_receipt_completion_was_lost(
    tmp_path, monkeypatch,
):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, client, owner, target = _installed(tmp_path)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    original = receipts.complete

    def crash(_receipt, _outcome):
        raise OSError("interrupted receipt completion")

    monkeypatch.setattr(receipts, "complete", crash)
    with pytest.raises(OSError, match="interrupted"):
        executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    target.mkdir()
    (target / "SKILL.md").write_text("new owner")
    monkeypatch.setattr(receipts, "complete", original)
    assert executor.uninstall_reviewed(plan["receipt"], receipts=receipts)["uninstalled"]
    assert (target / "SKILL.md").read_text() == "new owner"
    assert len(client.deactivate_calls) == 1


def test_uninstall_review_refuses_expired_unstarted_authority(tmp_path):
    from hermes_wisdom_plugin.errors import WisdomConflict
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, _client, owner, target = _installed(tmp_path)
    now = [1000.0]
    receipts = UninstallReceipts(store, lambda: dict(owner), clock=lambda: now[0])
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    assert plan["expires_at"] - plan["created_at"] == 900
    now[0] += 900
    with pytest.raises(WisdomConflict, match="expired"):
        executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert target.is_dir() and not store.pending_operations()


def test_uninstall_review_completes_missing_tree_without_touching_replacement(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, client, owner, target = _installed(tmp_path)
    backup = target.with_name("outside-wisdom")
    target.rename(backup)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    assert plan["local_snapshot"] is None
    outcome = executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert outcome["uninstalled"] and outcome["already_removed"]
    assert backup.is_dir() and len(client.deactivate_calls) == 1


def test_uninstall_review_cannot_be_used_by_another_enrolment(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, client, owner, target = _installed(tmp_path)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    owner["installation_id"] = "different-enrolment"
    with pytest.raises(WisdomAuthError, match="enrolment"):
        executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert target.is_dir() and not client.deactivate_calls and not store.pending_operations()


def test_uninstall_review_expiry_does_not_prevent_started_recovery(tmp_path):
    from hermes_wisdom_plugin.uninstall_review import UninstallReviewer, UninstallReceipts

    executor, store, client, owner, target = _installed(tmp_path)
    now = [1000.0]
    receipts = UninstallReceipts(store, lambda: dict(owner), clock=lambda: now[0])
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
    client.deactivate_error = RuntimeError("response lost")
    result = executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    assert result["pending"] and not target.exists()
    now[0] += 100000
    client.deactivate_error = None
    client.installation_rows = []
    assert executor.uninstall_reviewed(plan["receipt"], receipts=receipts)["uninstalled"]
    assert len(client.deactivate_calls) == 1
