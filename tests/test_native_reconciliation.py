"""Combined main/PR21 native reconciliation regressions."""
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access

from dataclasses import replace

import pytest

from installed_test_support import JsonState
from native_test_support import direct_owner
from test_commands import _NativeSuggestService, _native_invocation
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from hermes_wisdom_plugin.native_review import (
    NativeAction, NativeItem, NativeReviewController, NativeView, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.review_state import ReviewState


class CandidateWarningService(_NativeSuggestService):
    def native_candidate_inspect(self, event_id):
        return {"event_id": event_id, "facts": {"name": "reviewed-skill"}}

    def native_candidate_prepare(self, event_id):
        assert event_id == "candidate-1"
        return "local:draft-1"


def _controller(tmp_path):
    return NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))


def _display(controller, record):
    with controller.displaying(record):
        pass
    return controller.load(record["id"])


def _act_and_display(controller, record_id, action, service, caller):
    return _display(controller, controller.act(record_id, action, service, caller))


def test_suggest_transition_uses_full_record_cas(tmp_path, monkeypatch):
    service = _NativeSuggestService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    record = controller.bind_message(record["id"], caller, "card-1")
    newer = {
        **record,
        "plan": {**record["plan"], "security_check": {"summary": "new scanner result"}},
    }
    controller.state.save(newer)
    monkeypatch.setattr(controller, "load", lambda _record_id: record)

    with pytest.raises(WisdomConflict):
        controller.act(
            record["id"], "suggest.files", service,
            replace(caller, message_id="card-1"),
        )

    assert controller.state.get(record["id"]) == newer


def test_suggest_navigation_waits_for_provider_display_ack(tmp_path):
    service = _NativeSuggestService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    files = controller.act(record["id"], "suggest.files", service, clicked)
    assert files["display_ready"] is False
    with pytest.raises(WisdomConflict, match="finish displaying"):
        controller.act(record["id"], "suggest.back", service, clicked)

    displayed = _display(controller, files)
    back = controller.act(record["id"], "suggest.back", service, clicked)
    assert displayed["display_ready"] is True
    assert back["plan"]["native_stage"] == "prepared"
    assert back["display_ready"] is False


def test_candidate_warnings_use_bound_suggest_ack_before_exact_approval(tmp_path):
    service = CandidateWarningService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_candidate(service, caller, "candidate-1")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")

    warnings = controller.act(record["id"], "share", service, clicked)

    assert warnings["operation"] == "suggest"
    assert warnings["plan"]["draft_id"] == "local:draft-1"
    assert warnings["plan"]["native_stage"] == "warnings"
    assert warnings["plan"]["local_scan"]["guard"]["findings"][0]["pattern_id"] == "shell-download"
    assert not any(call[0] == "publication_review" for call in service.calls)

    warnings = _display(controller, warnings)
    original = service.prepared_review

    def changed_scanner(*args, **kwargs):
        prepared = original(*args, **kwargs)
        prepared["local_scan"]["guard"]["findings"][0]["pattern_id"] = "changed"
        return prepared

    service.prepared_review = changed_scanner
    with pytest.raises(WisdomConflict, match="scanner findings changed"):
        controller.act(record["id"], "suggest.ack", service, clicked)
    assert not any(call[0] == "publication_submit" for call in service.calls)


@pytest.mark.parametrize("changed", ["draft", "owner"])
def test_publication_review_cannot_change_prepared_draft_or_owner(tmp_path, changed):
    service = _NativeSuggestService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    warnings = _act_and_display(controller, record["id"], "suggest.share", service, clicked)
    assert warnings["plan"]["native_stage"] == "warnings"
    original = service.publication_review

    def changed_review(*args, **kwargs):
        review = original(*args, **kwargs)
        if changed == "draft":
            review["draft_id"] = "local:other"
        else:
            review["owner"] = {**direct_owner(), "account_id": "other"}
        return review

    service.publication_review = changed_review
    with pytest.raises(WisdomConflict, match="changed"):
        controller.act(record["id"], "suggest.ack", service, clicked)
    assert not any(call[0] == "publication_submit" for call in service.calls)


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
    assert "Owner account: account-1" in text
    assert '"draft_id": "local:draft-1"' in text
    assert "Review expires: 123.0" in text
    assert '"account_id": "other"' not in text


def test_hidden_suggest_warning_acknowledgement_cannot_be_forged(tmp_path):
    service = _NativeSuggestService()
    original = service.suggest

    def entity_heavy(*args, **kwargs):
        plan = original(*args, **kwargs)
        finding = plan["local_scan"]["guard"]["findings"][0]
        finding["description"] = "&" * 1000
        finding["file"] = "&" * 500
        return plan

    service.suggest = entity_heavy
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    warning = _act_and_display(controller, record["id"], "suggest.share", service, clicked)
    assert not any(action.primary for action in controller.view(warning).actions)

    with pytest.raises(WisdomValidationError, match="not offered"):
        controller.act(record["id"], "suggest.ack", service, clicked)
    assert not any(call[0].startswith("publication_") for call in service.calls)
    assert controller.load(record["id"]) == warning


def test_inflight_suggest_claim_cannot_be_reclaimed(tmp_path):
    service = _NativeSuggestService()
    controller = _controller(tmp_path)
    caller = _native_invocation()
    record = controller.request_suggest(service, caller, "local-skill")
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    _act_and_display(controller, record["id"], "suggest.share", service, clicked)
    review = _act_and_display(controller, record["id"], "suggest.ack", service, clicked)
    assert review["plan"]["native_stage"] == "sharing"
    # Persisted claim from another in-flight controller, not a completed operation.
    claimed = {**review, "state": "applying"}
    controller.state.save(claimed)

    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(record["id"], "suggest.confirm", service, clicked)
    assert not any(call[0] == "publication_submit" for call in service.calls)
    assert controller.load(record["id"]) == claimed
