"""Recipient offer budgets exercised through registered, offline SDK callbacks."""
import asyncio
from html import unescape
import re

import pytest

from test_restored_native_flow import (
    RichTelegramHarness, Service, _click_emitted, _emitted_controls,
)
from restoration_support import SlackHarness, TelegramHarness


HARNESS_TYPES = [SlackHarness, RichTelegramHarness, TelegramHarness]


def _visible_text(ui):
    """Read the emitted card body, not Slack's unbounded accessibility fallback."""
    payload = (ui.edits or ui.posts)[-1]
    if "blocks" in payload:
        return unescape("\n".join(block["text"]["text"] for block in payload["blocks"]
                                  if block["type"] in {"header", "section"}))
    if "rich_message" in payload:
        return unescape(re.sub(r"<[^>]+>", "\n", payload["rich_message"]["html"]))
    return unescape(payload["text"])


def _begin_offer(ui, operation, explanation, monkeypatch):
    """Keep begin_update real; seed optional persisted editorial copy before posting.

    request_update has no explanation argument today. This saved-record shape is
    supported by the shared projection; it is not a new update API capability.
    """
    method_name = "request_update" if operation == "update" else "request_install"
    request = getattr(ui.runtime.reviews, method_name)

    def with_explanation(*args):
        record = request(*args)
        record["explanation"] = explanation
        ui.runtime.reviews.state.save(record)
        return record

    monkeypatch.setattr(ui.runtime.reviews, method_name, with_explanation)
    begin = ui.runtime.begin_update if operation == "update" else ui.runtime.begin
    return begin(ui.caller, "incident")["review_id"]


def _assert_offer(ui, case):
    """Check each provider independently, including the pre-existing sixth item."""
    operation, setup, explanation = case
    # Exact base also overflows on update + setup + explanation. Do not relax
    # its guard or dispatch a hidden Update to pretend that Slack flow succeeds.
    overflow = operation == "update" and setup and explanation
    expected = ["View Details", "Not Now"]
    if not ("blocks" in (ui.edits or ui.posts)[-1] and overflow):
        expected.append(operation.title())
    assert [label for label, _value in _emitted_controls(ui)] == expected
    if isinstance(ui, RichTelegramHarness):
        assert ("rich_message" in (ui.edits or ui.posts)[-1]) is not bool(overflow)
    text = _visible_text(ui)
    assert "Triage incidents" in text
    assert "Example Publisher" in text
    assert "Team: Incident Response" in text
    assert "Files:" not in text and "On this profile" not in text
    if operation == "update":
        assert "Installed v1" in text
        assert "Change summary unavailable" in text
    else:
        assert "Installed v1" not in text
    if setup:
        assert "Setup requires separate permission" in text
        assert "connect example account" in text
    if explanation and not ("blocks" in (ui.edits or ui.posts)[-1] and overflow):
        assert "Agent explanation (not authorization)" in text
        assert "Use the shared checklist." in text
    return expected


@pytest.mark.parametrize("harness", HARNESS_TYPES)
@pytest.mark.parametrize("case", [
    (operation, setup, explanation)
    for operation in ("install", "update")
    for setup in (False, True)
    for explanation in ("", "Use the shared checklist.")
])
def test_recipient_rendered_offer_roundtrip(tmp_path, monkeypatch, harness, case):
    """Visible disclosures and controls survive Details/Back before any effect."""
    operation, setup, explanation = case
    service = Service()
    plan = {**service.install_plan("incident"), "operation": operation,
            "from_version": 1, "installation_status": {"text": "Installed v1"},
            "publisher_display_name": "Example Publisher", "team_display_name": "Incident Response",
            "setup_commands": ["connect example account"] if setup else []}
    method_name = "update_plan" if operation == "update" else "install_plan"
    monkeypatch.setattr(service, method_name, lambda *_a, **_kw: plan, raising=False)

    async def scenario():
        ui = harness(tmp_path, service)
        try:
            identity = _begin_offer(ui, operation, explanation, monkeypatch)
            await ui.flush()
            expected = _assert_offer(ui, case)
            assert not service.applied
            await _click_emitted(ui, identity, "details")
            assert "Confirm" not in [label for label, _value in _emitted_controls(ui)]
            assert "Inspection does not authorize" in _visible_text(ui)
            assert not service.applied
            await _click_emitted(ui, identity, "back")
            assert _assert_offer(ui, case) == expected
            assert not service.applied
            if operation.title() in expected:
                await _click_emitted(ui, identity, operation)
                assert service.applied == ["exact-receipt"]
            else:
                assert ui.record(identity)["state"] == "pending"
                assert not service.applied
        finally:
            ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", HARNESS_TYPES)
@pytest.mark.parametrize("operation", ["install", "update"])
def test_oversized_recipient_offer_has_no_emitted_approval(
    tmp_path, monkeypatch, harness, operation,
):
    """Existing lossy text remains fail-closed; never dispatch an absent action."""
    service = Service()
    plan = {**service.install_plan("incident"), "operation": operation,
            "description": "material disclosure " * 600}
    method_name = "update_plan" if operation == "update" else "install_plan"
    monkeypatch.setattr(service, method_name, lambda *_a, **_kw: plan, raising=False)

    async def scenario():
        ui = harness(tmp_path, service)
        try:
            identity = _begin_offer(ui, operation, "", monkeypatch)
            await ui.flush()
            assert [label for label, _value in _emitted_controls(ui)] == ["View Details", "Not Now"]
            await _click_emitted(ui, identity, "details")
            assert "Confirm" not in [label for label, _value in _emitted_controls(ui)]
            await _click_emitted(ui, identity, "back")
            assert [label for label, _value in _emitted_controls(ui)] == ["View Details", "Not Now"]
            assert not service.applied
        finally:
            ui.runtime.close()

    asyncio.run(scenario())
