"""Explicit terminal onboarding adapted from legacy ``hermes_wisdom.service`` setup."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError

_METADATA_KEY = "terminal_setup"
_LOCK_KEY = "terminal_setup"
_OPERATION_KIND = "terminal_setup"
_RECORD_VERSION = 1
_READ_SCOPE = "wisdom:read"
_INSTALL_SCOPE = "wisdom:install"
# Covers the bounded capability and registration requests, while a crashed setup
# becomes retryable much sooner than the store's general 15-minute operation lease.
_SETUP_LOCK_TTL_SECONDS = 120

WISDOM_DISCLOSURE = (
    "Candidate signals stay on this profile. Only owner-approved private draft bytes, "
    "author copy, manifest metadata, and managed-install state reach the Gateway."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_dict(value: object, label: str) -> dict[str, Any]:
    try:
        payload = value.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError) as exc:
        raise WisdomValidationError(f"Gateway returned an invalid {label}") from exc
    if not isinstance(payload, dict):
        raise WisdomValidationError(f"Gateway returned an invalid {label}")
    return payload


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class Onboarding:
    """Own the durable, consent-gated terminal setup state machine."""

    def __init__(self, account: object, store: object, client: object) -> None:
        self.account = account
        self.store = store
        self.client = client

    def _owner(self) -> dict[str, str]:
        owner = {
            "profile_name": _text(self.account.profile_name),
            "profile_home": _text(self.account.profile_home),
            "account_id": _text(self.account.account_id),
            "organization_id": _text(self.account.organization_id),
            "base_url": _text(self.account.base_url),
        }
        if not all(owner.values()):
            raise WisdomAuthError("Collective Wisdom account context is unavailable")
        return owner

    @staticmethod
    def _same_owner(record: dict[str, Any], owner: dict[str, str]) -> bool:
        return all(record.get(key) == value for key, value in owner.items())

    def _authorize_setup(self) -> dict[str, str]:
        """Check both setup scopes locally without retaining credential values."""
        self.account.credentials(scope=_READ_SCOPE)
        self.account.credentials(scope=_INSTALL_SCOPE)
        return self._owner()

    def _record(self) -> dict[str, Any] | None:
        value = self.store.get_metadata(_METADATA_KEY)
        return value if isinstance(value, dict) else None

    def _reserve(
        self,
        owner: dict[str, str],
        previous: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        same_owner = bool(previous and self._same_owner(previous, owner))
        installation_id = (
            _text(previous.get("installation_id")) if same_owner and previous else ""
        ) or f"hwi_{uuid.uuid4().hex}"
        accepted_at = (
            _text(previous.get("disclosure_accepted_at")) if same_owner and previous else ""
        ) or _now()
        record: dict[str, Any] = {
            "version": _RECORD_VERSION,
            **owner,
            "disclosure_accepted_at": accepted_at,
            "installation_id": installation_id,
            "state": "registering",
            "setup_attempt_id": str(uuid.uuid4()),
        }
        if same_owner and previous:
            if previous.get("conflict_rotated") is True:
                record["conflict_rotated"] = True
            previous_operation_id = _text(previous.get("operation_id"))
            if previous_operation_id:
                record["operation_id"] = previous_operation_id
        operation_id = _text(record.get("operation_id"))
        timestamp = _now()
        with self.store.transaction() as db:
            if not same_owner and previous:
                previous_operation_id = _text(previous.get("operation_id"))
                previous_installation_id = _text(previous.get("installation_id"))
                if previous_operation_id:
                    db.execute(
                        "UPDATE operation_journal SET phase='owner_changed',state='done',"
                        "updated_at=? WHERE id=? AND kind=? AND state='pending'",
                        (timestamp, previous_operation_id, _OPERATION_KIND),
                    )
                elif previous_installation_id:
                    db.execute(
                        "UPDATE operation_journal SET phase='owner_changed',state='done',"
                        "updated_at=? WHERE kind=? AND entity_id=? AND state='pending'",
                        (timestamp, _OPERATION_KIND, previous_installation_id),
                    )

            operation = (
                db.execute(
                    "SELECT id FROM operation_journal WHERE id=? AND kind=? AND state='pending'",
                    (operation_id, _OPERATION_KIND),
                ).fetchone()
                if operation_id
                else None
            )
            if operation is None:
                operation = db.execute(
                    "SELECT id FROM operation_journal "
                    "WHERE kind=? AND entity_id=? AND state='pending'",
                    (_OPERATION_KIND, installation_id),
                ).fetchone()
            if operation is None:
                operation_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO operation_journal VALUES(?,?,?,?,?,'pending',?,?)",
                    (
                        operation_id,
                        _OPERATION_KIND,
                        installation_id,
                        "registering",
                        "{}",
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                operation_id = str(operation["id"])
            record["operation_id"] = operation_id
            self.store.set_metadata(_METADATA_KEY, record, _db=db)
            db.execute(
                "UPDATE operation_journal SET payload_json=?,updated_at=? "
                "WHERE id=? AND kind=?",
                (json.dumps(record, sort_keys=True), timestamp, operation_id, _OPERATION_KIND),
            )
        return record, operation_id

    @staticmethod
    def _assert_current_attempt(db: object, lock_owner: str, record: dict[str, Any]) -> None:
        now = _now()
        lease = db.execute(
            "SELECT owner,expires_at FROM operation_lock WHERE entity_id=?",
            (_LOCK_KEY,),
        ).fetchone()
        if not lease or lease["owner"] != lock_owner or lease["expires_at"] <= now:
            raise WisdomConflict(
                "Collective Wisdom setup lease expired; retry setup",
                code="setup_lease_expired",
            )
        row = db.execute(
            "SELECT value FROM schema_meta WHERE key=?",
            (_METADATA_KEY,),
        ).fetchone()
        try:
            current = json.loads(row["value"]) if row else None
        except (TypeError, ValueError):
            current = None
        if not isinstance(current, dict) or any(
            current.get(key) != record.get(key)
            for key in (
                "setup_attempt_id",
                "installation_id",
                "profile_name",
                "profile_home",
                "account_id",
                "organization_id",
                "base_url",
                "state",
            )
        ):
            raise WisdomConflict(
                "Collective Wisdom setup was superseded; retry setup",
                code="setup_superseded",
            )

    def _rotate_after_conflict(
        self,
        record: dict[str, Any],
        operation_id: str,
        lock_owner: str,
    ) -> dict[str, Any]:
        rotated = dict(record)
        rotated["installation_id"] = f"hwi_{uuid.uuid4().hex}"
        rotated["state"] = "registering"
        rotated["conflict_rotated"] = True
        with self.store.transaction() as db:
            self._assert_current_attempt(db, lock_owner, record)
            self.store.set_metadata(_METADATA_KEY, rotated, _db=db)
            db.execute(
                "UPDATE operation_journal SET entity_id=?,payload_json=?,updated_at=? "
                "WHERE id=? AND kind=?",
                (
                    rotated["installation_id"], json.dumps(rotated, sort_keys=True),
                    _now(), operation_id, _OPERATION_KIND,
                ),
            )
        return rotated

    def setup(  # pylint: disable=too-many-locals
        self, accept_disclosure: bool
    ) -> dict[str, Any]:
        """Explicitly consent, discover capability, and register this installation."""
        if accept_disclosure is not True:
            raise WisdomValidationError(
                "setup requires explicit disclosure acceptance",
                code="disclosure_required",
            )

        lock_owner = self.store.acquire_operation_lock(
            _LOCK_KEY, ttl_seconds=_SETUP_LOCK_TTL_SECONDS
        )
        if not lock_owner:
            raise WisdomConflict(
                "Collective Wisdom setup is already in progress",
                code="setup_in_progress",
            )
        try:
            owner = self._authorize_setup()
            previous = self._record()
            if previous and self.status()["configured"] is True:
                operation_id = _text(previous.get("operation_id"))
                operation = self.store.operation(operation_id) if operation_id else None
                if operation and operation.get("state") == "pending":
                    with self.store.transaction() as db:
                        db.execute(
                            "UPDATE operation_journal SET phase='active',state='done',updated_at=? "
                            "WHERE id=? AND kind=?",
                            (_now(), operation_id, _OPERATION_KIND),
                        )
                result = self.status()
                result["disclosure"] = WISDOM_DISCLOSURE
                return result

            record, operation_id = self._reserve(owner, previous)
            capability = _model_dict(self.client.capability(), "capability response")
            features = capability.get("features")
            if not isinstance(features, list) or not all(
                isinstance(feature, str) for feature in features
            ):
                raise WisdomValidationError("Gateway returned an invalid capability response")
            if "wisdom" not in features:
                raise WisdomAuthError("Gateway does not advertise Collective Wisdom")
            with self.store.transaction() as db:
                self._assert_current_attempt(db, lock_owner, record)

            try:
                registered = _model_dict(
                    self.client.register_identity(record["installation_id"]),
                    "installation identity response",
                )
            except WisdomConflict as exc:
                if exc.code != "identity_conflict" or record.get("conflict_rotated") is True:
                    raise
                record = self._rotate_after_conflict(record, operation_id, lock_owner)
                registered = _model_dict(
                    self.client.register_identity(record["installation_id"]),
                    "installation identity response",
                )

            if (
                registered.get("installation_id") != record["installation_id"]
                or registered.get("state") != "active"
            ):
                raise WisdomValidationError(
                    "Gateway did not activate the reserved installation identity"
                )

            fresh_owner = self._authorize_setup()
            if fresh_owner != owner:
                raise WisdomAuthError("Collective Wisdom account changed during setup")

            active = {
                **record,
                "state": "active",
                "capabilities": {"features": features},
                "activated_at": _now(),
            }
            with self.store.transaction() as db:
                self._assert_current_attempt(db, lock_owner, record)
                self.store.activate_installation_identity(
                    record["installation_id"], owner["organization_id"], _db=db
                )
                self.store.set_metadata(_METADATA_KEY, active, _db=db)
                db.execute(
                    "UPDATE operation_journal SET phase='active',state='done',updated_at=? "
                    "WHERE id=? AND kind=?",
                    (_now(), operation_id, _OPERATION_KIND),
                )
            result = self.status()
            result["disclosure"] = WISDOM_DISCLOSURE
            return result
        finally:
            self.store.release_operation_lock(_LOCK_KEY, lock_owner)

    def status(self) -> dict[str, Any]:
        """Return local setup state without credentials, enrollment, or network access."""
        try:
            record = self._record()
        except Exception:  # pylint: disable=broad-exception-caught
            return {
                "configured": False,
                "state": "unavailable",
                "reason": "local_state_unavailable",
            }
        if not record or record.get("version") != _RECORD_VERSION:
            return {"configured": False, "state": "not_configured", "reason": "not_configured"}
        authenticated = True
        try:
            self.account.credentials(scope=_READ_SCOPE)
            owner = self._owner()
        except Exception:  # pylint: disable=broad-exception-caught
            authenticated = False
            owner = None
        owner_matches = bool(owner and self._same_owner(record, owner))
        installation_id = _text(record.get("installation_id"))
        try:
            identity_matches = bool(
                installation_id
                and self.store.existing_installation_identity() == installation_id
                and self.store.active_org_id() == record.get("organization_id")
            )
        except Exception:  # pylint: disable=broad-exception-caught
            identity_matches = False
        configured = bool(
            record.get("state") == "active"
            and _text(record.get("disclosure_accepted_at"))
            and owner_matches
            and identity_matches
        )
        reason = None
        if not configured:
            if not authenticated:
                reason = "authentication_unavailable"
            elif not owner_matches:
                reason = "owner_changed"
            elif record.get("state") != "active":
                reason = "registration_incomplete"
            elif not _text(record.get("disclosure_accepted_at")):
                reason = "disclosure_missing"
            else:
                reason = "identity_mismatch"
        stored_capabilities = record.get("capabilities")
        stored_features = (
            stored_capabilities.get("features")
            if isinstance(stored_capabilities, dict)
            else None
        )
        capabilities = (
            {"features": list(stored_features)}
            if isinstance(stored_features, list)
            and all(isinstance(feature, str) for feature in stored_features)
            else None
        )
        return {
            "configured": configured,
            "state": record.get("state"),
            "reason": reason,
            "profile_name": owner.get("profile_name") if owner else None,
            "account_id": owner.get("account_id") if owner else None,
            "organization_id": owner.get("organization_id") if owner else None,
            "installation_id": installation_id or None,
            "disclosure_accepted_at": record.get("disclosure_accepted_at"),
            "capabilities": capabilities,
        }

    def require_setup(self) -> None:
        """Reject use until local setup belongs to the current account and identity."""
        self.account.credentials(scope=_READ_SCOPE)
        status = self.status()
        if status["configured"] is not True:
            raise WisdomConflict(
                "Collective Wisdom is not set up for this profile; run setup first",
                code=str(status.get("reason") or "setup_required"),
            )
