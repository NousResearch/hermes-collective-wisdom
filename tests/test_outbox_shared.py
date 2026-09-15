"""Crash and uncertainty tests for the transplanted shared outboxes."""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.client_outcome import ClientOperationResponse
from hermes_wisdom_plugin.delivery import DeliveryReceipt
from hermes_wisdom_plugin.delivery_outbox import DeliveryOutbox, MAX_SYNC_ATTEMPTS
from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.mediation_store import LEASE_SECONDS, MediationStore
from hermes_wisdom_plugin.operation_outbox import MAX_ATTEMPTS, stage
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.sync_recovery import WisdomSyncRecovery


class RecordingOutboxClient:
    """Gateway-shaped transport that can revoke an invocation during owner reads."""

    expected_organization_id = "org"
    display_org_id = "org"
    identity = {"owner": "user"}
    base_url = "https://gateway.example"

    def __init__(self, store, current, clock):
        self.store, self.current, self.clock = store, current, clock
        self.calls = []
        self.revoke_after_claim = False
        self.revoke_after_report = False
        self.revoke_on_operation_lease = False

    @property
    def owner_user_id(self):
        if self.revoke_on_operation_lease:
            with self.store.transaction() as db:
                leased = db.execute(
                    "SELECT 1 FROM wisdom_operation_outbox WHERE sync_token IS NOT NULL"
                ).fetchone()
            if leased:
                self.current[0] = False
        return "user"

    def claim_notification_delivery(self, request_id, reference):
        self.calls.append(("claim", request_id))
        if self.revoke_after_claim:
            self.current[0] = False
        return ClientDeliveryResponse(
            org_id="org",
            recipient_user_id="user",
            event_id=f"event-{request_id}",
            request_id=request_id,
            reference=reference,
            state="claimed",
            reason=None,
            lease_until=(
                datetime.fromtimestamp(self.clock() + 120, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        )

    def settle_notification_delivery(
        self, event_id, request_id, reference, *, outcome, receipt
    ):
        del receipt
        self.calls.append(("settle", event_id, request_id, outcome))
        return ClientDeliveryResponse(
            org_id="org",
            recipient_user_id="user",
            event_id=event_id,
            request_id=request_id,
            reference=reference,
            state=outcome,
            reason=None,
            lease_until=None,
        )

    def report_operation_outcome(self, event_id, outcome):
        self.calls.append(("report", event_id, outcome["operation_key"]))
        if self.revoke_after_report:
            self.current[0] = False
        return ClientOperationResponse(
            org_id="org",
            recipient_user_id="user",
            event_id=event_id,
            outcome=outcome,
            attestation="client_reported",
            duplicate=False,
        )


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


def _recording_state(tmp_path):
    state = _state(tmp_path)
    current = [True]
    client = RecordingOutboxClient(state.store, current, lambda: state.now[0])
    revoke_on_delivery_lease = [False]

    def installation_owner():
        if revoke_on_delivery_lease[0]:
            with state.store.transaction() as db:
                leased = db.execute(
                    "SELECT 1 FROM wisdom_remote_delivery WHERE sync_token IS NOT NULL"
                ).fetchone()
            if leased:
                current[0] = False
        return dict(state.owner)

    service = SimpleNamespace(
        store=state.store,
        client=client,
        _installation_owner=installation_owner,
    )
    state.client = client
    state.service = service
    state.current = current
    state.revoke_on_delivery_lease = revoke_on_delivery_lease
    state.outbox = DeliveryOutbox(
        service, clock=lambda: state.now[0], owner=state.owner
    )
    return state


def _stage_retryable_delivery(state):
    request_id = state.outbox.reserve("org", state.job)
    assert request_id
    state.queue.cancel_reservation("org", state.job, state.now[0])
    with state.store.transaction() as db:
        db.execute(
            """UPDATE wisdom_remote_delivery SET attempts=?,last_error=?
            WHERE request_id=?""",
            (MAX_SYNC_ATTEMPTS, "settlement_unavailable", request_id),
        )
    state.client.calls.clear()
    return request_id


def _stage_retryable_operation(state):
    request_id = state.outbox.reserve("org", state.job)
    assert request_id
    assert state.queue.begin_delivery(
        "org", state.job["id"], state.job["lease_token"], request_id=request_id
    )
    receipt = DeliveryReceipt(
        platform="local",
        destination="terminal",
        message_id="1",
        acknowledgement="transport_accepted",
    )
    assert state.queue.complete_delivery(
        "org", state.job["id"], state.job["lease_token"], receipt=receipt
    )
    state.outbox.flush("org")
    value = {
        "id": "interaction",
        "organization_id": "org",
        "assessment_id": state.job["id"],
        "owner_session": "session",
        "operation": "install",
        "plan": {"skill_id": "skill", "version": 1},
    }
    with state.store.transaction() as db:
        db.execute(
            """INSERT INTO wisdom_shared_consent
            (id,organization_id,assessment_id,owner_session,actor_id,platform,operation,
             plan_json,state,expires_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "interaction",
                "org",
                state.job["id"],
                "session",
                "actor",
                "local",
                "install",
                json.dumps(value["plan"]),
                "completed",
                2000,
                1000,
                1000,
            ),
        )
        assert stage(db, value, "completed", {}, state.now[0])
        db.execute(
            """UPDATE wisdom_operation_outbox SET state='failed',attempts=?,last_error=?
            WHERE interaction_id=? AND report_state=?""",
            (MAX_ATTEMPTS, "outcome_unavailable", "interaction", "files_installed"),
        )
    state.client.calls.clear()


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


@pytest.mark.parametrize("event_known", [False, True], ids=["claim-and-settle", "settle"])
def test_selected_sync_retry_settles_delivery_with_current_invocation(
    tmp_path, event_known
):
    state = _recording_state(tmp_path)
    request_id = _stage_retryable_delivery(state)
    if not event_known:
        with state.store.transaction() as db:
            db.execute(
                "UPDATE wisdom_remote_delivery SET event_id=NULL WHERE request_id=?",
                (request_id,),
            )

    result = WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
        may_apply=lambda: state.current[0]
    )

    expected: list[tuple] = [] if event_known else [("claim", request_id)]
    expected.append(
        ("settle", f"event-{request_id}", request_id, "not_sent")
    )
    assert state.client.calls == expected
    assert _row(state)["state"] == "settled"
    assert result["delivery"]["retryable"] == 0


@pytest.mark.parametrize("event_known", [False, True], ids=["claim", "settle"])
def test_selected_sync_retry_revoked_during_late_owner_read_has_no_remote_effect(
    tmp_path, event_known
):
    state = _recording_state(tmp_path)
    request_id = _stage_retryable_delivery(state)
    if not event_known:
        with state.store.transaction() as db:
            db.execute(
                "UPDATE wisdom_remote_delivery SET event_id=NULL WHERE request_id=?",
                (request_id,),
            )
    state.revoke_on_delivery_lease[0] = True

    with pytest.raises(WisdomAuthError, match="no longer current"):
        WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
            may_apply=lambda: state.current[0]
        )

    assert state.client.calls == []
    assert _row(state)["state"] == "outcome"


def test_delivery_flush_revoked_after_claim_preserves_pending_event(tmp_path):
    state = _recording_state(tmp_path)
    request_id = _stage_retryable_delivery(state)
    with state.store.transaction() as db:
        db.execute(
            """UPDATE wisdom_remote_delivery SET event_id=NULL,attempts=0,last_error=NULL
            WHERE request_id=?""",
            (request_id,),
        )
    state.client.revoke_after_claim = True

    with pytest.raises(WisdomAuthError, match="no longer current"):
        state.outbox.flush("org", may_apply=lambda: state.current[0])

    assert state.client.calls == [("claim", request_id)]
    row = _row(state)
    assert row["state"] == "outcome"
    assert row["event_id"] == f"event-{request_id}"
    assert row["last_error"] == "settlement_unavailable"


def test_selected_sync_retry_reports_operation_with_current_invocation(tmp_path):
    state = _recording_state(tmp_path)
    _stage_retryable_operation(state)

    result = WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
        may_apply=lambda: state.current[0]
    )

    assert [call[0] for call in state.client.calls] == ["report"]
    with state.store.transaction() as db:
        row = db.execute(
            "SELECT state FROM wisdom_operation_outbox WHERE interaction_id='interaction'"
        ).fetchone()
    assert row["state"] == "settled"
    assert result["operation"]["retryable"] == 0


def test_selected_sync_retry_revoked_during_late_identity_read_does_not_report(tmp_path):
    state = _recording_state(tmp_path)
    _stage_retryable_operation(state)
    state.client.revoke_on_operation_lease = True

    with pytest.raises(WisdomAuthError, match="no longer current"):
        WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
            may_apply=lambda: state.current[0]
        )

    assert state.client.calls == []
    with state.store.transaction() as db:
        row = db.execute(
            "SELECT state FROM wisdom_operation_outbox WHERE interaction_id='interaction'"
        ).fetchone()
    assert row["state"] == "pending"


def test_selected_sync_retry_revoked_after_report_preserves_settled_outcome(tmp_path):
    state = _recording_state(tmp_path)
    _stage_retryable_operation(state)
    state.client.revoke_after_report = True

    with pytest.raises(WisdomAuthError, match="no longer current"):
        WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
            may_apply=lambda: state.current[0]
        )

    assert [call[0] for call in state.client.calls] == ["report"]
    with state.store.transaction() as db:
        row = db.execute(
            "SELECT state FROM wisdom_operation_outbox WHERE interaction_id='interaction'"
        ).fetchone()
    assert row["state"] == "settled"

    state.current[0] = True
    state.client.revoke_after_report = False
    WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
        may_apply=lambda: state.current[0]
    )
    assert [call[0] for call in state.client.calls] == ["report"]


def test_sync_retry_does_not_resurrect_conflicted_delivery_or_operation(tmp_path):
    state = _recording_state(tmp_path)
    _stage_retryable_operation(state)
    with state.store.transaction() as db:
        db.execute(
            """UPDATE wisdom_remote_delivery SET state='outcome',attempts=?,last_error=?""",
            (MAX_SYNC_ATTEMPTS, "settlement_conflict"),
        )
        db.execute(
            """UPDATE wisdom_operation_outbox SET state='failed',attempts=?,last_error=?""",
            (MAX_ATTEMPTS, "outcome_conflict"),
        )

    result = WisdomSyncRecovery(state.service, clock=lambda: state.now[0]).retry(
        may_apply=lambda: True
    )

    assert state.client.calls == []
    assert result["delivery"]["conflict"] == 1
    assert result["operation"]["conflict"] == 1
    with state.store.transaction() as db:
        delivery = db.execute(
            "SELECT state,attempts,last_error FROM wisdom_remote_delivery"
        ).fetchone()
        operation = db.execute(
            "SELECT state,attempts,last_error FROM wisdom_operation_outbox"
        ).fetchone()
    assert tuple(delivery) == ("outcome", MAX_SYNC_ATTEMPTS, "settlement_conflict")
    assert tuple(operation) == ("failed", MAX_ATTEMPTS, "outcome_conflict")


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
