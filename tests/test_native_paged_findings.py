"""Scanner warnings too long for one card are paged in the chat that started the flow.

Acknowledge appears once every page of these exact findings has been shown, and
changed findings start over; nothing hands the owner off to a terminal.
"""
# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from installed_test_support import JsonState
from native_test_support import direct_owner
from restoration_support import SlackHarness, TelegramHarness
from test_native_install_block_reason import Service as InstallService
from test_native_install_block_reason import _controller as install_controller
from test_native_install_block_reason import _show_service
from test_native_share_card import SHARE_CONTROLS, WarningService
from hermes_wisdom_plugin.errors import StaleControlError, WisdomAuthError
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.review_state import ReviewState

LOCKED = [("Cancel", "cancel"), ("View details", "paged.0")]


def _finding(number, rule="rule"):
    return {
        "severity": ("high", "medium", "low")[number % 3],
        "category": ("exfiltration", "hermes_env_access")[number % 2],
        "pattern_id": f"{rule}_{number:02d}", "file": "scripts/sync.sh", "line": number,
        "description": f"Finding {number:02d} sends local data to an external endpoint "
                       "without asking the operator first, then keeps a copy on disk.",
    }


def _findings(count=20, rule="rule"):
    return [_finding(number, rule) for number in range(1, count + 1)]


class ManyWarnings(WarningService):
    """A manual share whose local scan reports many findings."""

    def __init__(self, findings=None):
        super().__init__()
        self.findings = _findings() if findings is None else findings

    def publication_review(self, draft_id, *, accept_warnings=False):
        result = super().publication_review(draft_id, accept_warnings=accept_warnings)
        result["local_scan"] = {"available": True, "allowed": False,
                                "findings": deepcopy(self.findings)}
        return result


def _controls(view):
    return [(action.label, action.action) for action in view.actions]


def _labels(ui, payload):
    """Buttons the provider payload really carries (primary ones are dropped when lossy)."""
    if isinstance(ui, TelegramHarness):
        return [button.text for row in payload["reply_markup"].inline_keyboard for button in row]
    return [button["text"]["text"] for block in payload["blocks"]
            if block["type"] == "actions" for button in block["elements"]]


async def _read_every_page(ui, identity, acknowledge):
    """Open View details and press Next to the end; Acknowledge only on the last page."""
    await ui.click(identity, "paged.0")
    pages = []
    while True:
        view = ui.view(identity)
        assert view.title == "Scanner findings"
        assert f"page {len(pages) + 1} of " in view.summary
        pages.append(view.items[0].detail)
        if not any(action.label == "Next" for action in view.actions):
            break
        assert acknowledge not in [action.action for action in view.actions]
        assert "Acknowledge" not in _labels(ui, ui.edits[-1])
        await ui.next_page(identity)
    assert ("Acknowledge", acknowledge) in _controls(ui.view(identity))
    assert "Acknowledge" in _labels(ui, ui.edits[-1])
    return pages


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_twenty_findings_page_to_acknowledge_then_share_once(tmp_path, surface):
    async def scenario():
        service = ManyWarnings()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_suggest(ui.caller, "incident")["review_id"]
        await ui.flush()
        card = ui.view(identity)
        text = card.to_text()
        assert card.title == "⚠️ Review warnings" and _controls(card) == LOCKED
        assert "Acknowledge" not in _labels(ui, ui.posts[-1])
        assert "View details" in _labels(ui, ui.posts[-1])
        assert "View details lists all 20 findings." in card.notice
        assert "By severity: MEDIUM 7 · LOW 7 · HIGH 6" in text
        assert "By category: hermes_env_access 10 · exfiltration 10" in text
        assert "rule_01" in text and "terminal" not in text.lower()

        await ui.click(identity, "suggest.ack")  # forged before any page was shown
        assert ui.record(identity)["plan"]["native_stage"] == "warnings"
        assert not service.applied

        pages = await _read_every_page(ui, identity, "suggest.ack")
        assert len(pages) >= 3
        assert "  LOW / hermes_env_access (continued)\n" in "".join(pages[1:])
        for finding in service.findings:
            assert f"{finding['pattern_id']} at scripts/sync.sh:{finding['line']}" in "".join(pages)
        await ui.click(identity, "paged.back")
        assert _controls(ui.view(identity)) == [("Acknowledge", "suggest.ack"), *LOCKED]
        assert "Acknowledge" in _labels(ui, ui.edits[-1])

        await ui.click(identity, "suggest.ack")
        assert _controls(ui.view(identity)) == SHARE_CONTROLS and not service.applied
        await ui.click(identity, "confirm")
        await ui.click(identity, "confirm")
        await ui.click(identity, "suggest.ack")
        assert service.applied == ["publication-receipt"]
        assert ui.record(identity)["state"] == "completed"
        ui.runtime.close()
    asyncio.run(scenario())


def _invocation():
    return NativeInvocation(
        platform="slack", profile="default", chat_type="private", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="", scope_id="",
        message_id="M1", session_key="slack:D1", adapter=object(),
    )


def _suggest_controller(tmp_path, service):
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    record = controller.request_suggest(service, _invocation(), "incident")
    controller.bind_message(record["id"], _invocation(), "card-1")
    return controller, record["id"], replace(_invocation(), message_id="card-1")


def _act(controller, identity, action, service, clicked):
    record = controller.act(identity, action, service, clicked)
    with controller.displaying(record):
        pass
    return controller.load(identity)


def _offered(controller, record):
    return [action.action for action in controller.view(record).actions]


def _unlock(controller, identity, service, clicked):
    record = _act(controller, identity, "paged.0", service, clicked)
    while "paged." + str(record["paged"]["page"] + 1) in _offered(controller, record):
        record = _act(controller, identity, f"paged.{record['paged']['page'] + 1}",
                      service, clicked)
    return record


def test_forged_acknowledge_and_page_jumps_are_refused(tmp_path):
    service = ManyWarnings()
    controller, identity, clicked = _suggest_controller(tmp_path, service)
    before = controller.load(identity)
    for action in ("suggest.ack", "paged.3", "paged.back"):
        with pytest.raises(StaleControlError):
            controller.act(identity, action, service, clicked)
    assert controller.load(identity) == before

    record = _act(controller, identity, "paged.0", service, clicked)
    for action in ("suggest.ack", "paged.2"):
        with pytest.raises(StaleControlError):
            controller.act(identity, action, service, clicked)
    assert controller.load(identity) == record
    record = _act(controller, identity, "paged.back", service, clicked)
    assert "suggest.ack" not in _offered(controller, record)
    assert not service.applied


def test_changed_findings_after_viewing_reset_acknowledge(tmp_path):
    service = ManyWarnings()
    controller, identity, clicked = _suggest_controller(tmp_path, service)
    record = _unlock(controller, identity, service, clicked)
    assert "suggest.ack" in _offered(controller, record)

    service.findings = _findings(rule="changed")
    record = _act(controller, identity, "suggest.ack", service, clicked)

    view = controller.view(record)
    assert record["operation"] == "suggest" and record["plan"]["native_stage"] == "warnings"
    assert "paged" not in record
    assert view.title == "⚠️ Review warnings" and _controls(view) == LOCKED
    assert "changed_01" in view.to_text() and "rule_01" not in view.to_text()
    with pytest.raises(StaleControlError):
        controller.act(identity, "suggest.ack", service, clicked)
    # Pages seen for the old findings never count for the new ones.
    stale = {**controller.load(identity),
             "paged": {"identity": "old", "page": None, "seen": [0, 1, 2, 3, 4]}}
    assert "suggest.ack" not in _offered(controller, stale)

    record = _unlock(controller, identity, service, clicked)
    record = _act(controller, identity, "suggest.ack", service, clicked)
    assert record["operation"] == "publish" and "paged" not in record
    record = _act(controller, identity, "confirm", service, clicked)
    assert service.applied == ["publication-receipt"]


def test_small_findings_set_acknowledges_on_the_first_card(tmp_path):
    service = ManyWarnings(_findings(2))
    controller, identity, clicked = _suggest_controller(tmp_path, service)
    view = controller.view(controller.load(identity))
    assert _controls(view) == [("Acknowledge", "suggest.ack"), *LOCKED]
    assert "View details lists all 2 findings." in view.notice
    record = _act(controller, identity, "suggest.ack", service, clicked)
    assert record["operation"] == "publish"


def _many_install_service(service):
    service.findings = _findings()
    service.accepted_findings = service.findings
    return service


def test_install_with_many_findings_pages_then_installs_exactly_once(tmp_path):
    service = _many_install_service(InstallService(direct_owner()))
    controller, identity, clicked = install_controller(tmp_path, service)
    record = _act(controller, identity, "install", service, clicked)
    view = controller.view(record)
    assert record["screen"] == "install_warnings" and _controls(view) == LOCKED
    assert "Nothing has been installed. View details lists all 20 findings." in view.notice
    assert "terminal" not in view.to_text().lower()
    with pytest.raises(StaleControlError):
        controller.act(identity, "package.ack", service, clicked)

    record = _unlock(controller, identity, service, clicked)
    assert "package.ack" in _offered(controller, record)
    result = controller.act(identity, "package.ack", service, clicked)
    assert result["state"] == "completed" and service.applied == ["receipt-2"]
    with pytest.raises(WisdomAuthError, match="no longer current"):
        controller.act(identity, "package.ack", service, clicked)
    assert service.applied == ["receipt-2"]


def test_changed_install_findings_reset_the_pages_seen(tmp_path):
    service = _many_install_service(InstallService(direct_owner()))
    service.accepted_findings = _findings(rule="changed")
    controller, identity, clicked = install_controller(tmp_path, service)
    _act(controller, identity, "install", service, clicked)
    _unlock(controller, identity, service, clicked)

    record = _act(controller, identity, "package.ack", service, clicked)

    view = controller.view(record)
    assert record["screen"] == "install_warnings" and "paged" not in record
    assert _controls(view) == LOCKED and "changed_01" in view.to_text()
    assert not service.applied
    service.findings = service.accepted_findings
    _unlock(controller, identity, service, clicked)
    result = controller.act(identity, "package.ack", service, clicked)
    assert result["state"] == "completed" and len(service.applied) == 1


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_install_pages_many_findings_to_one_install(tmp_path, surface):
    async def scenario():
        service = _many_install_service(_show_service((), ()))
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_catalog(ui.caller, "show", "pr-infographic-embed")["review_id"]
        await ui.flush()
        await ui.click(identity, "catalog.install.0")
        assert "Review warnings" in ui.edits[-1]["text"]
        assert "Acknowledge" not in _labels(ui, ui.edits[-1])
        await ui.click(identity, "package.ack")
        assert not service.applied
        await _read_every_page(ui, identity, "package.ack")
        await ui.click(identity, "package.ack")
        await ui.click(identity, "package.ack")
        assert service.applied == ["receipt-2"]
        assert ui.edits[-1]["text"] == "✅ pr-infographic-embed installed"
        ui.runtime.close()
    asyncio.run(scenario())
