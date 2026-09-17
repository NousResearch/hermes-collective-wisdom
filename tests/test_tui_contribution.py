"""Interactive TUI contribution editing and publication adapter tests."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
# Test doubles intentionally compose real kernel helpers from sibling test modules.
# pylint: disable=too-many-locals,wrong-import-order,ungrouped-imports

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.tui_contribution import (
    handles_plugin_only_candidate_action,
    handles_tui_contribution,
    run_tui_contribution,
)
from hermes_wisdom_plugin.client import WisdomGatewayCredentials
from test_publication import RecordingPublicationClient
from test_contribution_review import reviewer
from hermes_wisdom_plugin.publication import PublicationController
from hermes_wisdom_plugin.qualification import record_successful_use
from hermes_wisdom_plugin.runtime import InvocationService


OWNER = {
    "profile_name": "default",
    "profile_home": "/tmp/profile",
    "account_id": "member-1",
    "organization_id": "org-1",
    "base_url": "https://gateway.invalid",
    "installation_id": "installation-1",
}


class Outcome(Enum):
    SELECTED = "selected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class Action:
    def __init__(self, action_id: str, label: str) -> None:
        self.id = action_id
        self.label = label

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Action) and (self.id, self.label) == (other.id, other.label)


class FakeContributionService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.payload = {
            "draft_id": "draft-1",
            "drafted_description": "Exact private description.",
            "files": [
                {"path": "SKILL.md", "content_utf8": "# exact private body\n", "hash": "sha256:a"},
                {"path": "skill.manifest.json", "content_utf8": "{}", "hash": "sha256:b"},
            ],
            "hashes": {
                "content": "sha256:c",
                "author_description": "sha256:d",
                "package_manifest": "sha256:e",
            },
            "security_check": {"status": "pass", "details": [{"path": "SKILL.md"}]},
            "professionalism_check": {"status": "pass", "checks": [{"key": "spam"}]},
        }

    def native_candidate_prepare(self, event_id, *, may_apply):
        self.calls.append(("native_candidate_prepare", event_id, may_apply))
        return "draft-1"

    def candidate_slash_action(self, assessment_id, action, *, may_apply):
        self.calls.append(("candidate_slash_action", assessment_id, action, may_apply))
        if action == "share":
            return {
                "action": "share", "candidate_id": assessment_id,
                "review": {
                    **self.payload, "receipt": "publication-receipt",
                    "publication_mode": "moderated", "policy_version": 7,
                },
            }
        if action == "portal":
            return {
                "action": "portal", "candidate_id": assessment_id,
                "state": "pending", "publication_state": "ready",
                "portal_url": "https://portal.example/wisdom/review/remote-draft",
            }
        return {
            "action": "later", "candidate_id": assessment_id,
            "deferred": True, "not_now_suppression_days": 7,
        }

    def candidate_publication_approve(self, receipt, *, may_apply):
        self.calls.append(("candidate_publication_approve", receipt, may_apply))
        return {
            "operation_id": "operation-1", "state": "pending",
            "publication_state": "pending_moderation",
        }

    def suggest(self, skill_name, *, allow_model_review, may_apply):
        self.calls.append(("suggest", skill_name, allow_model_review, may_apply))
        return dict(self.payload)

    def prepared_inspect(self, draft_id, *, may_apply):
        self.calls.append(("prepared_inspect", draft_id, may_apply))
        return dict(self.payload)

    def prepared_save(
        self, draft_id, *, author_description, files, expected_hashes, may_apply
    ):
        self.calls.append((
            "prepared_save", draft_id, author_description, files, expected_hashes,
            may_apply,
        ))
        return {**self.payload, "drafted_description": author_description, "files": files}

    def prepared_review(
        self, draft_id, *, allow_model_review, reviewed_hashes, may_apply
    ):
        self.calls.append((
            "prepared_review", draft_id, allow_model_review, reviewed_hashes, may_apply,
        ))
        return dict(self.payload)

    def publication_review(self, draft_id, *, may_apply):
        self.calls.append(("publication_review", draft_id, may_apply))
        return {
            **self.payload,
            "receipt": "publication-receipt",
            "publication_mode": "moderated",
            "policy_version": 7,
            "next_state": "pending_moderation",
        }

    def publication_submit(self, receipt, *, may_apply):
        self.calls.append(("publication_submit", receipt, may_apply))
        return {
            "operation_id": "operation-1",
            "state": "pending",
            "publication_state": "pending_moderation",
        }

    def publication_status(self, operation_id, *, may_apply):
        self.calls.append(("publication_status", operation_id, may_apply))
        return {"operation_id": operation_id, "state": "pending"}

    def publication_reopen(self, operation_id, *, may_apply):
        self.calls.append(("publication_reopen", operation_id, may_apply))
        return {"operation_id": operation_id, "state": "done", "publication_state": "published"}

    def publication_decline(self, operation_id, *, may_apply):
        self.calls.append(("publication_decline", operation_id, may_apply))
        return {"operation_id": operation_id, "state": "done", "publication_state": "declined"}


def selected_request(captured, *, action="selected"):
    def request_review(*, title, body, actions):
        captured.append((title, body, tuple(actions)))
        if action == "selected":
            return SimpleNamespace(outcome=Outcome.SELECTED, action_id=actions[0].id)
        if action == "cancelled":
            return SimpleNamespace(outcome=Outcome.CANCELLED, action_id=None)
        return SimpleNamespace(outcome=Outcome.UNAVAILABLE, action_id=None)

    return request_review


def write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_router_claims_only_complete_contribution_journeys():
    assert handles_tui_contribution("candidate event-1")
    assert handles_tui_contribution("suggest reviewed-skill")
    assert handles_tui_contribution("prepared inspect draft-1")
    assert handles_tui_contribution("prepared save /tmp/edit.json")
    assert handles_tui_contribution("prepared review draft-1")
    assert handles_tui_contribution("publication review draft-1")
    assert handles_tui_contribution("publication status operation-1")
    assert handles_tui_contribution("publish draft-1")
    assert not handles_tui_contribution("install skill-1")
    assert not handles_tui_contribution("prepared submit forged")
    assert not handles_tui_contribution("publication submit forged")


def test_plugin_only_router_claims_only_explicit_candidate_actions_and_approval():
    assert handles_plugin_only_candidate_action("candidate assessment-1 share")
    assert handles_plugin_only_candidate_action("candidate assessment-1 portal")
    assert handles_plugin_only_candidate_action("candidate assessment-1 later")
    assert handles_plugin_only_candidate_action("publication approve receipt-1")
    assert not handles_plugin_only_candidate_action("candidate event-1")
    assert not handles_plugin_only_candidate_action("publish draft-1")
    assert not handles_plugin_only_candidate_action("publication review draft-1")


def test_candidate_share_only_displays_complete_exact_review_and_second_command():
    service = FakeContributionService()
    output = run_tui_contribution(
        "candidate assessment-1 share", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )

    review_text, instruction = output.split("\n\n", 1)
    result = json.loads(review_text)
    assert result["review"]["files"] == service.payload["files"]
    assert result["review"]["hashes"] == service.payload["hashes"]
    assert result["review"]["receipt"] == "publication-receipt"
    assert "/wisdom publication approve publication-receipt" in instruction
    assert [call[:3] for call in service.calls] == [
        ("candidate_slash_action", "assessment-1", "share"),
    ]
    assert not any(call[0] in {"publication_submit", "candidate_publication_approve"}
                   for call in service.calls)


def test_candidate_publication_approval_is_a_separate_exact_receipt_action():
    service = FakeContributionService()
    output = run_tui_contribution(
        "publication approve publication-receipt", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )
    assert json.loads(output)["publication_state"] == "pending_moderation"
    assert [call[:2] for call in service.calls] == [
        ("candidate_publication_approve", "publication-receipt"),
    ]


@pytest.mark.parametrize("action", ["portal", "later"])
def test_candidate_portal_and_later_are_explicit_without_host_review(action):
    service = FakeContributionService()
    output = run_tui_contribution(
        f"candidate assessment-1 {action}", service,
        lambda **_kwargs: pytest.fail("stock TUI action must not request host review"),
        None, None, may_apply=lambda: True,
    )
    result = json.loads(output)
    assert result["action"] == action
    if action == "portal":
        assert result["portal_url"].startswith("https://portal.example/")
        assert result["publication_state"] == "ready"
    else:
        assert result["deferred"] is True
        assert result["not_now_suppression_days"] == 7


@pytest.mark.parametrize("raw", [
    "candidate", "candidate assessment-1 forged",
    "candidate assessment-1 share extra", "publication approve",
    "publication approve receipt-1 extra",
])
def test_candidate_slash_action_malformed_syntax_is_rejected_without_effect(raw):
    service = FakeContributionService()
    with pytest.raises(ValueError, match="Usage:"):
        run_tui_contribution(
            raw, service, lambda **_kwargs: None, None, None,
            may_apply=lambda: True,
        )
    assert not service.calls


def test_qualified_candidate_exports_complete_private_edit_payload():
    service = FakeContributionService()
    output = run_tui_contribution(
        "candidate event-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    )
    value = json.loads(output.split("\n\n", 1)[0])

    assert [call[:2] for call in service.calls] == [
        ("native_candidate_prepare", "event-1"),
        ("prepared_inspect", "draft-1"),
    ]
    assert value["files"] == service.payload["files"]
    assert value["hashes"] == service.payload["hashes"]
    assert "hermes wisdom prepared save" in output
    assert "Nothing was uploaded" in output


def test_private_save_reviews_complete_result_and_honors_cancel(tmp_path: Path):
    service = FakeContributionService()
    payload_path = tmp_path / "edit.json"
    payload = {
        "draft_id": "draft-1",
        "author_description": "Owner edited description.",
        "files": [
            {"path": "SKILL.md", "content_utf8": "edited\n"},
            {"path": "renamed.md", "content_utf8": "renamed\n"},
        ],
        "expected_hashes": dict(service.payload["hashes"]),
    }
    write_payload(payload_path, payload)
    captured = []

    output = run_tui_contribution(
        f"prepared save {payload_path}", service,
        selected_request(captured, action="cancelled"), Action, Outcome,
        may_apply=lambda: True,
    )

    assert "Nothing was changed" in output
    assert [call[0] for call in service.calls] == ["prepared_inspect"]
    title, body, actions = captured[0]
    assert title == "Save exact private Wisdom draft"
    assert json.loads(body)["resulting_payload"] == payload
    assert actions == (Action("save", "Save exact private draft"),)


def test_private_save_rechecks_invocation_after_selection(tmp_path: Path):
    service = FakeContributionService()
    payload_path = tmp_path / "edit.json"
    payload = {
        "draft_id": "draft-1",
        "author_description": "Owner edited description.",
        "files": [{"path": "SKILL.md", "content_utf8": "edited\n"}],
        "expected_hashes": dict(service.payload["hashes"]),
    }
    write_payload(payload_path, payload)

    output = run_tui_contribution(
        f"prepared save {payload_path}", service,
        selected_request([]), Action, Outcome, may_apply=lambda: False,
    )

    assert "unavailable" in output
    assert "inspect private state before retrying" in output
    assert not service.calls


def test_model_review_requires_its_own_explicit_private_disclosure():
    service = FakeContributionService()
    captured = []

    output = run_tui_contribution(
        "prepared review draft-1 --allow-model-review", service,
        selected_request(captured), Action, Outcome, may_apply=lambda: True,
    )

    assert [call[0] for call in service.calls] == [
        "prepared_inspect", "prepared_review",
    ]
    assert service.calls[0][:2] == ("prepared_inspect", "draft-1")
    assert service.calls[1][:3] == ("prepared_review", "draft-1", True)
    assert service.calls[1][3] == service.payload["hashes"]
    assert "current model provider" in captured[0][1]
    assert captured[0][2] == (Action("model-review", "Send exact private draft for model review"),)
    assert json.loads(output)["professionalism_check"]["status"] == "pass"


def test_one_tui_confirmation_submits_exact_policy_bound_publication():
    service = FakeContributionService()
    captured = []

    output = run_tui_contribution(
        "publish draft-1", service, selected_request(captured), Action, Outcome,
        may_apply=lambda: True,
    )

    assert [call[:2] for call in service.calls] == [
        ("publication_review", "draft-1"),
        ("publication_submit", "publication-receipt"),
    ]
    title, body, actions = captured[0]
    exact = json.loads(body)
    assert title == "Publish exact Collective Wisdom contribution"
    assert exact["publication_mode"] == "moderated"
    assert exact["files"] == service.payload["files"]
    assert exact["security_check"]["details"] == [{"path": "SKILL.md"}]
    assert actions == (Action("publish", "Submit exact reviewed contribution"),)
    assert json.loads(output)["publication_state"] == "pending_moderation"


def test_status_is_observational_and_reopen_decline_are_explicit():
    service = FakeContributionService()
    status = run_tui_contribution(
        "publication status operation-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    )
    assert json.loads(status)["state"] == "pending"

    captured = []
    reopened = run_tui_contribution(
        "publication reopen operation-1", service, selected_request(captured), Action, Outcome,
        may_apply=lambda: True,
    )
    assert json.loads(reopened)["publication_state"] == "published"
    assert captured[0][2] == (Action("reopen", "Reconcile exact publication"),)

    captured = []
    declined = run_tui_contribution(
        "publication decline operation-1", service, selected_request(captured), Action, Outcome,
        may_apply=lambda: True,
    )
    assert json.loads(declined)["publication_state"] == "declined"
    assert captured[0][2] == (Action("decline", "Decline exact remote draft"),)


def test_oversized_exact_review_fails_closed_to_terminal():
    service = FakeContributionService()
    service.payload["files"][0]["content_utf8"] = "x" * 70_000
    captured = []

    output = run_tui_contribution(
        "publication review draft-1", service, selected_request(captured), Action, Outcome,
        may_apply=lambda: True,
    )

    assert "cannot display the complete exact review" in output
    assert "terminal" in output.casefold()
    assert not captured
    assert [call[:2] for call in service.calls] == [("publication_review", "draft-1")]


def test_oversized_editable_payload_is_not_truncated_into_a_fake_editor():
    service = FakeContributionService()
    service.payload["files"][0]["content_utf8"] = "x" * 70_000

    output = run_tui_contribution(
        "candidate event-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    )

    assert "cannot display the complete editable draft without truncation" in output
    assert "prepared inspect draft-1 --json" in output
    assert "x" * 100 not in output


@pytest.mark.parametrize("unsafe_id", ["draft-\x1b[2J", "draft-\u202ejson"])
def test_oversized_draft_handoff_never_interpolates_display_controls(unsafe_id: str):
    service = FakeContributionService()
    service.payload["draft_id"] = unsafe_id
    service.payload["files"][0]["content_utf8"] = "x" * 70_000

    output = run_tui_contribution(
        "candidate event-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    )

    assert unsafe_id not in output
    assert "<draft-id>" in output


def test_entry_and_post_read_revocation_never_export_private_content():
    service = FakeContributionService()
    denied = run_tui_contribution(
        "candidate event-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: False,
    )
    assert "exact private body" not in denied
    assert not service.calls

    checks = iter((True, True, False))
    revoked = run_tui_contribution(
        "prepared inspect draft-1", service, selected_request([]), Action, Outcome,
        may_apply=lambda: next(checks),
    )
    assert "exact private body" not in revoked


def test_cancelled_model_disclosure_reports_prepared_private_draft():
    service = FakeContributionService()

    output = run_tui_contribution(
        "suggest reviewed-skill --allow-model-review", service,
        selected_request([], action="cancelled"), Action, Outcome,
        may_apply=lambda: True,
    )

    assert "private draft" in output
    assert "remains prepared" in output
    assert "Nothing was changed" not in output


class KernelService:
    """Thin adapter over the real contribution and publication kernels."""

    def __init__(self, prepared_reviewer, controller, *, candidate_service=None) -> None:
        self.reviewer = prepared_reviewer
        self.controller = controller
        self.candidate_service = candidate_service

    def native_candidate_prepare(self, event_id, *, may_apply):
        assert self.candidate_service is not None
        assert may_apply() is True
        result = self.candidate_service.native_candidate_prepare(event_id)
        assert may_apply() is True
        return result

    def suggest(self, skill_name, *, allow_model_review, may_apply):
        return self.reviewer.prepare(
            skill_name, allow_model_review=allow_model_review, may_apply=may_apply
        )

    def prepared_inspect(self, draft_id, *, may_apply):
        return self.reviewer.inspect_prepared(draft_id, may_apply=may_apply)

    def prepared_save(
        self, draft_id, *, author_description, files, expected_hashes, may_apply
    ):
        return self.reviewer.save_prepared(
            draft_id, author_description=author_description, files=files,
            expected_hashes=expected_hashes, may_apply=may_apply,
        )

    def publication_review(self, draft_id, *, may_apply):
        return self.controller.review(draft_id, may_apply=may_apply)

    def publication_submit(self, receipt, *, may_apply):
        return self.controller.submit(receipt, may_apply=may_apply)

    def publication_status(self, operation_id, *, may_apply):
        result = self.controller.status(operation_id)
        assert may_apply() is True
        return result


def test_real_tui_adapter_edit_add_remove_rename_then_publishes_exact_bytes(tmp_path: Path):
    prepared_reviewer, _source, binding = reviewer(tmp_path)
    client = RecordingPublicationClient(binding, mode="moderated")
    controller = PublicationController(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(binding),
    )
    service = KernelService(prepared_reviewer, controller)

    prepared = json.loads(run_tui_contribution(
        "suggest reviewed-skill", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    ).split("\n\n", 1)[0])
    baseline_files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ] + [
        {"path": "refs/old-name.md", "content_utf8": "rename me\n"},
        {"path": "refs/remove-me.md", "content_utf8": "remove me\n"},
    ]
    baseline = prepared_reviewer.save_prepared(
        prepared["draft_id"], author_description=prepared["drafted_description"],
        files=baseline_files, expected_hashes=prepared["hashes"],
    )
    manifest = json.loads(next(
        item["content_utf8"] for item in baseline["files"]
        if item["path"] == "skill.manifest.json"
    ))
    manifest["requirements"]["known_limitations"] = ["Reviewed limitation"]
    edited_files = []
    for item in baseline["files"]:
        if item["path"] in {"refs/old-name.md", "refs/remove-me.md"}:
            continue
        content = item["content_utf8"]
        if item["path"] == "SKILL.md":
            content = content.replace("# Incident review", "# Edited incident review")
        elif item["path"] == "skill.manifest.json":
            content = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        edited_files.append({"path": item["path"], "content_utf8": content})
    edited_files.extend([
        {"path": "refs/renamed.md", "content_utf8": "rename me\n"},
        {"path": "refs/added.md", "content_utf8": "new exact bytes\n"},
    ])
    payload_path = tmp_path / "complete-edit.json"
    write_payload(payload_path, {
        "draft_id": prepared["draft_id"],
        "author_description": "Owner edited exact package.",
        "files": edited_files,
        "expected_hashes": baseline["hashes"],
    })

    saved = json.loads(run_tui_contribution(
        f"prepared save {payload_path}", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    ))
    assert {item["path"] for item in saved["files"]} == {
        "SKILL.md", "skill.manifest.json", "refs/renamed.md",
        "refs/added.md",
    }

    result = json.loads(run_tui_contribution(
        f"publication review {prepared['draft_id']}", service,
        selected_request([]), Action, Outcome, may_apply=lambda: True,
    ))
    assert result["publication_state"] == "pending_moderation"
    assert controller.status(result["operation_id"])["state"] == "done"
    uploaded = {
        body for kind, body in client.state.uploaded_objects.values() if kind == "blob"
    }
    assert b"new exact bytes\n" in uploaded
    assert b"rename me\n" in uploaded


def test_real_tui_adapter_dirty_hash_rejects_stale_edit(tmp_path: Path):
    prepared_reviewer, _source, binding = reviewer(tmp_path)
    service = KernelService(
        prepared_reviewer,
        PublicationController(
            prepared_reviewer.store, prepared_reviewer,
            RecordingPublicationClient(binding), lambda: dict(binding),
        ),
    )
    prepared = prepared_reviewer.prepare("reviewed-skill")
    stale_path = tmp_path / "stale.json"
    write_payload(stale_path, {
        "draft_id": prepared["draft_id"],
        "author_description": prepared["drafted_description"],
        "files": [
            {"path": item["path"], "content_utf8": item["content_utf8"]}
            for item in prepared["files"]
        ],
        "expected_hashes": prepared["hashes"],
    })
    prepared_reviewer.save_prepared(
        prepared["draft_id"], author_description="Concurrent owner edit.",
        files=[
            {"path": item["path"], "content_utf8": item["content_utf8"]}
            for item in prepared["files"]
        ], expected_hashes=prepared["hashes"],
    )

    with pytest.raises(Exception, match="changed"):
        run_tui_contribution(
            f"prepared save {stale_path}", service, selected_request([]), Action, Outcome,
            may_apply=lambda: True,
        )


def test_real_usage_qualification_to_private_edit_and_exact_single_publication(
    tmp_path: Path,
):
    prepared_reviewer, source, binding = reviewer(tmp_path)
    store = prepared_reviewer.store
    store.activate_installation_identity(binding["installation_id"], binding["organization_id"])
    store.bind_qualification_owner(binding)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    event_id = None
    for offset in (0, 1, 2, 3, 4, 7, 8):
        event_id = record_successful_use(
            "reviewed-skill", store=store, skills_root=source.parent,
            skill_resolver=lambda _name: source, timezone_info=(timezone.utc, "UTC"),
            authorized=lambda: True, session_id="session-1", task_id="task-1",
            at=start + timedelta(days=offset),
        ) or event_id
    assert event_id
    assert store.local_event(event_id)["qualification"] == "high_usage"

    class CandidateRuntime(InvocationService):
        # This fixture supplies an isolated store instead of opening a real profile.
        def __init__(self):  # pylint: disable=super-init-not-called
            self._journey_store = store

        @property
        def store(self):
            return self._journey_store

        def _installation_owner(self):
            return dict(binding)

        def check_owner(self):
            return WisdomGatewayCredentials(
                base_url=binding["base_url"], access_token="test-token",
                organization_id=binding["organization_id"],
            )

        def suggest(self, skill_name, *, allow_model_review=False):
            return prepared_reviewer.prepare(
                skill_name, allow_model_review=allow_model_review
            )

    candidate_service = CandidateRuntime()
    client = RecordingPublicationClient(binding, mode="moderated")
    controller = PublicationController(store, prepared_reviewer, client, lambda: dict(binding))
    service = KernelService(
        prepared_reviewer, controller, candidate_service=candidate_service
    )

    prepared = json.loads(run_tui_contribution(
        f"candidate {event_id}", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    ).split("\n\n", 1)[0])
    edited_files = [
        {
            "path": item["path"],
            "content_utf8": (
                item["content_utf8"].replace("Use the checklist.", "Use the exact checklist.\n")
                if item["path"] == "SKILL.md" else item["content_utf8"]
            ),
        }
        for item in prepared["files"]
    ] + [{"path": "refs/qualified.md", "content_utf8": "qualified exact bytes\n"}]
    payload_path = tmp_path / "qualified-edit.json"
    write_payload(payload_path, {
        "draft_id": prepared["draft_id"],
        "author_description": "Owner-qualified exact package.",
        "files": edited_files,
        "expected_hashes": prepared["expected_hashes"],
    })
    saved = json.loads(run_tui_contribution(
        f"prepared save {payload_path}", service, selected_request([]), Action, Outcome,
        may_apply=lambda: True,
    ))
    result = json.loads(run_tui_contribution(
        f"publication review {prepared['draft_id']}", service,
        selected_request([]), Action, Outcome, may_apply=lambda: True,
    ))

    expected_blobs = {item["content_utf8"].encode("utf-8") for item in saved["files"]}
    uploaded_blobs = {
        body for kind, body in client.state.uploaded_objects.values() if kind == "blob"
    }
    assert result["publication_state"] == "pending_moderation"
    assert client.state.calls == {
        "upload": 1, "submit": 1, "approve": 1, "head": 1, "publish": 1,
    }
    assert uploaded_blobs == expected_blobs
