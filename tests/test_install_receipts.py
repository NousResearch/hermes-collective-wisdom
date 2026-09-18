"""Terminal receipt hardening beyond the reference file-only plan boundary."""

# pylint: disable=missing-function-docstring,redefined-outer-name

import pytest
from installed_test_support import installed_store

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from hermes_wisdom_plugin.install_receipts import InstallReceipts, REVIEW_SECONDS



@pytest.fixture
def receipt_state(tmp_path):
    root = tmp_path / "state"
    store = installed_store(root)
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


def test_review_is_stored_only_in_plugin_state(receipt_state):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)

    assert receipts.load(plan["receipt"]) == plan
    assert not (store.root / "plans").exists()


def test_accepted_operation_can_recover_after_expiry_and_replay_result(receipt_state):
    store, _, now, receipts = receipt_state
    plan = _plan(receipts)
    operation = store.begin("install", "skill", plan, stage="staged")
    receipts.start(plan["receipt"], operation)
    now[0] += REVIEW_SECONDS + 1
    assert receipts.load(plan["receipt"], allow_started=True) == plan
    receipts.complete(plan["receipt"], {"installed": True})
    assert receipts.outcome(plan["receipt"]) == {"installed": True}
    with pytest.raises(WisdomConflict):
        receipts.complete(plan["receipt"], {"installed": False})


@pytest.mark.parametrize("mismatch", ["owner", "receipt", "kind"])
def test_recovery_requires_matching_journal_binding(receipt_state, mismatch):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)
    payload = dict(plan)
    kind = "install"
    if mismatch == "owner":
        payload["owner"] = {}
    elif mismatch == "receipt":
        payload["receipt"] = _plan(receipts)["receipt"]
    else:
        kind = "update"
    operation = store.begin(kind, "skill", payload, stage="staged")
    with pytest.raises(WisdomConflict, match="does not match"):
        receipts.start(plan["receipt"], operation)
    assert receipts.outcome(plan["receipt"]) is None


def test_owner_change_during_receipt_read_denies_authority(receipt_state):
    store, owner, now, receipts = receipt_state
    plan = _plan(receipts)
    owners = iter([dict(owner), {**owner, "account_id": "different-member"}])
    changing_owner = InstallReceipts(store, lambda: next(owners), clock=lambda: now[0])

    with pytest.raises(WisdomAuthError, match="Installation owner changed"):
        changing_owner.load(plan["receipt"])


def test_receipt_cannot_be_rebound_to_a_second_valid_operation(receipt_state):
    store, _, _, receipts = receipt_state
    plan = _plan(receipts)
    first = store.begin("install", "skill", plan, stage="staged")
    receipts.start(plan["receipt"], first)
    receipts.start(plan["receipt"], first)  # Replaying the same binding is safe.
    store.finish(first, {"installed": True}, stage="complete")
    second = store.begin("install", "skill", plan, stage="staged")
    assert second != first

    with pytest.raises(WisdomConflict, match="already in use"):
        receipts.start(plan["receipt"], second)
    assert store.review("install_receipt:" + plan["receipt"])["operation_id"] == first
