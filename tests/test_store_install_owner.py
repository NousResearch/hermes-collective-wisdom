"""Managed-install owner binding and schema upgrade tests."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.store import (
    CURRENT_SCHEMA_SQL,
    SCHEMA_FAMILY,
    SCHEMA_VERSION,
    WisdomStore,
)


def _owner(**changes: str) -> dict[str, str]:
    value = {
        "profile_name": "default",
        "profile_home": "/profiles/default",
        "account_id": "account-1",
        "organization_id": "org-1",
        "base_url": "https://gateway.example",
        "installation_id": "install-1",
    }
    value.update(changes)
    return value


def _install(skill_id: str, owner=None) -> dict:
    return {
        "skill_id": skill_id,
        "org_id": "org-1",
        "slug": skill_id,
        "version": 1,
        "content_hash": "sha256:" + "a" * 64,
        "baseline": {"SKILL.md": "sha256:" + "b" * 64},
        "target_path": f"/skills/{skill_id}",
        "update_mode": "MANUAL",
        "owner": owner,
    }


def test_current_v1_schema_upgrades_only_after_full_shape_validation(tmp_path: Path):
    root = tmp_path / "wisdom"
    root.mkdir()
    v1_sql = CURRENT_SCHEMA_SQL.replace(
        "  updated_at TEXT NOT NULL,\n  owner_json TEXT\n",
        "  updated_at TEXT NOT NULL\n",
        1,
    )
    with sqlite3.connect(root / "wisdom.db") as db:
        db.executescript(v1_sql)
        db.execute("INSERT INTO schema_meta VALUES('schema_version','1')")
        db.execute(
            "INSERT INTO plugin_schema_identity VALUES(1,?,1)", (SCHEMA_FAMILY,)
        )

    store = WisdomStore(root)
    with store.transaction() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(managed_install)")]
        marker = db.execute(
            "SELECT version FROM plugin_schema_identity WHERE singleton=1"
        ).fetchone()[0]
        metadata = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert columns[-1] == "owner_json"
    assert marker == SCHEMA_VERSION == 2
    assert metadata == str(SCHEMA_VERSION)
    WisdomStore(root)


def test_malformed_v1_schema_is_refused_without_metadata_upgrade(tmp_path: Path):
    root = tmp_path / "wisdom"
    root.mkdir()
    v1_sql = CURRENT_SCHEMA_SQL.replace(
        "  updated_at TEXT NOT NULL,\n  owner_json TEXT\n",
        "  updated_at TEXT NOT NULL\n",
        1,
    )
    with sqlite3.connect(root / "wisdom.db") as db:
        db.executescript(v1_sql)
        db.execute("DROP INDEX operation_journal_one_pending")
        db.execute("INSERT INTO schema_meta VALUES('schema_version','1')")
        db.execute(
            "INSERT INTO plugin_schema_identity VALUES(1,?,1)", (SCHEMA_FAMILY,)
        )

    with pytest.raises(RuntimeError, match="schema objects"):
        WisdomStore(root)
    with sqlite3.connect(root / "wisdom.db") as db:
        assert db.execute(
            "SELECT version FROM plugin_schema_identity WHERE singleton=1"
        ).fetchone()[0] == 1
        assert "owner_json" not in {
            row[1] for row in db.execute("PRAGMA table_info(managed_install)")
        }


def test_owner_filtered_installs_never_adopt_unowned_or_other_owner_rows(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    owner = _owner()
    other = _owner(profile_name="other", profile_home="/profiles/other")
    store.record_install(_install("owned", owner))
    store.record_install(_install("other", other))
    store.record_install(_install("legacy"))

    assert [row["skill_id"] for row in store.installations(owner)] == ["owned"]
    assert store.installation("owned", owner)["owner"] == owner
    assert store.installation("other", owner) is None
    assert store.installation("legacy", owner) is None
    assert {row["skill_id"] for row in store.installations()} == {
        "owned", "other", "legacy"
    }
    assert store.installation("legacy")["owner"] is None


def test_owner_binding_requires_the_exact_nonempty_shape(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    with pytest.raises(ValueError, match="owner binding"):
        store.record_install(_install("missing", {"profile_name": "default"}))
    with pytest.raises(ValueError, match="owner binding"):
        store.installations({**_owner(), "extra": "value"})


def test_assert_operation_lock_rejects_lost_wrong_and_expired_leases(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    token = store.acquire_operation_lock("skill-1", ttl_seconds=30)
    assert token is not None
    store.assert_operation_lock("skill-1", token)
    with pytest.raises(WisdomConflict, match="lease was lost"):
        store.assert_operation_lock("skill-1", "wrong")
    with store.transaction() as db:
        db.execute(
            "UPDATE operation_lock SET expires_at=? WHERE entity_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "skill-1"),
        )
    with pytest.raises(WisdomConflict, match="lease was lost"):
        store.assert_operation_lock("skill-1", token)
