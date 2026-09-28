"""Combined main/PR21 native reconciliation regressions."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access

from dataclasses import replace
import json

import pytest

from installed_test_support import JsonState
from native_test_support import direct_owner
from test_commands import _NativeSuggestService, _native_invocation
from test_restored_native_flow import Service as SharingService
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeReviewController, NativeView, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.review_state import ReviewState


class ReadyService(SharingService):
    def suggest(self, _name):
        return {k: v for k, v in self.publication_review("local:draft-1").items()
                if k != "receipt"}

    def prepared_review(self, draft_id, *, allow_model_review=False):
        assert not allow_model_review and draft_id == "local:draft-1"
        return self.suggest("incident")

    def publication_submit_private(self, receipt, *, may_apply):
        assert may_apply() and receipt == "publication-receipt"
        self.applied.append("private")
        return {"state": "pending", "phase": "policy_review_required",
                "remote_draft_id": "remote-1", "portal_url": "https://portal.example/draft"}

@pytest.mark.parametrize("action", ["confirm", "portal.draft"])
@pytest.mark.parametrize("changed", ["actor", "message", "owner", "expiry", "stale"])
def test_ready_share_and_private_upload_refuse_changed_authority(tmp_path, action, changed):
    service = ReadyService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    if changed == "actor":
        clicked = replace(clicked, actor_id="other")
    elif changed == "message":
        clicked = replace(clicked, message_id="other")
    elif changed == "owner":
        service.native_owner = lambda: {**direct_owner(), "account_id": "other"}
    elif changed == "expiry":
        controller.state.save({**controller.load(record["id"]), "expires_at": 0})
    else:
        controller.state.save({**controller.load(record["id"]), "state": "applying"})
    with pytest.raises((WisdomAuthError, WisdomConflict, WisdomValidationError)):
        controller.act(record["id"], action, service, clicked)
    assert not service.applied

def test_large_proposal_never_exposes_effect_controls(tmp_path):
    service = ReadyService()
    original = service.publication_review
    service.publication_review = lambda draft_id, **kw: {
        **original(draft_id, **kw), "editorial_description": "<&" * 2000,
    }
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    assert not any(a.primary or a.action == "portal.draft" for a in controller.view(record).actions)
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "confirm", service, clicked)
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "portal.draft", service, clicked)
    assert not service.applied


@pytest.mark.parametrize("identity", [
    {}, {"draft_id": "local:other"},
    {"draft_id": "local:draft-1", "local_draft_id": "local:other"},
], ids=["missing", "swapped", "conflicting"])
def test_refresh_refuses_unbound_prepared_identity(tmp_path, identity):
    service = ReadyService()
    service.suggest = lambda _name: {
        **ReadyService.suggest(service, "incident"), "editorial_status": "incomplete",
    }
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    controller.bind_message(record["id"], caller, "card-1")
    before = controller.load(record["id"])

    def wrong_review(draft_id, *, allow_model_review=False):
        prepared = ReadyService.prepared_review(service, draft_id,
                                                allow_model_review=allow_model_review)
        prepared.pop("draft_id", None)
        prepared.pop("local_draft_id", None)
        return {**prepared, **identity}

    service.prepared_review = wrong_review
    with pytest.raises(WisdomConflict, match="prepared draft changed"):
        controller.act(record["id"], "suggest.refresh", service,
                       replace(caller, message_id="card-1"))
    assert controller.load(record["id"]) == before
    assert not service.applied


@pytest.mark.parametrize("changed", ["missing", "swapped", "conflicting", "owner"])
def test_publication_review_refuses_draft_or_owner_change(tmp_path, changed):
    service = ReadyService()
    original = service.publication_review
    service.suggest = lambda _name: {k: v for k, v in original("local:draft-1").items()
                                     if k != "receipt"}
    service.prepared_review = lambda draft_id, *, allow_model_review=False: {
        k: v for k, v in original(draft_id).items() if k != "receipt"
    }

    def altered(draft_id, *, accept_warnings=False):
        result = original(draft_id, accept_warnings=accept_warnings)
        if changed == "owner":
            result["owner"] = {**direct_owner(), "account_id": "other"}
        else:
            result.pop("draft_id")
            if changed == "swapped":
                result["draft_id"] = "local:other"
            elif changed == "conflicting":
                result.update(draft_id=draft_id, local_draft_id="local:other")
        return result

    service.publication_review = altered
    controller = _controller(tmp_path)
    with pytest.raises(WisdomConflict, match="changed"):
        controller.request_suggest(service, _native_invocation(), "incident")
    assert not service.applied


def test_incomplete_manual_editorial_cannot_upload_until_fresh_review(tmp_path, monkeypatch):
    service = ReadyService()
    original = service.suggest
    monkeypatch.setattr(service, "suggest", lambda name: {
        **original(name), "editorial_status": "incomplete",
        "editorial_failure": "model_unavailable",
    })
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    assert "model_unavailable" in controller.view(record).to_text()
    assert not any(a.primary or a.action == "portal.draft" for a in controller.view(record).actions)
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "confirm", service, clicked)
    monkeypatch.setattr(service, "suggest", original)
    refreshed = controller.act(record["id"], "suggest.refresh", service, clicked)
    assert refreshed["operation"] == "publish"
    assert not service.applied


@pytest.mark.parametrize("evidence", ["missing", "oversized"])
def test_warning_without_displayable_evidence_refuses_ack_and_upload(tmp_path, evidence):
    service = ReadyService()
    original = service.publication_review

    def warning(draft_id, *, accept_warnings=False):
        result = original(draft_id, accept_warnings=accept_warnings)
        result["local_scan"] = {"available": True, "allowed": False,
                                "verdict": "warning", "summary": "Review required",
                                "findings": ([] if evidence == "missing" else [{
                                    "severity": "warning", "file": "&" * 500,
                                    "description": "&" * 1000,
                                }])}
        result["security_check"]["local_status"] = "warning_required"
        return result

    service.publication_review = warning
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    controller.bind_message(record["id"], caller, "card-1")
    before = controller.load(record["id"])
    actions = {a.action for a in controller.view(before).actions}
    assert "suggest.ack" not in actions
    assert not {"confirm", "portal.draft"} & actions
    for action in ("suggest.ack", "confirm", "portal.draft"):
        with pytest.raises(WisdomValidationError, match="not offered"):
            controller.act(record["id"], action, service,
                           replace(caller, message_id="card-1"))
    assert controller.load(record["id"]) == before
    assert not service.applied


class CandidateWarningService(_NativeSuggestService):
    def native_team_name(self) -> str | None:
        return None

    def prepare_candidate_offer(self, event_id):
        return self.native_candidate_inspect(event_id)

    def native_candidate_inspect(self, event_id):
        return {"event_id": event_id, "facts": {"name": "reviewed-skill",
                                              "editorial_status": "complete"}}

    def native_candidate_prepare(self, event_id):
        assert event_id == "candidate-1"
        return "local:draft-1"

    def prepared_review(self, draft_id, *, allow_model_review=False):
        return {**super().prepared_review(draft_id, allow_model_review=allow_model_review),
                "editorial_status": "complete"}


def _controller(tmp_path):
    return NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))


def _display(controller, record):
    with controller.displaying(record):
        pass
    return controller.load(record["id"])


def _act_and_display(controller, record_id, action, service, caller):
    return _display(controller, controller.act(record_id, action, service, caller))

def test_optional_details_navigation_uses_full_record_cas_and_display_fence(tmp_path, monkeypatch):
    service = ReadyService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    stale = controller.load(record["id"])
    newer = {**stale, "plan": {**stale["plan"], "security_check": {"status": "advisory"}}}
    controller.state.save(newer)
    with monkeypatch.context() as patcher:
        patcher.setattr(controller, "load", lambda _record_id: stale)
        with pytest.raises(WisdomConflict):
            controller.act(record["id"], "details", service, clicked)
    assert controller.state.get(record["id"]) == newer
    details = controller.act(record["id"], "details", service, clicked)
    assert details["display_ready"] is False
    with pytest.raises(WisdomConflict, match="finish displaying"):
        controller.act(record["id"], "back", service, clicked)
    displayed = _display(controller, details)
    back = controller.act(record["id"], "back", service, clicked)
    assert displayed["display_ready"] is True
    assert back["display_ready"] is False
    assert not service.applied


def test_optional_file_inspection_preserves_long_controls_and_opaque_bytes(tmp_path):
    service = ReadyService()
    original = service.publication_review
    files = [
        {"path": "references/large.md", "content_utf8": "START" + "&" * 6000 + "TAIL"},
        {"path": "controls.txt", "content_utf8": "before\x00\x1b[2Jafter"},
        {"path": "opaque.bin", "content_base64": "AAH/", "size": 3},
    ]
    service.publication_review = lambda draft_id, **kw: {
        **original(draft_id, **kw), "files": files,
    }
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "incident")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    current = _display(controller, controller.act(record["id"], "details", service, clicked))
    fragments = [controller.view(current).items[0].detail]
    while any(a.action and a.action.startswith("page.") and a.label == "Next"
              for a in controller.view(current).actions):
        action = next(a.action for a in controller.view(current).actions
                      if a.label == "Next")
        assert action is not None
        current = _display(controller, controller.act(record["id"], action, service, clicked))
        fragments.append(controller.view(current).items[0].detail)
    text = "".join(fragments)
    for file in files:
        key = "content_utf8" if "content_utf8" in file else "content_base64"
        assert json.dumps(file[key], ensure_ascii=True) in text
        assert file["path"] in text
    assert "\x00" not in text and "\x1b" not in text
    assert not service.applied


def test_candidate_owner_change_during_local_preparation_never_posts_ready_card(tmp_path):
    service = ReadyService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    initial_owner = service.native_owner()
    original = service.native_candidate_prepare

    def change_owner(identity):
        draft = original(identity)
        service.native_owner = lambda: {**initial_owner, "account_id": "other"}
        return draft

    service.native_candidate_prepare = change_owner
    with pytest.raises(WisdomAuthError, match="owner changed"):
        controller.request_candidate(service, caller, "candidate-1")
    rows = controller.state.pending(initial_owner)
    assert not rows
    assert not service.applied


def test_candidate_warnings_use_bound_suggest_ack_before_exact_approval(tmp_path):
    service = CandidateWarningService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_candidate(service, caller, "candidate-1")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    warnings = controller.load(record["id"])

    assert warnings["operation"] == "suggest"
    assert warnings["plan"]["draft_id"] == "local:draft-1"
    assert warnings["plan"]["native_stage"] == "warnings"
    assert warnings["plan"]["local_scan"]["guard"]["findings"][0]["pattern_id"] == "shell-download"
    assert not any(call[0] == "publication_review" for call in service.calls)

    original = service.prepared_review

    def changed_scanner(*args, **kwargs):
        prepared = original(*args, **kwargs)
        prepared["local_scan"]["guard"]["findings"][0]["pattern_id"] = "changed"
        return prepared

    service.prepared_review = changed_scanner
    with pytest.raises(WisdomConflict, match="scanner findings changed"):
        controller.act(record["id"], "suggest.ack", service, clicked)
    assert not any(call[0] == "publication_submit" for call in service.calls)


def test_candidate_warning_ack_leads_to_readable_proposal(tmp_path):
    service = CandidateWarningService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_candidate(service, caller, "candidate-1")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    warnings = controller.load(record["id"])
    assert warnings["plan"]["candidate_flow"] is True
    proposal = _act_and_display(controller, record["id"], "suggest.ack", service, clicked)
    assert proposal["operation"] == "publish"
    assert proposal["plan"]["candidate_flow"] is True
    assert project_view(proposal).title == "Ready to share"

def test_existing_candidate_enriches_display_on_authenticated_navigation(tmp_path):
    service = CandidateWarningService()
    service.native_team_name = lambda: "Wisdom Staging Test"
    service.prepare_candidate_offer = lambda event_id: {
        "event_id": event_id, "facts": {"name": "reviewed-skill", "editorial_status": "incomplete"},
    }
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_candidate(service, caller, "candidate-1")
    record = controller.bind_message(record["id"], caller, "card-1")
    old = {key: value for key, value in record.items() if key != "team_name"}
    controller.state.save(old)
    clicked = replace(caller, message_id="card-1")
    navigated = controller.act(record["id"], "details", service, clicked)
    assert navigated["team_name"] == "Wisdom Staging Test"
    assert navigated["plan"] == old["plan"]
    assert "Destination team: Wisdom Staging Test" in controller.view(navigated).to_text()
    assert controller.state.get(record["id"])["team_name"] == "Wisdom Staging Test"

def test_team_enrichment_rejects_stale_record_after_network_lookup(tmp_path):
    service = CandidateWarningService()
    service.prepare_candidate_offer = lambda event_id: {
        "event_id": event_id, "facts": {"name": "reviewed-skill", "editorial_status": "incomplete"},
    }
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_candidate(service, caller, "candidate-1")
    record = controller.bind_message(record["id"], caller, "card-1")
    old = {key: value for key, value in record.items() if key != "team_name"}
    controller.state.save(old)
    def changed():
        controller.state.save({**old, "state": "cancelled"})
        return "Wrong team"
    service.native_team_name = changed
    with pytest.raises(WisdomConflict):
        controller.act(record["id"], "details", service, replace(caller, message_id="card-1"))
    assert controller.state.get(record["id"])["state"] == "cancelled"

def test_literal_slack_projection_is_plain_and_never_keeps_primary_on_truncation():
    literal = NativeItem("Exact JSON", '{"value":"<&*"}', literal=True)
    view = NativeView(
        "Exact review", "x" * 3000, [literal],
        actions=[NativeAction("Confirm", "confirm", primary=True)],
    )

    rendered = blocks(view, "review-1")
    sections = [block["text"] for block in rendered if block["type"] == "section"]

    assert sections[-1] == {"type": "plain_text", "text": 'Exact JSON\n{"value":"<&*"}'}
    assert "wi:agent:confirm:review-1" not in str(rendered)


def test_exact_inspection_projects_persisted_owner_not_plan_owner(tmp_path):
    del tmp_path
    record = {
        "state": "pending", "operation": "publish", "screen": "approval",
        "page": 0, "inspected_through": 0, "expires_at": 123.0,
        "owner": direct_owner(),
        "plan": {
            "draft_id": "local:draft-1", "receipt": "receipt-1", "allowed": True,
            "owner": {**direct_owner(), "account_id": "other"},
        },
    }

    text = project_view(record).to_text()
    assert "Destination team: Team name unavailable" in text
    assert "account-1" not in text
    assert '"draft_id"' not in text
    assert '"receipt"' not in text
    assert "Review expires: 01 Jan 1970" in text
    assert '"account_id": "other"' not in text
