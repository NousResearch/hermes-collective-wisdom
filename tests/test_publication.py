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
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomError
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
            head=None,
            heads=[],
            publish_results=[],
            publish_bases=[],
            publication_projection=None,
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
        return SimpleNamespace(
            draft=self.state.drafts[draft_id], effective_policy={},
            publication=self.state.publication_projection,
        )

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

    def organization_head(self):
        self.state.calls["head"] += 1
        if self.state.heads:
            value = self.state.heads.pop(0)
            if isinstance(value, Exception):
                raise value
            self.state.head = value
        return self.state.head

    def publish(self, draft_id, *, content_hash, base_commit):
        self.state.calls["publish"] += 1
        self.state.publish_bases.append(base_commit)
        draft = self.state.drafts[draft_id]
        assert content_hash == draft.contentHash
        if self.state.publish_results:
            result = self.state.publish_results.pop(0)
            if isinstance(result, Exception):
                raise result
            if result.get("state") == "pending_approve_conflict":
                self.state.publication_projection = SimpleNamespace(
                    state="base_conflict",
                    reason=result.get("reason"),
                    retryable_with_fresh_base=result.get("retryable_with_fresh_base"),
                    proposal_id=result.get("proposal_id"),
                )
                return result
            return result
        self.state.publication_projection = None
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
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)
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


def test_revocation_during_real_owner_validation_prevents_publication_intent_and_transport(
    tmp_path: Path,
):
    controller, prepared, client, _binding = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    admitted = [True]
    original_scan = controller.reviewer.host.scan

    def revoke_during_scan(overlay):
        result = original_scan(overlay)
        admitted[0] = False
        return result

    controller.reviewer.host.scan = revoke_during_scan
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.submit(review["receipt"], may_apply=lambda: admitted[0])

    assert client.state.calls == Counter()
    assert controller.store.operation(prepared["draft_id"]) is None


def test_revocation_after_object_upload_stops_before_remote_draft_submission(tmp_path: Path):
    controller, prepared, client, _binding = publication(tmp_path)
    review = controller.review(prepared["draft_id"])
    admitted = [True]
    original_upload = client.upload_private_objects

    def upload_then_revoke(objects):
        result = original_upload(objects)
        admitted[0] = False
        return result

    client.upload_private_objects = upload_then_revoke
    result = controller.submit(review["receipt"], may_apply=lambda: admitted[0])

    assert result["state"] == "pending"
    assert result["phase"] == "objects_uploaded"
    assert "invocation ended" in result["detail"].lower()
    assert client.state.calls == Counter(upload=1)


def test_revocation_after_decline_status_read_stops_before_remote_mutation(tmp_path: Path):
    controller, prepared, client, _binding = publication(tmp_path, mode="moderated")
    published = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    admitted = [True]
    original_draft = client.draft

    def read_then_revoke(draft_id):
        result = original_draft(draft_id)
        admitted[0] = False
        return result

    client.draft = read_then_revoke
    result = controller.decline(
        published["operation_id"], may_apply=lambda: admitted[0]
    )

    assert result["state"] == "pending"
    assert result["phase"] == "pending_moderation"
    assert client.state.drafts["draft-1"].state == "pending_moderation"


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


def test_confirmed_base_conflicts_retry_same_draft_with_fresh_head_bounded(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    client.state.heads = [
        None, "sha256:" + "a" * 64, "sha256:" + "b" * 64,
    ]
    conflict = {
        "state": "pending_approve_conflict", "reason": "base_mismatch",
        "retryable_with_fresh_base": True, "proposal_id": 11,
    }
    client.state.publish_results = [conflict, conflict]

    result = controller.submit(controller.review(prepared["draft_id"])["receipt"])

    assert result["publication_state"] == "published"
    assert client.state.publish_bases == [None, "sha256:" + "a" * 64, "sha256:" + "b" * 64]
    assert client.state.calls["submit"] == client.state.calls["approve"] == 1


def test_repeated_conflict_stays_durable_and_each_reopen_has_bounded_budget(tmp_path: Path):
    controller, prepared, client, binding = publication(tmp_path)
    conflict = {
        "state": "pending_approve_conflict", "reason": "base_mismatch",
        "retryable_with_fresh_base": True, "proposal_id": 11,
    }
    client.state.heads = ["sha256:" + character * 64 for character in "abcdef"]
    client.state.publish_results = [conflict] * 6
    result = controller.submit(controller.review(prepared["draft_id"])["receipt"])

    assert result["phase"] == "base_conflict"
    assert result["retry_attempts"] == 3
    reopened = PublicationController(
        controller.store, controller.reviewer, client, lambda: dict(binding)
    ).reopen(result["operation_id"])
    assert reopened["phase"] == "base_conflict"
    assert client.state.calls["publish"] == 6
    assert client.state.calls["submit"] == client.state.calls["approve"] == 1


def test_base_conflict_reopen_persists_authoritative_get_403_without_retry(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    conflict = {
        "state": "pending_approve_conflict", "reason": "base_mismatch",
        "retryable_with_fresh_base": True, "proposal_id": 11,
    }
    client.state.heads = ["sha256:" + character * 64 for character in "abc"]
    client.state.publish_results = [conflict] * 3
    pending = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    publish_calls = client.state.calls["publish"]
    client.draft = lambda _draft_id: (_ for _ in ()).throw(
        WisdomAuthError("publication forbidden", status=403)
    )

    with pytest.raises(WisdomAuthError, match="forbidden"):
        controller.reopen(pending["operation_id"])

    operation = controller.store.operation(pending["operation_id"])
    assert operation["phase"] == "publication_failed"
    assert operation["payload"]["failure_reason"] == "WisdomAuthError"
    assert client.state.calls["publish"] == publish_calls


def test_approval_403_is_persisted_and_never_repeats_approval(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)

    def reject_approval(_draft_id, **_hashes):
        client.state.calls["approve"] += 1
        raise WisdomAuthError("approval forbidden", status=403)

    client.approve = reject_approval
    with pytest.raises(WisdomAuthError, match="forbidden"):
        controller.submit(controller.review(prepared["draft_id"])["receipt"])

    operation = controller.store.pending_operations()[0]
    assert operation["phase"] == "publication_failed"
    reopened = controller.reopen(operation["id"])
    assert reopened["phase"] == "publication_failed"
    assert client.state.calls["approve"] == 1
    assert client.state.calls["publish"] == 0


def test_non_base_conflict_and_head_failure_are_not_blindly_retried(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    client.state.publish_results = [WisdomConflict("other", status=409, code="other")]
    with pytest.raises(WisdomConflict, match="other"):
        controller.submit(controller.review(prepared["draft_id"])["receipt"])
    assert client.state.calls["publish"] == 1
    operation = controller.store.pending_operations()[0]
    assert operation["phase"] == "publication_failed"

    controller, prepared, client, _ = publication(tmp_path / "head")
    client.state.heads = [WisdomError("offline")]
    with pytest.raises(WisdomError, match="offline"):
        controller.submit(controller.review(prepared["draft_id"])["receipt"])
    operation = controller.store.pending_operations()[0]
    assert operation["phase"] == "approved"
    assert client.state.calls["publish"] == 0


def test_unstructured_legacy_conflict_requires_authoritative_projection(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    client.state.publish_results = [{
        "state": "pending_approve_conflict", "proposal_id": 11,
    }]

    result = controller.submit(controller.review(prepared["draft_id"])["receipt"])

    assert result["phase"] == "server_pending"
    assert client.state.calls["publish"] == 1


def test_conflict_retry_rechecks_currentness_and_policy_before_next_effect(tmp_path: Path):
    controller, prepared, client, _ = publication(tmp_path)
    conflict = {
        "state": "pending_approve_conflict", "reason": "base_mismatch",
        "retryable_with_fresh_base": True, "proposal_id": 11,
    }
    client.state.publish_results = [conflict]
    active = [True]
    original_draft = client.draft

    def revoke_on_reconcile(draft_id):
        detail = original_draft(draft_id)
        if client.state.calls["publish"]:
            active[0] = False
        return detail

    client.draft = revoke_on_reconcile
    result = controller.submit(
        controller.review(prepared["draft_id"])["receipt"],
        may_apply=lambda: active[0],
    )
    assert result["phase"] == "base_conflict"
    assert client.state.calls["publish"] == 1

    controller, prepared, client, _ = publication(tmp_path / "policy")
    original_draft = client.draft

    def change_policy_on_reconcile(draft_id):
        detail = original_draft(draft_id)
        if client.state.calls["publish"]:
            client.state.policy_version += 1
        return detail

    client.draft = change_policy_on_reconcile
    client.state.publish_results = [conflict]
    stalled = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    assert stalled["phase"] == "policy_review_required"
    assert client.state.calls["publish"] == 1


def test_lost_publish_ack_retries_only_after_authoritative_conflict_evidence(tmp_path: Path):
    controller, prepared, client, binding = publication(tmp_path)
    original_publish = client.publish
    client.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("lost"))
    uncertain = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    client.publish = original_publish
    draft = client.state.drafts["draft-1"]
    draft.state = "owner_approved"
    original_draft = client.draft

    def conflict_projection(draft_id):
        detail = original_draft(draft_id)
        detail.publication = SimpleNamespace(
            state="base_conflict", reason="base_mismatch",
            retryable_with_fresh_base=True, proposal_id=11,
        )
        return detail

    client.draft = conflict_projection
    result = PublicationController(
        controller.store, controller.reviewer, client, lambda: dict(binding)
    ).reopen(uncertain["operation_id"])

    assert result["publication_state"] == "published"
    assert client.state.calls["submit"] == client.state.calls["approve"] == 1


def test_reopen_recovers_legacy_server_pending_only_from_new_projection(tmp_path: Path):
    controller, prepared, client, binding = publication(tmp_path)
    client.state.publish_results = [{"state": "unexpected_pending"}]
    stranded = controller.submit(controller.review(prepared["draft_id"])["receipt"])
    assert stranded["phase"] == "server_pending"
    original_draft = client.draft

    without_evidence = PublicationController(
        controller.store, controller.reviewer, client, lambda: dict(binding)
    ).reopen(stranded["operation_id"])
    assert without_evidence["phase"] == "server_pending"
    assert client.state.calls["publish"] == 1

    def conflict_projection(draft_id):
        detail = original_draft(draft_id)
        detail.publication = SimpleNamespace(
            state="base_conflict", reason="base_mismatch",
            retryable_with_fresh_base=True, proposal_id=17,
        )
        return detail

    client.draft = conflict_projection
    recovered = PublicationController(
        controller.store, controller.reviewer, client, lambda: dict(binding)
    ).reopen(stranded["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls["submit"] == client.state.calls["approve"] == 1


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

    assert recovered["phase"] == "publication_unknown"
    assert recovered["state"] == "uncertain"
    assert client.state.calls["publish"] == 0


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
