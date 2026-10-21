"""Native install cards name the real block reason and bind warning acknowledgement."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
import asyncio
import html
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from installed_test_support import JsonState
from native_test_support import direct_owner, owner
from restoration_support import SlackHarness, TelegramHarness
from hermes_wisdom_plugin.errors import (
    StaleControlError, WisdomAuthError, WisdomValidationError,
)
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.review_state import ReviewState

LOGGER = "hermes_wisdom_plugin.native_review"
REASON = "requires macOS; this machine is Linux"


def _finding(line):
    return {"severity": "critical", "category": "hermes_env_access",
            "pattern_id": "hermes_env_access", "file": "SKILL.md", "line": line,
            "description": "References the Hermes environment file"}


def _plan(current_owner, *, receipt="receipt-1", blocked=(), findings=(), accepted=False):
    scan_ok = not findings
    allowed = not blocked and (scan_ok or accepted)
    return {
        "operation": "install", "owner": current_owner, "skill_id": "skill-1",
        "slug": "pr-infographic-embed", "version": 2, "receipt": receipt,
        "allowed": allowed, "setup_commands": [],
        "files": [{"path": "SKILL.md", "hash": "sha256:" + "d" * 64}],
        "compatibility": {"outcome": "blocked_pending_action" if blocked else "compatible",
                          "blocked": list(blocked), "notes": []},
        "local_scan": {"allowed": scan_ok, "available": True, "findings": list(findings)},
        "policy": {"scan_requires_confirmation": not scan_ok,
                   "compatibility_requires_confirmation": False,
                   "warning_acceptance": {"accepted": bool(findings and accepted)}},
        "permitted_actions": ["confirm_install", "recheck"] if allowed else ["recheck"],
    }


class Service:  # pylint: disable=too-many-instance-attributes
    """Shared package whose fresh plan is scripted per call."""

    def __init__(self, current_owner, *, blocked=(), findings=((238,),),
                 accepted_findings=None):
        self.current_owner = current_owner
        self.blocked = blocked
        self.findings = [_finding(*item) for item in findings]
        self.accepted_findings = (self.findings if accepted_findings is None
                                  else [_finding(*item) for item in accepted_findings])
        self.plans = []
        self.inspected = []
        self.applied = []

    def native_owner(self):
        return self.current_owner

    def close(self):
        return None

    def install_plan(self, reference, *, update_mode=None, accept_warnings=False):
        del update_mode
        self.plans.append((reference, accept_warnings))
        findings = self.accepted_findings if accept_warnings else self.findings
        return _plan(self.current_owner, receipt=f"receipt-{len(self.plans)}",
                     blocked=self.blocked, findings=findings, accepted=accept_warnings)

    def native_install_inspect(self, receipt):
        self.inspected.append(receipt)
        return [{"path": "SKILL.md", "content": "offline fixture"}]

    def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
        assert not accept_partial and may_apply is not None and may_apply() is True
        self.applied.append(receipt)
        return {"installed": True, "skill_id": "skill-1", "version": 2}


def _invocation():
    return NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="C1",
        actor_id="account-1", actor_name="Shannon", thread_id="", scope_id="",
        message_id="M1", session_key="slack:C1", adapter=object(),
    )


def _controller(tmp_path: Path, service):
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    created = controller.request_install(service, _invocation(), "skill-1@v2", plan={
        "operation": "install", "owner": service.current_owner, "skill_id": "skill-1",
        "slug": "pr-infographic-embed", "version": 2,
    })
    controller.bind_message(created["id"], _invocation(), "card-1")
    return controller, created["id"], replace(_invocation(), message_id="card-1")


def _shown(controller, record):
    with controller.displaying(record):
        pass
    return controller.load(record["id"])


def _actions(controller, record):
    return [action.action for action in controller.view(record).actions]


def test_declared_requirement_block_says_cant_install_here(tmp_path, caplog):
    service = Service(direct_owner(), blocked=(REASON,), findings=())
    controller, identity, clicked = _controller(tmp_path, service)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        record = controller.act(identity, "install", service, clicked)

    view = controller.view(record)
    assert record["state"] == "failed"
    assert record["result"] == {"install_blocked": True, "reasons": [REASON]}
    assert view.summary == f"Can't install here: {REASON}"
    assert not view.actions
    assert not [block for block in blocks(view, identity) if block.get("type") == "actions"]
    assert not service.inspected and not service.applied
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"Collective Wisdom install blocked category=declared_requirement "
                        f"review={identity}"]


def test_scanner_findings_need_acknowledge_then_install_exactly_once(tmp_path):
    service = Service(direct_owner())
    controller, identity, clicked = _controller(tmp_path, service)

    record = _shown(controller, controller.act(identity, "install", service, clicked))
    view = controller.view(record)
    assert record["screen"] == "install_warnings" and record["state"] == "pending"
    assert "receipt" not in record["plan"]
    assert _actions(controller, record) == ["package.ack", "cancel", "paged.0"]
    assert "hermes_env_access" in view.to_text() and "SKILL.md:238" in view.to_text()
    assert "View details lists all 1 finding." in view.notice
    assert not service.inspected and not service.applied

    record = _shown(controller, controller.act(identity, "paged.0", service, clicked))
    assert "SKILL.md:238" in controller.view(record).to_text()
    assert _actions(controller, record) == ["package.ack", "paged.back"]
    record = _shown(controller, controller.act(identity, "paged.back", service, clicked))
    assert record["screen"] == "install_warnings"

    result = controller.act(identity, "package.ack", service, clicked)
    assert result["state"] == "completed"
    assert service.plans == [("skill-1@v2", False), ("skill-1@v2", True)]
    assert service.inspected == ["receipt-2"] and service.applied == ["receipt-2"]
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(identity, "package.ack", service, clicked)
    assert service.applied == ["receipt-2"]


def test_changed_findings_after_acknowledge_are_shown_again(tmp_path):
    service = Service(direct_owner(), accepted_findings=((238,), (12,)))
    controller, identity, clicked = _controller(tmp_path, service)
    record = _shown(controller, controller.act(identity, "install", service, clicked))

    record = _shown(controller, controller.act(identity, "package.ack", service, clicked))

    assert record["screen"] == "install_warnings" and record["state"] == "pending"
    assert "SKILL.md:12" in controller.view(record).to_text()
    assert not service.inspected and not service.applied
    service.findings = service.accepted_findings
    result = controller.act(identity, "package.ack", service, clicked)
    assert result["state"] == "completed" and len(service.applied) == 1


def test_acknowledge_is_refused_on_cards_that_do_not_offer_it(tmp_path):
    service = Service(direct_owner(), findings=())
    controller, identity, clicked = _controller(tmp_path, service)

    with pytest.raises(StaleControlError):
        controller.act(identity, "package.ack", service, clicked)
    assert not service.plans and not service.applied


def test_preparation_failure_logs_one_masked_warning(tmp_path, caplog):
    service = Service(direct_owner(), findings=())

    def broken(_receipt):
        raise WisdomValidationError("bad package token=sk-live-abcdefghijklmnop1234")

    service.native_install_inspect = broken
    controller, identity, clicked = _controller(tmp_path, service)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        record = controller.act(identity, "install", service, clicked,
                                progress=lambda _view, _claimed: None)

    assert record["failure"] == "package_preparation_failed"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"category=invalid_review review={identity}" in warnings[0]
    assert "sk-live-abcdefghijklmnop1234" not in warnings[0]


def test_install_effect_failure_is_not_reported_as_preparation(tmp_path, caplog):
    service = Service(direct_owner(), findings=())

    def failing(_receipt, *, accept_partial=False, may_apply=None):
        del accept_partial, may_apply
        raise ConnectionError("install write interrupted")

    service.install_apply = failing
    controller, identity, clicked = _controller(tmp_path, service)

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(ConnectionError):
        controller.act(identity, "install", service, clicked,
                       progress=lambda _view, _claimed: None)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "outcome unknown" in warnings[0]
    assert controller.load(identity)["state"] == "uncertain"


def _show_service(blocked, findings):
    class ShowService(Service):
        def show(self, _reference):
            return {
                "skill": {"id": "skill-1", "slug": "pr-infographic-embed", "state": "active"},
                "latest_version_detail": {"version": {"version": 2}},
                "installation_status": {"text": "Not installed", "state": "not_installed"},
            }

    return ShowService(owner(), blocked=blocked, findings=findings)


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_surfaces_show_real_reason_and_acknowledge(tmp_path, surface):
    async def scenario():
        blocked = _show_service((REASON,), ())
        ui = surface(tmp_path / "blocked", blocked)
        identity = ui.runtime.begin_catalog(ui.caller, "show", "pr-infographic-embed")["review_id"]
        await ui.flush()
        await ui.click(identity, "catalog.install.0")
        assert f"Can't install here: {REASON}" in html.unescape(ui.edits[-1]["text"])
        assert "preparation failed" not in ui.edits[-1]["text"]
        assert not blocked.applied

        warned = _show_service((), ((238,),))
        ui = surface(tmp_path / "warned", warned)
        identity = ui.runtime.begin_catalog(ui.caller, "show", "pr-infographic-embed")["review_id"]
        await ui.flush()
        await ui.click(identity, "catalog.install.0")
        assert "Review warnings" in ui.edits[-1]["text"]
        assert not warned.applied
        await ui.click(identity, "package.ack")
        assert warned.applied == ["receipt-2"]
        assert ui.edits[-1]["text"] == "✅ pr-infographic-embed installed"

    (tmp_path / "blocked").mkdir()
    (tmp_path / "warned").mkdir()
    asyncio.run(scenario())
