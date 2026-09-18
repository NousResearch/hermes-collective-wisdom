"""Behavioral tests for direct PluginState-backed publication."""
# pylint: disable=missing-function-docstring,too-few-public-methods

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_contribution_review import reviewer

from hermes_wisdom_plugin.direct_services import PublicationService
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict


class RecordingPublicationClient:
    """Gateway-shaped fake with controllable lost acknowledgements."""

    def __init__(self, owner: dict[str, str], *, mode: str = "open") -> None:
        self.state = SimpleNamespace(
            owner=owner, mode=mode, policy_version=7, calls=Counter(), drafts={},
            fail_after_effect=set(), uploaded_objects=None, head=None,
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
            id="draft-1", orgId=self.state.owner["organization_id"],
            ownerUserId=self.state.owner["account_id"], slug=payload["slug"],
            draftCommit=payload["commit"], contentHash=payload["content_hash"],
            authorDescription=payload["description"],
            authorDescriptionHash=payload["description_hash"],
            packageManifestHash=payload["manifest_hash"], state="ready",
            updatedAt="server-revision-1",
        )
        self.state.drafts[draft.id] = draft
        if "submit" in self.state.fail_after_effect:
            raise TimeoutError("lost submit acknowledgement")
        return draft

    def list_drafts(self):
        return list(self.state.drafts.values())

    def draft(self, draft_id):
        return SimpleNamespace(draft=self.state.drafts[draft_id])

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
        return self.state.head

    def publish(self, draft_id, *, content_hash, base_commit):
        del base_commit
        self.state.calls["publish"] += 1
        draft = self.state.drafts[draft_id]
        assert content_hash == draft.contentHash
        draft.state = "published" if self.state.mode == "open" else "pending_moderation"
        draft.updatedAt = "server-revision-3"
        if "publish" in self.state.fail_after_effect:
            raise TimeoutError("lost publish acknowledgement")
        return {"state": draft.state, "proposal_id": 11}

    def decline(self, draft_id):
        self.state.calls["decline"] += 1
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
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(binding)
    )
    return service, prepared, client, binding


@pytest.mark.parametrize(
    ("mode", "expected"), [("open", "published"), ("moderated", "pending_moderation")]
)
def test_one_confirmation_uses_server_policy_and_exact_review(tmp_path: Path, mode, expected):
    service, prepared, client, _ = publication(tmp_path, mode=mode)

    review = service.review(prepared["draft_id"])
    result = service.submit(review["receipt"])

    assert review["publication_mode"] == mode
    assert review["files"] == prepared["files"]
    assert result["publication_state"] == expected
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)
    assert "publication_mode" not in result["submitted_request"]
    assert service.status(result["operation_id"])["state"] == "done"


def test_private_draft_requires_fresh_confirmation(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path, mode="moderated")

    staged = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    assert staged["phase"] == "policy_review_required"
    assert client.state.calls == Counter(upload=1, submit=1)

    final = service.submit(service.review(staged["operation_id"])["receipt"])
    assert final["publication_state"] == "pending_moderation"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


def test_submit_ack_loss_reconciles_exact_remote_without_duplicate(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path)
    client.state.fail_after_effect.add("submit")

    uncertain = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert uncertain["phase"] == "draft_submission_unknown"
    client.state.fail_after_effect.clear()
    restarted = PublicationService(service.state, service.reviewer, client, lambda: dict(binding))

    recovered = restarted.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


def test_publish_ack_loss_reconciles_remote_without_republish(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path)
    client.state.fail_after_effect.add("publish")

    uncertain = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert uncertain["phase"] == "publication_unknown"
    client.state.fail_after_effect.clear()
    restarted = PublicationService(service.state, service.reviewer, client, lambda: dict(binding))

    recovered = restarted.reopen(uncertain["operation_id"])
    assert recovered["publication_state"] == "published"
    assert client.state.calls["publish"] == 1


def test_policy_change_after_private_draft_requires_another_review(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    staged = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    review = service.review(staged["operation_id"])
    client.state.policy_version += 1

    with pytest.raises(WisdomConflict, match="policy changed"):
        service.submit(review["receipt"])
    assert client.state.calls == Counter(upload=1, submit=1)


def test_owner_change_rejected_before_transport(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path)
    review = service.review(prepared["draft_id"])
    binding["account_id"] = "other-member"

    with pytest.raises(WisdomAuthError, match="owner changed"):
        service.submit(review["receipt"])
    assert not client.state.calls


def test_source_change_rejected_before_transport(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    review = service.review(prepared["draft_id"])
    draft = service.state.draft(prepared["draft_id"])
    assert draft is not None
    (Path(draft["source_path"]) / "SKILL.md").write_text("changed", encoding="utf-8")

    with pytest.raises(WisdomConflict, match="changed"):
        service.submit(review["receipt"])
    assert not client.state.calls


def test_currentness_loss_after_upload_stops_before_submit(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    active = [True]
    upload = client.upload_private_objects

    def upload_then_cancel(objects):
        result = upload(objects)
        active[0] = False
        return result

    client.upload_private_objects = upload_then_cancel
    stopped = service.submit(
        service.review(prepared["draft_id"])["receipt"], may_apply=lambda: active[0]
    )

    assert stopped["phase"] == "objects_uploaded"
    assert client.state.calls == Counter(upload=1)


def test_decline_lost_ack_reconciles_without_duplicate(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    staged = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    client.state.fail_after_effect.add("decline")

    uncertain = service.decline(staged["operation_id"])
    assert uncertain["phase"] == "decline_unknown"
    client.state.fail_after_effect.clear()
    recovered = service.reopen(staged["operation_id"])

    assert recovered["publication_state"] == "declined"
    assert client.state.calls["decline"] == 1


def test_currentness_loss_during_head_read_stops_before_publish(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    active = [True]
    read_head = client.organization_head

    def head_then_cancel():
        head = read_head()
        active[0] = False
        return head

    client.organization_head = head_then_cancel
    with pytest.raises(WisdomAuthError, match="no longer current"):
        service.submit(
            service.review(prepared["draft_id"])["receipt"], may_apply=lambda: active[0]
        )

    assert client.state.calls["publish"] == 0
    assert service.state.publication_for_draft(prepared["draft_id"])["phase"] == "approved"
