"""Crash and uncertainty tests for the transplanted shared outboxes."""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.delivery import DeliveryReceipt
from hermes_wisdom_plugin.delivery_outbox import DeliveryOutbox
from hermes_wisdom_plugin.mediation_store import LEASE_SECONDS, MediationStore
from hermes_wisdom_plugin.operation_outbox import stage
from hermes_wisdom_plugin.store import WisdomStore


def _state(tmp_path):
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("hwi_test", "org")
    owner = {
        "profile_name": "default", "profile_home": "/profile",
        "account_id": "user", "organization_id": "org",
        "base_url": "https://gateway.example", "installation_id": "hwi_test",
    }
    store.bind_qualification_owner(owner)
    now = [1000.0]
    queue = MediationStore(store, clock=lambda: now[0], owner=owner)
    queue.register_session(
        "org", session_key="session", session_id="session", platform="local",
        actor_id="actor", private=True, available=True, user_activity=True,
        address={"chat_id": "terminal"},
    )
    assessment = queue.enqueue_owned(
        "org", "feed:event", {"kind": "skill", "skill_id": "skill", "version": 1}
    )
    job = queue.claim("org", "session")[0]
    assert queue.save_advice("org", assessment, job["lease_token"], {"title": "Advice"})
    client = Mock(identity={"owner": "user"}, display_org_id="org")
    client.owner_user_id = "user"

    def response(request_id, reference, *, state="claimed"):
        return ClientDeliveryResponse(
            org_id="org", recipient_user_id="user", event_id="gateway-event",
            request_id=request_id, reference=reference, state=state, reason=None,
            lease_until=(datetime.fromtimestamp(now[0] + 120, timezone.utc)
                         .isoformat().replace("+00:00", "Z")) if state == "claimed" else None,
        )

    client.claim_notification_delivery.side_effect = response
    client.settle_notification_delivery.side_effect = (
        lambda event_id, request_id, reference, *, outcome, receipt: response(
            request_id, reference, state=outcome
        )
    )
    service = SimpleNamespace(
        store=store, client=client, _installation_owner=lambda: dict(owner),
    )
    return SimpleNamespace(
        store=store, queue=queue, job=job, now=now, client=client, service=service,
        owner=owner,
        outbox=DeliveryOutbox(service, clock=lambda: now[0], owner=owner), response=response,
    )


def _row(state):
    with state.store.transaction() as db:
        return dict(db.execute("SELECT * FROM wisdom_remote_delivery").fetchone())


def test_delivery_intent_precedes_network_and_reuses_request_id(tmp_path):
    state = _state(tmp_path)

    def lost(request_id, reference):
        assert _row(state)["request_id"] == request_id
        assert set(reference) == {"kind", "skill_id", "version", "event_type"}
        assert "private" not in json.dumps(reference)
        raise TimeoutError("response lost")

    state.client.claim_notification_delivery.side_effect = lost
    with pytest.raises(TimeoutError):
        state.outbox.reserve("org", state.job)
    request_id = _row(state)["request_id"]
    state.client.claim_notification_delivery.side_effect = state.response
    assert state.outbox.reserve("org", state.job) == request_id
    assert state.client.claim_notification_delivery.call_args_list[0] == (
        state.client.claim_notification_delivery.call_args_list[1]
    )


def test_delivery_classification_scopes_installation_to_exact_owner(tmp_path):
    state = _state(tmp_path)
    owner = {
        "profile_name": "default", "profile_home": "/profile",
        "account_id": "user", "organization_id": "org",
        "base_url": "https://gateway.example", "installation_id": "hwi_test",
    }
    service = SimpleNamespace(store=state.store, client=state.client)
    outbox = DeliveryOutbox(service, clock=lambda: state.now[0], owner=owner)
    original = state.store.installation
    state.store.installation = Mock(side_effect=original)

    outbox.reserve("org", state.job)

    state.store.installation.assert_called_once_with("skill", owner=owner)


def test_new_same_user_owner_cannot_settle_old_owner_reservation(tmp_path):
    state = _state(tmp_path)
    request_id = state.outbox.reserve("org", state.job)
    assert request_id
    state.now[0] += LEASE_SECONDS + 1
    new_owner = {**state.owner, "installation_id": "hwi_reenrolled"}
    state.store.activate_installation_identity("hwi_reenrolled", "org")
    state.store.bind_qualification_owner(new_owner)
    state.service._installation_owner = lambda: dict(new_owner)

    DeliveryOutbox(
        state.service, clock=lambda: state.now[0], owner=new_owner,
    ).flush("org")

    assert state.client.settle_notification_delivery.call_count == 0
    row = _row(state)
    assert row["request_id"] == request_id and row["state"] == "claimed"


@pytest.mark.parametrize("identity_read", [2, 3])
def test_owner_changed_during_identity_read_cannot_settle(tmp_path, identity_read):
    state = _state(tmp_path)
    state.outbox.reserve("org", state.job)
    state.queue.cancel_reservation("org", state.job, state.now[0])
    original = state.outbox.identity
    reads = 0

    def identity_then_revoke(org):
        nonlocal reads
        user = original(org)
        reads += 1
        if reads == identity_read:
            state.store.bind_qualification_owner({**state.owner, "account_id": "replacement"})
        return user

    state.outbox.identity = identity_then_revoke
    state.outbox.flush("org")
    assert state.client.settle_notification_delivery.call_count == 0


def test_unknown_send_is_settled_uncertain_and_never_reclaimed(tmp_path):
    state = _state(tmp_path)
    request_id = state.outbox.reserve("org", state.job)
    assert state.queue.begin_delivery(
        "org", state.job["id"], state.job["lease_token"], request_id=request_id
    )
    state.now[0] += LEASE_SECONDS + 1
    state.outbox.flush("org")
    row = _row(state)
    assert row["state"] == "settled" and row["outcome"] == "uncertain"
    assert state.client.claim_notification_delivery.call_count == 1
    assert state.client.settle_notification_delivery.call_args.kwargs["outcome"] == "uncertain"


@pytest.mark.parametrize("changed", ["org", "user", "request", "reference"])
def test_claim_acknowledgement_must_match_exact_local_reservation(tmp_path, changed):
    state = _state(tmp_path)

    def mismatched(request_id, reference):
        values = {
            "org_id": "org", "recipient_user_id": "user", "request_id": request_id,
            "reference": reference,
        }
        if changed == "org":
            values["org_id"] = "other-org"
        elif changed == "user":
            values["recipient_user_id"] = "other-user"
        elif changed == "request":
            values["request_id"] = "00000000-0000-0000-0000-000000000001"
        else:
            values["reference"] = {
                "kind": "skill", "skill_id": "foreign", "version": 9,
                "event_type": "teammate_published",
            }
        return ClientDeliveryResponse(
            **values, event_id="foreign-event", state="claimed", reason=None,
            lease_until=datetime.fromtimestamp(
                state.now[0] + 120, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
        )

    state.client.claim_notification_delivery.side_effect = mismatched
    with pytest.raises(Exception, match="reservation response"):
        state.outbox.reserve("org", state.job)
    assert _row(state)["event_id"] is None


def test_uncertain_claim_stays_uncertain_and_cannot_be_reclaimed(tmp_path):
    state = _state(tmp_path)
    state.client.claim_notification_delivery.side_effect = lambda request_id, reference: (
        state.response(request_id, reference, state="uncertain")
    )

    assert state.outbox.reserve("org", state.job) is None
    row = _row(state)
    assert (row["state"], row["outcome"]) == ("settled", "uncertain")
    assert state.outbox.reserve("org", state.job) is None
    assert state.client.claim_notification_delivery.call_count == 1


def test_recovery_claim_rejects_foreign_acknowledgement_without_binding_event(tmp_path):
    state = _state(tmp_path)
    state.client.claim_notification_delivery.side_effect = TimeoutError("lost")
    with pytest.raises(TimeoutError):
        state.outbox.reserve("org", state.job)
    state.now[0] += LEASE_SECONDS + 1

    def foreign(request_id, reference):
        return ClientDeliveryResponse(
            org_id="other-org", recipient_user_id="user", event_id="foreign-event",
            request_id=request_id, reference=reference, state="uncertain",
            reason=None, lease_until=None,
        )

    state.client.claim_notification_delivery.side_effect = foreign
    state.outbox.flush("org")
    row = _row(state)
    assert row["event_id"] is None
    assert row["state"] == "outcome"
    assert row["last_error"] == "settlement_unavailable"


def test_remote_deferral_is_terminal_for_the_local_reservation(tmp_path):
    state = _state(tmp_path)

    def deferred(request_id, reference):
        return ClientDeliveryResponse(
            org_id="org", recipient_user_id="user", event_id="gateway-event",
            request_id=request_id, reference=reference, state="deferred",
            reason="recipient_muted", lease_until=None,
        )

    state.client.claim_notification_delivery.side_effect = deferred
    assert state.outbox.reserve("org", state.job) is None
    row = _row(state)
    assert (row["state"], row["last_error"]) == ("settled", "remote_deferred")
    assert state.outbox.reserve("org", state.job) is None
    assert state.client.claim_notification_delivery.call_count == 1


def test_operation_outbox_never_reports_setup_and_orders_install_completion(tmp_path):
    state = _state(tmp_path)
    request_id = state.outbox.reserve("org", state.job)
    assert state.queue.begin_delivery(
        "org", state.job["id"], state.job["lease_token"], request_id=request_id
    )
    receipt = DeliveryReceipt(
        platform="local", destination="terminal", message_id="1",
        acknowledgement="transport_accepted",
    )
    assert state.queue.complete_delivery(
        "org", state.job["id"], state.job["lease_token"], receipt=receipt
    )
    value = {
        "id": "interaction", "organization_id": "org", "assessment_id": state.job["id"],
        "owner_session": "session", "operation": "install",
        "plan": {"skill_id": "skill", "version": 1},
    }
    with state.store.transaction() as db:
        db.execute(
            """INSERT INTO wisdom_shared_consent
            (id,organization_id,assessment_id,owner_session,actor_id,platform,operation,
             plan_json,state,expires_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("interaction", "org", state.job["id"], "session", "actor", "local",
             "install", json.dumps(value["plan"]), "completed", 2000, 1000, 1000),
        )
        assert stage(db, value, "completed", {}, 1000.0)
        assert db.execute(
            "SELECT report_state FROM wisdom_operation_outbox"
        ).fetchone()[0] == "files_installed"
        assert not stage(db, {**value, "id": "setup", "operation": "setup"},
                         "completed", {}, 1000.0)
