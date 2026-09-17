"""Durable, profile-local ownership of Wisdom advice and consent.

Delivery receipts and unread cursors remain in WisdomStore. These records
describe work, not whether the user has read or approved a notification.
"""
# pylint: disable=line-too-long,import-outside-toplevel,missing-function-docstring,missing-class-docstring,too-many-arguments,protected-access,cyclic-import,too-many-locals,too-many-public-methods,too-many-lines

from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from .delivery import DeliveryReceipt

if TYPE_CHECKING:
    import sqlite3

    from .store import WisdomStore

MAX_ATTEMPTS = 3
LEASE_SECONDS = 180
SESSION_TTL = 120
BATCH_SIZE = 8


def delivery_context_allowed() -> bool:
    """Background agents must not borrow their parent's interactive identity."""

    import os

    if os.environ.get("HERMES_CRON_SESSION"):
        return False
    if os.environ.get("HERMES_SESSION_PLATFORM") in {"subagent", "cron", "kanban"}:
        return False
    if any(
        value and "DELEGAT" in key and key.startswith("HERMES_")
        for key, value in os.environ.items()
    ):
        return False
    try:
        from agent.delegation_context import is_delegated_child_process_context
        from gateway.session_context import get_session_env
        from tools.skill_provenance import get_current_write_origin
    except ImportError:
        # The standalone terminal plugin has no host package imports. Its local
        # explicit command is human-owned; native registration supplies the
        # stronger host ContextVar checks when loaded by Hermes.
        return True
    return not (
        is_delegated_child_process_context()
        or get_session_env("HERMES_CRON_SESSION")
        or get_session_env("HERMES_SESSION_PLATFORM") in {"subagent", "cron", "kanban"}
        or get_current_write_origin() in {"background_review", "side_question"}
    )


def create_schema(db: sqlite3.Connection) -> None:
    # execute, not executescript: preserve the caller's migration transaction.
    for statement in (
        """CREATE TABLE IF NOT EXISTS wisdom_shared_agent_session (
          organization_id TEXT NOT NULL, session_key TEXT NOT NULL,
          session_id TEXT NOT NULL, platform TEXT NOT NULL, actor_id TEXT NOT NULL,
          last_activity REAL NOT NULL, alive_until REAL NOT NULL,
          available INTEGER NOT NULL, address_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(organization_id,session_key))""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_assessment (
          id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
          event_key TEXT NOT NULL, origin_session TEXT,
          reference_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          owner_session TEXT, lease_token TEXT, lease_until REAL,
          attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
          advice_json TEXT, last_error TEXT, delivered_at REAL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          UNIQUE(organization_id,event_key))""",
        """CREATE INDEX IF NOT EXISTS wisdom_shared_assessment_queue
          ON wisdom_shared_assessment(organization_id,state,available_at)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_delivery_receipt (
          assessment_id TEXT PRIMARY KEY REFERENCES wisdom_shared_assessment(id),
          organization_id TEXT NOT NULL, owner_session TEXT NOT NULL,
          receipt_json TEXT NOT NULL, recorded_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_agent_introduction (
          organization_id TEXT PRIMARY KEY, delivered_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_mediation_poll (
          organization_id TEXT PRIMARY KEY, next_poll REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_consent (
          id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
          assessment_id TEXT NOT NULL REFERENCES wisdom_shared_assessment(id),
          owner_session TEXT NOT NULL, actor_id TEXT NOT NULL,
          platform TEXT NOT NULL, operation TEXT NOT NULL,
          plan_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          expires_at REAL NOT NULL, result_json TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_consent_defer (
          interaction_id TEXT NOT NULL REFERENCES wisdom_shared_consent(id),
          surface TEXT NOT NULL, deferred_at REAL NOT NULL,
          PRIMARY KEY(interaction_id,surface))""",
        """CREATE TABLE IF NOT EXISTS wisdom_preference_outbox (
          organization_id TEXT NOT NULL, user_id TEXT NOT NULL, key TEXT NOT NULL,
          suppress_until REAL NOT NULL, created_at REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
          last_error TEXT,
          PRIMARY KEY(organization_id,user_id,key))""",
        """CREATE TABLE IF NOT EXISTS wisdom_mute_outbox (
          organization_id TEXT NOT NULL, user_id TEXT NOT NULL,
          mutation_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
          duration TEXT, requested_at REAL NOT NULL, expires_at REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
          last_error TEXT, PRIMARY KEY(organization_id,user_id))""",
        """CREATE TABLE IF NOT EXISTS wisdom_mute_control (
          id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, user_id TEXT NOT NULL,
          expected_revision INTEGER NOT NULL, prior_mutation_id TEXT,
          expires_at REAL NOT NULL, selected_at REAL, duration TEXT)""",
        """CREATE TABLE IF NOT EXISTS wisdom_shared_consent_outcome (
          interaction_id TEXT PRIMARY KEY REFERENCES wisdom_shared_consent(id),
          organization_id TEXT NOT NULL, owner_session TEXT NOT NULL,
          result_json TEXT NOT NULL, delivered_at REAL)""",
    ):
        db.execute(statement)
    session_columns = {
        row[1] for row in db.execute("PRAGMA table_info(wisdom_shared_agent_session)")
    }
    if "owner_key" not in session_columns:
        db.execute("ALTER TABLE wisdom_shared_agent_session ADD COLUMN owner_key TEXT")
    assessment_columns = {
        row[1] for row in db.execute("PRAGMA table_info(wisdom_shared_assessment)")
    }
    if "owner_key" not in assessment_columns:
        db.execute("ALTER TABLE wisdom_shared_assessment ADD COLUMN owner_key TEXT")
    from .delivery_outbox import create_schema as create_delivery_schema

    create_delivery_schema(db)
    from .operation_outbox import create_schema as create_operation_schema

    create_operation_schema(db)
    # Support development databases created before renewable consent IDs.
    definition = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='wisdom_shared_consent'"
    ).fetchone()[0]
    if "UNIQUE(assessment_id,operation)" in definition:
        db.execute("PRAGMA defer_foreign_keys=ON")
        db.execute(
            definition.replace("wisdom_shared_consent", "wisdom_shared_consent_new", 1).replace(
                ",\n          UNIQUE(assessment_id,operation)", ""
            )
        )
        db.execute("INSERT INTO wisdom_shared_consent_new SELECT * FROM wisdom_shared_consent")
        db.execute("DROP TABLE wisdom_shared_consent")
        db.execute("ALTER TABLE wisdom_shared_consent_new RENAME TO wisdom_shared_consent")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS wisdom_shared_consent_one_active
               ON wisdom_shared_consent(assessment_id) WHERE state IN ('pending','applying')""")


def _decode(row: Any) -> dict[str, Any]:
    value = dict(row)
    for key in ("reference", "advice", "plan", "result"):
        raw = value.pop(f"{key}_json", None)
        if raw is not None:
            value[key] = json.loads(raw)
    return value


class MediationStore:
    def __init__(
        self, store: WisdomStore, *, clock=time.time, owner=None, initialize_schema=True
    ) -> None:
        self.store = store
        self.clock = clock
        self.owner = dict(owner) if isinstance(owner, dict) else None
        self.owner_key = self._owner_key(self.owner) if self.owner else None
        if initialize_schema:
            # Shared mediation/outbox tables are additive to the existing native
            # review tables and are created transactionally on first use.
            with self.store.transaction() as db:
                create_schema(db)

    @staticmethod
    def _owner_key(owner: dict[str, str]) -> str:
        encoded = json.dumps(owner, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

    def _require_owner(self) -> str:
        if self.owner is None or self.store.get_metadata("qualification_owner") != self.owner:
            raise ValueError("Wisdom mediation owner is no longer active")
        self._require_org(str(self.owner.get("organization_id") or ""))
        return str(self.owner_key)

    def _require_org(self, org: str) -> None:
        if not org or self.store.active_org_id() != org:
            raise ValueError("Wisdom organization is no longer active")

    @staticmethod
    def _check_org(db: sqlite3.Connection, org: str) -> None:
        row = db.execute(
            "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != org:
            raise ValueError("Wisdom organization is no longer active")

    def _check_bound_owner(self, db: sqlite3.Connection) -> None:
        """Fence writes against an account re-enrollment in the same org."""

        if self.owner is None:
            return
        row = db.execute(
            "SELECT value FROM schema_meta WHERE key='qualification_owner'"
        ).fetchone()
        if row is None or json.loads(row[0]) != self.owner:
            raise ValueError("Wisdom mediation owner is no longer active")

    def register_session(
        self,
        org: str,
        *,
        session_key: str,
        session_id: str,
        platform: str,
        actor_id: str,
        private: bool,
        available: bool,
        user_activity: bool = False,
        address: dict[str, str] | None = None,
        activity_at: float | None = None,
    ) -> None:
        if not delivery_context_allowed() or platform not in {"local", "telegram", "slack"}:
            return
        self._require_org(org)
        if not session_key or not actor_id or not private:
            return
        now = self.clock()
        owner_key = self._require_owner() if self.owner is not None else None
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            db.execute(
                """INSERT INTO wisdom_shared_agent_session
                (organization_id,session_key,session_id,platform,actor_id,last_activity,
                 alive_until,available,address_json,owner_key) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(organization_id,session_key) DO UPDATE SET
                  session_id=excluded.session_id, platform=excluded.platform,
                  actor_id=excluded.actor_id, alive_until=excluded.alive_until,
                  available=excluded.available,address_json=excluded.address_json,
                  owner_key=excluded.owner_key,
                  last_activity=CASE WHEN ? THEN excluded.last_activity
                    ELSE wisdom_shared_agent_session.last_activity END""",
                (
                    org,
                    session_key,
                    session_id,
                    platform,
                    actor_id,
                    min(activity_at or now, now) if user_activity else 0,
                    now + SESSION_TTL,
                    int(available),
                    json.dumps(address or {}),
                    owner_key,
                    int(user_activity),
                ),
            )

    def claim_refresh(self, org: str) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            return bool(
                db.execute(
                    """INSERT INTO wisdom_shared_mediation_poll VALUES(?,?)
                ON CONFLICT(organization_id) DO UPDATE SET next_poll=excluded.next_poll
                WHERE wisdom_shared_mediation_poll.next_poll<=?""",
                    (org, now + 60, now),
                ).rowcount
            )

    def retire_candidates(self, org: str, current_ids: set[str]) -> None:
        with self.store.transaction() as db:
            self._check_org(db, org)
            rows = db.execute(
                """SELECT id,event_key FROM wisdom_shared_assessment WHERE organization_id=?
                AND event_key LIKE 'candidate:%' AND state NOT IN ('delivered','retired')""",
                (org,),
            ).fetchall()
            for row in rows:
                if row["event_key"].removeprefix("candidate:") not in current_ids:
                    db.execute(
                        "UPDATE wisdom_shared_assessment SET state='retired',lease_token=NULL,lease_until=NULL WHERE id=?",
                        (row["id"],),
                    )
                    db.execute(
                        "UPDATE wisdom_shared_consent SET state='stale' WHERE assessment_id=? AND state='pending'",
                        (row["id"],),
                    )

    def enqueue(
        self,
        org: str,
        event_key: str,
        reference: dict[str, Any],
        *,
        origin_session: str | None = None,
        _db: sqlite3.Connection | None = None,
    ) -> str:
        now = self.clock()
        with self.store.transaction() if _db is None else nullcontext(_db) as db:
            self._check_org(db, org)
            db.execute(
                """INSERT INTO wisdom_shared_assessment
                (id,organization_id,event_key,origin_session,reference_json,
                 available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(organization_id,event_key) DO NOTHING""",
                (
                    uuid.uuid4().hex,
                    org,
                    event_key,
                    origin_session,
                    json.dumps(reference, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
            return db.execute(
                "SELECT id FROM wisdom_shared_assessment WHERE organization_id=? AND event_key=?",
                (org, event_key),
            ).fetchone()[0]

    def enqueue_owned(
        self,
        org: str,
        event_key: str,
        reference: dict[str, Any],
        *,
        origin_session: str | None = None,
    ) -> str:
        """Enqueue private producer evidence under the exact enrolled owner."""

        identity, _inserted = self.enqueue_owned_if_new(
            org, event_key, reference, origin_session=origin_session
        )
        return identity

    def enqueue_owned_if_new(
        self,
        org: str,
        event_key: str,
        reference: dict[str, Any],
        *,
        origin_session: str | None = None,
    ) -> tuple[str, bool]:
        """Atomically enqueue owner evidence and report whether this call inserted it."""

        owner_key = self._require_owner()
        now = self.clock()
        stored_key = f"owned:{owner_key}:{event_key}"
        with self.store.transaction() as db:
            self._check_org(db, org)
            inserted = bool(db.execute(
                """INSERT INTO wisdom_shared_assessment
                (id,organization_id,event_key,origin_session,reference_json,
                 available_at,created_at,updated_at,owner_key) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(organization_id,event_key) DO NOTHING""",
                (
                    uuid.uuid4().hex, org, stored_key, origin_session,
                    json.dumps(reference, sort_keys=True), now, now, now, owner_key,
                ),
            ).rowcount)
            row = db.execute(
                """SELECT id FROM wisdom_shared_assessment
                WHERE organization_id=? AND event_key=? AND owner_key=?""",
                (org, stored_key, owner_key),
            ).fetchone()
            if row is None:
                raise ValueError("Wisdom mediation owner changed during enqueue")
            return str(row[0]), inserted

    def _coalesce_feed(
        self, db: sqlite3.Connection, org: str, *, skill_id: str | None = None,
    ) -> set[str]:
        """Retire unsent duplicates; report obsolete in-flight work without erasing it."""
        rows = db.execute(
            """SELECT id,state FROM (
                SELECT id,state,ROW_NUMBER() OVER (
                    PARTITION BY json_extract(reference_json,'$.skill_id')
                    ORDER BY json_extract(reference_json,'$.version') DESC,rowid
                ) AS position FROM wisdom_shared_assessment
                WHERE organization_id=? AND owner_key IS ?
                AND (event_key LIKE 'feed:%' OR event_key LIKE 'owned:%:feed:%')
                AND (? IS NULL OR json_extract(reference_json,'$.skill_id')=?)
                AND json_extract(reference_json,'$.kind')='skill'
                AND COALESCE(json_type(reference_json,'$.user_requested'),'null')!='true'
                AND json_type(reference_json,'$.skill_id')='text'
                AND json_extract(reference_json,'$.skill_id')!=''
                AND json_type(reference_json,'$.version')='integer'
                AND json_extract(reference_json,'$.version')>0
            ) WHERE position>1 AND state IN
                ('pending','assessing','ready','fallback','delivering')""",
            (org, self.owner_key, skill_id, skill_id),
        ).fetchall()
        unsent = [(row["id"],) for row in rows if row["state"] != "delivering"]
        if unsent:
            db.executemany(
                "UPDATE wisdom_shared_consent SET state='stale' WHERE assessment_id=? AND state='pending'",
                unsent,
            )
            db.executemany(
                """UPDATE wisdom_shared_assessment SET state='retired',lease_token=NULL,
                lease_until=NULL,updated_at=? WHERE id=?""",
                [(self.clock(), identity) for (identity,) in unsent],
            )
        return {row["id"] for row in rows}

    def reconcile_feed(self, org: str, event_id: str, reference: dict[str, Any]) -> str:
        """Repair old recommendation/notice classification without another delivery."""
        with self.store.transaction() as db:
            identity = self.enqueue(org, f"feed:{event_id}", reference, _db=db)
            row = db.execute(
                "SELECT reference_json,state FROM wisdom_shared_assessment WHERE id=?",
                (identity,),
            ).fetchone()
            if json.loads(row["reference_json"]).get("kind") == reference["kind"]:
                if reference["kind"] == "skill" and row["state"] in {
                    "pending", "assessing", "ready", "fallback", "delivering",
                }:
                    self._coalesce_feed(db, org, skill_id=reference.get("skill_id"))
                return identity
            # A send in flight is uncertain, not permission to send again.
            terminal = row["state"] in {
                "delivering",
                "delivered",
                "delivery_uncertain",
                "retired",
            }
            db.execute(
                """UPDATE wisdom_shared_assessment SET reference_json=?,updated_at=?
                WHERE id=?""",
                (json.dumps(reference, sort_keys=True), self.clock(), identity),
            )
            if not terminal:
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state='pending',advice_json=NULL,
                    lease_token=NULL,lease_until=NULL,attempts=0,available_at=? WHERE id=?""",
                    (self.clock(), identity),
                )
            db.execute(
                "UPDATE wisdom_shared_consent SET state='stale' WHERE assessment_id=? AND state='pending'",
                (identity,),
            )
            self._coalesce_feed(db, org, skill_id=reference.get("skill_id"))
            return identity

    def retire(self, org: str, job: dict[str, Any]) -> bool:
        """Fence terminal arrival invalidation against a replacement owner."""
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            changed = bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state='retired',lease_token=NULL,
                lease_until=NULL,attempts=MAX(0,attempts-CASE WHEN state='assessing' THEN 1 ELSE 0 END),
                updated_at=? WHERE id=? AND organization_id=?
                AND lease_token=? AND lease_until>? AND state IN ('assessing','ready','fallback')""",
                    (now, job["id"], org, job["lease_token"], now),
                ).rowcount
            )
            if changed:
                db.execute(
                    "UPDATE wisdom_shared_consent SET state='stale' WHERE assessment_id=? AND state='pending'",
                    (job["id"],),
                )
            return changed

    def check_claim(self, db, org: str, assessment_id: str, token: str) -> None:
        from .client import WisdomConflict

        self._check_org(db, org)
        self._check_bound_owner(db)
        if not db.execute(
            """SELECT 1 FROM wisdom_shared_assessment WHERE id=? AND organization_id=?
            AND state='assessing' AND lease_token=? AND lease_until>?""",
            (assessment_id, org, token, self.clock()),
        ).fetchone():
            raise WisdomConflict("Wisdom assessment ownership changed")

    def check_claims(self, org: str, jobs: list[dict[str, Any]]) -> None:
        """Atomically verify every lease immediately before external work."""
        if not jobs:
            raise ValueError("Wisdom assessment claim is empty")
        with self.store.transaction() as db:
            for job in jobs:
                self.check_claim(db, org, str(job["id"]), str(job["lease_token"]))

    def claim(
        self, org: str, session_key: str, *, requested_only: bool = False,
        allow_model_work: bool = True, assessment_only: bool = False,
        ready_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Elect one eligible session and claim assessment or ready-delivery work."""
        if not delivery_context_allowed():
            return []
        self._require_org(org)
        owner_key = self._require_owner() if self.owner is not None else None
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            self._coalesce_feed(db, org)
            session = db.execute(
                """SELECT * FROM wisdom_shared_agent_session WHERE organization_id=?
                AND owner_key IS ?
                AND session_key=? AND alive_until>? AND available=1
                AND last_activity>0""",
                (org, owner_key, session_key, now),
            ).fetchone()
            if session is None:
                return []
            if ready_only:
                if assessment_only or requested_only:
                    raise ValueError("ready-only delivery claim cannot be combined with other filters")
                if db.execute(
                    """SELECT 1 FROM wisdom_shared_assessment WHERE organization_id=?
                    AND owner_key IS ? AND state='delivering' AND lease_until>?
                    LIMIT 1""",
                    (org, owner_key, now),
                ).fetchone():
                    return []
                recent = db.execute(
                    """SELECT session_key FROM wisdom_shared_agent_session
                    WHERE organization_id=? AND owner_key IS ? AND alive_until>?
                    AND last_activity>0 ORDER BY last_activity DESC,session_key LIMIT 1""",
                    (org, owner_key, now),
                ).fetchone()[0]
                rows = db.execute(
                    """SELECT * FROM wisdom_shared_assessment WHERE organization_id=?
                    AND owner_key IS ? AND available_at<=? AND state IN ('ready','fallback')
                    AND (owner_session=? OR ((lease_token IS NULL OR lease_until<=?)
                      AND ?=? AND NOT EXISTS (
                        SELECT 1 FROM wisdom_shared_agent_session owner
                        WHERE owner.organization_id=wisdom_shared_assessment.organization_id
                        AND owner.owner_key IS wisdom_shared_assessment.owner_key
                        AND owner.session_key=wisdom_shared_assessment.owner_session
                        AND owner.alive_until>?)))
                    ORDER BY created_at,id LIMIT ?""",
                    (org, owner_key, now, session_key, now, session_key, recent, now, BATCH_SIZE),
                ).fetchall()
                claimed = []
                for row in rows:
                    if (
                        row["owner_session"] == session_key
                        and row["lease_token"]
                        and row["lease_until"] > now
                    ):
                        claimed.append(_decode(row))
                        continue
                    token = uuid.uuid4().hex
                    db.execute(
                        """UPDATE wisdom_shared_assessment SET owner_session=?,lease_token=?,
                        lease_until=?,updated_at=? WHERE id=?""",
                        (session_key, token, now + LEASE_SECONDS, now, row["id"]),
                    )
                    claimed.append(_decode(db.execute(
                        "SELECT * FROM wisdom_shared_assessment WHERE id=?", (row["id"],)
                    ).fetchone()))
                return claimed
            if db.execute(
                """SELECT 1 FROM wisdom_shared_assessment WHERE organization_id=?
                AND owner_key IS ?
                AND lease_until>? AND lease_token IS NOT NULL
                AND state IN ('assessing','ready','fallback','delivering')
                AND (?=0 OR state='assessing') LIMIT 1""",
                (org, owner_key, now, assessment_only),
            ).fetchone():
                return []
            recent = db.execute(
                """SELECT session_key FROM wisdom_shared_agent_session
                WHERE organization_id=? AND owner_key IS ? AND alive_until>? AND last_activity>0
                ORDER BY last_activity DESC,session_key LIMIT 1""",
                (org, owner_key, now),
            ).fetchone()[0]
            # A send with an unknown outcome must not be blindly replayed.
            db.execute(
                """UPDATE wisdom_shared_assessment SET state='delivery_uncertain',
                lease_token=NULL,lease_until=NULL WHERE organization_id=? AND owner_key IS ?
                AND state='delivering' AND lease_until<=?""",
                (org, owner_key, now),
            )
            rows = db.execute(
                """SELECT * FROM wisdom_shared_assessment WHERE organization_id=?
                AND owner_key IS ?
                AND available_at<=? AND (state IN ('pending','ready','fallback')
                  OR (state='assessing' AND lease_until<=?))
                AND (?=0 OR state IN ('pending','assessing'))
                AND (?=0 OR json_type(reference_json,'$.user_requested')='true')
                AND (?=1 OR state IN ('ready','fallback')
                  OR json_extract(reference_json,'$.kind')='setup_handoff')
                AND ((state='ready' AND (owner_session=? OR
                  (origin_session IS NULL AND ?=? AND NOT EXISTS (
                    SELECT 1 FROM wisdom_shared_agent_session owner WHERE owner.organization_id=wisdom_shared_assessment.organization_id
                    AND owner.session_key=wisdom_shared_assessment.owner_session AND owner.alive_until>?)))) OR (state!='ready' AND
                  ((origin_session IS NULL AND ?=?) OR origin_session IN (?,?))))
                ORDER BY created_at,id LIMIT ?""",
                (
                    org,
                    owner_key,
                    now,
                    now,
                    assessment_only,
                    requested_only,
                    allow_model_work,
                    session_key,
                    session_key,
                    recent,
                    now,
                    session_key,
                    recent,
                    session_key,
                    session["session_id"],
                    BATCH_SIZE,
                ),
            ).fetchall()
            claimed = []
            for row in rows:
                if row["owner_session"] and row["owner_session"] != session_key:
                    db.execute(
                        "UPDATE wisdom_shared_consent SET state='stale' WHERE assessment_id=? AND state='pending'",
                        (row["id"],),
                    )
                token = uuid.uuid4().hex
                state = row["state"]
                needs_assessment = state in {"pending", "assessing"}
                if needs_assessment and row["attempts"] >= MAX_ATTEMPTS:
                    state = "fallback"
                    needs_assessment = False
                else:
                    state = "assessing" if needs_assessment else state
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state=?,owner_session=?,
                    lease_token=?,lease_until=?,attempts=attempts+?,updated_at=?
                    WHERE id=?""",
                    (
                        state,
                        session_key,
                        token,
                        now + LEASE_SECONDS,
                        int(needs_assessment),
                        now,
                        row["id"],
                    ),
                )
                claimed.append(
                    _decode(
                        db.execute(
                            "SELECT * FROM wisdom_shared_assessment WHERE id=?", (row["id"],)
                        ).fetchone()
                    )
                )
            return claimed

    def renew(self, org: str, assessment_id: str, token: str) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            return bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET lease_until=? WHERE id=?
                AND organization_id=? AND lease_token=? AND lease_until>?
                AND state IN ('assessing','ready','fallback')""",
                    (now + LEASE_SECONDS, assessment_id, org, token, now),
                ).rowcount
            )

    def defer_for_preferences(self, org: str, job: dict, until: float) -> None:
        """Release without spending a model attempt; retain advice and unread state."""
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            db.execute(
                """UPDATE wisdom_shared_assessment SET
              state=CASE WHEN state='assessing' THEN 'pending' ELSE state END,
              attempts=MAX(0,attempts-CASE WHEN state='assessing' THEN 1 ELSE 0 END),
              available_at=?,lease_token=NULL,lease_until=NULL,updated_at=?
              WHERE id=? AND organization_id=? AND lease_token=? AND lease_until>?
              AND state IN ('assessing','ready','fallback')""",
                (max(now + 60, until), now, job["id"], org, job["lease_token"], now),
            )

    def cancel_reservation(self, org: str, job: dict, until: float) -> None:
        """Record a definite pre-send outcome against the captured owner lease."""

        now = self.clock()
        with self.store.transaction() as db:
            db.execute(
                """UPDATE wisdom_remote_delivery SET state='outcome',outcome='not_sent',
                available_at=? WHERE organization_id=? AND assessment_id=?
                AND local_token=? AND state IN ('claiming','claimed')""",
                (now, org, job["id"], job["lease_token"]),
            )
            db.execute(
                """UPDATE wisdom_shared_assessment SET lease_token=NULL,lease_until=NULL,
                available_at=?,updated_at=? WHERE id=? AND organization_id=?
                AND owner_key IS ? AND lease_token=? AND state IN ('ready','fallback')""",
                (until, now, job["id"], org, self.owner_key, job["lease_token"]),
            )

    def wait_for_model(self, org: str, job: dict) -> None:
        """Missing session runtime is a wait, not a failed model attempt."""
        now = self.clock()
        with self.store.transaction() as db:
            self.check_claim(db, org, job["id"], job["lease_token"])
            db.execute(
                """UPDATE wisdom_shared_assessment SET state='pending',attempts=MAX(0,attempts-1),
                last_error='session_model_unavailable',available_at=?,updated_at=?,
                lease_token=NULL,lease_until=NULL WHERE id=?""",
                (now + 60, now, job["id"]),
            )

    def save_advice(
        self, org: str, assessment_id: str, token: str, advice: dict[str, Any]
    ) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            return bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET advice_json=?,state='ready',
                updated_at=? WHERE id=? AND organization_id=? AND lease_token=?
                AND lease_until>? AND state='assessing'""",
                    (json.dumps(advice), now, assessment_id, org, token, now),
                ).rowcount
            )

    def fail(self, org: str, assessment_id: str, token: str, reason: str) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            return bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET
                state=CASE WHEN attempts>=? THEN 'fallback' ELSE 'pending' END,
                last_error=?,available_at=?,lease_token=NULL,lease_until=NULL,
                updated_at=? WHERE id=? AND organization_id=? AND lease_token=?
                AND lease_until>? AND state='assessing'""",
                    (
                        MAX_ATTEMPTS,
                        reason[:100],
                        now + 60,
                        now,
                        assessment_id,
                        org,
                        token,
                        now,
                    ),
                ).rowcount
            )

    def begin_delivery(
        self, org: str, assessment_id: str, token: str, *, request_id: str | None = None
    ) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._coalesce_feed(db, org)
            if request_id is not None:
                owned = db.execute(
                    """SELECT 1 FROM wisdom_shared_assessment WHERE id=? AND organization_id=?
                  AND lease_token=? AND lease_until>? AND state IN ('ready','fallback')""",
                    (assessment_id, org, token, now),
                ).fetchone()
                if not owned:
                    return False
                from .delivery_outbox import begin_reserved

                if not begin_reserved(db, org, assessment_id, token, request_id, now):
                    return False
            return bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state='delivering',updated_at=?
                WHERE id=? AND organization_id=? AND lease_token=? AND lease_until>?
                AND state IN ('ready','fallback')""",
                    (now, assessment_id, org, token, now),
                ).rowcount
            )

    def cancel_delivery(self, org: str, assessment_id: str, token: str) -> None:
        """Only before invoking the transport: preserve advice and release a known non-send."""
        with self.store.transaction() as db:
            self._check_org(db, org)
            from .delivery_outbox import cancel_reserved

            now = self.clock()
            cancel_reserved(db, org, assessment_id, token, now)
            db.execute(
                """UPDATE wisdom_shared_assessment SET state=CASE WHEN advice_json IS NULL THEN 'fallback' ELSE 'ready' END,
              lease_token=NULL,lease_until=NULL,available_at=?,updated_at=?
              WHERE id=? AND organization_id=? AND lease_token=? AND state='delivering'""",
                (now + 60, now, assessment_id, org, token),
            )

    def delivery_ready(
        self, org: str, assessment_id: str, token: str, *, user_id: str | None = None
    ) -> bool:
        with self.store.transaction() as db:
            self._check_org(db, org)
            if assessment_id in self._coalesce_feed(db, org):
                return False
            now = self.clock()
            owned = db.execute(
                """SELECT s.platform,s.address_json FROM wisdom_shared_assessment a
              JOIN wisdom_shared_agent_session s ON s.organization_id=a.organization_id AND s.session_key=a.owner_session
              WHERE a.organization_id=? AND a.id=? AND a.lease_token=? AND a.lease_until>?
              AND a.state='delivering'""",
                (org, assessment_id, token, now),
            ).fetchone()
            remote = db.execute(
                "SELECT * FROM wisdom_remote_delivery WHERE organization_id=? AND assessment_id=? AND local_token=?",
                (org, assessment_id, token),
            ).fetchone()
            return bool(
                owned
                and (
                    remote is None
                    or (
                        remote["state"] == "sending"
                        and remote["lease_until"] > now
                        and (user_id is None or remote["user_id"] == user_id)
                        and remote["platform"] == owned["platform"]
                        and json.loads(remote["address_json"])
                        == json.loads(owned["address_json"])
                    )
                )
            )

    def complete_delivery(
        self,
        org: str,
        assessment_id: str,
        token: str,
        *,
        receipt: DeliveryReceipt,
        introduced: bool = False,
    ) -> bool:
        now = self.clock()
        with self.store.transaction() as db:
            if not isinstance(receipt, DeliveryReceipt):
                raise ValueError("a validated delivery receipt is required")
            from .delivery_outbox import stage_outcome

            staged = stage_outcome(
                db,
                org,
                assessment_id,
                token,
                now,
                receipt=receipt,
                introduced=introduced,
            )
            # Sign-out cannot retract a dispatched send. Persist its receipt
            # against the immutable reservation, but do not revive the session.
            active = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            if active is None or active[0] != org:
                return False
            owner = db.execute(
                """SELECT a.owner_session,s.platform,s.address_json
                FROM wisdom_shared_assessment a JOIN wisdom_shared_agent_session s
                  ON s.organization_id=a.organization_id AND s.session_key=a.owner_session
                WHERE a.id=? AND a.organization_id=? AND a.lease_token=?
                AND a.state='delivering' AND a.lease_until>?""",
                (assessment_id, org, token, now),
            ).fetchone()
            if owner is None:
                return False
            address = json.loads(owner["address_json"])
            if (
                receipt.platform != owner["platform"]
                or receipt.destination != address.get("chat_id", "")
                or receipt.thread_id != address.get("thread_id", "")
                or receipt.scope_id != address.get("scope_id", "")
            ):
                if staged:
                    # The session moved during the send. Recover the receipt using
                    # the immutable reservation address, not the new destination.
                    return False
                raise ValueError("delivery receipt does not match the owning session")
            changed = bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state='delivered',delivered_at=?,
                updated_at=?,lease_token=NULL,lease_until=NULL
                WHERE id=? AND organization_id=? AND lease_token=?
                AND state='delivering' AND lease_until>?""",
                    (now, now, assessment_id, org, token, now),
                ).rowcount
            )
            if changed and introduced:
                db.execute(
                    "INSERT OR IGNORE INTO wisdom_shared_agent_introduction VALUES(?,?)",
                    (org, now),
                )
            if changed:
                db.execute(
                    "INSERT INTO wisdom_shared_delivery_receipt VALUES(?,?,?,?,?)",
                    (
                        assessment_id,
                        org,
                        owner["owner_session"],
                        receipt.model_dump_json(),
                        now,
                    ),
                )
                event_key = db.execute(
                    "SELECT event_key FROM wisdom_shared_assessment WHERE id=?",
                    (assessment_id,),
                ).fetchone()[0]
                if event_key.startswith("outcome:"):
                    db.execute(
                        "UPDATE wisdom_shared_consent_outcome SET delivered_at=? WHERE interaction_id=? AND organization_id=?",
                        (now, event_key.removeprefix("outcome:"), org),
                    )
            return changed

    def uncertain_delivery(
        self, org: str, assessment_id: str, token: str,
        *, receipt: DeliveryReceipt | None = None,
    ) -> bool:
        with self.store.transaction() as db:
            self._check_org(db, org)
            from .delivery_outbox import stage_outcome

            stage_outcome(
                db, org, assessment_id, token, self.clock(), receipt=receipt
            )
            return bool(
                db.execute(
                    """UPDATE wisdom_shared_assessment SET state='delivery_uncertain',
                lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE id=? AND organization_id=? AND lease_token=? AND state='delivering'""",
                    (self.clock(), assessment_id, org, token),
                ).rowcount
            )

    def bind_candidate_policy(
        self, org: str, assessment_id: str, token: str, days: int
    ) -> dict[str, Any]:
        """Bind a verified policy snapshot before a candidate card can be sent."""

        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise ValueError("Invalid candidate suppression policy")
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            row = db.execute(
                """SELECT * FROM wisdom_shared_assessment WHERE id=?
                AND organization_id=? AND owner_key IS ? AND lease_token=?
                AND lease_until>? AND state IN ('ready','fallback')""",
                (assessment_id, org, self.owner_key, token, now),
            ).fetchone()
            if row is None:
                raise PermissionError("Collective Wisdom candidate assessment changed.")
            value = _decode(row)
            if value.get("reference", {}).get("kind") != "candidate":
                return value
            if not isinstance(value.get("advice"), dict):
                raise PermissionError("Fallback candidate advice cannot be deferred natively.")
            reference = value["reference"]
            reference["not_now_suppression_days"] = days
            changed = db.execute(
                """UPDATE wisdom_shared_assessment SET reference_json=?,updated_at=?
                WHERE id=? AND organization_id=? AND owner_key IS ? AND lease_token=?
                AND lease_until>? AND state='ready'""",
                (json.dumps(reference, sort_keys=True), now, assessment_id, org,
                 self.owner_key, token, now),
            ).rowcount
            if changed != 1:
                raise PermissionError("Collective Wisdom candidate assessment changed.")
            return _decode(db.execute(
                "SELECT * FROM wisdom_shared_assessment WHERE id=?", (assessment_id,)
            ).fetchone())

    def candidate_callback_record(
        self, org: str, assessment_id: str
    ) -> dict[str, Any]:
        """Load one exact delivered candidate for authenticated native callback parsing."""

        self._require_owner()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            row = db.execute(
                """SELECT a.*,s.platform,s.actor_id,s.address_json,r.receipt_json
                FROM wisdom_shared_assessment a
                JOIN wisdom_shared_agent_session s
                  ON s.organization_id=a.organization_id AND s.session_key=a.owner_session
                JOIN wisdom_shared_delivery_receipt r
                  ON r.assessment_id=a.id AND r.organization_id=a.organization_id
                WHERE a.id=? AND a.organization_id=? AND a.owner_key IS ?
                AND a.state IN ('delivered','deferred')""",
                (assessment_id, org, self.owner_key),
            ).fetchone()
            if row is None:
                raise PermissionError("Collective Wisdom candidate assessment was not found.")
            return _decode(row)

    def defer_candidate(
        self, org: str, assessment_id: str, invocation: Any | None, *, stage: Any
    ) -> tuple[dict[str, Any], bool]:
        """Atomically fence one delivered candidate and stage its offline suppression."""

        owner = self.owner
        if owner is None:
            raise PermissionError("Collective Wisdom candidate owner is unavailable.")
        self._require_owner()
        now = self.clock()
        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            active = db.execute(
                "SELECT installation_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            row = db.execute(
                """SELECT a.*,s.platform,s.actor_id,s.address_json,r.receipt_json
                FROM wisdom_shared_assessment a
                JOIN wisdom_shared_agent_session s
                  ON s.organization_id=a.organization_id AND s.session_key=a.owner_session
                JOIN wisdom_shared_delivery_receipt r
                  ON r.assessment_id=a.id AND r.organization_id=a.organization_id
                WHERE a.id=? AND a.organization_id=? AND a.owner_key IS ?
                AND a.state IN ('delivered','deferred')""",
                (assessment_id, org, self.owner_key),
            ).fetchone()
            if row is None or active is None or active[0] != owner["installation_id"]:
                raise PermissionError("Collective Wisdom candidate assessment is stale.")
            value = _decode(row)
            reference = value.get("reference")
            if not isinstance(reference, dict) or reference.get("kind") != "candidate":
                raise PermissionError("Collective Wisdom control is not a candidate deferral.")
            days = reference.get("not_now_suppression_days")
            if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
                raise PermissionError("Collective Wisdom candidate policy snapshot is unavailable.")
            if invocation is not None:
                address = json.loads(row["address_json"])
                receipt = DeliveryReceipt.model_validate_json(row["receipt_json"])
                expected_route = (
                    owner["profile_name"], row["owner_session"], row["actor_id"],
                    row["platform"], address.get("chat_id", ""),
                    address.get("thread_id", ""), address.get("scope_id", ""),
                    receipt.message_id,
                )
                actual_route = (
                    invocation.profile, invocation.session_key, invocation.actor_id,
                    invocation.platform, invocation.chat_id, invocation.thread_id,
                    invocation.scope_id, invocation.message_id,
                )
                if actual_route != expected_route or (
                    receipt.platform != invocation.platform
                    or receipt.destination != invocation.chat_id
                    or receipt.thread_id != invocation.thread_id
                    or receipt.scope_id != invocation.scope_id
                ):
                    raise PermissionError("Collective Wisdom candidate control route changed.")
            local = db.execute(
                """SELECT canonical_path,current_hash,deleted_at,source_kind
                FROM local_skill WHERE id=?""",
                (reference.get("local_skill_id"),),
            ).fetchone()
            event = db.execute(
                """SELECT kind,state,organization_id,skill_id,content_hash
                FROM local_event WHERE id=?""", (reference.get("event_id"),)
            ).fetchone()
            local_changed = (
                local is None or local["deleted_at"] is not None
                or local["source_kind"] != "local"
                or local["current_hash"] != reference.get("content_hash")
            )
            if not local_changed:
                assert local is not None
                from pathlib import Path
                from .qualification import UnsafeSnapshotError, snapshot_tree

                try:
                    current_hash, _tree = snapshot_tree(Path(local["canonical_path"]))
                except (OSError, UnsafeSnapshotError):
                    local_changed = True
                else:
                    local_changed = current_hash != reference.get("content_hash")
            event_changed = (
                event is None or event["kind"] != "wisdom.candidate"
                or event["state"] != "unread" or event["organization_id"] != org
                or event["skill_id"] != reference.get("local_skill_id")
                or event["content_hash"] != reference.get("content_hash")
            )
            if local_changed or event_changed:
                raise PermissionError("Collective Wisdom candidate source changed.")
            if row["state"] == "deferred":
                return value, False
            stage(db, reference, days)
            changed = db.execute(
                """UPDATE wisdom_shared_assessment SET state='deferred',available_at=?,
                event_key=event_key||':deferred:'||id,last_error=NULL,updated_at=?
                WHERE id=? AND organization_id=? AND owner_key IS ? AND state='delivered'""",
                (now + days * 86400, now, assessment_id, org, self.owner_key),
            ).rowcount
            if changed != 1:
                raise PermissionError("Collective Wisdom candidate assessment changed.")
            return _decode(db.execute(
                "SELECT * FROM wisdom_shared_assessment WHERE id=?", (assessment_id,)
            ).fetchone()), True

    def defer_candidate_deliberate(
        self, org: str, assessment_id: str, *, stage: Any
    ) -> tuple[dict[str, Any], bool]:
        """Apply the same verified candidate cooldown for an explicit local slash action."""

        return self.defer_candidate(org, assessment_id, None, stage=stage)

    def candidate_edit_uncertain(
        self, org: str, assessment_id: str, *, error: str
    ) -> None:
        """Record an unknown original-card edit outcome without making it retryable."""

        with self.store.transaction() as db:
            self._check_org(db, org)
            self._check_bound_owner(db)
            changed = db.execute(
                """UPDATE wisdom_shared_assessment SET last_error=?,updated_at=?
                WHERE id=? AND organization_id=? AND owner_key IS ? AND state='deferred'""",
                ((error or "unknown")[:100], self.clock(), assessment_id, org, self.owner_key),
            ).rowcount
            if changed != 1:
                raise PermissionError("Collective Wisdom candidate assessment changed.")

    def introduced(self, org: str) -> bool:
        with self.store.transaction() as db:
            self._check_org(db, org)
            return (
                db.execute(
                    "SELECT 1 FROM wisdom_shared_agent_introduction WHERE organization_id=?",
                    (org,),
                ).fetchone()
                is not None
            )

    def assessments(self, org: str) -> list[dict[str, Any]]:
        owner_key = self._require_owner() if self.owner is not None else None
        with self.store.transaction() as db:
            self._check_org(db, org)
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='wisdom_shared_assessment'"
            ).fetchone() is None:
                return []
            return [
                _decode(row)
                for row in db.execute(
                    """SELECT * FROM wisdom_shared_assessment
                    WHERE organization_id=? AND owner_key IS ? ORDER BY created_at,id""",
                    (org, owner_key),
                ).fetchall()
            ]
