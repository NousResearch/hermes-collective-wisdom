"""Native cards use verified names, leaving opaque authority in controller state."""
# pylint: disable=missing-function-docstring,missing-class-docstring

from dataclasses import replace

import pytest

from installed_test_support import JsonState
from native_test_support import direct_owner
from hermes_wisdom_plugin.errors import WisdomValidationError
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController, project_view
from hermes_wisdom_plugin.review_state import ReviewState

from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import plain_html, rich_html


def _surfaces(record):
    view = project_view(record)
    return view, (view.to_text(), str(blocks(view, "card-1")),
                  plain_html(view), rich_html(view, "card-1"))


def test_install_and_update_cards_name_and_status_without_opaque_fields():
    for operation in ("install", "update"):
        view, surfaces = _surfaces({
            "operation": operation, "state": "pending",
            "plan": {"skill_id": "opaque-id", "slug": "incident-triage",
                     "version": 3, "receipt": "private-receipt",
                     "files": [{"path": "SKILL.md", "hash": "private-hash"}],
                     "installation_status": {"text": "Installed v2 · update to v3 available"}},
        })
        assert any(action.action == operation for action in view.actions)
        for text in surfaces:
            assert "incident-triage" in text
            assert "Installed v2" in text
            assert "Files: 1" in text
            assert "private-hash" not in text
            assert "opaque-id" not in text
            assert "private-receipt" not in text
            assert "prerequisites" not in text.lower()
            assert "declared setup commands" not in text.lower()
            assert "separate approval" not in text.lower()


def test_unknown_name_is_not_replaced_with_id_even_in_result():
    for state in ("pending", "uncertain", "completed"):
        _, surfaces = _surfaces({
            "operation": "install", "state": state,
            "plan": {"skill_id": "opaque-id", "slug": "opaque-id",
                     "receipt": "private-receipt", "version": 2},
            "result": {"installed": True, "skill_id": "opaque-id"},
        })
        for text in surfaces:
            assert "Name unavailable" in text
            assert "opaque-id" not in text
            assert "private-receipt" not in text


def test_sharing_continuations_use_local_name_not_draft_id():
    for state in ("pending", "kept", "uncertain", "completed"):
        _, surfaces = _surfaces({
            "operation": "suggest", "state": state,
            "plan": {"skill_name": "team-helper", "draft_id": "draft-opaque",
                     "native_stage": "prepared", "files": []},
        })
        for text in surfaces:
            assert "team-helper" in text
            assert "draft-opaque" not in text

    view = project_view({
        "operation": "suggest", "state": "completed",
        "plan": {"skill_name": "team-helper", "draft_id": "draft-opaque"},
        "result": {"publication_state": "pending_moderation"},
    })
    assert "Submitted for team review; not published yet" in view.to_text()
    assert "team-helper" in view.to_text()


def test_catalog_name_missing_is_honest_and_status_visible():
    _, surfaces = _surfaces({
        "operation": "catalog", "state": "pending",
        "plan": {"command": "browse", "data": [{"id": "opaque-id", "version": 2,
                  "installation_status": {"text": "Not installed"}}]},
    })
    for text in surfaces:
        assert "Name unavailable" in text
        assert "Not installed" in text
        assert "opaque-id" not in text


def test_catalog_show_uses_skill_envelope_not_unnamed_versions(tmp_path):
    class Service:
        def native_owner(self):
            return direct_owner()

        def show(self, _reference):
            return {
                "skill": {"id": "opaque-id", "slug": "team-helper", "state": "active"},
                "versions": [{"version": 1, "author_description": "Published helper"}],
                "latest_version_detail": {"version": {
                    "version": 1, "author_description": "Published helper",
                }},
                "installation_status": {"text": "Installed v1"},
            }

    caller = NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="user", actor_name="", thread_id="", scope_id="",
        message_id="command", session_key="private:C1", adapter=object(),
    )
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_catalog(Service(), caller, "show", "team-helper")
    text = controller.view(record).to_text()
    assert "Show: team-helper" in text
    assert "Published helper" in text
    assert "Installed v1" in text
    assert "Name unavailable" not in text
    assert "Installation status unavailable" not in text
    assert "opaque-id" not in text


def test_catalog_versions_preserves_multiline_description(tmp_path):
    class Service:
        def native_owner(self):
            return direct_owner()

        def versions(self, _reference):
            return {"versions": [{
                "version": 1,
                "author_description": "Disposable team helper.\n\nUsage notes for its publisher.",
                "installation_status": {"text": "Installed v1"},
            }]}

        def show(self, _reference):
            return {"skill": {"id": "opaque-id", "slug": "team-helper"}}

    caller = NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="user", actor_name="", thread_id="", scope_id="",
        message_id="command", session_key="private:C1", adapter=object(),
    )
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_catalog(Service(), caller, "versions", "team-helper")
    _, surfaces = _surfaces(record)
    for text in surfaces:
        assert "Versions: team-helper" in text
        assert "Disposable team helper." in text
        assert "Usage notes for its publisher." in text
        assert "Installed v1" in text
        assert "Unknown" not in text
        assert "opaque-id" not in text


def test_update_status_and_versions_use_read_only_service_facts(tmp_path):
    class Service:
        def native_owner(self):
            return direct_owner()

        def update_plan(self, skill_id):
            return {"operation": "update", "skill_id": skill_id, "slug": "team-helper",
                    "version": 3, "receipt": "private-receipt", "allowed": True}

        def list_installations(self):
            return [{"skill_id": "opaque-id", "version": 2, "state": "active"}]

        def versions(self, _reference):
            return [{"version": 3, "installation_status": {"text": "Installed v2"}}]

        def show(self, _reference):
            return {"skill": {"id": "opaque-id", "slug": "team-helper"}}

    caller = NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="user", actor_name="", thread_id="", scope_id="",
        message_id="command", session_key="private:C1", adapter=object(),
    )
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    service = Service()
    for record in (controller.request_update(service, caller, "opaque-id"),
                   controller.request_catalog(service, caller, "versions", "opaque-id")):
        text = controller.view(record).to_text()
        assert "team-helper" in text and "Installed v2" in text
        assert "opaque-id" not in text and "private-receipt" not in text


def test_kept_draft_resumes_only_from_the_bound_card(tmp_path):
    class Service:
        def native_owner(self):
            return direct_owner()

        def suggest(self, _name, *, allow_model_review):
            assert allow_model_review is False
            return {"draft_id": "draft-opaque", "files": []}

        def prepared_review(self, draft_id, *, allow_model_review):
            assert draft_id == "draft-opaque" and allow_model_review is False
            return {"draft_id": draft_id, "files": []}

    caller = NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="user", actor_name="", thread_id="", scope_id="",
        message_id="command", session_key="private:C1", adapter=object(),
    )
    service = Service()
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_suggest(service, caller, "team-helper")
    controller.bind_message(record["id"], caller, "card-1")
    bound = replace(caller, message_id="card-1")
    kept = controller.act(record["id"], "suggest.keep", service, bound)
    assert [action.action for action in controller.view(kept).actions] == ["suggest.resume"]
    with controller.displaying(kept):
        pass
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "suggest.confirm", service, bound)
    resumed = controller.act(record["id"], "suggest.resume", service, bound)
    assert resumed["state"] == "pending"
    assert resumed["plan"]["skill_name"] == "team-helper"
    assert "team-helper" in controller.view(resumed).to_text()
