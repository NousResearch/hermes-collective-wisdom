"""Behavioral tests for direct PluginState-backed publication."""
# pylint: disable=missing-function-docstring,too-few-public-methods

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState
from test_contribution_review import Host, make_skill, owner as contribution_owner, reviewer

from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.direct_services import DirectState, PublicationService
from hermes_wisdom_plugin.errors import (
    WisdomAuthError, WisdomConflict, WisdomValidationError,
)
from hermes_wisdom_plugin.local_state import QualificationState


class QuotaJsonState(JsonState):
    """File-backed state with the stock host's aggregate JSON quota."""

    quota_bytes = 10 * 1024 * 1024

    def set(self, key, value) -> None:
        current = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.is_file() else {}
        )
        current[key] = value
        encoded = json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > self.quota_bytes:
            raise ValueError(
                f"Plugin state quota exceeded: {len(encoded)} bytes is greater "
                f"than the {self.quota_bytes}-byte per-plugin quota"
            )
        super().set(key, value)


class RecordingPublicationClient:
    """Gateway-shaped fake with controllable lost acknowledgements."""

    def __init__(self, owner: dict[str, str], *, mode: str = "open") -> None:
        self.state = SimpleNamespace(
            owner=owner, mode=mode, policy_version=7, calls=Counter(), drafts={},
            fail_after_effect=set(), submit_error=None, upload_error=None,
            revise_error=None, approve_error=None, uploaded_objects=None, head=None,
            scan_verdict="pass",
        )

    def agent_led_policy(self):
        return SimpleNamespace(
            org_id=self.state.owner["organization_id"],
            publication_mode=self.state.mode,
            version=self.state.policy_version,
        )

    def upload_private_objects(self, objects):
        if self.state.upload_error is not None:
            raise self.state.upload_error
        assert objects
        self.state.calls["upload"] += 1
        self.state.uploaded_objects = dict(objects)
        return {"objects": len(objects)}

    def submit_draft(self, **payload):
        self.state.calls["submit"] += 1
        if self.state.submit_error is not None:
            raise self.state.submit_error
        draft = SimpleNamespace(
            id="draft-1", orgId=self.state.owner["organization_id"],
            ownerUserId=self.state.owner["account_id"], slug=payload["slug"],
            draftCommit=payload["commit"], contentHash=payload["content_hash"],
            authorDescription=payload["description"],
            authorDescriptionHash=payload["description_hash"],
            packageManifestHash=payload["manifest_hash"], state="ready",
            scanVerdict=self.state.scan_verdict, supersedesDraftId=None,
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
        if self.state.approve_error is not None:
            raise self.state.approve_error
        draft = self.state.drafts[draft_id]
        assert hashes == {
            "content_hash": draft.contentHash,
            "description_hash": draft.authorDescriptionHash,
            "manifest_hash": draft.packageManifestHash,
        }
        if draft.scanVerdict == "blocked":
            raise WisdomValidationError("scan_blocked", status=422, code="scan_blocked")
        draft.state = "owner_approved"
        draft.updatedAt = "server-revision-2"
        return draft

    def revise_draft(self, draft_id, **payload):
        self.state.calls["revise"] += 1
        if self.state.revise_error is not None:
            raise self.state.revise_error
        predecessor = self.state.drafts[draft_id]
        assert payload["expected_content_hash"] == predecessor.contentHash
        assert payload["expected_description_hash"] == predecessor.authorDescriptionHash
        assert payload["expected_manifest_hash"] == predecessor.packageManifestHash
        successor = SimpleNamespace(
            id="draft-2", orgId=predecessor.orgId,
            ownerUserId=predecessor.ownerUserId, slug=predecessor.slug,
            draftCommit=payload["commit"], contentHash=payload["content_hash"],
            authorDescription=payload["description"],
            authorDescriptionHash=payload["description_hash"],
            packageManifestHash=payload["manifest_hash"], state="ready",
            scanVerdict=self.state.scan_verdict, supersedesDraftId=draft_id,
            scan={
                "verdict": self.state.scan_verdict,
                "findings": ([{
                    "severity": "warning", "category": "gateway",
                    "path": "SKILL.md", "message": "Gateway advisory finding",
                }] if self.state.scan_verdict == "advisory" else []),
            },
            updatedAt="server-revision-2",
        )
        predecessor.state = "invalidated"
        self.state.drafts[successor.id] = successor
        if "revise" in self.state.fail_after_effect:
            raise TimeoutError("lost revision acknowledgement")
        return successor

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
        return {
            "state": draft.state,
            "proposal_id": 11,
            **(
                {"skill_id": "published-skill-1", "version": 1}
                if draft.state == "published" else {}
            ),
        }

    def decline(self, draft_id):
        self.state.calls["decline"] += 1
        draft = self.state.drafts[draft_id]
        if draft.state == "published":
            raise WisdomConflict("state_is_published")
        draft.state = "declined"
        if "decline" in self.state.fail_after_effect:
            raise TimeoutError("lost decline acknowledgement")
        return {"state": "declined"}


class UnexpectedEnrollment:
    """Fail a test if a non-published path attempts managed enrollment."""

    def link(self, *_args, **_kwargs):
        raise AssertionError("non-published operation must not enroll")

    def link_existing(self, *_args, **_kwargs):
        raise AssertionError("non-published operation must not enroll")


def publication(tmp_path: Path, *, mode: str = "open", scan_allowed: bool = True):
    prepared_reviewer, _source, binding = reviewer(tmp_path, allowed=scan_allowed)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    client = RecordingPublicationClient(binding, mode=mode)
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(binding)
    )
    return service, prepared, client, binding


class PublicationCli:
    """Expose the real publication service through the public CLI contract."""

    def __init__(self, service):
        self.service = service

    def publication_review(self, draft_id, *, accept_warnings=False):
        return self.service.review(draft_id, accept_warnings=accept_warnings)

    def publication_submit(self, receipt):
        return self.service.submit(receipt)

    def publication_reopen(self, operation_id):
        return self.service.reopen(operation_id)


def publication_parser():
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    return parser


def cli_json(parser, service, capsys, *arguments):
    code = run_cli(parser.parse_args([*arguments, "--json"]), service)
    return code, json.loads(capsys.readouterr().out)


def test_cli_recovers_persisted_blocked_approval_via_fresh_review_and_revision(
    tmp_path: Path, capsys,
):  # pylint: disable=too-many-locals
    service, prepared, client, binding = publication(tmp_path)
    client.state.scan_verdict = "blocked"
    parser = publication_parser()
    cli = PublicationCli(service)

    code, review = cli_json(
        parser, cli, capsys, "publication", "review", prepared["draft_id"]
    )
    assert code == 0
    code, rejected = cli_json(
        parser, cli, capsys, "publication", "submit", review["receipt"]
    )
    assert code == WisdomValidationError.exit_code
    assert rejected["phase"] == "approval_rejected"
    operation_id = rejected["operation_id"]
    assert service.state.publication(operation_id)["phase"] == "approval_rejected"

    restarted = PublicationService(
        DirectState(service.state.state, service.state.root),
        service.reviewer, client, lambda: dict(binding),
    )
    cli = PublicationCli(restarted)
    client.state.scan_verdict = "pass"
    code, fresh = cli_json(
        parser, cli, capsys, "publication", "review", operation_id
    )
    assert code == 0
    assert client.state.calls == Counter(upload=1, submit=1, approve=1)

    code, completed = cli_json(
        parser, cli, capsys, "publication", "submit", fresh["receipt"]
    )
    assert code == 0
    assert completed["publication_state"] == "published"
    assert completed["remote_draft_id"] == "draft-2"
    operation = restarted.state.publication(operation_id)
    assert operation["remote_draft_history"] == ["draft-1"]
    assert client.state.drafts["draft-1"].state == "invalidated"
    assert client.state.calls == Counter(
        upload=1, submit=1, approve=2, revise=1, head=1, publish=1
    )


def blocked_publication(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path)
    client.state.scan_verdict = "blocked"
    rejected = service.submit(service.review(prepared["draft_id"])["receipt"])
    return service, client, binding, rejected["operation_id"]


def test_reopen_converts_legacy_blocked_approval_unknown_to_reviewable_rejection(tmp_path: Path):
    service, client, binding, operation_id = blocked_publication(tmp_path)
    service.state.update_publication(operation_id, phase="approval_unknown")
    restarted = PublicationService(
        DirectState(service.state.state, service.state.root),
        service.reviewer, client, lambda: dict(binding),
    )

    recovered = restarted.reopen(operation_id)

    assert recovered["phase"] == "approval_rejected"
    assert restarted.review(operation_id)["network_submission"] is False
    assert client.state.calls == Counter(upload=1, submit=1, approve=1)


def test_blocked_rescan_requires_another_fresh_review_without_approval(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)

    rejected = service.submit(service.review(operation_id)["receipt"])

    assert rejected["phase"] == "approval_rejected"
    assert service.state.publication(operation_id)["remote_draft_id"] == "draft-2"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, revise=1)
    assert service.review(operation_id)["server_checks"]["security_check"]["status"] == "blocked"


def test_lost_revision_ack_reconciles_exact_successor_without_duplicate(tmp_path: Path):
    service, client, binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    client.state.scan_verdict = "pass"
    client.state.fail_after_effect.add("revise")

    uncertain = service.submit(review["receipt"])
    assert uncertain["phase"] == "revision_unknown"
    client.state.fail_after_effect.clear()
    restarted = PublicationService(
        DirectState(service.state.state, service.state.root),
        service.reviewer, client, lambda: dict(binding),
    )

    recovered = restarted.reopen(operation_id)

    assert recovered["publication_state"] == "published"
    assert recovered["remote_draft_id"] == "draft-2"
    assert client.state.calls == Counter(
        upload=1, submit=1, approve=2, revise=1, head=1, publish=1
    )


def test_revision_advisory_requires_review_before_any_approval(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    client.state.scan_verdict = "advisory"

    pending = service.submit(review["receipt"])

    assert pending["phase"] == "policy_review_required"
    assert "advisory" in pending["detail"]
    assert pending["server_checks"]["security_check"]["findings"] == (
        client.state.drafts["draft-2"].scan["findings"]
    )
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, revise=1)
    with pytest.raises(WisdomConflict, match="acknowledgement"):
        service.review(operation_id)
    fresh = service.review(operation_id, accept_warnings=True)
    assert fresh["server_checks"]["security_check"]["status"] == "advisory"
    assert fresh["warning_acceptance"]["accepted"] is True


def test_unknown_revision_verdict_stays_review_required_across_reopen(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    client.state.scan_verdict = None

    with pytest.raises(WisdomValidationError, match="revision"):
        service.submit(review["receipt"])

    assert service.state.publication_review(review["receipt"]) is None
    assert service.state.publication(operation_id)["phase"] == "revision_unknown"
    with pytest.raises(WisdomValidationError, match="revision"):
        service.reopen(operation_id)
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, revise=1)


def test_advisory_acceptance_binds_complete_gateway_evidence(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    client.state.scan_verdict = "advisory"
    service.submit(service.review(operation_id)["receipt"])
    review = service.review(operation_id, accept_warnings=True)
    client.state.drafts["draft-2"].scan["findings"][0]["message"] = "changed evidence"

    with pytest.raises(WisdomConflict, match="changed"):
        service.submit(review["receipt"])

    assert client.state.calls == Counter(upload=1, submit=1, approve=1, revise=1)


def test_private_existing_submission_stays_private_until_public_submit(tmp_path: Path):
    service, prepared, client, _binding = publication(tmp_path)
    staged = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    operation_id = staged["operation_id"]

    staged_again = service.submit_private_draft(service.review(operation_id)["receipt"])

    assert staged_again["phase"] == "policy_review_required"
    assert service.state.publication(operation_id)["requested_action"] == "private_draft"
    assert client.state.calls == Counter(upload=1, submit=1)

    completed = service.submit(service.review(operation_id)["receipt"])
    assert completed["publication_state"] == "published"


def test_malformed_revision_acknowledgement_is_uncertain_not_rejected(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    client.state.revise_error = WisdomValidationError("malformed response")

    result = service.submit(service.review(operation_id)["receipt"])

    assert result["phase"] == "revision_unknown"
    assert service.state.publication(operation_id)["phase"] == "revision_unknown"


def test_malformed_approval_acknowledgement_remains_uncertain(tmp_path: Path):
    service, prepared, client, _binding = publication(tmp_path)
    client.state.approve_error = WisdomValidationError("malformed response")

    result = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert result["phase"] == "approval_unknown"
    operation = service.state.publication(result["operation_id"])
    assert operation is not None
    assert operation["phase"] == "approval_unknown"


def test_expired_blocked_recovery_receipt_cannot_rescan(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    with service.state.change() as state:
        state["publication_reviews"][review["receipt"]]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()

    with pytest.raises(WisdomConflict, match="expired"):
        service.submit(review["receipt"])

    assert client.state.calls == Counter(upload=1, submit=1, approve=1)


def test_changed_blocked_remote_invalidates_recovery_receipt(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    client.state.drafts["draft-1"].updatedAt = "changed-after-review"

    with pytest.raises(WisdomConflict, match="changed"):
        service.submit(review["receipt"])

    assert client.state.calls == Counter(upload=1, submit=1, approve=1)


def test_definite_revision_conflict_does_not_become_unknown(tmp_path: Path):
    service, client, _binding, operation_id = blocked_publication(tmp_path)
    review = service.review(operation_id)
    client.state.revise_error = WisdomConflict("stale_revision", status=409, code="stale_revision")

    with pytest.raises(WisdomConflict, match="stale_revision"):
        service.submit(review["receipt"])

    assert service.state.publication(operation_id)["phase"] == "approval_rejected"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, revise=1)


def test_large_private_publication_stays_within_host_state_quota_and_rereviews_exactly(  # pylint: disable=too-many-locals
    tmp_path: Path,
):
    profile = tmp_path / "profile"
    source = make_skill(profile)
    for index in range(449):
        path = source / "references" / f"entry-{index:03d}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("x" * 10_000, encoding="utf-8")
    executable = source / "references" / "entry-000.txt"
    executable.chmod(0o700)
    binding = contribution_owner(profile)
    state_backend = QuotaJsonState(tmp_path / "state")
    state = DirectState(state_backend, tmp_path / "state")
    prepared_reviewer = ContributionReviewer(
        state,
        Host(profile / "skills"),
        lambda: dict(binding),
        source_store=QualificationState(state_backend, tmp_path / "state"),
    )
    prepared = prepared_reviewer.prepare("reviewed-skill")
    client = RecordingPublicationClient(binding, mode="moderated")
    service = PublicationService(
        state, prepared_reviewer, client, lambda: dict(binding)
    )

    staged = service.submit_private_draft(
        service.review(prepared["draft_id"])["receipt"]
    )
    restarted = PublicationService(
        DirectState(state_backend, tmp_path / "state"),
        prepared_reviewer,
        client,
        lambda: dict(binding),
    )
    rereview = restarted.review(staged["operation_id"])

    assert len(prepared["files"]) == 451
    assert rereview["files"] == prepared["files"]
    assert next(item for item in rereview["files"] if item["path"].endswith("000.txt"))[
        "mode"
    ] == "exec"
    assert state_backend.path.stat().st_size < state_backend.quota_bytes
    assert client.state.calls == Counter(upload=1, submit=1)


def test_interrupted_intent_keeps_its_exact_snapshot_when_draft_is_edited(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path, mode="moderated")
    client.state.upload_error = OSError("offline before upload")
    with pytest.raises(OSError, match="offline before upload"):
        service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    operation = service.state.publication_for_draft(prepared["draft_id"])
    assert operation is not None
    assert operation["phase"] == "intent"

    editable = service.reviewer.inspect_prepared(prepared["draft_id"])
    skill = next(item for item in editable["files"] if item["path"] == "SKILL.md")
    skill["content_utf8"] += "\n# Later edit\n"
    edited = service.reviewer.save_prepared(
        prepared["draft_id"],
        author_description=editable["author_description"],
        files=editable["files"],
        expected_hashes=editable["expected_hashes"],
    )
    assert edited["hashes"]["content"] != prepared["hashes"]["content"]

    client.state.upload_error = None
    restarted = PublicationService(
        service.state, service.reviewer, client, lambda: dict(binding)
    )
    resumed = restarted.reopen(operation["id"])
    rereview = restarted.review(resumed["operation_id"])

    assert resumed["phase"] == "policy_review_required"
    assert rereview["files"] == prepared["files"]
    assert rereview["hashes"] == prepared["hashes"]
    assert client.state.calls == Counter(upload=1, submit=1)

    snapshot_skill = Path(operation["prepared_package"]["overlay_path"]) / "SKILL.md"
    snapshot_skill.unlink()
    with pytest.raises(WisdomConflict, match="snapshot is missing or changed"):
        restarted.submit(rereview["receipt"])
    assert client.state.calls == Counter(upload=1, submit=1)


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


def test_published_result_links_exact_remote_identity_after_publication(tmp_path: Path):
    service, prepared, _client, _ = publication(tmp_path)
    linked = []

    class Enrollment:
        """Record enrollment input for exact-identity assertions."""

        def link(self, operation, result, *, may_apply):
            linked.append((operation, result, may_apply))
            return {
                "state": "managed",
                "skill_id": result["skill_id"],
                "version": result["version"],
            }

    service.enrollment = Enrollment()
    result = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert result["publication_state"] == "published"
    assert result["managed_enrollment"] == {
        "state": "managed", "skill_id": "published-skill-1", "version": 1,
    }
    assert len(linked) == 1
    operation, published, currentness = linked[0]
    assert operation["id"] == result["operation_id"]
    assert published == {
        "state": "published", "proposal_id": 11,
        "skill_id": "published-skill-1", "version": 1,
    }
    assert currentness is None


@pytest.mark.parametrize(("skill_name", "argument"), [
    ("reviewed-skill", "reviewed-skill"),
    ("reviewed skill; inert", "'reviewed skill; inert'"),
])
def test_publication_success_reports_local_link_failure_without_leaking_exception(
    tmp_path: Path, skill_name: str, argument: str,
):
    prepared_reviewer, source, binding = reviewer(tmp_path)
    source.rename(source.with_name(skill_name))
    prepared = prepared_reviewer.prepare(skill_name)
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, RecordingPublicationClient(binding),
        lambda: dict(binding),
    )

    class Enrollment:
        """Simulate a local enrollment failure after remote publication."""

        def link(self, *_args, **_kwargs):
            raise RuntimeError("secret transport detail")

    service.enrollment = Enrollment()
    result = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert result["publication_state"] == "published"
    assert result["managed_enrollment"] == {
        "state": "not_linked",
        "detail": (
            "Published successfully, but the original source is not linked to this version "
            f"locally. Recover with: hermes wisdom publication enroll {argument}"
        ),
    }
    assert "secret" not in str(result)
    assert result["detail"] == result["managed_enrollment"]["detail"]
    assert service.status(result["operation_id"])["detail"] == result["detail"]


def test_moderation_completion_enrolls_only_after_published_reconciliation(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path, mode="moderated")
    calls = []

    class Enrollment:
        """Reject early links and record reconciliation enrollment."""

        def link(self, *_args, **_kwargs):
            raise AssertionError("pending moderation must not enroll")

        def link_existing(self, operation, *, may_apply):
            calls.append((operation["id"], may_apply))
            return {
                "state": "managed", "skill_id": "published-skill-1", "version": 1,
            }

    service.enrollment = Enrollment()
    pending = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert pending["publication_state"] == "pending_moderation"
    assert "managed_enrollment" not in pending
    client.state.drafts["draft-1"].state = "published"

    completed = service.reopen(pending["operation_id"])

    assert completed["publication_state"] == "published"
    assert completed["managed_enrollment"]["state"] == "managed"
    assert calls == [(pending["operation_id"], None)]
    assert client.state.calls["publish"] == 1


def test_replayed_publication_receipt_cannot_duplicate_remote_effects(tmp_path: Path):
    """P06/X05: a consumed confirmation is not a second publication authority."""
    service, prepared, client, _ = publication(tmp_path)
    receipt = service.review(prepared["draft_id"])["receipt"]
    first = service.submit(receipt)
    calls_after_first = client.state.calls.copy()

    with pytest.raises(WisdomConflict, match="missing or already used"):
        service.submit(receipt)

    assert service.status(first["operation_id"])["state"] == "done"
    assert client.state.calls == calls_after_first


def test_concurrent_receipt_consumption_returns_bounded_conflict(tmp_path: Path):
    """P06/X05: an intervening exact submit cannot turn stale authority into a traceback."""
    service, prepared, client, binding = publication(tmp_path)
    receipt = service.review(prepared["draft_id"])["receipt"]
    racer = PublicationService(
        service.state, service.reviewer, client, lambda: dict(binding)
    )
    authorize = service._authorized  # pylint: disable=protected-access
    winner = {}

    def consume_after_authorization(value, may_apply):
        authorized = authorize(value, may_apply)
        winner["result"] = racer.submit(receipt)
        return authorized

    service._authorized = consume_after_authorization  # type: ignore[method-assign]  # pylint: disable=protected-access

    with pytest.raises(WisdomConflict, match="missing or already used"):
        service.submit(receipt)

    assert service.status(winner["result"]["operation_id"])["state"] == "done"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


def test_synthetic_doc_credentials_reach_exact_publication_after_warning_review(tmp_path: Path):
    prepared_reviewer, source, binding = reviewer(tmp_path, allowed=False)
    key = "sk-" + "ant-api03-testkey1234"
    source_file = source / "SKILL.md"
    original = source_file.read_text() + f'\nExample: api_key="{key}"\n'
    source_file.write_text(original)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    client = RecordingPublicationClient(binding)
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(binding)
    )

    assert prepared["security_check"]["local_status"] == "warning_required"
    with pytest.raises(WisdomConflict, match="explicit acknowledgement"):
        service.review(prepared["draft_id"])
    review = service.review(prepared["draft_id"], accept_warnings=True)
    assert review["warning_acceptance"]["content_hash"] == prepared["hashes"]["content"]
    assert review["local_scan"] == prepared["local_scan"]
    assert not client.state.calls
    assert key in next(f["content_utf8"] for f in review["files"] if f["path"] == "SKILL.md")
    assert source_file.read_text() == original

    result = service.submit(review["receipt"])
    assert result["publication_state"] == "published"
    assert result["submitted_request"]["content_hash"] == prepared["hashes"]["content"]
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


@pytest.mark.parametrize("label", ["api_key", "password"])
def test_warning_acceptance_cannot_bypass_real_or_user_chosen_credentials(tmp_path: Path, label):
    prepared_reviewer, source, binding = reviewer(tmp_path, allowed=False)
    key = "sk-" + ("a" * 32 if label == "api_key" else "ant-api03-testkey1234")
    source_file = source / "SKILL.md"
    source_file.write_text(source_file.read_text() + f'\n{label}="{key}"\n')
    prepared = prepared_reviewer.prepare("reviewed-skill")
    client = RecordingPublicationClient(binding)
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(binding)
    )

    assert prepared["security_check"]["local_status"] == "blocked"
    with pytest.raises(WisdomConflict, match="must be removed"):
        service.review(prepared["draft_id"], accept_warnings=True)
    assert not client.state.calls


def test_private_draft_requires_fresh_confirmation(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path, mode="moderated")

    staged = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    assert staged["phase"] == "policy_review_required"
    assert client.state.calls == Counter(upload=1, submit=1)

    final = service.submit(service.review(staged["operation_id"])["receipt"])
    assert final["publication_state"] == "pending_moderation"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


@pytest.mark.parametrize("mode", ["open", "moderated"])
@pytest.mark.parametrize("changed", ["owner", "policy", "content", "expiry"])
def test_private_draft_upload_rechecks_exact_review_before_transport(
    tmp_path: Path, mode, changed,
):
    service, prepared, client, binding = publication(tmp_path, mode=mode)
    review = service.review(prepared["draft_id"])
    if changed == "owner":
        binding["account_id"] = "other-member"
    elif changed == "policy":
        client.state.policy_version += 1
    elif changed == "content":
        draft = service.state.draft(prepared["draft_id"])
        assert draft is not None
        (Path(draft["source_path"]) / "SKILL.md").write_text("changed", encoding="utf-8")
    else:
        with service.state.change() as state:
            state["publication_reviews"][review["receipt"]]["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
    with pytest.raises((WisdomAuthError, WisdomConflict)):
        service.submit_private_draft(review["receipt"])
    assert not client.state.calls

def test_private_draft_preserves_exact_warning_acceptance_for_final_review(tmp_path: Path):
    service, prepared, _client, _ = publication(
        tmp_path, mode="moderated", scan_allowed=False
    )
    initial = service.review(prepared["draft_id"], accept_warnings=True)

    staged = service.submit_private_draft(initial["receipt"])
    operation = service.state.publication(staged["operation_id"])
    assert operation is not None
    assert operation["warning_acceptance"] == initial["warning_acceptance"]

    final = service.review(staged["operation_id"])
    assert final["warning_acceptance"] == initial["warning_acceptance"]

@pytest.mark.parametrize("mode", ["open", "moderated"])
def test_private_draft_uncertain_submission_reopens_without_publication(tmp_path: Path, mode):
    service, prepared, client, _ = publication(tmp_path, mode=mode)
    client.state.fail_after_effect.add("submit")
    uncertain = service.submit_private_draft(service.review(prepared["draft_id"])["receipt"])
    assert uncertain["state"] == "uncertain" and uncertain["retry_safe"] is False
    operation = service.state.publication(uncertain["operation_id"])
    assert operation is not None and operation["requested_action"] == "private_draft"
    client.state.fail_after_effect.clear()
    recovered = service.reopen(uncertain["operation_id"])
    assert recovered["phase"] == "policy_review_required"
    assert recovered["state"] == "pending"
    assert client.state.calls == Counter(upload=1, submit=1)


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


def test_definitive_submit_422_is_terminal_and_allows_fresh_review(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    service.enrollment = UnexpectedEnrollment()
    client.state.submit_error = WisdomValidationError(
        "raw response detail must not be shown", status=422,
        code="instruction_only_violation",
    )

    receipt = service.review(prepared["draft_id"])["receipt"]
    rejected = service.submit(receipt)

    assert rejected == {
        "operation_id": rejected["operation_id"],
        "state": "rejected",
        "phase": "rejected",
        "detail": "Gateway rejected the publication draft before creation (HTTP 422).",
        "code": "instruction_only_violation",
        "retry_safe": False,
    }
    assert service.status(rejected["operation_id"]) == rejected
    assert "managed_enrollment" not in rejected
    assert client.state.calls == Counter(upload=1, submit=1)
    with pytest.raises(WisdomConflict, match="missing or already used"):
        service.submit(receipt)

    client.state.submit_error = None
    completed = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert completed["publication_state"] == "published"
    assert client.state.calls == Counter(upload=2, submit=2, approve=1, head=1, publish=1)


def test_statusless_submit_validation_error_remains_uncertain(tmp_path: Path):
    service, prepared, client, _ = publication(tmp_path)
    client.state.submit_error = WisdomValidationError(
        "Gateway acknowledgement failed schema validation"
    )

    uncertain = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert uncertain["state"] == "uncertain"
    assert uncertain["phase"] == "draft_submission_unknown"
    assert uncertain["retry_safe"] is False
    assert service.status(uncertain["operation_id"])["state"] == "uncertain"
    assert client.state.calls == Counter(upload=1, submit=1)


def test_publish_ack_loss_reconciles_remote_without_republish(tmp_path: Path):
    service, prepared, client, binding = publication(tmp_path)
    service.enrollment = UnexpectedEnrollment()
    client.state.fail_after_effect.add("publish")

    uncertain = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert uncertain["phase"] == "publication_unknown"
    assert "managed_enrollment" not in uncertain
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


def test_publication_commit_identity_survives_clock_ticks(tmp_path, monkeypatch):
    """Rechecking unchanged bytes must not manufacture another commit identity."""
    from hermes_wisdom_plugin import wire  # pylint: disable=import-outside-toplevel

    ticks = iter(range(10))
    clock = SimpleNamespace(
        now=lambda _tz: datetime(2026, 1, 1, tzinfo=timezone.utc)
        + timedelta(seconds=next(ticks))
    )
    monkeypatch.setattr(wire, "datetime", clock)
    service, prepared, client, _ = publication(tmp_path)

    result = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert result["publication_state"] == "published"
    assert client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)
