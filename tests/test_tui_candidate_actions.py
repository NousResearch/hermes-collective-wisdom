"""Stock-TUI candidate actions over the shared mediation/publication kernels."""
# Test names document the security behavior more clearly than duplicate docstrings.
# pylint: disable=missing-function-docstring

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_test_support import native_invocation, owner, publication_review
from test_native_surfaces import _delivered_candidate

from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.qualification import snapshot_tree
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.tui_contribution import run_tui_contribution


class CandidateService(InvocationService):  # pylint: disable=too-many-instance-attributes
    """Bind the real candidate slash methods to one disposable owner/store."""

    def __init__(self, store, binding):  # pylint: disable=super-init-not-called
        self._candidate_store = store
        self._binding = dict(binding)
        self._closed = False
        self.client = SimpleNamespace(
            identity={"owner": binding["account_id"]},
            owner_user_id=binding["account_id"],
            display_org_id=binding["organization_id"],
            expected_organization_id=binding["organization_id"],
        )
        self.private_submissions = Mock()
        self.public_submissions = Mock()

    @property
    def store(self):
        return self._candidate_store

    def require_setup(self):
        return None

    def _installation_owner(self):
        return dict(self._binding)

    def native_owner(self):
        return dict(self._binding)

    def check_owner(self):
        return None

    def native_candidate_prepare(self, event_id, *, may_apply=None):
        assert may_apply is None or may_apply() is True
        inspected = self.native_candidate_inspect(event_id)
        event = self.store.local_event(event_id)
        local = self.store.local_skill(event["skill_id"])
        current_hash, _tree = snapshot_tree(Path(local["canonical_path"]))
        if current_hash != inspected["content_hash"]:
            raise WisdomConflict("candidate source changed")
        return "local-draft"

    def publication_review(self, draft_id, *, may_apply=None):
        assert may_apply is None or may_apply() is True
        return publication_review(draft_id)

    def publication_submit_private(
        self, receipt, *, may_apply=None, candidate_assessment_id=None,
    ):
        assert may_apply is None or may_apply() is True
        self.private_submissions(receipt, candidate_assessment_id)
        return {
            "operation_id": "private-operation", "state": "pending",
            "publication_state": "ready", "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft",
        }

    def publication_reopen_private(self, operation_id, *, may_apply=None):
        assert may_apply is None or may_apply() is True
        return {
            "operation_id": operation_id, "state": "pending",
            "publication_state": "ready", "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft",
        }

    def publication_submit(
        self, receipt, *, may_apply=None, candidate_assessment_id=None,
    ):
        assert may_apply is None or may_apply() is True
        self.public_submissions(receipt)
        return {
            "operation_id": "public-operation", "state": "pending",
            "publication_state": "pending_moderation",
        }


def delivered(tmp_path):
    binding = owner(profile_home=str(tmp_path / "profile"))
    route = native_invocation(thread_id="", message_id="card-1")
    # The shared fixture binds its module owner; use the same identity except for
    # the non-authoritative display path in this isolated service.
    store, _queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
    binding["profile_home"] = "/tmp/profile"
    return CandidateService(store, binding), assessment_id


def test_share_is_review_only_then_exact_approval_is_single_use(tmp_path):
    service, assessment_id = delivered(tmp_path)

    shared = service.candidate_slash_action(
        assessment_id, "share", may_apply=lambda: True
    )

    assert shared["review"]["receipt"] == "publication-receipt"
    assert shared["review"]["hashes"] == publication_review()["hashes"]
    service.public_submissions.assert_not_called()
    approved = service.candidate_publication_approve(
        "publication-receipt", may_apply=lambda: True
    )
    assert approved["publication_state"] == "pending_moderation"
    service.public_submissions.assert_called_once_with("publication-receipt")
    with pytest.raises(WisdomConflict, match="missing or already consumed"):
        service.candidate_publication_approve(
            "publication-receipt", may_apply=lambda: True
        )
    service.public_submissions.assert_called_once()


def test_completed_share_replay_is_unavailable_without_receipt(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    service.candidate_publication_approve(
        "publication-receipt", may_apply=lambda: True
    )

    output = run_tui_contribution(
        f"candidate {assessment_id} share", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )

    assert output == "This candidate action is no longer available. No action was taken."
    assert "publication-receipt" not in output
    assert "/wisdom publication approve" not in output
    service.private_submissions.assert_not_called()
    service.public_submissions.assert_called_once_with("publication-receipt")


def test_consumed_candidate_approval_has_safe_tui_message(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    service.candidate_publication_approve(
        "publication-receipt", may_apply=lambda: True
    )

    output = run_tui_contribution(
        "publication approve publication-receipt", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )

    assert output == (
        "This candidate publication approval is no longer available. "
        "No action was taken."
    )
    assert assessment_id not in output
    service.public_submissions.assert_called_once_with("publication-receipt")


def test_completed_share_portal_replay_has_no_private_side_effect(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    service.candidate_publication_approve(
        "publication-receipt", may_apply=lambda: True
    )

    with pytest.raises(
        WisdomConflict,
        match="Candidate action is already completed; Portal is unavailable",
    ):
        service.candidate_slash_action(
            assessment_id, "portal", may_apply=lambda: True
        )

    service.private_submissions.assert_not_called()
    service.public_submissions.assert_called_once_with("publication-receipt")


def test_exact_approval_rejects_candidate_source_changed_after_review(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    event = service.store.local_events(kind="wisdom.candidate")[0]
    local = service.store.local_skill(event["skill_id"])
    assert local is not None
    Path(local["canonical_path"]).joinpath("SKILL.md").write_text(
        "# Changed after review\n", encoding="utf-8"
    )

    with pytest.raises(PermissionError, match="assessment changed"):
        service.candidate_publication_approve(
            "publication-receipt", may_apply=lambda: True
        )
    service.public_submissions.assert_not_called()


def test_portal_creates_at_most_one_private_draft_and_never_publication(tmp_path):
    service, assessment_id = delivered(tmp_path)

    first = service.candidate_slash_action(
        assessment_id, "portal", may_apply=lambda: True
    )
    replay = service.candidate_slash_action(
        assessment_id, "portal", may_apply=lambda: True
    )

    assert first == replay
    assert first["portal_url"] == "https://portal.example/wisdom/review/remote-draft"
    assert first["publication_state"] == "ready"
    service.private_submissions.assert_called_once_with(
        "publication-receipt", assessment_id
    )
    service.public_submissions.assert_not_called()


def test_share_can_be_deliberately_upgraded_to_one_private_portal_draft(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)

    portal = service.candidate_slash_action(
        assessment_id, "portal", may_apply=lambda: True
    )
    replay = service.candidate_slash_action(
        assessment_id, "portal", may_apply=lambda: True
    )

    assert portal == replay
    service.private_submissions.assert_called_once()
    service.public_submissions.assert_not_called()


def test_share_portal_upgrade_recovers_after_private_submit_before_projection(
    tmp_path, monkeypatch,
):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    operation = {"id": "private-operation", "state": "pending"}
    submitted = []

    def submit_then_lose_projection(receipt, *, may_apply=None, candidate_assessment_id=None):
        assert may_apply is None or may_apply() is True
        submitted.append((receipt, candidate_assessment_id))
        if len(submitted) == 1:
            raise RuntimeError("injected after private submission")
        pytest.fail("private submission was replayed")

    service.publication_submit_private = submit_then_lose_projection
    monkeypatch.setattr(
        service.store,
        "publication_for_candidate_assessment",
        lambda target: operation if target == assessment_id and submitted else None,
    )

    with pytest.raises(RuntimeError, match="injected after private submission"):
        service.candidate_slash_action(assessment_id, "portal", may_apply=lambda: True)

    recovered = service.candidate_slash_action(
        assessment_id, "portal", may_apply=lambda: True
    )

    assert recovered["portal_url"] == "https://portal.example/wisdom/review/remote-draft"
    assert submitted == [("publication-receipt", assessment_id)]


def test_candidate_runtime_uses_only_public_controller_transitions():
    source = inspect.getsource(InvocationService)
    assert "controller._replace_candidate_action" not in source
    assert "controller._complete_candidate_portal" not in source
    assert callable(getattr(NativeReviewController, "upgrade_candidate_to_portal", None))


def test_share_after_portal_refuses_without_displaying_unusable_approval(tmp_path):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "portal", may_apply=lambda: True)

    output = run_tui_contribution(
        f"candidate {assessment_id} share", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )

    assert output == "This candidate action is no longer available. No action was taken."
    assert "/wisdom publication approve" not in output
    service.private_submissions.assert_called_once()
    service.public_submissions.assert_not_called()


def test_public_approval_recovers_after_submit_before_consent_completion(
    tmp_path, monkeypatch,
):
    service, assessment_id = delivered(tmp_path)
    service.candidate_slash_action(assessment_id, "share", may_apply=lambda: True)
    operation = {"id": "public-operation", "state": "done"}
    submissions = []

    def submit_then_lose_projection(receipt, *, may_apply=None,
                                    candidate_assessment_id=None):
        assert may_apply is None or may_apply() is True
        submissions.append((receipt, candidate_assessment_id))
        if len(submissions) == 1:
            raise RuntimeError("injected after public submission")
        pytest.fail("public submission was replayed")

    service.publication_submit = submit_then_lose_projection
    service.publication_reopen = Mock(return_value={
        "operation_id": "public-operation", "state": "pending",
        "publication_state": "pending_moderation",
    })
    monkeypatch.setattr(
        service.store,
        "publication_for_candidate_assessment",
        lambda target: operation if target == assessment_id and submissions else None,
    )

    with pytest.raises(RuntimeError, match="injected after public submission"):
        service.candidate_publication_approve(
            "publication-receipt", may_apply=lambda: True
        )

    recovered = service.candidate_publication_approve(
        "publication-receipt", may_apply=lambda: True
    )
    assert recovered["publication_state"] == "pending_moderation"
    assert submissions == [("publication-receipt", assessment_id)]
    assert service.publication_reopen.call_count == 1

    output = run_tui_contribution(
        f"candidate {assessment_id} share", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )
    assert output == "This candidate action is no longer available. No action was taken."
    assert "publication-receipt" not in output


def test_later_uses_bound_policy_and_is_idempotent(tmp_path):
    service, assessment_id = delivered(tmp_path)

    first = service.candidate_slash_action(
        assessment_id, "later", may_apply=lambda: True
    )
    replay = service.candidate_slash_action(
        assessment_id, "later", may_apply=lambda: True
    )

    assert first["not_now_suppression_days"] == 7
    assert first["already_deferred"] is False
    assert replay["already_deferred"] is True
    with service.store.transaction() as db:
        rows = db.execute(
            "SELECT suppress_until-created_at FROM wisdom_preference_outbox"
        ).fetchall()
    assert [row[0] for row in rows] == [7 * 86400]
    service.private_submissions.assert_not_called()
    service.public_submissions.assert_not_called()

    with pytest.raises(PermissionError, match="already consumed"):
        service.candidate_slash_action(
            assessment_id, "share", may_apply=lambda: True
        )


@pytest.mark.parametrize(
    "failure",
    ["missing", "foreign-account", "foreign-profile", "foreign-org", "stale"],
)
def test_candidate_actions_reject_undelivered_foreign_or_stale_targets(tmp_path, failure):
    service, assessment_id = delivered(tmp_path)
    target = assessment_id
    if failure == "missing":
        target = "not-delivered"
    elif failure == "foreign-account":
        service._binding["account_id"] = "other-member"  # pylint: disable=protected-access
    elif failure == "foreign-profile":
        service._binding["profile_name"] = "other-profile"  # pylint: disable=protected-access
    elif failure == "foreign-org":
        service._binding["organization_id"] = "other-org"  # pylint: disable=protected-access
    else:
        record = service.store.local_event(
            service.store.local_events(kind="wisdom.candidate")[0]["id"]
        )
        local = service.store.local_skill(record["skill_id"])
        Path(local["canonical_path"]).joinpath("SKILL.md").write_text(
            "# Changed\n", encoding="utf-8"
        )

    with pytest.raises((PermissionError, WisdomConflict)):
        service.candidate_slash_action(target, "share", may_apply=lambda: True)
    service.private_submissions.assert_not_called()
    service.public_submissions.assert_not_called()


def test_candidate_action_rechecks_invocation_before_mutation(tmp_path):
    service, assessment_id = delivered(tmp_path)
    current = [True]

    def revoke_during_prepare(event_id, *, may_apply=None):
        result = CandidateService.native_candidate_prepare(
            service, event_id, may_apply=may_apply
        )
        current[0] = False
        return result

    service.native_candidate_prepare = revoke_during_prepare
    with pytest.raises(PermissionError):
        service.candidate_slash_action(
            assessment_id, "portal", may_apply=lambda: current[0]
        )
    service.private_submissions.assert_not_called()
    service.public_submissions.assert_not_called()
