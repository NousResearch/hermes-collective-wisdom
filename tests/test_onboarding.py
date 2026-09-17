"""Terminal onboarding state-machine tests adapted from legacy service setup tests."""
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=too-few-public-methods,use-implicit-booleaness-not-comparison
# pylint: disable=too-many-instance-attributes

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from hermes_wisdom_plugin.errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomValidationError,
)
from hermes_wisdom_plugin.onboarding import Onboarding, WISDOM_DISCLOSURE
from hermes_wisdom_plugin.store import WisdomStore


class Response(BaseModel):
    installation_id: str
    state: str


class Capability(BaseModel):
    features: list[str]
    protocol_version: int = 1


class Account:
    def __init__(self) -> None:
        self.profile_name = "work"
        self.profile_home = "/profiles/work"
        self.account_id = "user-1"
        self.organization_id = "org-1"
        self.base_url = "https://gateway.example"
        self.secret = "bearer-secret"
        self.scopes: list[str] = []

    def credentials(self, scope="wisdom:read"):
        self.scopes.append(scope)
        return {"access_token": self.secret}


class Client:
    def __init__(self, account: Account, store: WisdomStore) -> None:
        self.account = account
        self.store = store
        self.calls: list[tuple[str, str | None]] = []
        self.register_effects: list[object] = []

    def capability(self):
        assert self.account.scopes[:2] == ["wisdom:read", "wisdom:install"]
        record = self.store.get_metadata("terminal_setup")
        assert record and record["state"] == "registering"
        assert self.store.operation(record["operation_id"])
        self.calls.append(("capability", None))
        return Capability(features=["wisdom"])

    def register_identity(self, installation_id):
        self.calls.append(("register", installation_id))
        if self.register_effects:
            effect = self.register_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            if callable(effect):
                return effect(installation_id)
        return Response(installation_id=installation_id, state="active")


def runtime(tmp_path: Path):
    account = Account()
    store = WisdomStore(tmp_path / "state")
    client = Client(account, store)
    return account, store, client, Onboarding(account, store, client)


def operation_count(store: WisdomStore) -> int:
    with store.transaction() as db:
        return int(db.execute("SELECT COUNT(*) FROM operation_journal").fetchone()[0])


def test_setup_requires_explicit_consent_before_credentials_or_network(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)

    with pytest.raises(WisdomValidationError, match="explicit") as denied:
        onboarding.setup(False)

    assert denied.value.code == "disclosure_required"
    assert account.scopes == []
    assert client.calls == []
    assert store.get_metadata("terminal_setup") is None


def test_install_entitlement_failure_happens_before_gateway_network(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    original_credentials = account.credentials

    def credentials(scope="wisdom:read"):
        if scope == "wisdom:install":
            raise WisdomAuthError("install denied")
        return original_credentials(scope)

    account.credentials = credentials

    with pytest.raises(WisdomAuthError, match="install denied"):
        onboarding.setup(True)

    assert client.calls == []
    assert store.get_metadata("terminal_setup") is None


def test_revocation_during_authorization_stops_before_reservation_or_network(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    current = {"value": True}
    original_credentials = account.credentials

    def credentials(scope="wisdom:read"):
        result = original_credentials(scope)
        if scope == "wisdom:install":
            current["value"] = False
        return result

    account.credentials = credentials

    with pytest.raises(WisdomAuthError, match="no longer current"):
        onboarding.setup(True, may_apply=lambda: current["value"])

    assert client.calls == []
    assert store.get_metadata("terminal_setup") is None


def test_revocation_during_capability_io_stops_before_identity_registration(tmp_path):
    _account, store, client, onboarding = runtime(tmp_path)
    current = {"value": True}
    original_capability = client.capability

    def capability():
        result = original_capability()
        current["value"] = False
        return result

    client.capability = capability

    with pytest.raises(WisdomAuthError, match="no longer current"):
        onboarding.setup(True, may_apply=lambda: current["value"])

    assert [call[0] for call in client.calls] == ["capability"]
    assert store.get_metadata("terminal_setup")["state"] == "registering"
    assert store.existing_installation_identity() is None


def test_revocation_during_identity_conflict_prevents_rotation_and_retry(tmp_path):
    _account, store, client, onboarding = runtime(tmp_path)
    current = {"value": True}

    def conflict_and_revoke(_installation_id):
        current["value"] = False
        raise WisdomConflict("conflict", code="identity_conflict")

    client.register_effects = [conflict_and_revoke]

    with pytest.raises(WisdomAuthError, match="no longer current"):
        onboarding.setup(True, may_apply=lambda: current["value"])

    register_calls = [call for call in client.calls if call[0] == "register"]
    assert len(register_calls) == 1
    record = store.get_metadata("terminal_setup")
    assert record["installation_id"] == register_calls[0][1]
    assert record.get("conflict_rotated") is None
    assert store.existing_installation_identity() is None


def test_setup_reserves_before_network_and_records_no_credentials(tmp_path):
    account, store, _client, onboarding = runtime(tmp_path)

    result = onboarding.setup(True)

    assert result["configured"] is True
    assert result["state"] == "active"
    assert result["installation_id"].startswith("hwi_")
    assert result["disclosure"] == WISDOM_DISCLOSURE
    assert account.scopes == [
        "wisdom:read",
        "wisdom:install",
        "wisdom:read",
        "wisdom:install",
        "wisdom:read",
    ]
    record = store.get_metadata("terminal_setup")
    assert record["account_id"] == "user-1"
    assert record["organization_id"] == "org-1"
    assert record["profile_name"] == "work"
    assert record["profile_home"] == "/profiles/work"
    assert record["base_url"] == "https://gateway.example"
    assert record["disclosure_accepted_at"]
    assert record["capabilities"] == {"features": ["wisdom"]}
    assert account.secret not in repr(record)
    operation = store.operation(record["operation_id"])
    assert account.secret not in repr(operation)
    assert operation["state"] == "done"


def test_reservation_metadata_and_journal_are_atomic(monkeypatch, tmp_path):
    _account, store, _client, onboarding = runtime(tmp_path)
    original_set_metadata = store.set_metadata

    def fail_registering_metadata(key, value, *, _db=None):
        if value.get("state") == "registering":
            raise OSError("metadata unavailable")
        return original_set_metadata(key, value, _db=_db)

    monkeypatch.setattr(store, "set_metadata", fail_registering_metadata)

    with pytest.raises(OSError, match="metadata unavailable"):
        onboarding.setup(True)

    assert store.get_metadata("terminal_setup") is None
    assert operation_count(store) == 0


def test_timeout_restart_reuses_reserved_identity_and_operation(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [WisdomError("timed out")]

    with pytest.raises(WisdomError, match="timed out"):
        onboarding.setup(True)

    first_record = store.get_metadata("terminal_setup")
    assert first_record["state"] == "registering"
    assert store.existing_installation_identity() is None
    restarted = Onboarding(account, store, client)
    result = restarted.setup(True)

    assert result["configured"] is True
    assert [call[1] for call in client.calls if call[0] == "register"] == [
        first_record["installation_id"],
        first_record["installation_id"],
    ]
    assert store.get_metadata("terminal_setup")["operation_id"] == first_record["operation_id"]
    assert operation_count(store) == 1


def test_expired_crash_lease_resumes_reserved_identity(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [WisdomError("timed out")]
    with pytest.raises(WisdomError, match="timed out"):
        onboarding.setup(True)
    record = store.get_metadata("terminal_setup")

    crashed_owner = store.acquire_operation_lock("terminal_setup", ttl_seconds=900)
    assert crashed_owner
    with pytest.raises(WisdomConflict) as blocked:
        Onboarding(account, store, client).setup(True)
    assert blocked.value.code == "setup_in_progress"
    with store.transaction() as db:
        db.execute(
            "UPDATE operation_lock SET expires_at=? WHERE entity_id=?",
            ("2000-01-01T00:00:00+00:00", "terminal_setup"),
        )

    result = Onboarding(account, store, client).setup(True)

    assert result["configured"] is True
    assert result["installation_id"] == record["installation_id"]
    assert operation_count(store) == 1


def test_expired_inflight_setup_cannot_clobber_new_owner(tmp_path):
    _account, store, client, onboarding = runtime(tmp_path)
    replacement: dict[str, object] = {}

    def supersede(installation_id):
        with store.transaction() as db:
            db.execute(
                "UPDATE operation_lock SET expires_at=? WHERE entity_id=?",
                ("2000-01-01T00:00:00+00:00", "terminal_setup"),
            )
        new_account = Account()
        new_account.account_id = "user-2"
        new_client = Client(new_account, store)
        replacement.update(Onboarding(new_account, store, new_client).setup(True))
        return Response(installation_id=installation_id, state="active")

    client.register_effects = [supersede]

    with pytest.raises(WisdomConflict) as stale:
        onboarding.setup(True)

    current = store.get_metadata("terminal_setup")
    assert stale.value.code == "setup_lease_expired"
    assert replacement["configured"] is True
    assert current["account_id"] == "user-2"
    assert current["installation_id"] == replacement["installation_id"]
    assert store.existing_installation_identity() == replacement["installation_id"]


def test_owner_switch_while_pending_closes_old_operation_and_reserves_new_identity(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [WisdomError("timed out")]
    with pytest.raises(WisdomError, match="timed out"):
        onboarding.setup(True)
    old_record = store.get_metadata("terminal_setup")

    account.account_id = "user-2"
    result = Onboarding(account, store, client).setup(True)

    new_record = store.get_metadata("terminal_setup")
    old_operation = store.operation(old_record["operation_id"])
    assert result["configured"] is True
    assert new_record["installation_id"] != old_record["installation_id"]
    assert new_record["operation_id"] != old_record["operation_id"]
    assert old_operation["state"] == "done"
    assert old_operation["phase"] == "owner_changed"
    assert operation_count(store) == 2


def test_activation_identity_metadata_and_journal_are_atomic(monkeypatch, tmp_path):
    _account, store, _client, onboarding = runtime(tmp_path)
    original_set_metadata = store.set_metadata

    def fail_active_metadata(key, value, *, _db=None):
        if value.get("state") == "active":
            raise OSError("metadata unavailable")
        return original_set_metadata(key, value, _db=_db)

    monkeypatch.setattr(store, "set_metadata", fail_active_metadata)

    with pytest.raises(OSError, match="metadata unavailable"):
        onboarding.setup(True)

    record = store.get_metadata("terminal_setup")
    assert record["state"] == "registering"
    assert store.operation(record["operation_id"])["state"] == "pending"
    assert store.existing_installation_identity() is None


def test_identity_conflict_rotates_once_durably_before_retry(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [
        WisdomConflict("conflict", code="identity_conflict"),
        WisdomError("timed out"),
    ]

    with pytest.raises(WisdomError, match="timed out"):
        onboarding.setup(True)

    first_id, rotated_id = [call[1] for call in client.calls if call[0] == "register"]
    assert rotated_id != first_id
    record = store.get_metadata("terminal_setup")
    assert record["installation_id"] == rotated_id
    assert record["conflict_rotated"] is True
    assert store.operation(record["operation_id"])["entity_id"] == rotated_id
    assert store.operation(record["operation_id"])["payload"]["installation_id"] == rotated_id

    client.register_effects = [WisdomConflict("conflict", code="identity_conflict")]
    with pytest.raises(WisdomConflict, match="conflict"):
        Onboarding(account, store, client).setup(True)
    assert client.calls[-1] == ("register", rotated_id)
    assert store.get_metadata("terminal_setup")["installation_id"] == rotated_id
    assert operation_count(store) == 1


def test_explicit_setup_repairs_missing_disclosure_record(tmp_path):
    """An old active marker alone must not short-circuit renewed consent."""
    _account, store, client, onboarding = runtime(tmp_path)
    onboarding.setup(True)
    record = store.get_metadata("terminal_setup")
    record.pop("disclosure_accepted_at")
    store.set_metadata("terminal_setup", record)
    assert onboarding.status()["configured"] is False
    count = len(client.calls)
    assert onboarding.setup(True)["configured"] is True
    assert len(client.calls) > count


@pytest.mark.parametrize(
    "failure",
    [WisdomError("timed out"), WisdomConflict("revoked", code="identity_revoked")],
)
def test_non_identity_failures_never_rotate(failure, tmp_path):
    _account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [failure]

    with pytest.raises(type(failure)):
        onboarding.setup(True)

    record = store.get_metadata("terminal_setup")
    reserved = record["installation_id"]
    assert client.calls[-1] == ("register", reserved)
    assert record.get("conflict_rotated") is None
    assert store.existing_installation_identity() is None


def test_new_account_in_same_org_requires_consent_and_new_identity(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    first = onboarding.setup(True)
    account.account_id = "user-2"

    status = onboarding.status()
    assert status["configured"] is False
    assert status["reason"] == "owner_changed"
    assert status["account_id"] == "user-2"
    with pytest.raises(WisdomConflict, match="run setup"):
        onboarding.require_setup()
    prior_calls = list(client.calls)
    with pytest.raises(WisdomValidationError, match="explicit"):
        onboarding.setup(False)
    assert client.calls == prior_calls

    second = onboarding.setup(True)
    assert second["configured"] is True
    assert second["installation_id"] != first["installation_id"]
    assert store.get_metadata("terminal_setup")["account_id"] == "user-2"


def test_status_and_require_setup_deny_stale_local_credentials(tmp_path):
    account, _store, _client, onboarding = runtime(tmp_path)
    onboarding.setup(True)

    def denied(scope="wisdom:read"):
        del scope
        raise WisdomAuthError("credentials expired")

    account.credentials = denied

    status = onboarding.status()
    assert status["configured"] is False
    assert status["reason"] == "authentication_unavailable"
    assert status["profile_name"] is None
    assert status["account_id"] is None
    assert status["organization_id"] is None
    with pytest.raises(WisdomAuthError, match="credentials expired"):
        onboarding.require_setup()


def test_account_change_after_ack_never_activates_old_owner(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)

    def switch_owner(installation_id):
        account.account_id = "user-2"
        return Response(installation_id=installation_id, state="active")

    client.register_effects = [switch_owner]
    with pytest.raises(WisdomAuthError, match="changed during setup"):
        onboarding.setup(True)

    assert store.existing_installation_identity() is None
    assert store.get_metadata("terminal_setup")["state"] == "registering"
    assert onboarding.status()["reason"] == "owner_changed"


@pytest.mark.parametrize(
    "response",
    [
        lambda installation_id: Response(installation_id="hwi_wrong", state="active"),
        lambda installation_id: Response(installation_id=installation_id, state="pending"),
    ],
)
def test_unmatched_ack_never_enables(response, tmp_path):
    _account, store, client, onboarding = runtime(tmp_path)
    client.register_effects = [response]

    with pytest.raises(WisdomValidationError, match="did not activate"):
        onboarding.setup(True)

    assert store.existing_installation_identity() is None
    assert store.get_metadata("terminal_setup")["state"] == "registering"


def test_status_is_local_only_and_does_not_reserve_or_enroll(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)

    assert onboarding.status() == {
        "configured": False,
        "state": "not_configured",
        "reason": "not_configured",
    }
    assert account.scopes == []
    assert client.calls == []
    assert store.existing_installation_identity() is None
    assert operation_count(store) == 0


def test_status_denies_malformed_metadata_without_replacing_it(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO schema_meta(key,value) VALUES(?,?)",
            ("terminal_setup", "not-json"),
        )

    assert onboarding.status() == {
        "configured": False,
        "state": "unavailable",
        "reason": "local_state_unavailable",
    }
    assert account.scopes == []
    assert client.calls == []
    with pytest.raises(ValueError):
        onboarding.setup(True)
    with store.transaction() as db:
        value = db.execute(
            "SELECT value FROM schema_meta WHERE key=?", ("terminal_setup",)
        ).fetchone()[0]
    assert value == "not-json"


def test_operation_lock_prevents_duplicate_setup_race(tmp_path):
    account, store, client, onboarding = runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def block(installation_id):
        entered.set()
        assert release.wait(timeout=2)
        return Response(installation_id=installation_id, state="active")

    client.register_effects = [block]
    errors: list[BaseException] = []

    def run_setup():
        try:
            onboarding.setup(True)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    worker = threading.Thread(target=run_setup)
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(WisdomConflict) as duplicate:
        Onboarding(account, store, client).setup(True)
    assert duplicate.value.code == "setup_in_progress"
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert len([call for call in client.calls if call[0] == "register"]) == 1
