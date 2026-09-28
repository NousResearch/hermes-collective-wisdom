"""Restored journeys through real registered native callbacks; no live services."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods,too-many-lines
import asyncio
import sys
from datetime import datetime
from html import unescape
from types import ModuleType, SimpleNamespace

import pytest

from installed_test_support import JsonState
from native_test_support import owner
from restoration_support import (
    SlackHarness, TelegramHarness, assert_cancelled_review_is_inert,
    assert_owner_switch_after_post_is_cleaned,
)
from test_client import Response, Session, client as recording_client
from hermes_wisdom_plugin.errors import SharePackagingError
from hermes_wisdom_plugin.native_inspection import publication_proposal_pages
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import rich_html, plain_html
from hermes_wisdom_plugin.runtime import InvocationService


def test_registered_slack_wisdom_slash_uses_host_admission(tmp_path, monkeypatch):
    """A menu-registered /wisdom must reach the admitted host command path."""
    async def scenario():
        ui = SlackHarness(tmp_path, Service())
        assert ui.command_handler is not None
        assert ui.command_pattern == "/wisdom"
        events, acknowledgements = [], []

        async def handle_message(event):
            events.append(event)

        ui.adapter.build_source = SimpleNamespace
        ui.adapter.handle_message = handle_message
        event_module = ModuleType("gateway.platforms.event")
        event_module.__dict__.update(
            MessageEvent=SimpleNamespace, MessageType=SimpleNamespace(COMMAND="command")
        )
        for name in ("gateway", "gateway.platforms"):
            monkeypatch.setitem(sys.modules, name, ModuleType(name))
        monkeypatch.setitem(sys.modules, "gateway.platforms.event", event_module)

        async def ack(**details):
            acknowledgements.append(details)

        command = {
            "command": "/wisdom", "text": "install incident",
            "user_id": "U1", "channel_id": "D1", "team_id": "W1",
        }
        await ui.command_handler(ack, command)
        assert len(acknowledgements) == 1
        assert len(events) == 1
        assert events[0].text == "/wisdom install incident"
        assert events[0].message_type == "command"
        assert events[0].source.chat_id == "D1"
        assert events[0].source.user_id == "U1"
        assert events[0].source.scope_id == "W1"

        await ui.command_handler(ack, {**command, "channel_id": "C1"})
        assert len(events) == 1
        ui.runtime.close()
        await ui.command_handler(ack, command)
        assert len(events) == 1

    asyncio.run(scenario())


def test_slack_review_started_by_host_command_worker_posts_one_card(tmp_path):
    async def scenario():
        ui = SlackHarness(tmp_path, Service())

        def host_spawn(coroutine, **_kwargs):
            assert asyncio.get_running_loop().is_running()
            ui.tasks.append(coroutine)

        ui.runtime.spawn_task = host_spawn
        result = await asyncio.to_thread(ui.runtime.begin, ui.caller, "incident")
        assert result["state"] == "queued"
        await asyncio.sleep(0)
        await ui.flush()
        assert len(ui.posts) == 1
        labels = [action.label for action in ui.view(result["review_id"]).actions]
        assert labels[-1] == "Install Skill"
        ui.runtime.close()

    asyncio.run(scenario())


class Service:
    def __init__(self):
        self.applied = []

    def native_owner(self):
        return owner()

    def native_team_name(self) -> str | None:
        return "Incident Response"

    def close(self):
        return None

    def prepare_candidate_offer(self, identity):
        return self.native_candidate_inspect(identity)

    def native_candidate_inspect(self, identity):
        return {"event_id": identity, "facts": {"name": "incident", "editorial_status": "complete",
                "editorial": {"editorial_name": "Incident Review",
                              "editorial_description": "Review incident notes.",
                              "coworker_benefit": "Teammates can use the same checklist."}},
                "networked": False}

    def native_candidate_prepare(self, identity):
        assert identity == "candidate-1"
        return "local:draft-1"

    def prepared_review(self, draft_id, *, allow_model_review=False):
        assert allow_model_review is False
        return self.publication_review(draft_id)

    def publication_review(self, draft_id, *, accept_warnings=False):
        del accept_warnings
        return {
            "draft_id": draft_id, "receipt": "publication-receipt", "allowed": True,
            "editorial_name": "Incident Review", "editorial_description": "Review incident notes.",
            "coworker_benefit": "Teammates can use the same checklist.",
            "editorial_status": "complete", "slug": "incident-review",
            "hashes": {"content": "sha256:content", "author_description": "sha256:description",
                       "package_manifest": "sha256:manifest"},
            "policy_version": 3, "authorization_expires_at": "2026-09-30T00:00:00Z",
            "files": [{"path": "SKILL.md", "content_utf8": "first\nlast"}],
            "security_check": {"status": "pass"},
            "professionalism_check": {"status": "pass"},
            "publication_mode": "moderated",
        }

    def publication_submit(self, receipt, *, may_apply):
        assert may_apply()
        self.applied.append(receipt)
        return {"state": "pending_moderation"}

    def install_plan(self, reference, **_kwargs):
        assert reference in {"incident", "incident@v2", "skill", "skill-1", "skill-1@v2"}
        return {
            "operation": "install", "skill_id": "skill-1", "slug": "incident",
            "receipt": "exact-receipt", "allowed": True,
            "description": "Triage incidents", "version": 2,
        }

    def native_install_inspect(self, receipt):
        assert receipt == "exact-receipt"
        return [{"path": "SKILL.md", "content": "offline fixture"}]

    native_update_inspect = native_install_inspect

    def install_apply(self, receipt, *, may_apply, **_kwargs):
        assert may_apply()
        self.applied.append(receipt)
        return {"installed": True}

    def update_apply(self, receipt, *, may_apply, **_kwargs):
        assert may_apply()
        self.applied.append(receipt)
        return {"updated": True}


def _clean_install_plan(service, reference):
    return {
        **Service.install_plan(service, reference),
        "setup_commands": [], "compatibility": {"outcome": "compatible"},
        "local_scan": {"allowed": True, "available": True},
        "policy": {"scan_requires_confirmation": False,
                   "compatibility_requires_confirmation": False},
    }


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_explicit_install_applies_clean_skill_without_review(tmp_path, harness):
    class CleanService(Service):
        def install_plan(self, reference, **_kwargs):
            return _clean_install_plan(self, reference)

    async def scenario():
        service = CleanService()
        ui = harness(tmp_path, service)
        initial = JsonState(tmp_path).get("wisdom-reviews")
        view = await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        assert service.applied == ["exact-receipt"]
        assert view.summary == "✅ incident installed"
        assert JsonState(tmp_path).get("wisdom-reviews") == initial
        await ui.runtime.send_direct_result(ui.caller, view)
        assert len(ui.posts) == 1
        assert "incident installed" in ui.posts[0]["text"]
        ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_explicit_install_with_declared_setup_shows_exact_review_before_effect(
    tmp_path, harness,
):
    class SetupService(Service):
        def __init__(self):
            super().__init__()
            self.plans = []

        def install_plan(self, reference, **_kwargs):
            self.plans.append(reference)
            return {**_clean_install_plan(self, reference),
                    "setup_commands": ["connect example account"]}

        def install_apply(self, receipt, *, may_apply, **_kwargs):
            assert may_apply()
            self.applied.append(receipt)
            return {"installed": True, "setup_required": True, "ready_to_use": None}

        def install_setup_apply(self, receipt, *, may_apply):
            assert may_apply()
            self.applied.append("setup:" + receipt)
            return {"installed": True, "setup_required": True, "ready_to_use": None,
                    "setup_execution_authorized": True,
                    "setup": {"state": "passed", "steps": 1}}

    async def scenario():
        service = SetupService()
        ui = harness(tmp_path, service)
        queued = await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        assert queued["state"] == "queued"
        assert not service.applied
        assert service.plans == ["incident"]
        await ui.flush()
        identity = queued["review_id"]
        assert "connect example account" in ui.view(identity).to_text()
        assert len(ui.posts) == 1
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert "files installed" in ui.view(identity).to_text().lower()
        assert "connect example account" in ui.view(identity).to_text()
        assert [a.label for a in ui.view(identity).actions] == ["Run setup", "Not now"]
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        await ui.click(identity, "setup.run")
        assert service.applied == ["exact-receipt", "setup:exact-receipt"]
        receipt_text = ui.view(identity).to_text()
        assert "✅ incident installed\n\nSetup ran successfully" in receipt_text
        assert "Have your agent confirm the skill is ready" in receipt_text
        assert "setup incomplete" not in receipt_text
        await ui.click(identity, "setup.run")
        assert service.applied == ["exact-receipt", "setup:exact-receipt"]
        ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_declining_setup_keeps_files_without_executing_command(tmp_path, harness):
    class SetupService(Service):
        def install_plan(self, reference, **_kwargs):
            return {**_clean_install_plan(self, reference), "setup_commands": ["printf harmless"]}

        def install_apply(self, receipt, *, may_apply, **_kwargs):
            assert may_apply()
            self.applied.append(receipt)
            return {"installed": True, "setup_required": True, "ready_to_use": None}

        def install_setup_apply(self, *_args, **_kwargs):
            raise AssertionError("declined setup must not run")

    async def scenario():
        service = SetupService()
        ui = harness(tmp_path, service)
        queued = await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        await ui.flush()
        identity = queued["review_id"]
        await ui.click(identity, "install")
        assert [a.action for a in ui.view(identity).actions] == ["setup.run", "setup.defer"]
        await ui.click(identity, "setup.defer")
        assert service.applied == ["exact-receipt"]
        assert "setup incomplete" in ui.view(identity).to_text()
        await ui.click(identity, "setup.run")
        assert service.applied == ["exact-receipt"]
        ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["warning", "scanner", "compatibility", "missing_scan"])
def test_explicit_install_stops_on_nonclean_plan(tmp_path, unsafe):
    class UnsafeService(Service):
        def install_plan(self, reference, **_kwargs):
            plan = _clean_install_plan(self, reference)
            if unsafe == "warning":
                plan["policy"]["scan_requires_confirmation"] = True
            elif unsafe == "scanner":
                plan["local_scan"]["allowed"] = False
            elif unsafe == "compatibility":
                plan["compatibility"]["outcome"] = "review"
            else:
                del plan["local_scan"]
            return plan

    async def scenario():
        service = UnsafeService()
        ui = SlackHarness(tmp_path, service)
        view = await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        assert view.summary == "⚠️ incident wasn't installed"
        assert "No skill was installed" in view.notice
        assert not service.applied
        assert not view.actions
        ui.runtime.close()

    asyncio.run(scenario())


def test_explicit_install_rechecks_authority_before_apply(tmp_path):
    class RevokedService(Service):
        def __init__(self):
            super().__init__()
            self.revoke = lambda: None

        def install_plan(self, reference, **_kwargs):
            self.revoke()
            return _clean_install_plan(self, reference)

    async def scenario():
        service = RevokedService()
        ui = SlackHarness(tmp_path, service)
        def revoke():
            ui.adapter._is_interactive_user_authorized = (  # pylint: disable=protected-access
                lambda *_args, **_kw: False
            )

        service.revoke = revoke
        with pytest.raises(PermissionError):
            await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        assert not service.applied
        ui.runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_not_now_defers_exact_version_once_and_collapses_original_card(tmp_path, harness):
    class DeferringService(Service):
        def __init__(self):
            super().__init__()
            self.deferrals = []

        def prepare_review_deferral(self, plan):
            assert (plan["skill_id"], plan["version"]) == ("skill-1", 2)
            return {"reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
                    "not_now_suppression_days": 30}

        def defer_review(self, deferral, *, may_apply):
            assert may_apply() is True
            self.deferrals.append(deferral["reference"])
            return {"deferred": True, "preference_sync": "confirmed"}

    async def scenario():
        service = DeferringService()
        ui = harness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()
        original = ui.posts[-1].get("text")
        assert next(action.action for action in ui.view(identity).actions
                    if action.label == "Not Now") == "defer"
        await ui.click(identity, "cancel")  # A hidden old callback must not skip suppression.
        assert ui.record(identity)["state"] == "pending"
        await ui.click(identity, "defer")
        assert ui.record(identity)["state"] == "cancelled"
        assert service.deferrals == [{"kind": "skill", "skill_id": "skill-1", "version": 2}]
        assert not service.applied
        assert ui.view(identity).actions == []
        assert len(ui.posts) == 1
        assert ui.edits[-1].get("text") == "⏸ incident postponed"
        assert ui.edits[-1].get("text") != original
        if harness is SlackHarness:
            assert not any(block["type"] == "actions" for block in ui.edits[-1]["blocks"])
        await ui.click(identity, "defer")
        await ui.click(identity, "install")
        assert len(service.deferrals) == 1

    asyncio.run(scenario())


def test_not_now_does_not_claim_success_when_preference_is_unconfirmed(tmp_path):
    class UnconfirmedService(Service):
        def __init__(self):
            super().__init__()
            self.deferrals = []

        def prepare_review_deferral(self, plan):
            return {"reference": {"kind": "skill", "skill_id": plan["skill_id"],
                                  "version": plan["version"]}}

        def defer_review(self, prepared, *, may_apply):
            assert may_apply() is True
            self.deferrals.append(prepared)
            return {"deferred": True, "preference_sync": "unknown"}

    async def scenario():
        service = UnconfirmedService()
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()
        await ui.click(identity, "defer")
        assert ui.record(identity)["state"] == "uncertain"
        assert not ui.edits
        await ui.click(identity, "defer")
        assert len(service.deferrals) == 1
        assert not service.applied

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_invitation_has_actionable_ordered_controls(tmp_path, surface):
    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        response = ui.runtime.begin_candidate(ui.caller, "candidate-1")
        await ui.flush()
        identity = response["review_id"]
        view = ui.view(identity)
        assert view.title == "Hermes Collective Wisdom"
        assert "You created a skill that could help your team" in view.summary
        assert [action.label for action in view.actions] == [
            "Review Checks", "View More Details", "Snooze Collective Wisdom",
            "Maybe Later", "Review proposal",
        ]
        assert "Safe To Share" not in view.to_text()
        assert "Destination team: Incident Response" in view.to_text()
        assert "organization ID" not in view.to_text()
        assert not any(a.label == "Share My Skill" for a in view.actions)
        await ui.click(identity, "share")
        assert ui.record(identity)["operation"] == "publish"
        assert ui.record(identity)["plan"]["receipt"] == "publication-receipt"
        assert not service.applied
        await ui.click(identity, "back")
        await ui.click(identity, "cancel")
        assert not ui.view(identity).actions
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_transport_details_are_readable_and_do_not_expose_ids(tmp_path, surface):
    async def scenario():
        class HostileService(Service):
            def native_team_name(self):
                return "R&D <Team>"
        ui = surface(tmp_path, HostileService())
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "details")
        view = ui.view(identity)
        assert "Approval permission: blocked" not in view.to_text()
        assert "organization ID" not in view.to_text()
        assert "org" not in view.to_text()
        assert "{" not in view.items[0].detail
        slack = blocks(view, identity)
        assert "R&amp;D &lt;Team&gt;" in str(slack)
        assert "R&D <Team>" in unescape(rich_html(view, identity))
        assert "R&D <Team>" in unescape(plain_html(view))
        assert "Review details and files" in str(slack)
    asyncio.run(scenario())

@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_missing_team_name_never_falls_back_to_owner_id(tmp_path, surface):
    async def scenario():
        class Unnamed(Service):
            def native_team_name(self) -> str | None:
                return None
        ui = surface(tmp_path, Unnamed())
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        assert "Destination team: Team name unavailable" in ui.view(identity).to_text()
        assert "Destination team: org" not in ui.view(identity).to_text()
        await ui.click(identity, "share")
        assert "Destination team: Team name unavailable" in ui.view(identity).to_text()
        assert "Destination team: org" not in ui.view(identity).to_text()
    asyncio.run(scenario())

@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_final_share_spends_only_inspected_receipt_once(tmp_path, surface):
    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "confirm")
        assert not service.applied
        await ui.click(identity, "share")
        assert ui.record(identity)["plan"]["candidate_flow"] is True
        assert "Exact review data (JSON)" not in [item.title for item in ui.view(identity).items]
        assert "publication-receipt" not in ui.view(identity).to_text()
        assert "Destination" in ui.view(identity).to_text()
        assert "Incident Review" in ui.view(identity).to_text()
        assert not service.applied
        while True:
            following = next((a for a in ui.view(identity).actions if a.label == "Next"), None)
            if following is None:
                break
            await ui.click(identity, following.action)
        assert "Share My Skill" in [a.label for a in ui.view(identity).actions]
        assert not service.applied
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
    asyncio.run(scenario())

@pytest.mark.parametrize("allowed,findings,expected", [
    (True, [], "allowed; findings: 0"),
    (False, [{"severity": "high"}], "needs review; findings: 1"),
    (None, [{"severity": "low"}], "needs review; findings: 1"),
])
def test_publication_proposal_displays_flat_host_scan(allowed, findings, expected):
    plan = Service().publication_review("local:draft-1")
    plan["local_scan"] = {"available": True, "allowed": allowed,
                          "findings": findings}
    text = "".join(publication_proposal_pages({"plan": plan}))
    assert "Local scanner: " + expected in text


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_optional_details_roundtrip_has_no_effect(tmp_path, surface):
    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "share")
        await ui.click(identity, "details")
        assert ui.view(identity).items[0].title != "Exact review data (JSON)"
        assert "{\n" not in ui.view(identity).items[0].detail
        assert not service.applied
        await ui.click(identity, "back")
        assert ui.view(identity).items[0].title == "Sharing proposal"
        assert not service.applied
        while next((a for a in ui.view(identity).actions if a.label == "Next"), None):
            following = next(a.action for a in ui.view(identity).actions if a.label == "Next")
            await ui.click(identity, following)
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_proposal_overflow_preserves_approval_facts(tmp_path, surface, monkeypatch):
    async def scenario():
        service = Service()
        exact = service.publication_review("local:draft-1")
        exact["security_check"]["checks"] = [
            {"key": f"finding-{number}-" + "<&" * 300, "status": "pass"}
            for number in range(6)
        ]
        monkeypatch.setattr(service, "publication_review", lambda _draft, **_kw: exact)
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "share")
        pages = []
        while True:
            view = ui.view(identity)
            assert len(view.to_text()) <= 3500
            pages.append(view.items[0].detail)
            following = next((a for a in view.actions if a.label == "Next"), None)
            if following is None:
                break
            await ui.click(identity, following.action)
        text = "".join(pages)
        assert all(f"finding-{number}-" in text for number in range(6))
        assert "<&" * 300 in text
        assert "Share My Skill" in [action.label for action in ui.view(identity).actions]
        assert not service.applied
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_incomplete_candidate_only_retries_local_preparation(tmp_path, surface):
    class IncompleteService(Service):
        def __init__(self):
            super().__init__()
            self.retries = 0

        def prepare_candidate_offer(self, identity):
            self.retries += 1
            plan = super().prepare_candidate_offer(identity)
            plan["facts"]["editorial_status"] = "incomplete"
            plan["facts"]["editorial_failure"] = "invalid_response"
            return plan

        def native_candidate_prepare(self, identity):
            raise AssertionError("not ready for publication review")

    async def scenario():
        service = IncompleteService()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        assert "Retry preparation" in [action.label for action in ui.view(identity).actions]
        await ui.click(identity, "share")
        assert service.retries == 2
        assert ui.record(identity)["operation"] == "candidate"
        assert not service.applied
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_candidate_missing_receipt_cannot_offer_final_share(tmp_path, surface):
    class MissingReceipt(Service):
        def publication_review(self, draft_id, *, accept_warnings=False):
            result = super().publication_review(draft_id, accept_warnings=accept_warnings)
            result.pop("receipt")
            return result

    async def scenario():
        service = MissingReceipt()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "share")
        assert not service.applied
        assert not any(a.label == "Share My Skill" for a in ui.view(identity).actions)
        await ui.click(identity, "confirm")
        assert not service.applied
    asyncio.run(scenario())


def test_complete_private_inspection_returns_without_authorizing(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        exact = service.publication_review("local:draft-1")
        exact["files"][0]["content_utf8"] = "first\n" + "<&\"" * 1800 + "\nLAST FILE BYTE"
        monkeypatch.setattr(service, "publication_review", lambda _draft: exact)
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin_publication(ui.caller, "local:draft-1")
        await ui.flush()
        identity = response["review_id"]
        before = ui.record(identity)["plan"]
        await ui.click(identity, "details")
        chunks = []
        while True:
            view = ui.view(identity)
            assert len(view.to_text()) < 3500
            assert "Confirm" not in [action.label for action in view.actions]
            chunks.append(view.items[0].detail)
            next_action = next((a for a in view.actions if a.label == "Next"), None)
            if next_action is None:
                break
            await ui.click(identity, next_action.action)
        import json  # pylint: disable=import-outside-toplevel
        visible = "".join(chunks)
        assert "📄 File:" in visible and "ℹ️ What it does" in visible
        assert json.dumps(exact["files"][0]["content_utf8"])[1:-1] in visible
        assert "publication-receipt" not in visible
        assert ui.view(identity).actions[-1].label == "Looks good."
        await ui.click(identity, "back")
        assert "Review proposal" in [a.label for a in ui.view(identity).actions]
        assert ui.record(identity)["plan"] == before
        assert not service.applied
        # A forged final callback cannot skip the explicit Share/review screen.
        await ui.click(identity, "confirm")
        assert not service.applied
        await ui.click(identity, "share")
        while True:
            view = ui.view(identity)
            next_action = next((a for a in view.actions if a.label == "Next"), None)
            if next_action is None:
                break
            await ui.click(identity, next_action.action)
        assert "Confirm" in [a.label for a in ui.view(identity).actions]
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
    asyncio.run(scenario())


def test_safe_summary_requires_all_actual_hash_bound_checks(tmp_path, monkeypatch):
    from hermes_wisdom_plugin.professionalism import CHECK_KEYS  # pylint: disable=import-outside-toplevel
    async def scenario():
        service = Service()
        exact = service.publication_review("local:draft-1")
        exact["hashes"] = {"content": "sha256:content", "author_description": "sha256:desc"}
        security_keys = (
            "private_keys", "live_credentials", "secret_assignments", "skills_guard",
            "organization_policy", "personal_information",
        )
        exact["security_check"]["checks"] = [
            {"key": key, "status": "pass", "finding_count": 0} for key in security_keys
        ]
        exact["professionalism_check"].update(
            content_hash="sha256:content", author_description_hash="sha256:desc",
            checks=[{"key": key, "status": "pass", "finding_count": 0} for key in CHECK_KEYS],
        )
        monkeypatch.setattr(service, "publication_review", lambda _draft: exact)
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin_publication(ui.caller, "local:draft-1")
        await ui.flush()
        identity = response["review_id"]
        assert ui.view(identity).actions[0].label == "✅ Safe To Share"
        await ui.click(identity, "checks")
        expanded = ui.view(identity).to_text()
        assert "✅ Safe To Share" in expanded
        assert "  ✅ Private keys: pass" in expanded
        assert "  ✅ Profanity or abusive language: pass" in expanded
        await ui.click(identity, "back")
        assert not service.applied
        for status in ("pending", "unavailable", "blocked"):
            exact["security_check"]["checks"][-1]["status"] = status
            next_review = ui.runtime.begin_publication(ui.caller, "local:draft-1")
            await ui.flush()
            view = ui.view(next_review["review_id"])
            assert "Safe To Share" not in view.to_text()
            assert view.actions[0].label == "Review Checks"
    asyncio.run(scenario())


def test_snooze_opens_disclosed_settings_without_muting(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        changed = []
        monkeypatch.setattr(service, "notification_settings", lambda **_kw: {
            "delivery_mode": "agent", "mode_writable": True,
            "mute_control": {"revision": 7},
        }, raising=False)
        monkeypatch.setattr(service, "choose_notification_mute",
                            lambda *_a, **_kw: changed.append(True), raising=False)
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin_candidate(ui.caller, "candidate-1")
        await ui.flush()
        identity = response["review_id"]
        await ui.click(identity, "settings")
        assert ui.record(identity)["operation"] == "settings"
        view = ui.view(identity)
        text = view.to_text()
        assert "sharing suggestions and skill recommendations" in text
        assert "not your teammates" in text
        assert "Current: On" in text
        assert not any(word in text for word in ("Gateway", "Revision", "None", "agent", "Org id"))
        assert [action.label for action in view.actions] == [
            "Mute one week", "Turn off automatic suggestions", "Cancel",
        ]
        assert not changed
        assert not service.applied
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("stale", [False, True])
def test_native_one_week_mute_uses_guarded_gateway_duration(tmp_path, surface, stale):
    class MuteSession(Session):
        def __init__(self):
            super().__init__()
            self.muted = False

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "PUT":
                self.muted = True
            return Response(200, {
                "org_id": "org-1", "muted": self.muted,
                "duration": "1_week" if self.muted else None,
                "muted_until": "2030-01-08T00:00:00Z" if self.muted else None,
                "forever": False, "revision": 8 if self.muted else 7,
            })

    class MuteService(Service):
        choose_notification_mute = InvocationService.choose_notification_mute

        def __init__(self, session):
            super().__init__()
            self.client = recording_client(session=session)

        def native_owner(self):
            return owner(organization_id="org-1")

        def notification_settings(self, **_kwargs):
            return {"delivery_mode": "agent", "mode_writable": True,
                    "mute_control": self.client.recommendation_mute().model_dump(mode="json")}

    async def scenario():
        transport = MuteSession()
        service = MuteService(transport)
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "settings")
        assert not [call for call in transport.calls if call[0] == "PUT"]
        forged = ({"user": {"id": "U2"}} if ui.caller.platform == "slack"
                  else {"actor_id": 22})
        await ui.click(identity, "settings.mute.1w", **forged)
        assert not [call for call in transport.calls if call[0] == "PUT"]
        if stale:
            transport.muted = True  # Another client changed the Gateway revision.
            await ui.click(identity, "settings.mute.1w")
            assert not [call for call in transport.calls if call[0] == "PUT"]
            return
        await ui.click(identity, "settings.mute.1w")
        writes = [call for call in transport.calls if call[0] == "PUT"]
        assert len(writes) == 1
        assert writes[0][2]["json"]["duration"] == "1_week"
        assert writes[0][2]["json"]["expected_revision"] == 7
        assert writes[0][2]["json"]["mutation_id"]
        requested_at = writes[0][2]["json"]["requested_at"]
        assert datetime.fromisoformat(requested_at).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z") == requested_at
        assert ui.record(identity)["state"] == "completed"
        assert transport.calls[-1][0] == "GET", "Confirmation needs Gateway readback"
        assert service.client.recommendation_mute().duration == "1_week"
        confirmation = ui.view(identity)
        assert confirmation.title == "Notifications muted"
        assert "one week" in confirmation.summary
        assert "only affects you" in confirmation.summary
        assert confirmation.dismissed and not confirmation.actions
        assert "Gateway" not in confirmation.to_text()
        await ui.click(identity, "settings.mute.1w")
        restarted = surface(tmp_path / "restart", MuteService(transport))
        another = restarted.runtime.begin_candidate(restarted.caller, "candidate-2")["review_id"]
        await restarted.flush()
        await restarted.click(another, "settings")
        assert restarted.record(another)["plan"]["mute_control"]["duration"] == "1_week"
        assert len([call for call in transport.calls if call[0] == "PUT"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_failed_mute_click_replaces_card_with_uncertain_confirmation(tmp_path, surface):
    class FailingMuteService(Service):
        def notification_settings(self, **_kwargs):
            return {"delivery_mode": "agent", "mode_writable": True,
                    "mute_control": {"revision": 7, "muted": False}}

        def choose_notification_mute(self, _revision, _duration, *, may_apply):
            assert may_apply()
            raise ConnectionError("unknown write outcome")

    async def scenario():
        ui = surface(tmp_path, FailingMuteService())
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "settings")
        edit_count = len(ui.edits)
        await ui.click(identity, "settings.mute.1w")
        assert ui.record(identity)["state"] == "uncertain"
        assert len(ui.edits) == edit_count + 1
        assert "Couldn't confirm the change" in unescape(str(ui.edits[-1]))
        assert not ui.view(identity).actions

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("readback", ["unchanged", "stale_revision", "later_revision"])
def test_mute_response_without_persisted_readback_never_confirms(tmp_path, surface, readback):
    class UnconfirmedSession(Session):
        written = False

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "PUT":
                self.written = True
            muted = method == "PUT" or (self.written and readback != "unchanged")
            revision = (8 if method == "PUT" else
                        9 if self.written and readback == "later_revision" else 7)
            return Response(200, {
                "org_id": "org-1", "muted": muted,
                "duration": "1_week" if muted else None,
                "muted_until": "2030-01-08T00:00:00Z" if muted else None,
                "forever": False, "revision": revision,
            })

    class MuteService(Service):
        choose_notification_mute = InvocationService.choose_notification_mute

        def __init__(self, session):
            super().__init__()
            self.client = recording_client(session=session)

        def native_owner(self):
            return owner(organization_id="org-1")

        def notification_settings(self, **_kwargs):
            return {"delivery_mode": "agent", "mode_writable": True,
                    "mute_control": self.client.recommendation_mute().model_dump(mode="json")}

    async def scenario():
        transport = UnconfirmedSession()
        ui = surface(tmp_path, MuteService(transport))
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "settings")
        await ui.click(identity, "settings.mute.1w")
        assert [method for method, _url, _body in transport.calls][-2:] == ["PUT", "GET"]
        assert ui.record(identity)["state"] == "uncertain"
        assert ui.view(identity).title == "Couldn't confirm the change"
        assert not ui.view(identity).actions

    asyncio.run(scenario())


def test_real_local_packaging_failure_reaches_native_card(tmp_path, monkeypatch):
    from test_contribution_review import reviewer  # pylint: disable=import-outside-toplevel
    async def scenario():
        prepared, source, _owner = reviewer(tmp_path / "package")
        (source / "unsafe-link").symlink_to(source / "SKILL.md")
        service = Service()
        monkeypatch.setattr(service, "native_candidate_inspect", lambda identity: {
            "event_id": identity, "facts": {
                **Service.native_candidate_inspect(service, identity)["facts"],
                "name": source.name,
            },
        })
        monkeypatch.setattr(service, "suggest", prepared.prepare, raising=False)
        monkeypatch.setattr(service, "native_candidate_prepare",
                            lambda identity: InvocationService.native_candidate_prepare(
                                service, identity,
                            ))
        with pytest.raises(SharePackagingError):
            service.native_candidate_prepare("candidate-1")
        ui = SlackHarness(tmp_path / "native", service)
        response = ui.runtime.begin_candidate(ui.caller, "candidate-1")
        await ui.flush()
        identity = response["review_id"]
        await ui.click(identity, "share")
        assert ui.edits[-1]["text"].startswith("Share Packaging failed")
        assert "Nothing has been uploaded or published" in ui.edits[-1]["text"]
        assert not ui.view(identity).actions
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


def test_install_invitation_is_one_click_and_details_remain_read_only(tmp_path):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        view = ui.view(identity)
        assert "your organization" in view.title
        assert "Would you like to install this skill?" in view.to_text()
        assert [a.label for a in view.actions] == [
            "View Details", "Not Now", "Mute Skill Recommendations", "Install Skill",
        ]
        assert "Triage incidents" in view.to_text()
        await ui.click(identity, "confirm")
        assert not service.applied
        await ui.click(identity, "details")
        assert ui.view(identity).actions[-1].label == "Looks good."
        await ui.click(identity, "back")
        assert not service.applied
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert ui.view(identity).to_text() == "✅ incident installed"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_native_install_discloses_setup_then_asks_separate_permission(tmp_path, harness):
    class SetupService(Service):
        def install_plan(self, reference, **_kwargs):
            return {**super().install_plan(reference),
                    "setup_commands": ["connect example account"]}

        def install_apply(self, receipt, *, may_apply, **_kwargs):
            assert may_apply()
            self.applied.append(receipt)
            return {"installed": True, "setup_required": True, "ready_to_use": None}

        def install_setup_apply(self, receipt, *, may_apply):
            assert may_apply()
            self.applied.append("setup:" + receipt)
            return {"installed": True, "setup_required": True, "ready_to_use": None,
                    "setup": {"state": "passed", "steps": 1}}

    async def scenario():
        service = SetupService()
        ui = harness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()
        assert "connect example account" in ui.view(identity).to_text()
        assert "choose whether to run setup afterward" in ui.view(identity).to_text()
        await ui.click(identity, "details")
        assert not service.applied
        await ui.click(identity, "back")
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert "files installed; setup incomplete" in ui.view(identity).to_text()
        assert [action.label for action in ui.view(identity).actions] == ["Run setup", "Not now"]
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("decision", ["setup.run", "setup.defer"])
def test_update_with_new_setup_requires_separate_command_permission(
    tmp_path, harness, decision,
):
    class SetupUpdateService(Service):
        def update_plan(self, _skill):
            return {**self.install_plan("incident"), "operation": "update",
                    "setup_commands": ["printf 'new version\\n'"]}

        def update_apply(self, receipt, *, may_apply, **_kwargs):
            assert may_apply()
            self.applied.append(receipt)
            return {"updated": True, "setup_required": True, "ready_to_use": None}

        def update_setup_apply(self, receipt, *, may_apply):
            assert may_apply()
            self.applied.append("setup:" + receipt)
            return {"updated": True, "setup_required": True, "ready_to_use": None,
                    "setup_execution_authorized": True,
                    "setup": {"state": "passed", "steps": 1}}

    async def scenario():
        service = SetupUpdateService()
        ui = harness(tmp_path, service)
        identity = ui.runtime.begin_update(ui.caller, "incident")["review_id"]
        await ui.flush()
        assert "printf 'new version" in ui.view(identity).to_text()
        await ui.click(identity, "details")
        assert not service.applied
        await ui.click(identity, "back")
        await ui.click(identity, "update")
        assert service.applied == ["exact-receipt"]
        view = ui.view(identity)
        assert "files updated; setup incomplete" in view.to_text()
        assert [a.label for a in view.actions] == ["Run setup", "Not now"]
        await ui.click(identity, "update")
        assert service.applied == ["exact-receipt"]
        await ui.click(identity, decision)
        if decision == "setup.run":
            assert service.applied == ["exact-receipt", "setup:exact-receipt"]
            receipt = ui.view(identity).to_text()
            assert "✅ incident updated\n\nSetup ran successfully" in receipt
            assert "Have your agent confirm the skill is ready" in receipt
            assert "setup incomplete" not in receipt
        else:
            assert service.applied == ["exact-receipt"]
            assert "setup incomplete" in ui.view(identity).to_text()
        await ui.click(identity, "setup.run")
        assert service.applied == (["exact-receipt", "setup:exact-receipt"]
                                   if decision == "setup.run" else ["exact-receipt"])
        ui.runtime.close()

    asyncio.run(scenario())


def test_forged_review_and_confirm_actions_have_no_effect(tmp_path):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        await ui.click(identity, "review")
        await ui.click(identity, "confirm")
        assert not service.applied
        assert ui.record(identity)["state"] == "pending"
    asyncio.run(scenario())


def test_failed_result_edit_does_not_erase_a_completed_effect(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        original_edit = ui.chat_update
        async def failed_edit(**kwargs):
            if service.applied:
                raise ConnectionError("provider refused result edit")
            return await original_edit(**kwargs)
        monkeypatch.setattr(ui, "chat_update", failed_edit)
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
    asyncio.run(scenario())


@pytest.mark.parametrize("category,label", [
    ("skill_ready_to_share", "Review proposal"),
    ("new_skill", "Install Skill"),
    ("update_available", "Update Skill"),
])
def test_eligible_notice_reuses_actionable_native_candidate_flow(
    tmp_path, monkeypatch, category, label,
):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        monkeypatch.setattr(service, "update_plan", lambda _skill: {
            **service.install_plan("incident"), "operation": "update",
        }, raising=False)
        await ui.runtime._send_notice(ui.caller, [{  # pylint: disable=protected-access
            "category": category, "event_id": "candidate-1",
            "skill_name": "incident", "skill_id": "incident", "version": 2,
        }])
        await ui.flush()
        assert "available. Use /wisdom" not in ui.posts[-1]["text"]
        controls = [element for block in ui.posts[-1]["blocks"]
                    for element in block.get("elements", []) if element.get("type") == "button"]
        value = next(button["value"] for button in controls if button["text"]["text"] == label)
        identity = value.rsplit(":", 1)[-1]
        action = {
            "skill_ready_to_share": "share", "new_skill": "install",
            "update_available": "update",
        }[category]
        await ui.click(identity, action)
        assert ui.record(identity)["operation"] == {
            "skill_ready_to_share": "publish", "new_skill": "install", "update_available": "update",
        }[category]
        if category == "skill_ready_to_share":
            assert not service.applied
        else:
            assert service.applied == ["exact-receipt"]
    asyncio.run(scenario())


def test_slack_inspection_payload_preserves_literal_whitespace_and_markup(tmp_path, monkeypatch):
    import json  # pylint: disable=import-outside-toplevel
    async def scenario():
        service = Service()
        plan = service.publication_review("draft")
        plan["files"][0]["content_utf8"] = "a" + " " * 4400 + "*not bold* <https://invalid|label>"
        monkeypatch.setattr(service, "publication_review", lambda _draft: plan)
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin_publication(ui.caller, "draft")
        await ui.flush()
        identity = response["review_id"]
        await ui.click(identity, "details")
        chunks = []
        while True:
            sections = [b["text"] for b in ui.edits[-1]["blocks"] if b["type"] == "section"]
            content = sections[-1]
            assert content["type"] == "plain_text"
            assert len(content["text"]) <= 2900
            chunks.append(content["text"].split("\n", 1)[1])
            following = next((a for a in ui.view(identity).actions if a.label == "Next"), None)
            if following is None:
                break
            await ui.click(identity, following.action)
        visible = "".join(chunks)
        assert json.dumps(plan["files"][0]["content_utf8"])[1:-1] in visible
        assert "*not bold* <https://invalid|label>" in visible
        assert "receipt" not in visible
    asyncio.run(scenario())


def test_queued_private_card_is_not_posted_after_account_switch(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        ui.runtime.begin(ui.caller, "incident")
        monkeypatch.setattr(service, "native_owner", lambda: owner(account_id="other"))
        await ui.flush()
        assert not ui.posts
        assert not service.applied
    asyncio.run(scenario())


def test_slack_posted_card_is_cancelled_and_deleted_after_account_switch(
    tmp_path, monkeypatch,
):
    asyncio.run(assert_owner_switch_after_post_is_cleaned(
        monkeypatch, SlackHarness, tmp_path, Service(),
        {"channel": "D1", "ts": "card-1"},
    ))


def test_slack_owner_lookup_failure_and_delete_failure_leave_card_cancelled(
    tmp_path, monkeypatch,
):
    async def scenario():
        service = Service()
        owner_lookup = {"failed": False}

        def native_owner():
            if owner_lookup["failed"]:
                raise ConnectionError("owner unavailable")
            return owner()

        monkeypatch.setattr(service, "native_owner", native_owner)
        ui = SlackHarness(tmp_path, service)
        posted = ui.chat_postMessage

        async def fail_owner_lookup_after_post(**payload):
            response = await posted(**payload)
            owner_lookup["failed"] = True
            return response

        async def failed_delete(**payload):
            ui.deletes.append(payload)
            raise ConnectionError("delete unavailable")

        monkeypatch.setattr(ui, "chat_postMessage", fail_owner_lookup_after_post)
        monkeypatch.setattr(ui, "chat_delete", failed_delete)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()

        assert ui.deletes == [{"channel": "D1", "ts": "card-1"}]
        assert ui.record(identity)["state"] == "cancelled"
        await assert_cancelled_review_is_inert(
            ui, service, identity, lambda: owner_lookup.update(failed=False)
        )

    asyncio.run(scenario())


def test_stale_post_cleanup_does_not_overwrite_newer_review_state(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        lookups = {"count": 0}

        def native_owner():
            lookups["count"] += 1
            if lookups["count"] == 2:
                current = ui.record(identity)
                newer = {
                    **current, "state": "cancelled", "result": {"newer": True},
                    "updated_at": current["updated_at"] + 1,
                }
                assert ui.runtime.reviews.state.compare_replace(current, newer)
                return owner(account_id="other")
            return owner()

        monkeypatch.setattr(service, "native_owner", native_owner)
        await ui.flush()

        assert ui.deletes == [{"channel": "D1", "ts": "card-1"}]
        assert ui.record(identity)["result"] == {"newer": True}
        assert ui.record(identity)["state"] == "cancelled"

    asyncio.run(scenario())


def test_stale_post_with_uncertain_ack_is_cancelled_without_invented_message_id(
    tmp_path, monkeypatch,
):
    async def scenario():
        service = Service()
        current_owner = {"value": owner()}
        monkeypatch.setattr(service, "native_owner", lambda: current_owner["value"])
        ui = SlackHarness(tmp_path, service)

        async def uncertain_post(**payload):
            ui.posts.append(payload)
            current_owner["value"] = owner(account_id="other")
            return {"ok": True, "channel": "D1"}

        monkeypatch.setattr(ui, "chat_postMessage", uncertain_post)
        identity = ui.runtime.begin(ui.caller, "incident")["review_id"]
        await ui.flush()

        assert not ui.deletes
        assert ui.record(identity)["state"] == "cancelled"
        assert ui.record(identity)["message_id"] == ""

    asyncio.run(scenario())


def test_forged_page_jump_cannot_skip_exact_review(tmp_path, monkeypatch):
    from hermes_wisdom_plugin.native_inspection import review_pages  # pylint: disable=import-outside-toplevel

    async def scenario():
        service = Service()
        plan = service.publication_review("local:draft-1")
        plan["files"][0]["content_utf8"] = "complete content " * 1000
        monkeypatch.setattr(service, "publication_review", lambda _identity: plan)
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin_publication(ui.caller, "local:draft-1")["review_id"]
        await ui.flush()
        last = len(review_pages(ui.record(identity)["plan"])) - 1
        assert last > 1
        await ui.click(identity, f"page.{last}")
        await ui.click(identity, "confirm")
        assert not service.applied
        await ui.click(identity, "share")
        await ui.click(identity, f"page.{last}")
        await ui.click(identity, "confirm")
        assert not service.applied
        assert ui.record(identity)["inspected_through"] == 0
    asyncio.run(scenario())
