"""Terminal receipt hardening beyond the reference file-only plan boundary."""

# pylint: disable=missing-function-docstring,redefined-outer-name

import json
import os

import pytest

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from hermes_wisdom_plugin.install_receipts import InstallReceipts, REVIEW_SECONDS
from hermes_wisdom_plugin.store import WisdomStore


@pytest.fixture
def receipt_state(tmp_path):
    store = WisdomStore(tmp_path / "state")
    owner = {"account_id": "member", "organization_id": "org", "installation_id": "hwi_one",
             "profile_name": "test", "profile_home": str(tmp_path),
             "base_url": "https://test.invalid"}
    now = [100.0]
    receipts = InstallReceipts(store, lambda: dict(owner), clock=lambda: now[0])
    return store, owner, now, receipts


def _plan(receipts):
    return receipts.create({"skill_id": "skill", "version": 1, "content_hash": "sha256:a"})


def test_review_expiry_and_fresh_recheck(receipt_state):
    _, _, now, receipts = receipt_state
    plan = _plan(receipts)
    assert receipts.load(plan["receipt"]) == plan
    now[0] += REVIEW_SECONDS
    with pytest.raises(WisdomConflict, match="expired"):
        receipts.load(plan["receipt"])
    renewed = _plan(receipts)
    assert renewed["receipt"] != plan["receipt"]
    assert renewed["expires_at"] > plan["expires_at"]


@pytest.mark.parametrize("key", [
    "account_id", "organization_id", "installation_id", "profile_home", "base_url",
])
def test_owner_change_denies_receipt(receipt_state, key):
    _, owner, _, receipts = receipt_state
    plan = _plan(receipts)
    owner[key] += "changed"
    with pytest.raises(WisdomAuthError):
        receipts.load(plan["receipt"], allow_started=True)


@pytest.mark.parametrize("identifier", ["../escape", "wip_../x", "wip_" + "a" * 31, "/tmp/x", None])
def test_invalid_identifier_denied_before_storage(receipt_state, identifier):
    _, _, _, receipts = receipt_state
    with pytest.raises(WisdomValidationError):
        receipts.load(identifier)


def test_plan_tampering_is_not_authority(receipt_state):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)
    path = store.root / "plans" / (plan["receipt"] + ".json")
    value = json.loads(path.read_bytes())
    value["version"] = 2
    path.write_text(json.dumps(value))
    with pytest.raises(WisdomValidationError, match="changed"):
        receipts.load(plan["receipt"])


@pytest.mark.parametrize("redirect", ["file", "directory", "hardlink"])
def test_redirected_storage_denied(receipt_state, tmp_path, redirect):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)
    path = store.root / "plans" / (plan["receipt"] + ".json")
    if redirect == "directory":
        (store.root / "plans").rename(tmp_path / "original")
        (store.root / "plans").symlink_to(tmp_path / "original", target_is_directory=True)
    elif redirect == "file":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    else:
        os.link(path, tmp_path / "duplicate")
    with pytest.raises(WisdomValidationError):
        receipts.load(plan["receipt"])


def test_accepted_operation_can_recover_after_expiry_and_replay_result(receipt_state):
    store, _, now, receipts = receipt_state
    plan = _plan(receipts)
    operation = store.journal("install", "skill", "staged", plan)
    receipts.start(plan["receipt"], operation)
    now[0] += REVIEW_SECONDS + 1
    assert receipts.load(plan["receipt"], allow_started=True) == plan
    receipts.complete(plan["receipt"], {"installed": True})
    assert receipts.outcome(plan["receipt"]) == {"installed": True}
    with pytest.raises(WisdomConflict):
        receipts.complete(plan["receipt"], {"installed": False})


def test_recovery_requires_matching_journal_owner_and_receipt(receipt_state):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)
    operation = store.journal("install", "skill", "staged", {**plan, "owner": {}})
    with pytest.raises(WisdomConflict, match="does not match"):
        receipts.start(plan["receipt"], operation)
    assert receipts.outcome(plan["receipt"]) is None
