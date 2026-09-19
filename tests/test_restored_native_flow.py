"""Restored journeys through real registered native callbacks; no live services."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
import asyncio

import pytest

from native_test_support import owner
from restoration_support import SlackHarness
from hermes_wisdom_plugin.errors import SharePackagingError


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

    def publication_review(self, draft_id):
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
        return {
            "operation": "install", "skill_id": reference, "receipt": "exact-receipt",
            "allowed": True, "description": "Triage incidents", "version": 2,
        }

    def install_apply(self, receipt, *, may_apply, **_kwargs):
        assert may_apply()
        self.applied.append(receipt)
        return {"installed": True}


def test_registered_dismissal_preserves_body_and_revokes_consent(tmp_path):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        original = ui.posts[-1]["text"]
        await ui.click(identity, "cancel")
        assert ui.edits[-1]["text"] == original
        assert not any(block["type"] == "actions" for block in ui.edits[-1]["blocks"])
        assert len(ui.posts) == 1
        await ui.click(identity, "confirm")
        assert not service.applied
        assert ui.record(identity)["state"] == "cancelled"
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
        assert json.loads("".join(chunks)) == exact
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
        assert "sharing and installation" in ui.view(identity).to_text()
        assert not changed
        assert not service.applied
    asyncio.run(scenario())


def test_real_local_packaging_failure_reaches_native_card(tmp_path, monkeypatch):
    from test_contribution_review import reviewer  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.runtime import InvocationService  # pylint: disable=import-outside-toplevel
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


def test_install_invitation_leads_to_exact_review_not_implicit_consent(tmp_path):
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
        await ui.click(identity, "review")
        while True:
            action = next((a for a in ui.view(identity).actions if a.label == "Next"), None)
            if action is None:
                break
            await ui.click(identity, action.action)
        assert not service.applied
        await ui.click(identity, "confirm")
        assert service.applied == ["exact-receipt"]
    asyncio.run(scenario())


def test_failed_provider_edit_cannot_leave_unseen_confirmation_usable(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        async def failed_edit(**_kwargs):
            raise ConnectionError("provider refused edit")
        monkeypatch.setattr(ui, "chat_update", failed_edit)
        await ui.click(identity, "review")
        await ui.click(identity, "confirm")
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


def test_failed_result_edit_does_not_erase_a_completed_effect(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        ui = SlackHarness(tmp_path, service)
        response = ui.runtime.begin(ui.caller, "incident")
        await ui.flush()
        identity = response["review_id"]
        await ui.click(identity, "review")
        async def failed_edit(**_kwargs):
            raise ConnectionError("provider refused result edit")
        monkeypatch.setattr(ui, "chat_update", failed_edit)
        await ui.click(identity, "confirm")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "confirm")
        assert service.applied == ["exact-receipt"]
    asyncio.run(scenario())
