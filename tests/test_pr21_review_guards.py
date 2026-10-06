"""Regression guards for PR21 consent and preparation fixes (offline only)."""
# pylint: disable=missing-function-docstring,protected-access
import asyncio
import threading

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_restored_native_flow import Service
from hermes_wisdom_plugin.errors import WisdomConflict


def test_binding_does_not_overwrite_a_newer_pending_presentation(tmp_path, monkeypatch):
    ui = SlackHarness(tmp_path, Service())
    controller = ui.runtime.reviews
    record = controller.request_candidate(Service(), ui.caller, "candidate-1")
    controller.state.save({**record, "screen": "checks"})
    monkeypatch.setattr(controller, "load", lambda _identity: record)
    with pytest.raises(WisdomConflict):
        controller.bind_message(record["id"], ui.caller, "card-1")
    assert controller.state.get(record["id"])["screen"] == "checks"


def test_catalog_selection_does_not_overwrite_a_newer_pending_plan(tmp_path, monkeypatch):
    service = Service()
    monkeypatch.setattr(service, "search_skills", lambda _q: [{
        "id": "skill-1", "installation_status": {"state": "not_installed", "text": "Not installed"},
    }], raising=False)
    ui = SlackHarness(tmp_path, service)
    controller = ui.runtime.reviews
    record = controller.request_catalog(service, ui.caller, "browse")
    record = controller.bind_message(record["id"], ui.caller, "card-1")
    newer = {**record, "plan": {**record["plan"], "target": "new search"}}
    original = service.install_plan
    def plan(reference):
        controller.state.save(newer)
        return original(reference)
    monkeypatch.setattr(service, "install_plan", plan)
    with pytest.raises(WisdomConflict):
        controller.act(record["id"], "catalog.install.0", service, ui.clicked())
    assert controller.load(record["id"]) == newer


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("fails", [False, True])
def test_cancel_and_duplicate_share_during_preparation(tmp_path, monkeypatch, surface, fails):
    async def scenario():
        service = Service()
        started, release = threading.Event(), threading.Event()
        calls = []
        def prepare(identity):
            del identity
            calls.append(True)
            started.set()
            assert release.wait(10)
            if fails:
                raise ConnectionError("preparation failed after dismissal")
            return "local:draft-1"
        monkeypatch.setattr(service, "native_candidate_prepare", lambda identity: (
            _ for _ in ()).throw(ConnectionError("first preparation unavailable")))
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        monkeypatch.setattr(service, "native_candidate_prepare", prepare)
        pending = asyncio.create_task(ui.click(identity, "share"))
        assert await asyncio.to_thread(started.wait, 5)
        await ui.click(identity, "share")
        await ui.click(identity, "cancel")
        release.set()
        await pending
        assert calls == [True]
        assert ui.record(identity)["state"] == "cancelled"
        assert not ui.view(identity).actions
        assert not service.applied
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("stage", ["native_candidate_prepare", "publication_review"])
def test_preparation_error_is_visible_retryable_and_not_packaging(
    tmp_path, monkeypatch, surface, stage,
):
    async def scenario():
        service = Service()
        original = getattr(service, stage)
        def fail(_identity):
            raise ConnectionError("private diagnostic must not be displayed")
        monkeypatch.setattr(service, stage, fail)
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        await ui.click(identity, "share")
        assert ui.record(identity)["state"] == "pending"
        text = ui.edits[-1]["text"]
        assert "Share preparation failed" in text
        assert "Packaging" not in text and "private diagnostic" not in text
        assert {a.action for a in ui.view(identity).actions} == {"share", "cancel"}
        assert not service.applied
        monkeypatch.setattr(service, stage, original)
        await ui.click(identity, "share")
        assert ui.record(identity)["operation"] == "publish"
        assert not ui.record(identity).get("failure")
        await ui.click(identity, "back")
        await ui.click(identity, "cancel")
        assert ui.record(identity)["state"] == "cancelled"
    asyncio.run(scenario())


@pytest.mark.usefixtures("full_release_scope")
@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("fails", [False, True])
def test_settings_ack_fences_effects_and_preserves_completed_result(
    tmp_path, monkeypatch, surface, fails,
):
    async def scenario():
        service = Service()
        changes = []
        monkeypatch.setattr(service, "notification_settings", lambda **_kw: {
            "delivery_mode": "agent", "mode_writable": True, "mute_control": {"revision": 7},
        }, raising=False)
        def mute(_revision, _duration, *, may_apply):
            assert may_apply()
            changes.append(True)
            return {"muted": True}
        monkeypatch.setattr(service, "choose_notification_mute", mute, raising=False)
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
        await ui.flush()
        started, release = asyncio.Event(), asyncio.Event()
        original = ui.chat_update
        async def edit(**kwargs):
            if "State: completed" in kwargs["text"]:
                raise ConnectionError("result display failed")
            started.set()
            await release.wait()
            if fails:
                raise ConnectionError("settings display failed")
            return await original(**kwargs)
        monkeypatch.setattr(ui, "chat_update", edit)
        pending = asyncio.create_task(ui.click(identity, "settings"))
        await asyncio.wait_for(started.wait(), 5)
        await ui.click(identity, "settings.mute.1w")
        assert not changes
        release.set()
        await pending
        await ui.click(identity, "settings.mute.1w")
        assert changes == ([] if fails else [True])
        assert ui.record(identity)["state"] == ("failed" if fails else "completed")
        await ui.click(identity, "settings.mute.1w")
        assert changes == ([] if fails else [True])
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("operation", ["install", "update"])
@pytest.mark.parametrize("damage", ["missing", "empty", "receipt"])
def test_confirmation_independently_requires_receipt_bound_inspection(
    tmp_path, monkeypatch, surface, operation, damage,
):
    async def scenario():
        service = Service()
        monkeypatch.setattr(service, f"native_{operation}_inspect", lambda _r: [
            {"path": "SKILL.md", "content": "exact bytes"},
        ], raising=False)
        monkeypatch.setattr(service, "update_plan", lambda _s: {
            **service.install_plan("skill"), "operation": "update",
            "files": [{"path": "SKILL.md", "hash": "verified-by-service"}],
        }, raising=False)
        monkeypatch.setattr(service, "update_apply", service.install_apply, raising=False)
        ui = surface(tmp_path, service)
        begin = ui.runtime.begin if operation == "install" else ui.runtime.begin_update
        identity = begin(ui.caller, "skill")["review_id"]
        await ui.flush()
        await ui.click(identity, "review")
        while await ui.next_page(identity):
            pass
        record = ui.record(identity)
        # Corrupt only the persisted disclosure witness, not the effect receipt.
        if damage == "missing":
            record.pop("package_files", None)
        elif damage == "empty":
            record["package_files"] = []
        else:
            record["inspection_receipt"] = "other-receipt"
        from hermes_wisdom_plugin.native_inspection import inspection_pages  # pylint: disable=import-outside-toplevel
        record.update(page=len(inspection_pages(record)) - 1,
                      inspected_through=len(inspection_pages(record)) - 1)
        ui.runtime.reviews.state.save(record)
        await ui.click(identity, "confirm")
        assert not service.applied
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("operation,action", [("install", "share"), ("update", "share"),
                                             ("publish", "review"), ("install", "checks")])
def test_unoffered_operation_actions_do_not_change_review(
    tmp_path, monkeypatch, surface, operation, action,
):
    async def scenario():
        service = Service()
        monkeypatch.setattr(service, "update_plan", lambda _s: {
            **service.install_plan("skill"), "operation": "update",
        }, raising=False)
        ui = surface(tmp_path, service)
        begin = {"install": ui.runtime.begin, "update": ui.runtime.begin_update,
                 "publish": ui.runtime.begin_publication}[operation]
        identity = begin(ui.caller, "skill")["review_id"]
        await ui.flush()
        before = ui.record(identity)
        await ui.click(identity, action)
        assert ui.record(identity) == before
        assert not service.applied
    asyncio.run(scenario())
