"""Hash-bound publication submission and recovery tests.

Behavior is adapted from Agent 0924f7946b tests/wisdom/test_publication_recovery.py
and test_share_queue.py, narrowed to the terminal plugin workflow.
"""
# pylint: disable=missing-function-docstring,too-few-public-methods

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_contribution_review import reviewer

from hermes_wisdom_plugin import publication as publication_module
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict
from hermes_wisdom_plugin.publication import PublicationController


class RecordingPublicationClient:
    """Pinned Gateway-shaped boundary with controllable acknowledgement loss."""

    def __init__(self, owner: dict[str, str], *, mode: str = "open") -> None:
        self.state = SimpleNamespace(
            owner=owner,
            mode=mode,
            policy_version=7,
            calls=Counter(),
            drafts={},
            fail_after_effect=set(),
            uploaded_objects=None,
        )

    def agent_led_policy(self):
        return SimpleNamespace(
            org_id=self.state.owner["organization_id"],
            publication_mode=self.state.mode,
            version=self.state.policy_version,
        )

    def upload_private_objects(self, objects):
        assert objects
        self.state.calls["upload"] += 1
        self.state.uploaded_objects = dict(objects)
        return {"objects": len(objects)}

    def submit_draft(self, **payload):
        self.state.calls["submit"] += 1
        draft = SimpleNamespace(
            id="draft-1",
            orgId=self.state.owner["organization_id"],
            ownerUserId=self.state.owner["account_id"],
            slug=payload["slug"],
            draftCommit=payload["commit"],
            contentHash=payload["content_hash"],
            authorDescription=payload["description"],
            authorDescriptionHash=payload["description_hash"],
            packageManifestHash=payload["manifest_hash"],
            state="ready",
            updatedAt="server-revision-1",
            professionalism_check=payload.get("professionalism_review"),
        )
        self.state.drafts[draft.id] = draft
        if "submit" in self.state.fail_after_effect:
            raise TimeoutError("lost submit acknowledgement")
        return draft

    def list_drafts(self):
        return list(self.state.drafts.values())

    def draft(self, draft_id):
        return SimpleNamespace(draft=self.state.drafts[draft_id], effective_policy={})

    def approve(self, draft_id, **hashes):
        self.state.calls["approve"] += 1
        draft = self.state.drafts[draft_id]
        assert hashes == {
            "content_hash": draft.contentHash,
            "description_hash": draft.authorDescriptionHash,
            "manifest_hash": draft.packageManifestHash,
        }
        draft.state = "owner_approved"
        draft.updatedAt = "server-revision-2"
        return draft

    def publish(self, draft_id, *, content_hash):
        self.state.calls["publish"] += 1
        draft = self.state.drafts[draft_id]
        assert content_hash == draft.contentHash
        draft.state = (
            "published" if self.state.mode == "open" else "pending_moderation"
        )
        draft.updatedAt = "server-revision-3"
        if "publish" in self.state.fail_after_effect:
            raise TimeoutError("lost publish acknowledgement")
        return {"state": draft.state, "proposal_id": 11}

    def decline(self, draft_id):
        draft = self.state.drafts[draft_id]
        if draft.state == "published":
            raise WisdomConflict("state_is_published")
        draft.state = "declined"
        if "decline" in self.state.fail_after_effect:
            raise TimeoutError("lost decline acknowledgement")
        return {"state": "declined"}


def publication(tmp_path: Path, *, mode: str = "open"):
    prepared_reviewer, _source, binding = reviewer(tmp_path)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    client = RecordingPublicationClient(binding, mode=mode)
    controller = PublicationController(
        prepared_reviewer.store,
        prepared_reviewer,
        client,
        lambda: dict(binding),
    )
    return controller, prepared, client, binding


@pytest.mark.parametrize(
    ("mode", "expected"), [("open", "published"), ("moderated", "pending_moderation")]
)
def test_one_explicit_hash_bound_confirmation_uses_server_policy(tmp_path: Path, mode, expected):
    controller, prepared, client, _ = publication(tmp_path, mode=mode)

    review = controller.review(prepared["draft_id"])
    result = controller.submit(review["receipt"])

    assert review["publication_mode"] == mode
    assert review["files"] == prepared["files"]
    assert result["publication_state"] == expected
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, publish=1)
    assert "publication_mode" not in result["submitted_request"]
    assert controller.status(result["operation_id"])["state"] == "done"


@pytest.mark.parametrize("changed", ["content", "description", "manifest", "policy", "owner"])
def test_stale_or_foreign_review_is_rejected_before_transport(tmp_path: Path, changed):
    controller, prepared, client, binding = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    if changed == "content":
        (Path(prepared["overlay_path"]) / "SKILL.md").write_text("changed", encoding="utf-8")
    elif changed == "description":
        with controller.store.transaction() as db:
            db.execute(
                "UPDATE local_draft SET description='changed' WHERE id=?",
                (prepared["draft_id"],),
            )
    elif changed == "manifest":
        with controller.store.transaction() as db:
            db.execute(
                "UPDATE local_draft SET manifest_hash=? WHERE id=?",
                ("sha256:" + "0" * 64, prepared["draft_id"]),
            )
    elif changed == "policy":
        client.state.policy_version += 1
    else:
        foreign = {**binding, "account_id": "other"}
        controller.owner_provider = lambda: foreign

    with pytest.raises((WisdomAuthError, WisdomConflict)):
        controller.submit(review["receipt"])
    assert not client.state.calls


def test_file_set_save_invalidates_old_review_and_publishes_fresh_exact_bytes(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    stale_review = controller.review(prepared["draft_id"])
    files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ]
    files.append({"path": "refs/added.md", "content_utf8": "fresh reviewed bytes\n"})
    saved = controller.reviewer.save_prepared(
        prepared["draft_id"], author_description=prepared["drafted_description"],
        files=files, expected_hashes=prepared["hashes"],
    )

    with pytest.raises(WisdomConflict, match="changed|review"):
        controller.submit(stale_review["receipt"])
    assert client.state.calls == Counter()

    fresh_review = controller.review(prepared["draft_id"])
    assert fresh_review["hashes"] == saved["hashes"]
    assert {item["path"] for item in fresh_review["files"]} == {
        "SKILL.md", "skill.manifest.json", "refs/added.md"
    }
    result = controller.submit(fresh_review["receipt"])

    assert result["publication_state"] == "published"
    assert client.state.uploaded_objects is not None
    uploaded_blobs = {
        body for kind, body in client.state.uploaded_objects.values() if kind == "blob"
    }
    assert b"fresh reviewed bytes\n" in uploaded_blobs


def test_prior_remote_failure_preserves_editable_draft(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    client.submit_draft = lambda **_payload: (_ for _ in ()).throw(RuntimeError("offline"))

    result = controller.submit(review["receipt"])

    assert result["state"] == "uncertain"
    assert result["phase"] == "draft_submission_unknown"
    assert controller.store.draft(prepared["draft_id"])["state"] == "prepared"
    assert Path(prepared["overlay_path"]).is_dir()


def test_submit_rechecks_source_and_current_upload_guard(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    source = Path(controller.store.local_skill(prepared["skill_id"])["canonical_path"])
    (source / "SKILL.md").write_text("changed source", encoding="utf-8")

    with pytest.raises(WisdomConflict, match="source changed"):
        controller.submit(review["receipt"])
    assert client.state.calls["upload"] == 0

    controller, prepared, client, _ = publication(tmp_path / "guard")
    review = controller.review(prepared["draft_id"])
    controller.reviewer.host.allowed = False
    with pytest.raises(WisdomConflict, match="security"):
        controller.submit(review["receipt"])
    assert client.state.calls["upload"] == 0


def test_cas_uses_same_validated_snapshot_when_overlay_changes_before_tree_build(
    tmp_path: Path, monkeypatch
):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    overlay = Path(prepared["overlay_path"])
    reviewed_skill = (overlay / "SKILL.md").read_bytes()
    unreviewed = b"unreviewed secret bytes"
    original_verify = publication_module.verify_content_files
    changed = False

    def mutate_after_validation(raw):
        nonlocal changed
        result = original_verify(raw)
        if not changed:
            changed = True
            (overlay / "SKILL.md").write_bytes(unreviewed)
        return result

    monkeypatch.setattr(publication_module, "verify_content_files", mutate_after_validation)
    result = controller.submit(review["receipt"])

    assert result["publication_state"] == "published"
    assert client.state.uploaded_objects is not None
    uploaded_blobs = [
        body for kind, body in client.state.uploaded_objects.values() if kind == "blob"
    ]
    assert reviewed_skill in uploaded_blobs
    assert unreviewed not in uploaded_blobs


def test_submit_ack_loss_reconciles_exact_remote_draft_without_duplicate(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    client.state.fail_after_effect.add("submit")

    uncertain = controller.submit(review["receipt"])
    assert uncertain["state"] == "uncertain"
    assert uncertain["phase"] == "draft_submission_unknown"
    client.state.fail_after_effect.remove("submit")

    recovered = controller.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls["submit"] == client.state.calls["publish"] == 1


def test_publish_ack_loss_reconciles_after_restart_without_republishing(tmp_path: Path):
    controller, prepared, client, binding = publication(tmp_path, mode="moderated")
    review = controller.review(prepared["draft_id"])
    client.state.fail_after_effect.add("publish")

    uncertain = controller.submit(review["receipt"])
    assert uncertain["phase"] == "publication_unknown"
    client.state.fail_after_effect.remove("publish")
    reopened = PublicationController(controller.store, controller.reviewer, client, lambda: binding)

    recovered = reopened.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "pending_moderation"
    assert client.state.calls["publish"] == 1


def test_db_failure_after_remote_submit_recovers_by_exact_listing(tmp_path: Path, monkeypatch):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    original = controller.store.replace_operation_payload
    failed = False

    def fail_once(operation_id, payload):
        nonlocal failed
        if payload.get("remote_draft_id") and not failed:
            failed = True
            raise OSError("injected local commit failure")
        return original(operation_id, payload)

    monkeypatch.setattr(controller.store, "replace_operation_payload", fail_once)
    uncertain = controller.submit(review["receipt"])
    assert uncertain["phase"] == "draft_submission_unknown"
    monkeypatch.setattr(controller.store, "replace_operation_payload", original)

    recovered = controller.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls["submit"] == client.state.calls["publish"] == 1


def test_policy_change_does_not_hide_authoritative_committed_outcome(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path, mode="moderated")
    client.state.fail_after_effect.add("publish")
    uncertain = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    client.state.fail_after_effect.remove("publish")
    client.state.policy_version += 1

    recovered = controller.reopen(uncertain["operation_id"])

    assert recovered["publication_state"] == "pending_moderation"
    assert client.state.calls["publish"] == 1


def test_policy_change_stalls_new_effect_until_fresh_operation_review(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    original_publish = client.publish
    client.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        TimeoutError("offline")
    )
    uncertain = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    client.publish = original_publish
    client.state.policy_version += 1

    stalled = controller.reopen(uncertain["operation_id"])
    assert stalled["phase"] == "policy_review_required"
    fresh = controller.review(uncertain["operation_id"])
    recovered = controller.submit(fresh["receipt"])

    assert recovered["publication_state"] == "published"
    assert client.state.calls["publish"] == 1


def test_final_projection_is_atomic_and_repaired_after_database_failure(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    with controller.store.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_publication_projection BEFORE UPDATE OF state ON local_draft "
            "WHEN NEW.state='published' "
            "BEGIN SELECT RAISE(ABORT,'injected projection failure'); END"
        )

    with pytest.raises(Exception, match="injected projection failure"):
        controller.submit(review["receipt"])
    operation = controller.store.pending_operations()[0]
    assert operation["state"] == "pending"
    with controller.store.transaction() as db:
        db.execute("DROP TRIGGER fail_publication_projection")

    recovered = controller.reopen(operation["id"])
    assert recovered["publication_state"] == "published"
    assert controller.store.draft(prepared["draft_id"])["state"] == "published"
    assert client.state.calls["publish"] == 1


def test_concurrent_controllers_do_not_double_submit(tmp_path: Path):
    controller, prepared, client, binding = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    lock_key = "publication:" + prepared["skill_id"]
    held = controller.store.acquire_operation_lock(lock_key)
    assert held
    other = PublicationController(controller.store, controller.reviewer, client, lambda: binding)
    try:
        with pytest.raises(WisdomConflict, match="already being submitted"):
            other.submit(review["receipt"])
    finally:
        controller.store.release_operation_lock(lock_key, held)
    assert client.state.calls["submit"] == 0


def test_lost_lease_after_remote_effect_cannot_be_finalized_by_stale_worker(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    original_publish = client.publish

    def publish_then_steal_lease(*args, **kwargs):
        result = original_publish(*args, **kwargs)
        with controller.store.transaction() as db:
            db.execute(
                "UPDATE operation_lock SET owner='takeover',"
                "expires_at='2999-01-01T00:00:00+00:00' WHERE entity_id=?",
                ("publication:" + prepared["skill_id"],),
            )
        return result

    client.publish = publish_then_steal_lease
    uncertain = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    assert uncertain["phase"] == "publication_unknown"
    assert controller.store.operation(uncertain["operation_id"])["state"] == "pending"
    with controller.store.transaction() as db:
        db.execute("UPDATE operation_lock SET expires_at='2000-01-01T00:00:00+00:00'")
    client.publish = original_publish

    recovered = controller.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls["publish"] == 1


def test_reopen_is_idempotent_and_decline_obeys_safe_terminal_semantics(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path, mode="moderated")
    result = controller.submit(controller.review(prepared["draft_id"])["receipt"])

    assert controller.reopen(result["operation_id"])["publication_state"] == "pending_moderation"
    declined = controller.decline(result["operation_id"])
    assert declined["publication_state"] == "declined"
    assert controller.decline(result["operation_id"])["publication_state"] == "declined"

    client.state.drafts["draft-1"].state = "published"
    with pytest.raises(WisdomConflict):
        controller.decline(result["operation_id"])


def test_decline_persists_intent_before_effect_and_recovers_lost_ack(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path, mode="moderated")
    result = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    original_decline = client.decline

    def checked_decline(draft_id):
        operation = controller.store.operation(result["operation_id"])
        assert operation["state"] == "pending"
        assert operation["phase"] == "decline_unknown"
        return original_decline(draft_id)

    client.decline = checked_decline
    client.state.fail_after_effect.add("decline")
    uncertain = controller.decline(result["operation_id"])
    assert uncertain["phase"] == "decline_unknown"
    client.state.fail_after_effect.remove("decline")

    recovered = controller.reopen(result["operation_id"])
    assert recovered["publication_state"] == "declined"
    assert client.state.calls["publish"] == 1
