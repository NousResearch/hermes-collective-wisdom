# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring,too-few-public-methods

import sqlite3
import os
from pathlib import Path

import pytest

from hermes_wisdom_plugin.store import (
    SCHEMA_FAMILY,
    SCHEMA_VERSION,
    UnsupportedSchemaError,
    WisdomStore,
)
from hermes_wisdom_plugin.errors import WisdomConflict, WisdomError


def test_profile_store_requires_explicit_host_data_root():
    with pytest.raises(TypeError):
        WisdomStore()  # pylint: disable=no-value-for-parameter


def test_profile_store_uses_host_context_data_directory(tmp_path: Path):
    class State:
        data_dir = tmp_path / "profile-data"

    class HostContext:
        state = State()

    hostctx = HostContext()
    store = WisdomStore(hostctx.state.data_dir)

    assert store.root == hostctx.state.data_dir
    assert store.path == hostctx.state.data_dir / "wisdom.db"


def test_profile_store_refuses_unversioned_existing_database(tmp_path: Path):
    root = tmp_path / "profile-data"
    root.mkdir()
    with sqlite3.connect(root / "wisdom.db") as db:
        db.execute("CREATE TABLE legacy_state(value TEXT NOT NULL)")
        db.execute("INSERT INTO legacy_state VALUES('do not adopt')")

    with pytest.raises(UnsupportedSchemaError, match="schema marker"):
        WisdomStore(root)

    with sqlite3.connect(root / "wisdom.db") as db:
        assert db.execute("SELECT value FROM legacy_state").fetchone()[0] == "do not adopt"
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='local_skill'"
        ).fetchone()[0] == 0


def test_profile_store_refuses_old_feature_schema_v12(tmp_path: Path):
    root = tmp_path / "profile-data"
    root.mkdir()
    with sqlite3.connect(root / "wisdom.db") as db:
        db.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO schema_meta VALUES('schema_version', '12')")

    with pytest.raises(UnsupportedSchemaError, match="schema marker"):
        WisdomStore(root)


def test_profile_store_refuses_current_version_without_family_marker(tmp_path: Path):
    root = tmp_path / "profile-data"
    root.mkdir()
    with sqlite3.connect(root / "wisdom.db") as db:
        db.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "INSERT INTO schema_meta VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    with pytest.raises(UnsupportedSchemaError, match="schema marker"):
        WisdomStore(root)


def test_profile_store_refuses_foreign_or_malformed_schema_marker(tmp_path: Path):
    for name, family, version, match in (
        ("foreign", "another.product.store", 1, "Foreign schema family"),
        ("malformed", SCHEMA_FAMILY, "one", "expected integer 1"),
    ):
        root = tmp_path / name
        root.mkdir()
        with sqlite3.connect(root / "wisdom.db") as db:
            db.execute(
                "CREATE TABLE plugin_schema_identity("
                "singleton INTEGER PRIMARY KEY, family TEXT, version)"
            )
            db.execute(
                "INSERT INTO plugin_schema_identity VALUES(1,?,?)",
                (family, version),
            )

        with pytest.raises(UnsupportedSchemaError, match=match):
            WisdomStore(root)


def test_profile_store_refuses_partial_marked_schema(tmp_path: Path):
    root = tmp_path / "profile-data"
    root.mkdir()
    with sqlite3.connect(root / "wisdom.db") as db:
        db.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE plugin_schema_identity(
              singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
              family TEXT NOT NULL,
              version INTEGER NOT NULL CHECK(typeof(version) = 'integer' AND version > 0)
            );
            """
        )
        db.execute(
            "INSERT INTO schema_meta VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        db.execute(
            "INSERT INTO plugin_schema_identity VALUES(1,?,?)",
            (SCHEMA_FAMILY, SCHEMA_VERSION),
        )

    with pytest.raises(UnsupportedSchemaError, match="schema objects"):
        WisdomStore(root)


def test_profile_store_reopens_full_current_plugin_schema(tmp_path: Path):
    root = tmp_path / "profile-data"
    first = WisdomStore(root)
    installation_id = first.installation_identity()

    reopened = WisdomStore(root)

    assert reopened.installation_identity() == installation_id
    with reopened.transaction() as db:
        marker = db.execute(
            "SELECT family,version,typeof(version) FROM plugin_schema_identity"
        ).fetchone()
    assert tuple(marker) == (SCHEMA_FAMILY, 1, "integer")
    assert SCHEMA_VERSION == 1


def test_feed_generation_conflict_keeps_public_error_contract(tmp_path: Path):
    store = WisdomStore(tmp_path / "profile-data")

    with store.transaction() as db:
        with pytest.raises(WisdomConflict) as raised:
            store.check_feed_generation(db, expected=1)

    assert raised.value.code == "account_session_changed"
    assert raised.value.exit_code == 5
    assert isinstance(raised.value, WisdomError)


def test_profile_store_permissions_identity_and_rename(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    assert store.existing_installation_identity() is None
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hello", encoding="utf-8")
    first = store.register_skill(skill, content_hash="sha256:a", source_kind="local")
    moved = tmp_path / "renamed"
    skill.rename(moved)
    second = store.register_skill(moved, content_hash="sha256:b", source_kind="local")
    assert first == second
    assert store.installation_identity() == store.installation_identity()
    assert store.existing_installation_identity() == store.installation_identity()
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available")
def test_profile_store_secures_sqlite_wal_sidecars(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    with store.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('mode','test')"
        )
        for path in (
            store.path,
            Path(f"{store.path}-wal"),
            Path(f"{store.path}-shm"),
        ):
            assert path.exists()
            assert path.stat().st_mode & 0o777 == 0o600


def test_delete_recreate_does_not_inherit_identity(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    path = tmp_path / "skill"
    path.mkdir()
    (path / "SKILL.md").write_text("one", encoding="utf-8")
    first = store.register_skill(path, content_hash="sha256:a", source_kind="local")
    (path / "SKILL.md").unlink()
    path.rmdir()
    path.mkdir()
    (path / "SKILL.md").write_text("two", encoding="utf-8")
    store.mark_missing_skills(set())
    second = store.register_skill(path, content_hash="sha256:b", source_kind="local")
    assert first != second


def test_rename_falls_back_to_unambiguous_content_hash_without_filesystem_identity(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        "hermes_wisdom_plugin.store.filesystem_identity", lambda _path: None
    )
    store = WisdomStore(tmp_path / "wisdom")
    original = tmp_path / "original"
    original.mkdir()
    (original / "SKILL.md").write_text("same", encoding="utf-8")
    first = store.register_skill(
        original, content_hash="sha256:same", source_kind="local"
    )
    renamed = tmp_path / "renamed"
    original.rename(renamed)
    store.mark_missing_skills({str(renamed.resolve())})
    second = store.register_skill(
        renamed, content_hash="sha256:same", source_kind="local"
    )
    assert second == first


def test_ambiguous_content_hash_move_creates_new_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "hermes_wisdom_plugin.store.filesystem_identity", lambda _path: None
    )
    store = WisdomStore(tmp_path / "wisdom")
    for name in ("one", "two"):
        path = tmp_path / name
        path.mkdir()
        (path / "SKILL.md").write_text("same", encoding="utf-8")
        store.register_skill(path, content_hash="sha256:same", source_kind="local")
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "SKILL.md").write_text("same", encoding="utf-8")
    new_id = store.register_skill(
        moved, content_hash="sha256:same", source_kind="local"
    )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM local_skill").fetchone()[0] == 3
        assert db.execute(
            "SELECT canonical_path FROM local_skill WHERE id=?", (new_id,)
        ).fetchone()[0] == str(moved.resolve())


def test_operation_journal_survives_restart(tmp_path: Path):
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    operation = store.journal("install", "skill-1", "downloaded", {"version": 1})
    store.advance(operation, "files_committed")
    resumed = WisdomStore(root).pending_operations()
    assert resumed[0]["id"] == operation
    assert resumed[0]["phase"] == "files_committed"


def test_local_events_hide_a_contribution_that_already_reached_publication(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="session-1",
        task_id="task-1",
        qualification="manual_selection",
    )
    store.record_draft({
        "id": "draft-1",
        "skill_id": skill_id,
        "source_hash": "sha256:source",
        "overlay_path": str(tmp_path / "overlay"),
        "state": "published",
        "description": "Owner copy",
        "content_hash": "sha256:content",
        "description_hash": "sha256:description",
        "manifest_hash": "sha256:manifest",
    })

    assert store.local_events(kind="wisdom.candidate", session_id="session-1") == []
    assert store.reconcile_candidate_events() == 1
    assert event_id is not None
    assert store.local_event(event_id)["state"] == "handled"


def test_candidate_reconciliation_preserves_current_reviewable_events(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="session-1",
        task_id="task-1",
        qualification="manual_selection",
    )

    assert event_id is not None
    assert store.reconcile_candidate_events() == 0
    assert store.local_event(event_id)["state"] == "unread"


def test_candidate_editorial_metadata_carries_forward_within_the_same_org(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    skill_path = tmp_path / "legacy-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("legacy", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:v1", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:v1",
        payload={
            "skill_name": "legacy-skill",
            "editorial_name": "Incident Response Guide",
            "editorial_description": "Coordinate a consistent incident response.",
        },
        session_id="session-1",
        task_id="task-1",
        qualification="high_usage",
    )
    assert event_id is not None
    with store.transaction() as db:
        db.execute("UPDATE local_event SET state='handled' WHERE id=?", (event_id,))

    assert store.candidate_editorial_metadata(
        skill_id, content_hash="sha256:v2"
    ) == {
        "editorial_name": "Incident Response Guide",
        "editorial_description": "Coordinate a consistent incident response.",
    }

    store.verify_installation_identity("org-2")
    assert store.candidate_editorial_metadata(
        skill_id, content_hash="sha256:v2"
    ) is None


def test_verified_org_change_deactivates_stale_managed_installs(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    store.installation_identity()
    store.verify_installation_identity("org-1")
    store.record_install({
        "skill_id": "skill-1",
        "org_id": "org-1",
        "slug": "managed",
        "version": 1,
        "content_hash": "sha256:content",
        "baseline": {"SKILL.md": "sha256:file"},
        "target_path": str(tmp_path / "skills" / "_wisdom" / "org-1" / "managed"),
        "update_mode": "MANUAL",
    })

    store.verify_installation_identity("org-2")

    assert store.active_org_id() == "org-2"
    assert store.installation("skill-1")["state"] == "inactive"


def test_identity_rotation_is_atomic_with_org_activation(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    old_identity = store.installation_identity()
    store.verify_installation_identity("org-1")

    store.activate_installation_identity("hwi_" + "n" * 32, "org-2")

    assert store.existing_installation_identity() != old_identity
    assert store.existing_installation_identity() == "hwi_" + "n" * 32
    assert store.active_org_id() == "org-2"


def test_current_schema_tracks_profile_local_usage_surface_delivery_reviews_and_notices(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    with store.transaction() as db:
        snapshot_columns = {
            row[1] for row in db.execute("PRAGMA table_info(snapshot)").fetchall()
        }
        stability_columns = {
            row[1] for row in db.execute("PRAGMA table_info(stability_job)").fetchall()
        }
        usage_columns = {
            row[1] for row in db.execute("PRAGMA table_info(usage_day)").fetchall()
        }
        event_columns = {
            row[1] for row in db.execute("PRAGMA table_info(local_event)").fetchall()
        }
        review_columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(professionalism_review)"
            ).fetchall()
        }
        organization_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(wisdom_organization)").fetchall()
        }
        version = db.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        feed_columns = {
            row[1] for row in db.execute("PRAGMA table_info(feed_state)").fetchall()
        }
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "skill_text" in snapshot_columns
    assert {"session_id", "task_id"} <= stability_columns
    assert {"day_local", "timezone_name"} <= usage_columns
    assert "day_utc" not in usage_columns
    assert "telegram_delivered_at" in event_columns
    assert {"organization_id", "qualification_sequence"} <= event_columns
    assert {"display_name", "resolved", "checked_at"} <= organization_columns
    assert {
        "content_hash",
        "author_description_hash",
        "package_json",
        "state",
        "attempts",
        "lease_expires_at",
        "result_json",
    } <= review_columns
    assert {
        "generation",
        "organization_id",
        "installation_id",
        "resume_required",
    } <= feed_columns
    assert {
        "local_event_org_identity",
        "local_event_org_qualification_sequence",
        "operation_journal_one_pending",
    } <= indexes
    assert version == str(SCHEMA_VERSION)


def test_candidate_sequence_is_atomic_idempotent_and_scoped_by_organization(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    store.installation_identity()
    store.verify_installation_identity("org-1")

    def emit(name: str, content_hash: str) -> str | None:
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        (path / "SKILL.md").write_text(name, encoding="utf-8")
        skill_id = store.register_skill(
            path, content_hash=content_hash, source_kind="local"
        )
        return store.emit_local_event(
            kind="wisdom.candidate",
            skill_id=skill_id,
            content_hash=content_hash,
            payload={"skill_name": name},
            session_id="session-1",
            task_id="task-1",
            qualification="high_usage",
        )

    first_id = emit("first", "sha256:first")
    assert first_id is not None
    assert emit("first", "sha256:first") is None
    second_id = emit("second", "sha256:second")
    assert second_id is not None

    org_one = store.local_events(kind="wisdom.candidate")
    assert [event["qualification_sequence"] for event in reversed(org_one)] == [1, 2]
    assert {event["organization_id"] for event in org_one} == {"org-1"}

    store.verify_installation_identity("org-2")
    third_id = emit("third", "sha256:third")
    assert third_id is not None
    org_two = store.local_events(kind="wisdom.candidate")
    assert [event["id"] for event in org_two] == [third_id]
    assert org_two[0]["qualification_sequence"] == 1

    store.verify_installation_identity("org-1")
    assert {event["id"] for event in store.local_events(kind="wisdom.candidate")} == {
        first_id,
        second_id,
    }






def test_professionalism_review_queue_is_hash_bound_idempotent_and_leased(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    first = store.enqueue_professionalism_review(
        skill_id="skill-1",
        content_hash="sha256:" + "a" * 64,
        author_description_hash="sha256:" + "b" * 64,
        package=[{"path": "SKILL.md", "content_utf8": "hello"}],
        author_description="A useful skill.",
    )
    duplicate = store.enqueue_professionalism_review(
        skill_id="skill-1",
        content_hash="sha256:" + "a" * 64,
        author_description_hash="sha256:" + "b" * 64,
        package=[{"path": "SKILL.md", "content_utf8": "ignored duplicate"}],
        author_description="A useful skill.",
    )

    assert duplicate["id"] == first["id"]
    claimed = store.claim_professionalism_review(worker_id="worker-1")
    assert claimed and claimed["id"] == first["id"]
    assert claimed["attempts"] == 1
    assert store.claim_professionalism_review(worker_id="worker-2") is None

    result = {"status": "pass"}
    assert store.complete_professionalism_review(
        first["id"], worker_id="worker-1", result=result
    )
    saved = store.professionalism_review(
        skill_id="skill-1",
        content_hash="sha256:" + "a" * 64,
        author_description_hash="sha256:" + "b" * 64,
    )
    assert saved and saved["state"] == "complete" and saved["result"] == result


def test_candidate_delivery_is_independent_per_surface(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="session-1",
        task_id="task-1",
        qualification="manual_selection",
    )
    assert event_id

    store.mark_surface_delivered([event_id], surface="slack")

    assert (
        store.pending_surface_events(
            kind="wisdom.candidate", session_id="session-1", surface="slack"
        )
        == []
    )
    assert [
        event["id"]
        for event in store.pending_surface_events(
            kind="wisdom.candidate", session_id="session-1", surface="telegram"
        )
    ] == [event_id]




def test_telegram_delivery_is_session_scoped_without_consuming_candidate(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "wisdom")
    skill_path = tmp_path / "skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text("hello", encoding="utf-8")
    skill_id = store.register_skill(
        skill_path, content_hash="sha256:source", source_kind="local"
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate",
        skill_id=skill_id,
        content_hash="sha256:source",
        payload={"skill_name": "skill"},
        session_id="telegram-session",
        task_id="task-1",
        qualification="high_usage",
    )
    assert event_id is not None

    assert (
        store.pending_telegram_events(
            kind="wisdom.candidate", session_id="other-session"
        )
        == []
    )
    pending = store.pending_telegram_events(
        kind="wisdom.candidate", session_id="telegram-session"
    )
    assert [item["id"] for item in pending] == [event_id]

    store.mark_telegram_delivered([event_id])

    assert (
        store.pending_telegram_events(
            kind="wisdom.candidate", session_id="telegram-session"
        )
        == []
    )
    assert [
        item["id"]
        for item in store.local_events(
            kind="wisdom.candidate", session_id="telegram-session"
        )
    ] == [event_id]




def test_feed_delivery_is_independent_per_surface(tmp_path: Path):
    store = WisdomStore(tmp_path / "wisdom")
    assert store.persist_local_notice(
        event_id="feed-1",
        kind="new",
        skill_id="skill-1",
        payload={"version": 1},
    )

    store.mark_feed_surface_delivered(["feed-1"], surface="slack")

    assert (
        store.feed_events(surface="slack", surface_due_at="2999-01-01T00:00:00+00:00")
        == []
    )
    assert [
        item["event_id"]
        for item in store.feed_events(
            surface="telegram", surface_due_at="2999-01-01T00:00:00+00:00"
        )
    ] == ["feed-1"]
