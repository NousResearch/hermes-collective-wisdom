"""Private receipt/report recovery. No message send or operation execution."""
# pylint: disable=line-too-long,missing-class-docstring,missing-function-docstring,protected-access,too-many-return-statements,too-many-branches

from __future__ import annotations

import time

from .client import WisdomError
from .delivery_outbox import DeliveryOutbox, MAX_SYNC_ATTEMPTS
from .errors import WisdomAuthError
from .mediation_store import MediationStore
from .operation_outbox import MAX_ATTEMPTS, OperationOutbox
from .preferences import WisdomPreferences


class WisdomSyncRecovery:
    def __init__(self, service, *, clock=time.time):
        self.service, self.store, self.clock = service, service.store, clock

    def _identity(self):
        org = self.store.active_org_id()
        return org, WisdomPreferences(self.service).identity(org)

    def _check_identity(self, org, user):
        if self._identity() != (org, user):
            raise WisdomError("The active account changed; reopen sync status")

    @staticmethod
    def _require_current(may_apply):
        if may_apply is not None and may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")

    def _rows(self, db, org, user):
        MediationStore._check_org(db, org)
        preferences = db.execute(
            """SELECT key,state,attempts,lease_until AS sync_until,last_error,
            suppress_until AS expires_at FROM wisdom_preference_outbox
            WHERE organization_id=? AND user_id=? AND state!='synced'""",
            (org, user),
        ).fetchall()
        mutes = db.execute(
            """SELECT mutation_id,state,attempts,lease_until AS sync_until,last_error,
            expires_at FROM wisdom_mute_outbox
            WHERE organization_id=? AND user_id=? AND state!='synced'""",
            (org, user),
        ).fetchall()
        deliveries = db.execute(
            """SELECT request_id,state,outcome,attempts,sync_until,last_error
            FROM wisdom_remote_delivery WHERE organization_id=? AND user_id=?
            AND (state!='settled' OR outcome='uncertain')""",
            (org, user),
        ).fetchall()
        operations = db.execute(
            """SELECT o.interaction_id,o.report_state,o.state,o.attempts,o.sync_until,o.last_error,
            (d.state='settled' AND d.outcome='acknowledged'
             AND d.event_id IS NOT NULL AND d.receipt_json IS NOT NULL) AS receipt_ready
            FROM wisdom_operation_outbox o LEFT JOIN wisdom_remote_delivery d
            ON d.request_id=o.request_id AND d.organization_id=o.organization_id
            AND d.user_id=o.user_id
            WHERE o.organization_id=? AND o.user_id=? AND o.state!='settled'""",
            (org, user),
        ).fetchall()
        return preferences, mutes, deliveries, operations

    @staticmethod
    def _state(row, kind, now):
        if row["sync_until"] is not None and row["sync_until"] > now:
            return "syncing"
        if kind in {"preference", "mute"}:
            if row["state"] == "conflict":
                return "conflict"
            if row["state"] == "expired" or row["expires_at"] <= now:
                return "expired"
            if row["state"] == "failed" or row["attempts"] >= 3:
                return "retryable"
            return "pending"
        if kind == "delivery":
            if row["state"] == "settled":
                return "uncertain"
            if row["last_error"] == "settlement_conflict":
                return "conflict"
            if row["state"] == "outcome" and row["attempts"] >= MAX_SYNC_ATTEMPTS:
                return "retryable"
        else:
            if row["last_error"] == "outcome_conflict":
                return "conflict"
            if not row["receipt_ready"]:
                return "waiting_for_receipt"
            if row["attempts"] >= MAX_ATTEMPTS or (
                row["state"] == "failed" and row["last_error"] == "outcome_unavailable"
            ):
                return "retryable"
        return "pending"

    @staticmethod
    def _counts(kind, rows, now):
        names = ["pending", "syncing", "retryable", "conflict"]
        if kind in {"preference", "mute"}:
            names.append("expired")
        else:
            names.extend(("uncertain", "waiting_for_receipt"))
        counts = dict.fromkeys(names, 0)
        for row in rows:
            counts[WisdomSyncRecovery._state(row, kind, now)] += 1
        return counts

    def status(self):
        org, user = self._identity()
        now = self.clock()
        with self.store.transaction() as db:
            rows = self._rows(db, org, user)
            groups = {
                kind: self._counts(kind, values, now)
                for kind, values in zip(
                    ("preference", "mute", "delivery", "operation"), rows
                )
            }
        # No private references, transport identifiers, raw errors or payloads.
        self._check_identity(org, user)
        return {**groups, "can_retry": any(g["retryable"] for g in groups.values())}

    def retry(self, *, may_apply=None):
        org, user = self._identity()
        now = self.clock()
        with self.store.transaction() as db:
            preferences, mutes, deliveries, operations = self._rows(db, org, user)
            self._require_current(may_apply)
            for row in preferences:
                if self._state(row, "preference", now) == "retryable":
                    db.execute(
                        """UPDATE wisdom_preference_outbox SET state='pending',attempts=0,
                        available_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL
                        WHERE organization_id=? AND user_id=? AND key=?""",
                        (now, org, user, row["key"]),
                    )
            for row in mutes:
                if self._state(row, "mute", now) == "retryable":
                    db.execute(
                        """UPDATE wisdom_mute_outbox SET state='pending',attempts=0,
                        available_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL
                        WHERE organization_id=? AND user_id=? AND mutation_id=?""",
                        (now, org, user, row["mutation_id"]),
                    )
            for row in deliveries:
                if self._state(row, "delivery", now) == "retryable":
                    db.execute(
                        """UPDATE wisdom_remote_delivery SET attempts=0,available_at=?,
                        sync_token=NULL,sync_until=NULL,last_error=NULL WHERE request_id=?""",
                        (now, row["request_id"]),
                    )
            for row in operations:
                if self._state(row, "operation", now) == "retryable":
                    db.execute(
                        """UPDATE wisdom_operation_outbox SET state='pending',attempts=0,available_at=?,
                        sync_token=NULL,sync_until=NULL,last_error=NULL WHERE interaction_id=? AND report_state=?""",
                        (now, row["interaction_id"], row["report_state"]),
                    )
        # Reconcile saved transport acceptance before reporting completed work.
        # Existing leases and immutable request keys make concurrent retries safe.
        self._check_identity(org, user)
        self._require_current(may_apply)
        WisdomPreferences(self.service, clock=self.clock).flush(
            org, may_apply=may_apply
        )
        self._check_identity(org, user)
        self._require_current(may_apply)
        DeliveryOutbox(
            self.service, clock=self.clock,
            owner=self.store.get_metadata("qualification_owner"),
        ).flush(org, may_apply=may_apply)
        self._check_identity(org, user)
        self._require_current(may_apply)
        OperationOutbox(self.service, clock=self.clock).flush(
            org, may_apply=may_apply
        )
        self._check_identity(org, user)
        self._require_current(may_apply)
        return self.status()
