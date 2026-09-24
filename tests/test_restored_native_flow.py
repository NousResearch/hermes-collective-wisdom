"""Restored journeys through real registered native callbacks; no live services."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
import asyncio
import sys
from datetime import datetime
from html import unescape
from types import ModuleType, SimpleNamespace

import pytest

from native_test_support import owner
from restoration_support import (
    SlackHarness, TelegramHarness, assert_cancelled_review_is_inert,
    assert_owner_switch_after_post_is_cleaned,
)
from test_client import Response, Session, client as recording_client
from hermes_wisdom_plugin.errors import SharePackagingError
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


class Service:
    def __init__(self):
        self.applied = []

    def native_owner(self):
        return owner()

    def close(self):
        return None

    def native_candidate_inspect(self, identity):
        return {"event_id": identity, "facts": {"name": "incident"}, "networked": False}

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


def test_candidate_invitation_has_actionable_ordered_controls(tmp_path):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin_candidate(ui.caller, "candidate-1")
        await ui.flush()
        identity = response["review_id"]
        view = ui.view(identity)
        assert view.title == "You created a skill that could help your team"
        assert [action.label for action in view.actions] == [
            "Review checks", "View More Details", "Snooze Collective Wisdom",
            "Maybe Later", "Share My Skill",
        ]
        assert "Safe To Share" not in view.to_text()
        await ui.click(identity, "share")
        assert ui.record(identity)["operation"] == "publish"
        assert ui.record(identity)["plan"]["receipt"] == "publication-receipt"
        assert not service.applied
        await ui.click(identity, "back")
        await ui.click(identity, "cancel")
        assert not ui.view(identity).actions
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
        assert json.loads("".join(chunks)) == {
            key: value for key, value in exact.items() if key not in {"draft_id", "receipt"}
        }
        assert ui.view(identity).actions[-1].label == "Looks good."
        await ui.click(identity, "back")
        assert "Share My Skill" in [a.label for a in ui.view(identity).actions]
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
        assert (
            "✅ Safe To Share (security and professionalism checks all passed)"
            in ui.view(identity).to_text()
        )
        assert ui.view(identity).actions[0].label == "✅ Safe To Share"
        await ui.click(identity, "checks")
        expanded = ui.view(identity).to_text()
        assert "✅ Safe To Share" in expanded
        assert "  Private keys: pass" in expanded
        assert "  Profanity or abusive language: pass" in expanded
        await ui.click(identity, "back")
        assert not service.applied
        for status in ("pending", "unavailable", "blocked"):
            exact["security_check"]["checks"][-1]["status"] = status
            next_review = ui.runtime.begin_publication(ui.caller, "local:draft-1")
            await ui.flush()
            view = ui.view(next_review["review_id"])
            assert "Safe To Share" not in view.to_text()
            assert view.actions[0].label == "Review checks"
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
            "event_id": identity, "facts": {"name": "reviewed-skill"},
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
            "View Details", "Not Now", "Mute Skill Recommendations", "Install",
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
        async def failed_edit(**_kwargs):
            raise ConnectionError("provider refused result edit")
        monkeypatch.setattr(ui, "chat_update", failed_edit)
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
    asyncio.run(scenario())


@pytest.mark.parametrize("category,label", [
    ("skill_ready_to_share", "Share My Skill"),
    ("new_skill", "Install"),
    ("update_available", "Update"),
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
        assert json.loads("".join(chunks)) == {
            key: value for key, value in plan.items() if key not in {"draft_id", "receipt"}
        }
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
