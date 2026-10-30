"""The one-click /wisdom share card: exact controls, no Portal drafts, bounded layout."""
# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio
from copy import deepcopy

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_restored_native_flow import Service
from hermes_wisdom_plugin.native_review import (
    MAX_ACTION_LABEL, NativeAction, NativeView, action_label, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import keyboard, plain_html
from hermes_wisdom_plugin import release_scope

SHARE_CONTROLS = [("Share", "confirm"), ("Cancel", "cancel"), ("View details", "details")]
_PLAN = {
    "candidate_flow": True, "editorial_name": "incident-review", "slug": "incident-review",
    "editorial_description": "Review incident notes.", "publication_mode": "open",
    "policy_version": 2, "allowed": True, "receipt": "r", "editorial_status": "complete",
    "authorization_expires_at": "2030-01-01T00:00:00+00:00", "hashes": {"content": "sha256:a"},
    "security_check": {"status": "pass"}, "professionalism_check": {"status": "pass"},
    "files": [{"path": "SKILL.md", "content_utf8": "first\nlast"}],
}
_BASE = {"operation": "publish", "screen": "approval", "state": "pending",
         "team_name": "Incident Response", "catalog_version": 0,
         "owner": {"organization_id": "org", "profile_name": "default"}, "plan": _PLAN}
_FINDINGS = {"available": True, "allowed": False, "findings": [
    {"severity": "medium", "file": "SKILL.md", "line": 3, "category": "exfiltration",
     "pattern_id": "curl_upload", "description": "Uploads a file with curl."}]}
CASES = {
    "share-clean": _BASE,
    "share-warnings": {"operation": "suggest", "state": "pending", "plan": {
        **_PLAN, "native_stage": "warnings", "local_scan": _FINDINGS}},
    "share-details": {**_BASE, "screen": "details", "page": 0, "inspected_through": 0},
    "shared": {**_BASE, "state": "completed",
               "portal_url": "https://portal.example/orgs/acme/wisdom/skills/abc",
               "result": {"state": "done", "publication_state": "published",
                          "publication_result": {"version": 1, "skill_id": "abc"}}},
    "blocked": {**_BASE, "state": "completed", "result": {
        "state": "rejected", "phase": "rejected", "code": "scan_blocked",
        "detail": "rejected before creation"}},
}


class WarningService(Service):
    """A manual share whose local scan needs an explicit acknowledgement."""

    def suggest(self, name):
        del name
        return self.prepared_review("local:draft-1")

    def prepared_review(self, draft_id, *, allow_model_review=False):
        assert allow_model_review is False
        return {key: value for key, value in self.publication_review(draft_id).items()
                if key not in {"receipt", "warning_acceptance"}}

    def publication_review(self, draft_id, *, accept_warnings=False):
        result = super().publication_review(draft_id, accept_warnings=accept_warnings)
        result["local_scan"] = deepcopy(_FINDINGS)
        result["security_check"].update(status="pending", local_status="warning_required",
                                         upload_allowed=False, warning_acceptance_required=True)
        result["scan_fingerprint"] = "sha256:scan"
        result["warning_acceptance"] = {
            "accepted": True, "operation": "publication",
            "content_hash": result["hashes"]["content"], "scan_fingerprint": "sha256:scan",
        } if accept_warnings else None
        return result


async def _first_card(ui, begin, argument):
    identity = getattr(ui.runtime, begin)(ui.caller, argument)["review_id"]
    await ui.flush()
    return identity, ui.view(identity)


def _controls(view):
    return [(action.label, action.action) for action in view.actions]


def _transport_labels(ui, payload):
    if isinstance(ui, TelegramHarness):
        return [[button.text for button in row] for row in payload["reply_markup"].inline_keyboard]
    return [[button["text"]["text"] for button in block["elements"]]
            for block in payload["blocks"] if block["type"] == "actions"]


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_share_card_offers_exactly_share_cancel_and_details(tmp_path, surface):
    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        view = ui.view(identity)
        assert view.title == "📤 Share skill"
        assert _controls(view) == SHARE_CONTROLS
        assert [action.primary for action in view.actions] == [True, False, False]
        expected = ([["Share", "Cancel"], ["View details"]] if surface is TelegramHarness
                    else [["Share", "Cancel", "View details"]])
        assert _transport_labels(ui, ui.posts[-1]) == expected
        for label in ("Snooze", "Maybe Later", "Review Checks", "Portal"):
            assert label not in str(ui.posts[-1])
        await ui.click(identity, "details")
        assert ui.view(identity).title == "Skill details"
        assert "Back" in [action.label for action in ui.view(identity).actions]
        await ui.click(identity, "back")
        assert _controls(ui.view(identity)) == SHARE_CONTROLS
        await ui.click(identity, "cancel")
        assert ui.record(identity)["state"] == "cancelled"
        assert not ui.view(identity).actions
        await ui.click(identity, "confirm")
        assert not service.applied, "Cancel leaves no remote effect"
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_scanner_warnings_need_acknowledge_before_share(tmp_path, surface):
    async def scenario():
        service = WarningService()
        ui = surface(tmp_path, service)
        identity, view = await _first_card(ui, "begin_suggest", "incident")
        assert view.title == "⚠️ Review warnings"
        assert _controls(view) == [("Acknowledge", "suggest.ack"), ("Cancel", "cancel"),
                                   ("View details", "paged.0")]
        await ui.click(identity, "confirm")
        assert not service.applied
        await ui.click(identity, "suggest.ack")
        assert _controls(ui.view(identity)) == SHARE_CONTROLS and not service.applied
        await ui.click(identity, "confirm")
        assert (service.applied, ui.record(identity)["state"]) == (
            ["publication-receipt"], "completed")
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_share_card_too_long_for_one_card_is_paged_to_share(tmp_path, surface):
    description = "Review incident notes and draft the follow-up. " * 80

    class LongShare(Service):
        def suggest(self, name):
            del name
            return self.prepared_review("local:draft-1")

        def publication_review(self, draft_id, *, accept_warnings=False):
            return {**super().publication_review(draft_id, accept_warnings=accept_warnings),
                    "editorial_description": description}

    async def scenario():
        service = LongShare()
        ui = surface(tmp_path, service)
        identity, view = await _first_card(ui, "begin_suggest", "incident")
        assert view.title == "📤 Share skill" and "Page 1 of " in view.summary
        assert _controls(view) == [("Next", "paged.1"), ("Cancel", "cancel"),
                                   ("View details", "details")]
        assert "terminal" not in view.to_text().lower()
        await ui.click(identity, "confirm")
        assert not service.applied
        pages = []
        while True:
            current = ui.view(identity)
            pages.append(current.items[0].detail)
            if not any(action.label == "Next" for action in current.actions):
                break
            assert "Share" not in sum(_transport_labels(ui, ui.edits[-1] if ui.edits
                                                        else ui.posts[-1]), [])
            await ui.next_page(identity)
        text = "".join(pages)
        assert description.strip() in text and "Incident Response" in text
        assert ("Share", "confirm") in _controls(ui.view(identity))
        assert "Share" in sum(_transport_labels(ui, ui.edits[-1]), [])
        await ui.click(identity, "confirm")
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_share_card_missing_a_fact_names_it_and_refreshes_in_chat(tmp_path, surface):
    class NoTeamYet(Service):
        team: str | None = None

        def native_team_name(self):
            return self.team

        def suggest(self, name):
            del name
            return self.prepared_review("local:draft-1")

    async def scenario():
        service = NoTeamYet()
        ui = surface(tmp_path, service)
        identity, view = await _first_card(ui, "begin_suggest", "incident")
        assert _controls(view) == [("Refresh", "refresh.review"), *SHARE_CONTROLS[1:]]
        assert "Missing: your team's name. Press Refresh" in view.notice
        assert "terminal" not in view.to_text().lower()
        await ui.click(identity, "confirm")
        await ui.click(identity, "refresh.review")
        assert _controls(ui.view(identity))[0] == ("Refresh", "refresh.review")
        assert not service.applied
        service.team = "Incident Response"
        await ui.click(identity, "refresh.review")
        assert _controls(ui.view(identity)) == SHARE_CONTROLS
        await ui.click(identity, "confirm")
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("plan", [
    {"operation": "share_status", "skill_name": "incident", "allowed": False,
     "status": {"operation_id": "op-1", "state": "pending", "phase": "policy_review_required"}},
    {"operation": "share_status", "skill_name": "incident", "allowed": False,
     "status": {"operation_id": "op-1", "state": "pending", "phase": "objects_uploaded"}},
    {"skill_name": "incident", "native_stage": "retired-stage"},
])
def test_unfinished_share_stops_point_to_the_chat_command(plan):
    operation = plan.get("operation", "suggest")
    view = project_view({"operation": operation, "state": "pending", "plan": plan})
    text = view.to_text()
    assert "/wisdom share incident again" in text
    assert "terminal" not in text.lower() and "hermes wisdom" not in text


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_portal_draft_is_unreachable_in_manual_sharing(tmp_path, surface):
    assert release_scope.active() == release_scope.AUTO_SUGGEST
    assert not release_scope.active().model_editorial

    class DraftService(Service):
        def publication_submit_private(self, *_args, **_kwargs):
            raise AssertionError("native share must never create a Portal draft")

    async def scenario():
        service = DraftService()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        before = ui.record(identity)
        await ui.click(identity, "portal.draft")
        assert ui.record(identity) == before
        assert not service.applied
        ui.runtime.close()
    asyncio.run(scenario())
    for record in CASES.values():
        view = project_view(deepcopy(record))
        offered = view.actions + view.navigation_actions + [
            action for item in view.items for action in item.actions]
        assert not any(action.action == "portal.draft" for action in offered)
        assert "Portal draft" not in view.to_text() and "Review/edit" not in view.to_text()


def test_result_cards_use_the_shared_layout():
    shared = project_view(deepcopy(CASES["shared"]))
    assert shared.title == "✅ Shared"
    assert [(field.label, field.value) for field in shared.fields] == [
        ("Team", "Incident Response"), ("Skill", "incident-review"), ("Version", "v1")]
    assert [(action.label, action.url) for action in shared.actions] == [
        ("View in Portal", "https://portal.example/orgs/acme/wisdom/skills/abc")]
    blocked = project_view(deepcopy(CASES["blocked"]))
    assert blocked.title == "❌ Not shared" and not blocked.actions
    for view in (shared, blocked, project_view(deepcopy(CASES["share-clean"]))):
        assert "!" not in view.to_text()


def test_action_labels_are_capped_on_every_transport():
    assert action_label("x" * 40) == "x" * (MAX_ACTION_LABEL - 1) + "…"
    assert action_label("Share") == "Share"
    long_view = NativeView("Title", "", actions=[
        NativeAction("A very long button label indeed", "confirm", primary=True)])
    slack = [button["text"]["text"] for block in blocks(long_view, "r")
             if block["type"] == "actions" for button in block["elements"]]
    telegram = [button.text for row in keyboard(long_view, "r").inline_keyboard for button in row]
    assert all(len(label) <= MAX_ACTION_LABEL for label in slack + telegram)
    for record in CASES.values():
        view = project_view(deepcopy(record))
        assert all(len(action.label) <= MAX_ACTION_LABEL for action in view.actions), view.title


def _assert_valid_slack_card(rendered):
    assert rendered[0]["type"] == "header"
    assert rendered[0]["text"]["type"] == "plain_text"
    assert len(rendered[0]["text"]["text"]) <= 150
    assert len(rendered) <= 50
    kinds = [block["type"] for block in rendered]
    assert set(kinds) <= {"header", "section", "context", "divider", "actions"}
    for block in rendered:
        if block["type"] == "section":
            assert "text" in block or "fields" in block
            assert len(block.get("fields", [])) <= 10
            assert all(len(field["text"]) <= 2000 for field in block.get("fields", []))
            assert len(block.get("text", {}).get("text", "")) <= 3000
        if block["type"] == "actions":
            assert 1 <= len(block["elements"]) <= 25
            for button in block["elements"]:
                assert len(button["text"]["text"]) <= MAX_ACTION_LABEL
                assert ("url" in button) != ("value" in button)
                assert button.get("style") in {None, "primary", "danger"}
    if "actions" in kinds:
        first = kinds.index("actions")
        assert kinds[first - 1] == "divider" and kinds.count("divider") == 1
        assert all(kind == "actions" for kind in kinds[first:])


@pytest.mark.parametrize("name", sorted(CASES))
def test_slack_blocks_validate_structurally(name):
    rendered = blocks(project_view(deepcopy(CASES[name])), "record-1")
    _assert_valid_slack_card(rendered)
    if name == "share-clean":
        section = rendered[1]
        assert [field["text"].split("\n")[0] for field in section["fields"]] == [
            "*Team*", "*Skill*", "*Version*", "*Files*", "*Checks*"]
        assert [block["type"] for block in rendered] == [
            "header", "section", "context", "divider", "actions"]
        share = rendered[-1]["elements"][0]
        assert share["text"]["text"] == "Share" and share["style"] == "primary"


def test_telegram_share_card_is_html_with_bold_labels_one_fact_per_line():
    view = project_view(deepcopy(CASES["share-clean"]))
    rendered = plain_html(view)
    assert rendered.startswith("<b>📤 Share skill</b>\n")
    for label in ("Team", "Skill", "Version", "Files", "Checks"):
        assert sum(line.startswith(f"<b>{label}:</b> ") for line in rendered.split("\n")) == 1
    assert "#" not in rendered
