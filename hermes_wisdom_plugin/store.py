"""Profile-scoped crash-safe SQLite state for Collective Wisdom."""

# The extracted store intentionally retains the pinned, broad persistence API.
# pylint: disable=line-too-long,missing-function-docstring,too-many-arguments
# pylint: disable=too-many-lines,too-many-locals,too-many-public-methods

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import WisdomConflict


SCHEMA_FAMILY = "hermes_wisdom_plugin.profile_store"
SCHEMA_VERSION = 5
_OWNER_KEYS = (
    "profile_name", "profile_home", "account_id",
    "organization_id", "base_url", "installation_id",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def filesystem_identity(path: Path) -> str | None:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else None


def _encode_owner(owner: dict[str, str] | None) -> str | None:
    if owner is None:
        return None
    if not isinstance(owner, dict) or tuple(sorted(owner)) != tuple(sorted(_OWNER_KEYS)):
        raise ValueError("invalid managed-install owner binding")
    if any(not isinstance(owner[key], str) or not owner[key].strip() for key in _OWNER_KEYS):
        raise ValueError("invalid managed-install owner binding")
    return json.dumps(owner, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode_install_row(row: dict[str, Any]) -> None:
    row["baseline"] = json.loads(row.pop("baseline_json"))
    owner_json = row.pop("owner_json")
    row["owner"] = json.loads(owner_json) if owner_json is not None else None


def _draft_owner_key(draft_id: str) -> str:
    return f"contribution_draft_owner:{draft_id}"


class UnsupportedSchemaError(RuntimeError):
    """The database is not an exact current-schema profile store."""


BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plugin_schema_identity (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  family TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0)
);
CREATE TABLE IF NOT EXISTS local_skill (
  id TEXT PRIMARY KEY,
  canonical_path TEXT NOT NULL,
  fs_identity TEXT,
  current_hash TEXT,
  source_kind TEXT NOT NULL,
  deleted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS local_skill_fs_identity
  ON local_skill(fs_identity) WHERE fs_identity IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS local_skill_active_path
  ON local_skill(canonical_path) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS snapshot (
  skill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  tree_json TEXT NOT NULL DEFAULT '{}',
  skill_text TEXT,
  PRIMARY KEY(skill_id, content_hash),
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS candidate (
  skill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  qualification TEXT NOT NULL,
  state TEXT NOT NULL,
  suggested_at TEXT,
  dismissed_at TEXT,
  PRIMARY KEY(skill_id, content_hash, qualification),
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS local_draft (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  overlay_path TEXT NOT NULL,
  draft_commit TEXT,
  server_revision TEXT,
  state TEXT NOT NULL,
  description TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  description_hash TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS review_receipt (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL UNIQUE,
  server_revision TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  description_hash TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  reviewed_at TEXT NOT NULL,
  consumed_at TEXT,
  FOREIGN KEY(draft_id) REFERENCES local_draft(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS installation_identity (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  installation_id TEXT NOT NULL,
  verified_org_id TEXT,
  verified_at TEXT,
  disclosure_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS managed_install (
  skill_id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version > 0),
  content_hash TEXT NOT NULL,
  baseline_json TEXT NOT NULL,
  target_path TEXT NOT NULL UNIQUE,
  update_mode TEXT NOT NULL,
  state TEXT NOT NULL,
  installed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  owner_json TEXT
);
CREATE TABLE IF NOT EXISTS operation_journal (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS operation_journal_one_pending
  ON operation_journal(kind,entity_id) WHERE state='pending';
CREATE TABLE IF NOT EXISTS usage_day (
  skill_id TEXT NOT NULL,
  day_local TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  use_count INTEGER NOT NULL CHECK(use_count >= 0),
  PRIMARY KEY(skill_id, timezone_name, day_local),
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS refinement (
  skill_id TEXT NOT NULL,
  from_hash TEXT NOT NULL,
  to_hash TEXT NOT NULL,
  classification TEXT NOT NULL,
  structural_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(skill_id, to_hash),
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stability_job (
  skill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  due_at TEXT NOT NULL,
  state TEXT NOT NULL,
  evaluated_at TEXT,
  session_id TEXT,
  task_id TEXT,
  PRIMARY KEY(skill_id, content_hash),
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS local_event (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  session_id TEXT,
  task_id TEXT,
  skill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  qualification TEXT NOT NULL,
  organization_id TEXT,
  qualification_sequence INTEGER,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  telegram_delivered_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS local_event_org_identity
  ON local_event(kind,COALESCE(organization_id,''),skill_id,
  content_hash,qualification);
CREATE UNIQUE INDEX IF NOT EXISTS local_event_org_qualification_sequence
  ON local_event(organization_id,qualification_sequence)
  WHERE kind='wisdom.candidate' AND qualification_sequence IS NOT NULL;
CREATE TABLE IF NOT EXISTS local_event_delivery (
  event_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  delivered_at TEXT NOT NULL,
  PRIMARY KEY(event_id, surface),
  FOREIGN KEY(event_id) REFERENCES local_event(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS wisdom_organization (
  organization_id TEXT PRIMARY KEY,
  display_name TEXT,
  resolved INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  cursor TEXT,
  updated_at TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  organization_id TEXT,
  installation_id TEXT,
  owner_user_id TEXT,
  base_url TEXT,
  resume_required INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feed_event (
  event_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  version INTEGER,
  installation_id TEXT,
  update_mode TEXT,
  payload_json TEXT NOT NULL,
  cadence TEXT NOT NULL,
  due_at TEXT NOT NULL,
  local_seen_at TEXT,
  telegram_delivered_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_event_delivery (
  event_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  delivered_at TEXT NOT NULL,
  PRIMARY KEY(event_id, surface),
  FOREIGN KEY(event_id) REFERENCES feed_event(event_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS operation_lock (
  entity_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS professionalism_review (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  author_description_hash TEXT NOT NULL,
  package_json TEXT NOT NULL,
  author_description TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  result_json TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(skill_id, content_hash, author_description_hash)
);
CREATE INDEX IF NOT EXISTS professionalism_review_queue
  ON professionalism_review(state, available_at);
"""

NATIVE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wisdom_agent_session (
  organization_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  profile TEXT NOT NULL,
  address_json TEXT NOT NULL,
  last_activity_at REAL NOT NULL,
  PRIMARY KEY(organization_id, session_key)
);
CREATE TABLE IF NOT EXISTS wisdom_consent (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  owner_session TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  profile TEXT NOT NULL,
  operation TEXT NOT NULL,
  state TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  result_json TEXT,
  expires_at REAL NOT NULL,
  delivery_state TEXT NOT NULL,
  delivery_error TEXT,
  request_message_id TEXT,
  message_id TEXT,
  checks_expanded INTEGER NOT NULL DEFAULT 0,
  inspection_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  delivery_revision INTEGER NOT NULL DEFAULT 0,
  delivery_attempt INTEGER NOT NULL DEFAULT 0,
  assessment_expanded INTEGER NOT NULL DEFAULT 0,
  state_revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS wisdom_consent_owner
  ON wisdom_consent(organization_id,owner_session,created_at);
CREATE TABLE IF NOT EXISTS wisdom_setup_handoff (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  consent_id TEXT NOT NULL UNIQUE,
  owner_session TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  state TEXT NOT NULL,
  lease_token TEXT,
  lease_until REAL,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(consent_id) REFERENCES wisdom_consent(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS wisdom_setup_handoff_queue
  ON wisdom_setup_handoff(organization_id,owner_session,state,created_at);
"""

WEEKLY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wisdom_assessment (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  origin_session TEXT,
  reference_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  owner_session TEXT,
  advice_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(organization_id,event_key)
);
CREATE INDEX IF NOT EXISTS wisdom_assessment_queue
  ON wisdom_assessment(organization_id,state,created_at);
CREATE TABLE IF NOT EXISTS wisdom_candidate_defer (
  assessment_id TEXT PRIMARY KEY,
  suppress_until REAL NOT NULL,
  deferred_at REAL NOT NULL,
  FOREIGN KEY(assessment_id) REFERENCES wisdom_assessment(id) ON DELETE CASCADE
);
"""

CURRENT_SCHEMA_SQL = BASE_SCHEMA_SQL + NATIVE_SCHEMA_SQL + WEEKLY_SCHEMA_SQL


class WisdomStore:
    """Profile-local SQLite persistence rooted in host-provided plugin data."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "wisdom.db"
        self._lock = threading.RLock()
        self._prepare()

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        with self.transaction() as db:
            existing_version = self._existing_schema_version(db)
            if existing_version is None:
                self._create_current_schema(db)
                db.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                db.execute(
                    "INSERT INTO plugin_schema_identity(singleton,family,version) "
                    "VALUES(1,?,?)",
                    (SCHEMA_FAMILY, SCHEMA_VERSION),
                )
            elif existing_version in {1, 2, 3, 4}:
                if existing_version == 1:
                    db.execute("ALTER TABLE managed_install ADD COLUMN owner_json TEXT")
                upgrade_sql = (
                    NATIVE_SCHEMA_SQL + WEEKLY_SCHEMA_SQL
                    if existing_version < 3
                    else WEEKLY_SCHEMA_SQL
                )
                for statement in upgrade_sql.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("ALTER TABLE feed_state ADD COLUMN owner_user_id TEXT")
                db.execute("ALTER TABLE feed_state ADD COLUMN base_url TEXT")
                db.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                db.execute(
                    "UPDATE plugin_schema_identity SET version=? WHERE singleton=1",
                    (SCHEMA_VERSION,),
                )
                self._validate_current_schema(db)
        self._secure_database_files()

    @staticmethod
    def _create_current_schema(db: sqlite3.Connection) -> None:
        for statement in CURRENT_SCHEMA_SQL.split(";"):
            if statement.strip():
                db.execute(statement)

    @classmethod
    def _existing_schema_version(cls, db: sqlite3.Connection) -> int | None:
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if not tables:
            return None
        if "plugin_schema_identity" not in tables:
            raise UnsupportedSchemaError(
                "Refusing database without the Collective Wisdom plugin schema marker"
            )
        identity = db.execute(
            "SELECT family,version,typeof(version) FROM plugin_schema_identity "
            "WHERE singleton=1"
        ).fetchone()
        if identity is None:
            raise UnsupportedSchemaError("Malformed Collective Wisdom plugin schema marker")
        if identity[0] != SCHEMA_FAMILY:
            raise UnsupportedSchemaError(
                f"Foreign schema family {identity[0]!r}; expected {SCHEMA_FAMILY!r}"
            )
        if identity[2] != "integer" or identity[1] not in (1, 2, 3, 4, SCHEMA_VERSION):
            raise UnsupportedSchemaError(
                f"Unsupported plugin schema version {identity[1]!r}; "
                f"expected integer 1, 2, 3, 4, or {SCHEMA_VERSION}"
            )
        if "schema_meta" not in tables:
            raise UnsupportedSchemaError("Incomplete Collective Wisdom plugin schema")
        metadata = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if metadata is None or metadata[0] != str(identity[1]):
            raise UnsupportedSchemaError(
                "Malformed Collective Wisdom plugin schema version metadata"
            )
        if identity[1] == 1:
            cls._validate_v1_schema(db)
        elif identity[1] == 2:
            cls._validate_v2_schema(db)
        elif identity[1] == 3:
            cls._validate_v3_schema(db)
        elif identity[1] == 4:
            cls._validate_v4_schema(db)
        else:
            cls._validate_current_schema(db)
        return int(identity[1])

    @classmethod
    def _validate_v1_schema(cls, db: sqlite3.Connection) -> None:
        expected_db = sqlite3.connect(":memory:")
        try:
            v1_sql = cls._without_feed_owner_columns(
                CURRENT_SCHEMA_SQL.replace(NATIVE_SCHEMA_SQL, "").replace(
                    WEEKLY_SCHEMA_SQL, ""
                )
            ).replace(
                "  updated_at TEXT NOT NULL,\n  owner_json TEXT\n",
                "  updated_at TEXT NOT NULL\n",
                1,
            )
            for statement in v1_sql.split(";"):
                if statement.strip():
                    expected_db.execute(statement)
            expected = cls._schema_objects(expected_db)
        finally:
            expected_db.close()
        actual = cls._schema_objects(db)
        invalid = sorted(
            name for name, definition in expected.items() if actual.get(name) != definition
        )
        if invalid:
            raise UnsupportedSchemaError(
                "Incomplete or incompatible Collective Wisdom plugin schema objects: "
                + ", ".join(invalid)
            )

    @classmethod
    def _validate_v2_schema(cls, db: sqlite3.Connection) -> None:
        expected_db = sqlite3.connect(":memory:")
        try:
            expected_db.executescript(cls._without_feed_owner_columns(
                CURRENT_SCHEMA_SQL.replace(NATIVE_SCHEMA_SQL, "").replace(
                    WEEKLY_SCHEMA_SQL, ""
                )
            ))
            expected = cls._schema_objects(expected_db)
        finally:
            expected_db.close()
        actual = cls._schema_objects(db)
        invalid = sorted(
            name for name, definition in expected.items() if actual.get(name) != definition
        )
        if invalid:
            raise UnsupportedSchemaError(
                "Incomplete or incompatible Collective Wisdom plugin schema objects: "
                + ", ".join(invalid)
            )

    @classmethod
    def _validate_v3_schema(cls, db: sqlite3.Connection) -> None:
        expected_db = sqlite3.connect(":memory:")
        try:
            expected_db.executescript(cls._without_feed_owner_columns(
                CURRENT_SCHEMA_SQL.replace(WEEKLY_SCHEMA_SQL, "")
            ))
            expected = cls._schema_objects(expected_db)
        finally:
            expected_db.close()
        actual = cls._schema_objects(db)
        invalid = sorted(
            name for name, definition in expected.items() if actual.get(name) != definition
        )
        if invalid:
            raise UnsupportedSchemaError(
                "Incomplete or incompatible Collective Wisdom plugin schema objects: "
                + ", ".join(invalid)
            )

    @classmethod
    def _validate_v4_schema(cls, db: sqlite3.Connection) -> None:
        expected_db = sqlite3.connect(":memory:")
        try:
            expected_db.executescript(cls._without_feed_owner_columns(CURRENT_SCHEMA_SQL))
            expected = cls._schema_objects(expected_db)
        finally:
            expected_db.close()
        actual = cls._schema_objects(db)
        invalid = sorted(
            name for name, definition in expected.items() if actual.get(name) != definition
        )
        if invalid:
            raise UnsupportedSchemaError(
                "Incomplete or incompatible Collective Wisdom plugin schema objects: "
                + ", ".join(invalid)
            )

    @staticmethod
    def _without_feed_owner_columns(sql: str) -> str:
        return sql.replace("  owner_user_id TEXT,\n  base_url TEXT,\n", "")

    @classmethod
    def _validate_current_schema(cls, db: sqlite3.Connection) -> None:
        expected_db = sqlite3.connect(":memory:")
        try:
            cls._create_current_schema(expected_db)
            expected = cls._schema_objects(expected_db)
        finally:
            expected_db.close()
        actual = cls._schema_objects(db)
        invalid = sorted(
            name for name, definition in expected.items() if actual.get(name) != definition
        )
        if "feed_state" in invalid:
            migrated_db = sqlite3.connect(":memory:")
            try:
                migrated_db.executescript(
                    cls._without_feed_owner_columns(CURRENT_SCHEMA_SQL)
                )
                migrated_db.execute(
                    "ALTER TABLE feed_state ADD COLUMN owner_user_id TEXT"
                )
                migrated_db.execute("ALTER TABLE feed_state ADD COLUMN base_url TEXT")
                migrated = cls._schema_objects(migrated_db)["feed_state"]
            finally:
                migrated_db.close()
            if actual.get("feed_state") == migrated:
                invalid.remove("feed_state")
        if invalid == ["managed_install"]:
            migrated_db = sqlite3.connect(":memory:")
            try:
                v1_sql = CURRENT_SCHEMA_SQL.replace(
                    "  updated_at TEXT NOT NULL,\n  owner_json TEXT\n",
                    "  updated_at TEXT NOT NULL\n",
                    1,
                )
                migrated_db.executescript(v1_sql)
                migrated_db.execute(
                    "ALTER TABLE managed_install ADD COLUMN owner_json TEXT"
                )
                migrated = cls._schema_objects(migrated_db)["managed_install"]
            finally:
                migrated_db.close()
            if actual.get("managed_install") == migrated:
                invalid = []
        if invalid:
            raise UnsupportedSchemaError(
                "Incomplete or incompatible Collective Wisdom plugin schema objects: "
                + ", ".join(invalid)
            )

    @staticmethod
    def _schema_objects(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
        rows = db.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE type IN ('table','index') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            str(row[1]): (str(row[0]), " ".join(str(row[2]).split())) for row in rows
        }

    def _secure_database_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            self._secure_database_files()
            try:
                yield db
                if db.in_transaction:
                    db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
                self._secure_database_files()

    def get_metadata(self, key: str) -> dict[str, Any] | None:
        """Read one plugin-owned JSON metadata record without changing its state."""
        if key == "schema_version":
            raise ValueError("schema_version is reserved")
        with self.transaction() as db:
            row = db.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"])
        if not isinstance(value, dict):
            raise ValueError("invalid plugin metadata")
        return value

    def set_metadata(
        self, key: str, value: dict[str, Any], *, _db: sqlite3.Connection | None = None
    ) -> None:
        """Persist metadata, optionally alongside its operation journal transaction."""
        if key == "schema_version" or not isinstance(value, dict):
            raise ValueError("invalid plugin metadata")
        if _db is None:
            with self.transaction() as db:
                self.set_metadata(key, value, _db=db)
            return
        _db.execute(
            "INSERT INTO schema_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, sort_keys=True, allow_nan=False)),
        )

    def bind_qualification_owner(self, owner: dict[str, str]) -> bool:
        """Bind local qualification evidence to one exact enrolled account.

        A changed owner keeps historical rows for audit, but tombstones their local-skill
        identities and retires unfinished recommendation work so a new enrolment starts with
        fresh counters instead of inheriting another account's evidence.
        """
        encoded = _encode_owner(owner)
        if encoded is None:  # pragma: no cover - guarded by _encode_owner
            raise ValueError("qualification owner is required")
        key = "qualification_owner"
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
            previous = str(row[0]) if row else None
            if previous == encoded:
                return False
            if previous is not None:
                db.execute(
                    "UPDATE local_skill SET deleted_at=?,updated_at=? WHERE deleted_at IS NULL",
                    (now, now),
                )
                db.execute(
                    "UPDATE wisdom_assessment SET state='retired',owner_session=NULL,updated_at=? "
                    "WHERE state NOT IN ('retired','reviewed') AND "
                    "json_extract(reference_json,'$.kind') IN ('weekly_review','candidate')",
                    (datetime.now(timezone.utc).timestamp(),),
                )
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
        return previous is not None

    def register_skill(
        self,
        path: Path,
        *,
        content_hash: str | None,
        source_kind: str,
        tree: dict[str, str] | None = None,
        snapshot_text: str | None = None,
    ) -> str:
        resolved = path.resolve()
        fs_identity = filesystem_identity(resolved)
        now = utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM local_skill WHERE canonical_path=? AND deleted_at IS NULL",
                (str(resolved),),
            ).fetchone()
            if row is None and fs_identity:
                matches = db.execute(
                    "SELECT id FROM local_skill WHERE fs_identity=? AND deleted_at IS NULL",
                    (fs_identity,),
                ).fetchall()
                row = matches[0] if len(matches) == 1 else None
            if row is None and content_hash:
                matches = db.execute(
                    "SELECT id FROM local_skill WHERE current_hash=? AND canonical_path<>? AND deleted_at IS NOT NULL",
                    (content_hash, str(resolved)),
                ).fetchall()
                row = matches[0] if len(matches) == 1 else None
            skill_id = str(row["id"]) if row else str(uuid.uuid4())
            if row:
                db.execute(
                    "UPDATE local_skill SET canonical_path=?,fs_identity=?,current_hash=?,"
                    "source_kind=?,deleted_at=NULL,updated_at=? WHERE id=?",
                    (
                        str(resolved),
                        fs_identity,
                        content_hash,
                        source_kind,
                        now,
                        skill_id,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO local_skill VALUES(?,?,?,?,?,?,?,?)",
                    (
                        skill_id,
                        str(resolved),
                        fs_identity,
                        content_hash,
                        source_kind,
                        None,
                        now,
                        now,
                    ),
                )
            if content_hash:
                db.execute(
                    "INSERT INTO snapshot(skill_id,content_hash,captured_at,tree_json,skill_text) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(skill_id,content_hash) DO UPDATE SET "
                    "tree_json=excluded.tree_json,skill_text=COALESCE(snapshot.skill_text,excluded.skill_text)",
                    (
                        skill_id,
                        content_hash,
                        now,
                        json.dumps(tree or {}, sort_keys=True),
                        snapshot_text,
                    ),
                )
        return skill_id

    def local_skill(self, skill_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_skill WHERE id=?", (skill_id,)
            ).fetchone()
            return dict(row) if row else None

    def latest_snapshot(self, skill_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM snapshot WHERE skill_id=? ORDER BY captured_at DESC LIMIT 1",
                (skill_id,),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            value["tree"] = json.loads(value.pop("tree_json"))
            return value

    def record_usage_day(
        self,
        skill_id: str,
        day_local: str,
        *,
        timezone_name: str,
        retain_after: str,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO usage_day VALUES(?,?,?,1) "
                "ON CONFLICT(skill_id,timezone_name,day_local) "
                "DO UPDATE SET use_count=MIN(use_count+1,2147483647)",
                (skill_id, day_local, timezone_name),
            )
            db.execute(
                "DELETE FROM usage_day WHERE skill_id=? AND day_local<?",
                (skill_id, retain_after),
            )

    def usage_days(self, skill_id: str, *, since: str, timezone_name: str) -> list[str]:
        with self.transaction() as db:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT day_local FROM usage_day WHERE skill_id=? "
                    "AND timezone_name=? AND day_local>=? ORDER BY day_local",
                    (skill_id, timezone_name, since),
                ).fetchall()
            ]

    def record_refinement(
        self,
        skill_id: str,
        *,
        from_hash: str,
        to_hash: str,
        classification: str,
        structural: dict[str, Any],
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO refinement VALUES(?,?,?,?,?,?)",
                (
                    skill_id,
                    from_hash,
                    to_hash,
                    classification,
                    json.dumps(structural, sort_keys=True),
                    utc_now(),
                ),
            )

    def meaningful_refinement_count(self, skill_id: str, *, since: str) -> int:
        with self.transaction() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM refinement WHERE skill_id=? AND classification='meaningful' "
                "AND recorded_at>=?",
                (skill_id, since),
            ).fetchone()
            return int(row[0]) if row else 0

    def resolve_ambiguous_refinement(
        self,
        skill_id: str,
        *,
        from_hash: str,
        to_hash: str,
        classification: str,
        structural: dict[str, Any],
        owner: dict[str, str],
        due_at: str | None,
        session_id: str | None,
        task_id: str | None,
    ) -> bool:
        """Apply one advisory result only to its exact current owner/content snapshot."""
        if classification not in {"meaningful", "non_meaningful"}:
            raise ValueError("invalid refinement classification")
        encoded_owner = _encode_owner(owner)
        if encoded_owner is None:
            return False
        with self.transaction() as db:
            bound = db.execute(
                "SELECT value FROM schema_meta WHERE key='qualification_owner'"
            ).fetchone()
            skill = db.execute(
                "SELECT current_hash,deleted_at FROM local_skill WHERE id=?", (skill_id,)
            ).fetchone()
            refinement = db.execute(
                "SELECT classification FROM refinement "
                "WHERE skill_id=? AND from_hash=? AND to_hash=?",
                (skill_id, from_hash, to_hash),
            ).fetchone()
            owner_matches = bound is not None and str(bound[0]) == encoded_owner
            skill_matches = (
                skill is not None
                and skill["deleted_at"] is None
                and str(skill["current_hash"] or "") == to_hash
            )
            refinement_matches = (
                refinement is not None and refinement["classification"] == "ambiguous"
            )
            if not all((owner_matches, skill_matches, refinement_matches)):
                return False
            db.execute(
                "UPDATE refinement SET classification=?,structural_json=? "
                "WHERE skill_id=? AND from_hash=? AND to_hash=? AND classification='ambiguous'",
                (
                    classification, json.dumps(structural, sort_keys=True),
                    skill_id, from_hash, to_hash,
                ),
            )
            if classification == "meaningful" and due_at:
                db.execute(
                    "INSERT INTO stability_job(skill_id,content_hash,due_at,state,evaluated_at,session_id,task_id) "
                    "VALUES(?,?,?,'pending',NULL,?,?) ON CONFLICT(skill_id,content_hash) "
                    "DO UPDATE SET due_at=excluded.due_at,state='pending',evaluated_at=NULL,"
                    "session_id=excluded.session_id,task_id=excluded.task_id",
                    (skill_id, to_hash, due_at, session_id, task_id),
                )
            return True

    def schedule_stability(
        self,
        skill_id: str,
        content_hash: str,
        due_at: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO stability_job(skill_id,content_hash,due_at,state,evaluated_at,session_id,task_id) "
                "VALUES(?,?,?,'pending',NULL,?,?) ON CONFLICT(skill_id,content_hash) "
                "DO UPDATE SET due_at=excluded.due_at,state='pending',evaluated_at=NULL,"
                "session_id=excluded.session_id,task_id=excluded.task_id",
                (skill_id, content_hash, due_at, session_id, task_id),
            )

    def due_stability_jobs(self, now: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM stability_job WHERE state='pending' AND due_at<=? ORDER BY due_at",
                    (now,),
                ).fetchall()
            ]

    def finish_stability_job(self, skill_id: str, content_hash: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE stability_job SET state='done',evaluated_at=? WHERE skill_id=? AND content_hash=?",
                (utc_now(), skill_id, content_hash),
            )

    def emit_local_event(
        self,
        *,
        kind: str,
        skill_id: str,
        content_hash: str,
        payload: dict[str, Any],
        session_id: str | None,
        task_id: str | None,
        qualification: str,
        _db: sqlite3.Connection | None = None,
    ) -> str | None:
        event_id = str(uuid.uuid4())
        now = utc_now()
        with (self.transaction() if _db is None else nullcontext(_db)) as db:
            organization_id: str | None = None
            if kind == "wisdom.candidate":
                organization = db.execute(
                    "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
                ).fetchone()
                organization_id = (
                    str(organization[0])
                    if organization is not None and organization[0]
                    else None
                )
            existing = db.execute(
                "SELECT 1 FROM local_event WHERE kind=? AND skill_id=? "
                "AND content_hash=? AND qualification=? "
                "AND COALESCE(organization_id,'')=COALESCE(?,'')",
                (kind, skill_id, content_hash, qualification, organization_id),
            ).fetchone()
            if existing is not None:
                return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO candidate VALUES(?,?,?,'suggested',?,NULL)",
                (skill_id, content_hash, qualification, now),
            )
            if cursor.rowcount == 0:
                row = db.execute(
                    "SELECT state FROM candidate WHERE skill_id=? AND content_hash=? AND qualification=?",
                    (skill_id, content_hash, qualification),
                ).fetchone()
                if row and row["state"] == "dismissed":
                    return None
            qualification_sequence: int | None = None
            if kind == "wisdom.candidate":
                if organization_id is None:
                    sequence_row = db.execute(
                        "SELECT COALESCE(MAX(qualification_sequence),0) FROM local_event "
                        "WHERE kind='wisdom.candidate' AND organization_id IS NULL"
                    ).fetchone()
                else:
                    sequence_row = db.execute(
                        "SELECT COALESCE(MAX(qualification_sequence),0) FROM local_event "
                        "WHERE kind='wisdom.candidate' AND organization_id=?",
                        (organization_id,),
                    ).fetchone()
                qualification_sequence = int(sequence_row[0]) + 1
            cursor = db.execute(
                "INSERT OR IGNORE INTO local_event("
                "id,kind,session_id,task_id,skill_id,content_hash,qualification,"
                "organization_id,qualification_sequence,payload_json,state,"
                "telegram_delivered_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,'unread',NULL,?)",
                (
                    event_id,
                    kind,
                    session_id,
                    task_id,
                    skill_id,
                    content_hash,
                    qualification,
                    organization_id,
                    qualification_sequence,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            return event_id if cursor.rowcount else None

    def local_events(
        self, *, kind: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT e.* FROM local_event e WHERE e.state='unread' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM local_draft d "
            "WHERE d.skill_id=e.skill_id AND d.source_hash=e.content_hash "
            "AND d.state IN ('pending_moderation','published')"
            ")"
        )
        params: list[str] = []
        if kind:
            query += " AND e.kind=?"
            params.append(kind)
        if kind == "wisdom.candidate":
            active_org_id = self.active_org_id()
            if active_org_id is None:
                query += " AND e.organization_id IS NULL"
            else:
                query += " AND e.organization_id=?"
                params.append(active_org_id)
        if session_id:
            query += " AND e.session_id=?"
            params.append(session_id)
        query += " ORDER BY e.created_at DESC"
        with self.transaction() as db:
            rows = [dict(row) for row in db.execute(query, params).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def local_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one local event without changing its delivery or consent state."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_event WHERE id=?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def candidate_editorial_metadata(
        self, skill_id: str, *, content_hash: str
    ) -> dict[str, str] | None:
        """Return exact or latest generated presentation copy for one local skill."""

        organization_id = self.active_org_id()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT payload_json FROM local_event "
                "WHERE kind='wisdom.candidate' AND skill_id=? "
                "AND ((? IS NULL AND organization_id IS NULL) OR organization_id=?) "
                "ORDER BY CASE WHEN content_hash=? THEN 0 ELSE 1 END, created_at DESC",
                (skill_id, organization_id, organization_id, content_hash),
            ).fetchall()
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            name = payload.get("editorial_name")
            description = payload.get("editorial_description")
            if isinstance(name, str) and name.strip() and isinstance(description, str):
                return {
                    "editorial_name": name,
                    "editorial_description": description,
                }
        return None

    def reconcile_candidate_events(self) -> int:
        """Retire unread qualification prompts whose exact bytes already moved on."""

        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE local_event AS e SET state='handled' "
                "WHERE e.kind='wisdom.candidate' AND e.state='unread' AND ("
                "NOT EXISTS (SELECT 1 FROM local_skill s WHERE s.id=e.skill_id) OR "
                "EXISTS (SELECT 1 FROM local_skill s WHERE s.id=e.skill_id AND "
                "(s.deleted_at IS NOT NULL OR s.current_hash IS NULL OR "
                "s.current_hash<>e.content_hash)) OR "
                "NOT EXISTS (SELECT 1 FROM candidate c WHERE c.skill_id=e.skill_id "
                "AND c.content_hash=e.content_hash AND c.qualification=e.qualification) OR "
                "EXISTS (SELECT 1 FROM candidate c WHERE c.skill_id=e.skill_id "
                "AND c.content_hash=e.content_hash AND c.qualification=e.qualification "
                "AND c.state IN ('dismissed','contributed')) OR "
                "EXISTS (SELECT 1 FROM local_draft d WHERE d.skill_id=e.skill_id "
                "AND d.source_hash=e.content_hash AND d.state IN "
                "('owner_approved','publishing','pending_moderation','published',"
                "'declined','invalidated'))"
                ")"
            )
            return int(cursor.rowcount)

    def enqueue_professionalism_review(
        self,
        *,
        skill_id: str,
        content_hash: str,
        author_description_hash: str,
        package: list[dict[str, str]],
        author_description: str,
    ) -> dict[str, Any]:
        """Persist one exact hash-bound review job without resetting completed work."""

        now = utc_now()
        review_id = str(uuid.uuid4())
        package_json = json.dumps(package, sort_keys=True, separators=(",", ":"))
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO professionalism_review("
                "id,skill_id,content_hash,author_description_hash,package_json,"
                "author_description,state,attempts,available_at,lease_owner,"
                "lease_expires_at,result_json,last_error,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,?,?)",
                (
                    review_id,
                    skill_id,
                    content_hash,
                    author_description_hash,
                    package_json,
                    author_description,
                    now,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM professionalism_review WHERE skill_id=? "
                "AND content_hash=? AND author_description_hash=?",
                (skill_id, content_hash, author_description_hash),
            ).fetchone()
        if row is None:  # pragma: no cover - insert/select invariant
            raise RuntimeError("professionalism review was not persisted")
        return self._decode_professionalism_review(dict(row))

    def professionalism_review(
        self,
        *,
        skill_id: str,
        content_hash: str,
        author_description_hash: str,
    ) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM professionalism_review WHERE skill_id=? "
                "AND content_hash=? AND author_description_hash=?",
                (skill_id, content_hash, author_description_hash),
            ).fetchone()
        return self._decode_professionalism_review(dict(row)) if row else None

    def claim_professionalism_review(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        review_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim pending work or recover an expired lease atomically."""

        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as db:
            query = (
                "SELECT * FROM professionalism_review WHERE "
                "((state IN ('pending','retry') AND available_at<=?) OR "
                "(state='running' AND lease_expires_at<=?))"
            )
            params: list[str] = [now_text, now_text]
            if review_id:
                query += " AND id=?"
                params.append(review_id)
            query += " ORDER BY available_at,created_at LIMIT 1"
            row = db.execute(query, params).fetchone()
            if row is None:
                return None
            cursor = db.execute(
                "UPDATE professionalism_review SET state='running',attempts=attempts+1,"
                "lease_owner=?,lease_expires_at=?,updated_at=? WHERE id=? AND "
                "((state IN ('pending','retry') AND available_at<=?) OR "
                "(state='running' AND lease_expires_at<=?))",
                (
                    worker_id,
                    lease_expires_at,
                    now_text,
                    row["id"],
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = db.execute(
                "SELECT * FROM professionalism_review WHERE id=?", (row["id"],)
            ).fetchone()
        return self._decode_professionalism_review(dict(claimed)) if claimed else None

    def expedite_professionalism_review(self, review_id: str) -> None:
        """Make a queued retry immediately claimable before an explicit submission."""

        with self.transaction() as db:
            db.execute(
                "UPDATE professionalism_review SET available_at=?,updated_at=? "
                "WHERE id=? AND state IN ('pending','retry')",
                (utc_now(), utc_now(), review_id),
            )

    def complete_professionalism_review(
        self, review_id: str, *, worker_id: str, result: dict[str, Any]
    ) -> bool:
        now = utc_now()
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE professionalism_review SET state='complete',result_json=?,"
                "lease_owner=NULL,lease_expires_at=NULL,last_error=NULL,updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                    now,
                    review_id,
                    worker_id,
                ),
            )
            return cursor.rowcount == 1

    def retry_professionalism_review(
        self,
        review_id: str,
        *,
        worker_id: str,
        error: str,
        unavailable_result: dict[str, Any],
        max_attempts: int = 2,
        retry_delay_seconds: int = 5,
    ) -> str | None:
        """Release a failed claim or make its bounded unavailable result terminal."""

        now = datetime.now(timezone.utc)
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts FROM professionalism_review WHERE id=? "
                "AND state='running' AND lease_owner=?",
                (review_id, worker_id),
            ).fetchone()
            if row is None:
                return None
            terminal = int(row["attempts"]) >= max_attempts
            state = "complete" if terminal else "retry"
            result_json = (
                json.dumps(unavailable_result, sort_keys=True, separators=(",", ":"))
                if terminal
                else None
            )
            available_at = (
                now if terminal else now + timedelta(seconds=retry_delay_seconds)
            ).isoformat()
            db.execute(
                "UPDATE professionalism_review SET state=?,available_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL,result_json=?,last_error=?,"
                "updated_at=? WHERE id=? AND state='running' AND lease_owner=?",
                (
                    state,
                    available_at,
                    result_json,
                    error[:512],
                    now.isoformat(),
                    review_id,
                    worker_id,
                ),
            )
        return state

    @staticmethod
    def _decode_professionalism_review(value: dict[str, Any]) -> dict[str, Any]:
        value["package"] = json.loads(value.pop("package_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result else None
        return value

    def pending_telegram_events(
        self, *, kind: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Return unread session events not yet surfaced in Telegram.

        Telegram delivery is deliberately independent from ``state``. The same
        candidate must remain available to Desktop and Dashboard until the
        owner dismisses it or advances its exact bytes into publication.
        """
        return self.pending_surface_events(
            kind=kind, session_id=session_id, surface="telegram"
        )

    def pending_surface_events(
        self, *, kind: str, session_id: str, surface: str
    ) -> list[dict[str, Any]]:
        """Return unread session events not yet delivered to one surface."""
        with self.transaction() as db:
            organization = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            active_org_id = (
                str(organization[0])
                if organization is not None and organization[0]
                else None
            )
            organization_clause = (
                "AND e.organization_id=? "
                if active_org_id is not None and kind == "wisdom.candidate"
                else "AND e.organization_id IS NULL "
                if kind == "wisdom.candidate"
                else ""
            )
            params: list[str] = [kind, session_id]
            if active_org_id is not None and kind == "wisdom.candidate":
                params.append(active_org_id)
            params.append(surface)
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT e.* FROM local_event e WHERE e.kind=? AND e.session_id=? "
                    + organization_clause
                    + "AND e.state='unread' AND NOT EXISTS ("
                    "SELECT 1 FROM local_event_delivery d "
                    "WHERE d.event_id=e.id AND d.surface=?"
                    ") ORDER BY e.created_at",
                    params,
                ).fetchall()
            ]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def mark_telegram_delivered(self, event_ids: list[str]) -> None:
        self.mark_surface_delivered(event_ids, surface="telegram")
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self.transaction() as db:
            db.execute(
                f"UPDATE local_event SET telegram_delivered_at=? "
                f"WHERE id IN ({placeholders}) AND telegram_delivered_at IS NULL",
                (utc_now(), *event_ids),
            )

    def mark_surface_delivered(self, event_ids: list[str], *, surface: str) -> None:
        if not event_ids:
            return
        delivered_at = utc_now()
        with self.transaction() as db:
            db.executemany(
                "INSERT OR IGNORE INTO local_event_delivery "
                "(event_id,surface,delivered_at) VALUES(?,?,?)",
                [(event_id, surface, delivered_at) for event_id in event_ids],
            )

    def enqueue_weekly_assessment(
        self,
        organization_id: str,
        event_key: str,
        reference: dict[str, Any],
        *,
        origin_session: str | None = None,
    ) -> str:
        """Durably enqueue one organization/week identity across processes."""
        identity = uuid.uuid4().hex
        now = datetime.now(timezone.utc).timestamp()
        with self.transaction() as db:
            active = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            if active is None or active[0] != organization_id:
                raise ValueError("Wisdom organization is no longer active")
            db.execute(
                "INSERT INTO wisdom_assessment("
                "id,organization_id,event_key,origin_session,reference_json,state,"
                "owner_session,advice_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?,'pending',NULL,NULL,?,?) "
                "ON CONFLICT(organization_id,event_key) DO NOTHING",
                (
                    identity, organization_id, event_key, origin_session,
                    json.dumps(reference, sort_keys=True), now, now,
                ),
            )
            return str(db.execute(
                "SELECT id FROM wisdom_assessment WHERE organization_id=? AND event_key=?",
                (organization_id, event_key),
            ).fetchone()[0])

    @staticmethod
    def _decode_weekly_assessment(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["reference"] = json.loads(value.pop("reference_json"))
        advice = value.pop("advice_json")
        value["advice"] = json.loads(advice) if advice else None
        return value

    def weekly_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM wisdom_assessment WHERE id=?", (assessment_id,)
            ).fetchone()
        return self._decode_weekly_assessment(row) if row else None

    def weekly_assessments(self, organization_id: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM wisdom_assessment WHERE organization_id=? ORDER BY created_at,id",
                (organization_id,),
            ).fetchall()
        return [self._decode_weekly_assessment(row) for row in rows]

    def dismiss_candidate(self, skill_id: str, content_hash: str) -> None:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "UPDATE candidate SET state='dismissed',dismissed_at=? WHERE skill_id=? AND content_hash=?",
                (now, skill_id, content_hash),
            )
            db.execute(
                "UPDATE local_event SET state='dismissed' WHERE skill_id=? AND content_hash=?",
                (skill_id, content_hash),
            )

    def complete_contribution(self, draft_id: str, state: str, *, _db: sqlite3.Connection | None = None) -> None:
        """Retire a candidate event after its owner-approved contribution succeeds."""

        now = utc_now()
        with self.transaction() if _db is None else nullcontext(_db) as db:
            draft = db.execute(
                "SELECT skill_id,source_hash FROM local_draft WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                return
            skill_id = str(draft["skill_id"])
            content_hash = str(draft["source_hash"])
            db.execute(
                "UPDATE local_draft SET state=?,updated_at=? WHERE id=?",
                (state, now, draft_id),
            )
            db.execute(
                "UPDATE candidate SET state='contributed',dismissed_at=NULL "
                "WHERE skill_id=? AND content_hash=?",
                (skill_id, content_hash),
            )
            db.execute(
                "UPDATE local_event SET state='handled' "
                "WHERE skill_id=? AND content_hash=?",
                (skill_id, content_hash),
            )

    def mark_missing_skills(self, seen_paths: set[str]) -> None:
        """Tombstone disappeared paths so delete/recreate gets a new identity."""
        now = utc_now()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,canonical_path FROM local_skill WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                if str(row["canonical_path"]) not in seen_paths:
                    db.execute(
                        "UPDATE local_skill SET deleted_at=?,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )

    def existing_installation_identity(self) -> str | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT installation_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            return str(row["installation_id"]) if row else None

    def installation_identity(self) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT installation_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            if row:
                return str(row["installation_id"])
            installation_id = "hwi_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO installation_identity VALUES(1,?,?,?,?)",
                (installation_id, None, None, utc_now()),
            )
            return installation_id

    def activate_installation_identity(
        self,
        installation_id: str,
        org_id: str,
        *,
        _db: sqlite3.Connection | None = None,
    ) -> None:
        now = utc_now()
        with self.transaction() if _db is None else nullcontext(_db) as db:
            db.execute(
                "UPDATE managed_install SET state='inactive',updated_at=? "
                "WHERE org_id<>? AND state='active'",
                (now, org_id),
            )
            db.execute(
                """INSERT INTO installation_identity(
                     singleton,installation_id,verified_org_id,verified_at,disclosure_at
                   ) VALUES(1,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     installation_id=excluded.installation_id,
                     verified_org_id=excluded.verified_org_id,
                     verified_at=excluded.verified_at""",
                (installation_id, org_id, now, now),
            )

    def verify_installation_identity(self, org_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE managed_install SET state='inactive',updated_at=? "
                "WHERE org_id<>? AND state='active'",
                (utc_now(), org_id),
            )
            db.execute(
                "UPDATE installation_identity SET verified_org_id=?,verified_at=? WHERE singleton=1",
                (org_id, utc_now()),
            )

    def active_org_id(self) -> str | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def organization_display_name(self, organization_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT display_name,resolved,checked_at FROM wisdom_organization "
                "WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_organization_display_name_check(
        self, organization_id: str, display_name: str | None
    ) -> None:
        normalized = display_name.strip()[:160] if display_name else None
        with self.transaction() as db:
            db.execute(
                "INSERT INTO wisdom_organization(organization_id,display_name,resolved,checked_at) "
                "VALUES(?,?,?,?) ON CONFLICT(organization_id) DO UPDATE SET "
                "display_name=COALESCE(excluded.display_name,wisdom_organization.display_name),"
                "resolved=excluded.resolved,"
                "checked_at=excluded.checked_at",
                (organization_id, normalized or None, int(bool(normalized)), utc_now()),
            )

    def record_draft(
        self, values: dict[str, str], *, _db: sqlite3.Connection | None = None
    ) -> None:
        now = utc_now()
        with self.transaction() if _db is None else nullcontext(_db) as db:
            db.execute(
                """INSERT INTO local_draft(
                   id,skill_id,source_hash,overlay_path,draft_commit,server_revision,state,
                   description,content_hash,description_hash,manifest_hash,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET draft_commit=excluded.draft_commit,
                   overlay_path=excluded.overlay_path,
                   server_revision=excluded.server_revision,state=excluded.state,
                   description=excluded.description,content_hash=excluded.content_hash,
                   description_hash=excluded.description_hash,manifest_hash=excluded.manifest_hash,
                   updated_at=excluded.updated_at""",
                (
                    values["id"],
                    values["skill_id"],
                    values["source_hash"],
                    values["overlay_path"],
                    values.get("draft_commit"),
                    values.get("server_revision"),
                    values["state"],
                    values["description"],
                    values["content_hash"],
                    values["description_hash"],
                    values["manifest_hash"],
                    now,
                    now,
                ),
            )

    def replace_prepared_draft(
        self,
        draft_id: str,
        *,
        expected_owner: dict[str, str],
        expected_hashes: dict[str, str],
        values: dict[str, str],
    ) -> bool:
        """CAS one prepared draft's durable overlay pointer and exact hashes."""

        encoded_owner = _encode_owner(expected_owner)
        if encoded_owner is None:  # pragma: no cover - guarded by _encode_owner
            raise ValueError("contribution draft owner is required")
        now = utc_now()
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE local_draft SET overlay_path=?,draft_commit=?,description=?,"
                "content_hash=?,description_hash=?,manifest_hash=?,updated_at=? "
                "WHERE id=? AND state='prepared' AND content_hash=? "
                "AND description_hash=? AND manifest_hash=? AND EXISTS ("
                "SELECT 1 FROM schema_meta WHERE key=? AND value=?)",
                (
                    values["overlay_path"],
                    values.get("draft_commit"),
                    values["description"],
                    values["content_hash"],
                    values["description_hash"],
                    values["manifest_hash"],
                    now,
                    draft_id,
                    expected_hashes["content"],
                    expected_hashes["author_description"],
                    expected_hashes["package_manifest"],
                    _draft_owner_key(draft_id),
                    encoded_owner,
                ),
            )
            return cursor.rowcount == 1

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_draft WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            owner = db.execute(
                "SELECT value FROM schema_meta WHERE key=?", (_draft_owner_key(draft_id),)
            ).fetchone()
            value["owner"] = json.loads(owner[0]) if owner is not None else None
            return value

    def prepared_draft(self, skill_id: str, source_hash: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_draft WHERE skill_id=? AND source_hash=? "
                "AND state='prepared' ORDER BY updated_at DESC LIMIT 1",
                (skill_id, source_hash),
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            owner = db.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (_draft_owner_key(str(row["id"])),),
            ).fetchone()
            value["owner"] = json.loads(owner[0]) if owner is not None else None
            return value

    def bind_draft_owner(
        self,
        draft_id: str,
        owner: dict[str, str],
        *,
        _db: sqlite3.Connection | None = None,
    ) -> None:
        """Persist the full enrollment owner alongside one private draft."""

        encoded = _encode_owner(owner)
        if encoded is None:
            raise ValueError("contribution draft owner is required")
        with self.transaction() if _db is None else nullcontext(_db) as db:
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_draft_owner_key(draft_id), encoded),
            )

    def latest_draft_for_source(
        self, skill_id: str, source_hash: str
    ) -> dict[str, Any] | None:
        """Return the newest contribution state for one exact local source version."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_draft WHERE skill_id=? AND source_hash=? "
                "ORDER BY updated_at DESC, CASE WHEN id LIKE 'local:%' THEN 1 ELSE 0 END, id DESC "
                "LIMIT 1",
                (skill_id, source_hash),
            ).fetchone()
            return dict(row) if row else None

    def set_draft_state(self, draft_id: str, state: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE local_draft SET state=?,updated_at=? WHERE id=?",
                (state, utc_now(), draft_id),
            )

    def save_receipt(
        self,
        *,
        draft_id: str,
        server_revision: str,
        content_hash: str,
        description_hash: str,
        manifest_hash: str,
    ) -> str:
        receipt_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("DELETE FROM review_receipt WHERE draft_id=?", (draft_id,))
            db.execute(
                "INSERT INTO review_receipt VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    receipt_id,
                    draft_id,
                    server_revision,
                    content_hash,
                    description_hash,
                    manifest_hash,
                    utc_now(),
                ),
            )
        return receipt_id

    def receipt(self, draft_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM review_receipt WHERE draft_id=? AND consumed_at IS NULL",
                (draft_id,),
            ).fetchone()
            return dict(row) if row else None

    def publication_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        """Read one unconsumed publication review receipt by its opaque ID."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM review_receipt WHERE id=? AND consumed_at IS NULL",
                (receipt_id,),
            ).fetchone()
            return dict(row) if row else None

    def begin_publication(
        self,
        *,
        receipt_id: str,
        draft_id: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        """Atomically consume final authorization and persist exact send intent."""

        operation_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as db:
            receipt = db.execute(
                "SELECT id FROM review_receipt WHERE id=? AND draft_id=? "
                "AND consumed_at IS NULL",
                (receipt_id, draft_id),
            ).fetchone()
            if receipt is None:
                raise WisdomConflict("Publication review receipt is missing or already used")
            existing = db.execute(
                "SELECT id,payload_json FROM operation_journal "
                "WHERE kind='publication' AND entity_id=? AND state='pending'",
                (entity_id,),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["payload_json"]) != payload:
                    raise WisdomConflict("A different publication operation is already pending")
                return str(existing["id"])
            db.execute(
                "INSERT INTO operation_journal VALUES(?, 'publication', ?, 'intent', ?, "
                "'pending', ?, ?)",
                (operation_id, entity_id, json.dumps(payload, sort_keys=True), now, now),
            )
            db.execute(
                "UPDATE review_receipt SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, receipt_id),
            )
        return operation_id

    def consume_receipt(self, draft_id: str, *, _db: sqlite3.Connection | None = None) -> None:
        with self.transaction() if _db is None else nullcontext(_db) as db:
            db.execute(
                "UPDATE review_receipt SET consumed_at=? WHERE draft_id=?",
                (utc_now(), draft_id),
            )

    def journal(
        self, kind: str, entity_id: str, phase: str, payload: dict[str, Any]
    ) -> str:
        operation_id = str(uuid.uuid4())
        with self.transaction() as db:
            existing = db.execute(
                "SELECT id FROM operation_journal WHERE kind=? AND entity_id=? AND state='pending'",
                (kind, entity_id),
            ).fetchone()
            if existing:
                return str(existing["id"])
            db.execute(
                "INSERT INTO operation_journal VALUES(?,?,?,?,?,'pending',?,?)",
                (
                    operation_id,
                    kind,
                    entity_id,
                    phase,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                    utc_now(),
                ),
            )
        return operation_id

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operation_journal WHERE id=?", (operation_id,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload_json"])
        return value

    def completed_automatic_updates(
        self, owner: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Return completed policy-authorized updates for local notice repair."""

        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM operation_journal WHERE kind='update' "
                "AND state='done' AND phase='gateway_recorded' ORDER BY updated_at,id"
            ).fetchall()
        completed = []
        for row in rows:
            value = dict(row)
            payload = json.loads(value["payload_json"])
            if (
                payload.get("owner") == owner
                and payload.get("authorization", {}).get("kind") == "automatic_policy"
                and isinstance(payload.get("result"), dict)
                and payload["result"].get("updated") is True
            ):
                value["payload"] = payload
                completed.append(value)
        return completed

    def persist_automatic_update_notice(
        self, operation_id: str, owner: dict[str, str]
    ) -> str:
        """Project one completed automatic update into the owned local feed exactly once."""

        event_id = "automatic-update:" + operation_id
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM operation_journal WHERE id=? AND kind='update' "
                "AND state='done' AND phase='gateway_recorded'",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise WisdomConflict("automatic update is not durably complete")
            payload = json.loads(row["payload_json"])
            result = payload.get("result")
            if (
                payload.get("owner") != owner
                or payload.get("authorization", {}).get("kind") != "automatic_policy"
                or not isinstance(result, dict)
                or result.get("updated") is not True
            ):
                raise WisdomConflict("automatic update notice authority is invalid")
            checkpoint = db.execute(
                "SELECT * FROM feed_state WHERE singleton=1"
            ).fetchone()
            expected = {
                "organization_id": owner.get("organization_id"),
                "installation_id": owner.get("installation_id"),
                "owner_user_id": owner.get("account_id"),
                "base_url": owner.get("base_url"),
            }
            if checkpoint is None or any(
                checkpoint[key] != value for key, value in expected.items()
            ):
                raise WisdomConflict("automatic update notice owner is no longer active")
            event = {
                "event_id": event_id,
                "kind": "installation_updated",
                "skill_id": str(payload["skill_id"]),
                "version": int(result["version"]),
                "installation_id": owner["installation_id"],
                "update_mode": str(result["effective_update_mode"]),
                "occurred_at": str(row["updated_at"]),
                "skill_name": str(payload["slug"]),
                "metadata_skill_id": str(payload["skill_id"]),
                "metadata_version": int(result["version"]),
                "metadata_verified": True,
            }
            db.execute(
                "INSERT OR IGNORE INTO feed_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, event["kind"], event["skill_id"], event["version"],
                    event["installation_id"], event["update_mode"],
                    json.dumps(event, sort_keys=True), "immediate", row["updated_at"],
                    None, None, row["updated_at"],
                ),
            )
        return event_id

    def replace_operation_payload(
        self, operation_id: str, payload: dict[str, Any]
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE operation_journal SET payload_json=?,updated_at=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), utc_now(), operation_id),
            )

    def advance(self, operation_id: str, phase: str, *, done: bool = False) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE operation_journal SET phase=?,state=?,updated_at=? WHERE id=?",
                (phase, "done" if done else "pending", utc_now(), operation_id),
            )

    @staticmethod
    def _assert_lock_row(
        db: sqlite3.Connection, entity_id: str, owner: str, now: str
    ) -> None:
        row = db.execute(
            "SELECT owner,expires_at FROM operation_lock WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if row is None or row["owner"] != owner or row["expires_at"] <= now:
            raise WisdomConflict("Collective Wisdom operation lease was lost")

    def advance_claimed(
        self,
        operation_id: str,
        phase: str,
        *,
        entity_id: str,
        owner: str,
    ) -> None:
        """Advance only while the caller still owns the unexpired operation lease."""

        now = utc_now()
        with self.transaction() as db:
            self._assert_lock_row(db, entity_id, owner, now)
            cursor = db.execute(
                "UPDATE operation_journal SET phase=?,state='pending',updated_at=? "
                "WHERE id=? AND state='pending'",
                (phase, now, operation_id),
            )
            if cursor.rowcount != 1:
                raise WisdomConflict("Collective Wisdom publication state changed")

    def publication_for_local_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Return the one pending publication bound to an exact local draft."""

        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM operation_journal WHERE kind='publication' AND state='pending'"
            ).fetchall()
        for row in rows:
            value = dict(row)
            payload = json.loads(value["payload_json"])
            if payload.get("local_draft_id") == draft_id:
                value["payload"] = payload
                return value
        return None

    def publication_for_candidate_assessment(
        self, assessment_id: str, *, owner: dict[str, str], operation_kind: str
    ) -> dict[str, Any] | None:
        """Return the latest owner-bound candidate publication of one exact kind."""

        if operation_kind not in {"public", "private_draft"}:
            raise ValueError("invalid candidate publication operation kind")

        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM operation_journal WHERE kind='publication' "
                "ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            value = dict(row)
            payload = json.loads(value["payload_json"])
            requested_action = payload.get("requested_action")
            payload_kind = "private_draft" if requested_action == "private_draft" else (
                "public" if requested_action is None else "other"
            )
            if (
                payload.get("candidate_assessment_id") == assessment_id
                and payload.get("owner") == owner
                and payload_kind == operation_kind
            ):
                value["payload"] = payload
                return value
        return None

    def reauthorize_publication(
        self,
        *,
        receipt_id: str,
        operation_id: str,
        policy: dict[str, Any],
        entity_id: str,
        owner: str,
    ) -> None:
        """Consume a fresh review and resume the same stalled operation atomically."""

        now = utc_now()
        with self.transaction() as db:
            self._assert_lock_row(db, entity_id, owner, now)
            receipt = db.execute(
                "SELECT id FROM review_receipt WHERE id=? AND consumed_at IS NULL",
                (receipt_id,),
            ).fetchone()
            row = db.execute(
                "SELECT phase,payload_json FROM operation_journal WHERE id=? "
                "AND kind='publication' AND state='pending'",
                (operation_id,),
            ).fetchone()
            if receipt is None or row is None or row["phase"] != "policy_review_required":
                raise WisdomConflict("Publication review receipt is missing or already used")
            payload = json.loads(row["payload_json"])
            resume_phase = payload.pop("resume_phase", None)
            if not isinstance(resume_phase, str):
                raise WisdomConflict("Publication recovery phase is unavailable")
            if payload.get("requested_action") == "private_draft":
                payload.pop("requested_action")
            payload["policy"] = policy
            db.execute(
                "UPDATE operation_journal SET phase=?,payload_json=?,updated_at=? WHERE id=?",
                (resume_phase, json.dumps(payload, sort_keys=True), now, operation_id),
            )
            db.execute(
                "UPDATE review_receipt SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, receipt_id),
            )

    def begin_publication_decline(
        self, operation_id: str, *, entity_id: str, owner: str
    ) -> bool:
        """Persist decline intent before its remote effect; return false when already pending."""

        now = utc_now()
        with self.transaction() as db:
            self._assert_lock_row(db, entity_id, owner, now)
            row = db.execute(
                "SELECT phase,state,payload_json FROM operation_journal WHERE id=? "
                "AND kind='publication'",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise WisdomConflict("Publication operation not found")
            if row["state"] == "pending" and row["phase"] == "decline_unknown":
                return False
            if row["state"] != "done":
                raise WisdomConflict("Publication must be reconciled before it can be declined")
            payload = json.loads(row["payload_json"])
            payload["requested_action"] = "decline"
            db.execute(
                "UPDATE operation_journal SET phase='decline_unknown',state='pending',"
                "payload_json=?,updated_at=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), now, operation_id),
            )
            return True

    def complete_publication(
        self,
        operation_id: str,
        payload: dict[str, Any],
        state: str,
        *,
        entity_id: str | None = None,
        owner: str | None = None,
    ) -> None:
        """Commit authoritative outcome and both local projections in one transaction."""

        now = utc_now()
        encoded = json.dumps(payload, sort_keys=True)
        with self.transaction() as db:
            if entity_id is not None and owner is not None:
                self._assert_lock_row(db, entity_id, owner, now)
            db.execute(
                "UPDATE operation_journal SET phase=?,state='done',payload_json=?,updated_at=? "
                "WHERE id=?",
                (state, encoded, now, operation_id),
            )
            remote_id = payload.get("remote_draft_id")
            if remote_id:
                db.execute(
                    "UPDATE local_draft SET state=?,updated_at=? WHERE id=?",
                    (state, now, remote_id),
                )
            db.execute(
                "UPDATE local_draft SET state=?,updated_at=? WHERE id=?",
                (state, now, payload["local_draft_id"]),
            )

    def pending_operations(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operation_journal WHERE state='pending' ORDER BY created_at"
                ).fetchall()
            ]

    def acquire_operation_lock(
        self, entity_id: str, *, ttl_seconds: int = 900
    ) -> str | None:
        owner = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.transaction() as db:
            db.execute(
                "DELETE FROM operation_lock WHERE expires_at<=?", (now.isoformat(),)
            )
            cursor = db.execute(
                "INSERT OR IGNORE INTO operation_lock VALUES(?,?,?)",
                (entity_id, owner, expires.isoformat()),
            )
            return owner if cursor.rowcount else None

    def release_operation_lock(self, entity_id: str, owner: str) -> None:
        with self.transaction() as db:
            db.execute(
                "DELETE FROM operation_lock WHERE entity_id=? AND owner=?",
                (entity_id, owner),
            )

    def assert_operation_lock(self, entity_id: str, owner: str) -> None:
        """Require an unexpired lease owned by the current operation."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT owner,expires_at FROM operation_lock WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
        if row is None or row["owner"] != owner or row["expires_at"] <= utc_now():
            raise WisdomConflict("Collective Wisdom operation lease was lost")

    def record_install(self, values: dict[str, Any]) -> None:
        now = utc_now()
        owner_json = _encode_owner(values.get("owner"))
        with self.transaction() as db:
            db.execute(
                """INSERT INTO managed_install(
                     skill_id,org_id,slug,version,content_hash,baseline_json,target_path,
                     update_mode,state,installed_at,updated_at,owner_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(skill_id) DO UPDATE SET org_id=excluded.org_id,
                   slug=excluded.slug,version=excluded.version,content_hash=excluded.content_hash,
                   baseline_json=excluded.baseline_json,target_path=excluded.target_path,
                   update_mode=excluded.update_mode,state=excluded.state,
                   updated_at=excluded.updated_at,owner_json=excluded.owner_json""",
                (
                    values["skill_id"],
                    values["org_id"],
                    values["slug"],
                    int(values["version"]),
                    values["content_hash"],
                    json.dumps(values["baseline"], sort_keys=True),
                    values["target_path"],
                    values["update_mode"],
                    values.get("state", "active"),
                    now,
                    now,
                    owner_json,
                ),
            )

    def installations(self, owner: dict[str, str] | None = None) -> list[dict[str, Any]]:
        owner_json = _encode_owner(owner)
        with self.transaction() as db:
            query = "SELECT * FROM managed_install"
            params: tuple[str, ...] = ()
            if owner is not None:
                query += " WHERE owner_json=?"
                params = (owner_json,)
            query += " ORDER BY slug"
            rows = [
                dict(row)
                for row in db.execute(query, params).fetchall()
            ]
        for row in rows:
            _decode_install_row(row)
        return rows

    def installation(
        self, skill_id: str, owner: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        owner_json = _encode_owner(owner)
        with self.transaction() as db:
            query = "SELECT * FROM managed_install WHERE skill_id=?"
            params = (skill_id,)
            if owner is not None:
                query += " AND owner_json=?"
                params = (skill_id, owner_json)
            row = db.execute(
                query, params
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        _decode_install_row(value)
        return value

    def catalog_install_action(
        self,
        owner: dict[str, str],
        *,
        skill_id: str,
        version: int | None,
        creator_user_id: str = "",
        content_hash: str = "",
    ) -> str:
        """Return install/update/none from exact profile-owned provenance."""

        installed = self.installation(skill_id, owner)
        if installed is not None and installed.get("state") == "active" and version is not None:
            installed_version = int(installed["version"])
            if version <= installed_version:
                return "none"
            return "update"

        if not content_hash or creator_user_id != owner.get("account_id"):
            return "install"
        with self.transaction() as db:
            rows = db.execute(
                "SELECT entity_id,phase,payload_json FROM operation_journal "
                "WHERE kind='publication' AND state='done' AND phase='published' "
                "ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            request = payload.get("request") or {}
            published_hash = str((payload.get("hashes") or {}).get("content") or "")
            local_draft_id = str(payload.get("local_draft_id") or "")
            if (
                payload.get("owner") != owner
                or payload.get("publication_state") != "published"
                or request.get("content_hash") != published_hash
                or published_hash != content_hash
                or not local_draft_id
            ):
                continue
            draft = self.draft(local_draft_id)
            if (
                draft is None
                or draft.get("owner") != owner
                or draft.get("skill_id") != row["entity_id"]
                or draft.get("content_hash") != published_hash
                or draft.get("state") != "published"
            ):
                continue
            source = self.local_skill(str(draft["skill_id"]))
            if source is None or source.get("deleted_at") is not None:
                continue
            source_path = Path(str(source.get("canonical_path") or ""))
            if source_path.is_dir() and (source_path / "SKILL.md").is_file():
                return "none"
        return "install"

    def deactivate_install(self, skill_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE managed_install SET state='inactive',updated_at=? WHERE skill_id=?",
                (utc_now(), skill_id),
            )

    def bind_feed_owner(
        self,
        organization_id: str,
        installation_id: str,
        owner_user_id: str,
        base_url: str,
    ) -> dict[str, Any]:
        """Bind cursor/events to one exact org and installation generation.

        Switching either key retires the previous projection before a new network
        request. This prevents a reconnect from exposing or resuming another
        account's feed while retaining a monotonic generation fence.
        """

        if not all((organization_id, installation_id, owner_user_id, base_url)):
            raise ValueError("feed owner requires organization, installation, account, and route")
        now = utc_now()
        with self.transaction() as db:
            active = db.execute(
                "SELECT verified_org_id,installation_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            if (
                active is None
                or active["verified_org_id"] != organization_id
                or active["installation_id"] != installation_id
            ):
                raise WisdomConflict("Wisdom feed owner is no longer active")
            row = db.execute("SELECT * FROM feed_state WHERE singleton=1").fetchone()
            changed = bool(row and (
                row["organization_id"] != organization_id
                or row["installation_id"] != installation_id
                or row["owner_user_id"] != owner_user_id
                or row["base_url"] != base_url
            ))
            generation = (int(row["generation"]) if row else 0) + int(changed)
            if changed:
                db.execute("DELETE FROM feed_event_delivery")
                db.execute("DELETE FROM feed_event")
            db.execute(
                """INSERT INTO feed_state(singleton,cursor,updated_at,generation,
                   organization_id,installation_id,owner_user_id,base_url,resume_required)
                   VALUES(1,NULL,?,?,?,?,?,?,0)
                   ON CONFLICT(singleton) DO UPDATE SET
                   cursor=CASE WHEN feed_state.organization_id=excluded.organization_id
                     AND feed_state.installation_id=excluded.installation_id
                     AND feed_state.owner_user_id=excluded.owner_user_id
                     AND feed_state.base_url=excluded.base_url
                     THEN feed_state.cursor ELSE NULL END,
                   updated_at=excluded.updated_at,generation=excluded.generation,
                   organization_id=excluded.organization_id,
                   installation_id=excluded.installation_id,
                   owner_user_id=excluded.owner_user_id,base_url=excluded.base_url,
                   resume_required=0""",
                (now, generation, organization_id, installation_id, owner_user_id, base_url),
            )
            current = db.execute("SELECT * FROM feed_state WHERE singleton=1").fetchone()
            return dict(current)

    def feed_cursor(self) -> str | None:
        return self.feed_position()[0]

    def feed_position(self) -> tuple[str | None, int]:
        checkpoint = self.feed_checkpoint()
        return checkpoint["cursor"], checkpoint["generation"]

    def feed_checkpoint(self) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM feed_state WHERE singleton=1"
            ).fetchone()
            return dict(row) if row else {
                "cursor": None,
                "generation": 0,
                "organization_id": None,
                "installation_id": None,
                "owner_user_id": None,
                "base_url": None,
                "resume_required": 0,
            }

    @staticmethod
    def check_feed_generation(db: sqlite3.Connection, expected: int) -> None:
        row = db.execute(
            "SELECT generation FROM feed_state WHERE singleton=1"
        ).fetchone()
        if (int(row[0]) if row else 0) != expected:
            raise WisdomConflict(
                "Wisdom account changed while fetching the feed; sign in and retry",
                code="account_session_changed",
            )

    def persist_feed_page(
        self,
        events: list[dict[str, Any]],
        *,
        next_cursor: str,
        cadences: dict[str, str],
        now: str,
        expected_generation: int | None = None,
    ) -> int:
        current = datetime.fromisoformat(now).astimezone(timezone.utc)

        def due_at(cadence: str) -> str:
            if cadence == "daily":
                due = (current + timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )
            elif cadence == "weekly":
                days = 7 - current.weekday()
                due = (current + timedelta(days=days)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )
            else:
                due = current
            return due.isoformat()

        inserted = 0
        with self.transaction() as db:
            if expected_generation is not None:
                self.check_feed_generation(db, expected_generation)
            for event in events:
                kind = str(event["kind"])
                cadence = cadences.get(
                    str(event["event_id"]), cadences.get(kind, "immediate")
                )
                cursor = db.execute(
                    "INSERT OR IGNORE INTO feed_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(event["event_id"]),
                        kind,
                        str(event["skill_id"]),
                        event.get("version"),
                        event.get("installation_id"),
                        event.get("update_mode"),
                        json.dumps(event, sort_keys=True),
                        cadence,
                        due_at(cadence),
                        None,
                        None,
                        str(event.get("occurred_at") or now),
                    ),
                )
                inserted += max(cursor.rowcount, 0)
            db.execute(
                "INSERT INTO feed_state(singleton,cursor,updated_at) VALUES(1,?,?) "
                "ON CONFLICT(singleton) "
                "DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                (next_cursor, now),
            )
        return inserted

    def feed_events(
        self,
        *,
        unseen_only: bool = False,
        telegram_due_at: str | None = None,
        surface: str | None = None,
        surface_due_at: str | None = None,
        owner: dict[str, str] | None = None,
        expected_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT e.* FROM feed_event e WHERE 1=1"
        params: list[str] = []
        if unseen_only:
            query += " AND e.local_seen_at IS NULL AND e.cadence!='off' AND e.due_at<=?"
            params.append(utc_now())
        if telegram_due_at is not None:
            query += (
                " AND NOT EXISTS (SELECT 1 FROM feed_event_delivery d "
                "WHERE d.event_id=e.event_id AND d.surface='telegram') "
                "AND e.cadence!='off' AND e.due_at<=?"
            )
            params.append(telegram_due_at)
        if surface is not None and surface_due_at is not None:
            query += (
                " AND NOT EXISTS (SELECT 1 FROM feed_event_delivery d "
                "WHERE d.event_id=e.event_id AND d.surface=?) "
                "AND e.cadence!='off' AND e.due_at<=?"
            )
            params.extend([surface, surface_due_at])
        query += " ORDER BY e.created_at,e.event_id"
        with self.transaction() as db:
            if owner is not None:
                checkpoint = db.execute(
                    "SELECT * FROM feed_state WHERE singleton=1"
                ).fetchone()
                if checkpoint is None or any(
                    checkpoint[key] != owner[key]
                    for key in (
                        "organization_id", "installation_id", "owner_user_id", "base_url"
                    )
                ):
                    raise WisdomConflict(
                        "Wisdom feed belongs to a previous account; run maintenance to refresh it",
                        code="account_session_changed",
                    )
                if (
                    expected_generation is not None
                    and int(checkpoint["generation"]) != expected_generation
                ):
                    raise WisdomConflict(
                        "Wisdom feed changed while it was being read; run maintenance and retry",
                        code="account_session_changed",
                    )
            rows = [dict(row) for row in db.execute(query, params).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def enrich_feed_event(
        self,
        event_id: str,
        values: dict[str, Any],
        *,
        owner: dict[str, str] | None = None,
        expected_generation: int | None = None,
    ) -> None:
        """Persist bounded presentation metadata alongside an immutable event."""

        allowed = {
            key: value
            for key, value in values.items()
            if key in {
                "editorial_name", "editorial_description", "skill_name",
                "published_by_user_id", "metadata_skill_id", "metadata_version",
                "metadata_verified",
            }
            and (
                isinstance(value, str)
                or (
                    key == "metadata_version"
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                )
                or (key == "metadata_verified" and isinstance(value, bool))
            )
            and (key in {"editorial_description", "metadata_verified"} or bool(value))
        }
        if not allowed:
            return
        with self.transaction() as db:
            if owner is not None:
                checkpoint = db.execute(
                    "SELECT * FROM feed_state WHERE singleton=1"
                ).fetchone()
                if checkpoint is None or any(
                    checkpoint[key] != owner[key]
                    for key in (
                        "organization_id", "installation_id", "owner_user_id", "base_url"
                    )
                ) or (
                    expected_generation is not None
                    and int(checkpoint["generation"]) != expected_generation
                ):
                    raise WisdomConflict(
                        "Wisdom feed changed during metadata refresh; run maintenance and retry",
                        code="account_session_changed",
                    )
            row = db.execute(
                "SELECT payload_json FROM feed_event WHERE event_id=?", (event_id,)
            ).fetchone()
            if not row:
                return
            try:
                payload = json.loads(str(row[0]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            if not isinstance(payload, dict):
                return
            payload.update(allowed)
            db.execute(
                "UPDATE feed_event SET payload_json=? WHERE event_id=?",
                (json.dumps(payload, sort_keys=True), event_id),
            )

    def persist_local_notice(
        self,
        *,
        event_id: str,
        kind: str,
        skill_id: str,
        payload: dict[str, Any],
        cadence: str = "immediate",
    ) -> bool:
        now = utc_now()
        current = datetime.fromisoformat(now)
        if cadence == "daily":
            due = (
                (current + timedelta(days=1))
                .replace(hour=9, minute=0, second=0, microsecond=0)
                .isoformat()
            )
        elif cadence == "weekly":
            due = (
                (current + timedelta(days=7 - current.weekday()))
                .replace(hour=9, minute=0, second=0, microsecond=0)
                .isoformat()
            )
        else:
            due = now
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO feed_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    kind,
                    skill_id,
                    payload.get("version"),
                    None,
                    None,
                    json.dumps(payload, sort_keys=True),
                    cadence,
                    due,
                    None,
                    None,
                    now,
                ),
            )
            return bool(cursor.rowcount)

    def mark_feed_local_seen(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self.transaction() as db:
            db.execute(
                f"UPDATE feed_event SET local_seen_at=? WHERE event_id IN ({placeholders})",
                [utc_now(), *event_ids],
            )

    def mark_feed_telegram_delivered(self, event_ids: list[str]) -> None:
        self.mark_feed_surface_delivered(event_ids, surface="telegram")
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self.transaction() as db:
            db.execute(
                f"UPDATE feed_event SET telegram_delivered_at=? WHERE event_id IN ({placeholders})",
                [utc_now(), *event_ids],
            )

    def mark_feed_surface_delivered(
        self, event_ids: list[str], *, surface: str
    ) -> None:
        if not event_ids:
            return
        delivered_at = utc_now()
        with self.transaction() as db:
            db.executemany(
                "INSERT OR IGNORE INTO feed_event_delivery "
                "(event_id,surface,delivered_at) VALUES(?,?,?)",
                [(event_id, surface, delivered_at) for event_id in event_ids],
            )
