"""In-card progress on the real registered native callback paths."""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access
import asyncio
import threading

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_restored_native_flow import Service as SharingService
from test_slack_sdk_callbacks import Service, _record_id
from native_test_support import owner
from hermes_wisdom_plugin import native_progress
from hermes_wisdom_plugin.native_review import callback_value


class NamedService(Service):
    def install_plan(self, reference, **kwargs):
        return {**super().install_plan(reference, **kwargs), "slug": "Incident response"}

    def update_plan(self, _skill_id, **_kwargs):
        return {**self.install_plan("incident@v2"), "operation": "update"}

    native_update_inspect = Service.native_install_inspect

    def update_apply(self, receipt, *, may_apply, **_kwargs):
        return self.install_apply(receipt, may_apply=may_apply)


async def sharing_card(ui):
    identity = ui.runtime.begin_candidate(ui.caller, "candidate-1")["review_id"]
    await ui.flush()
    await ui.click(identity, "share")
    while await ui.next_page(identity):
        pass
    assert any(action.label == "Share My Skill" for action in ui.view(identity).actions)
    return identity


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_share_progress_precedes_publication_and_removes_action(tmp_path, harness):
    async def scenario():
        class Moderated(SharingService):
            def publication_submit(self, receipt, *, may_apply):
                super().publication_submit(receipt, may_apply=may_apply)
                return {"state": "done", "publication_state": "pending_moderation"}
        service = Moderated()
        ui = harness(tmp_path, service)
        identity = await sharing_card(ui)
        before = len(ui.edits)
        observed = []
        original = ui.chat_update
        async def observe(**payload):
            if "Sharing…" in payload["text"]:
                observed.append(True)
                assert not service.applied
                assert ui.record(identity)["state"] == "applying"
                assert "Share My Skill" not in str(payload)
                assert not any(block.get("elements") for block in payload.get("blocks", []))
                assert "wi:agent:" not in str(payload.get("reply_markup", ""))
                await ui.click(identity, "confirm")
                assert not service.applied
            return await original(**payload)
        ui.chat_update = observe
        await ui.click(identity, "confirm")
        assert service.applied == ["publication-receipt"]
        assert ui.record(identity)["state"] == "completed"
        assert observed and len(ui.edits) == before + 2
        assert "Sharing…" not in ui.edits[-1]["text"]
        assert "Waiting for team review" in ui.edits[-1]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("failure", ["edit", "rejected", "uncertain"])
def test_share_progress_failure_and_publication_outcome(tmp_path, harness, failure):
    class Submission(SharingService):
        def publication_submit(self, receipt, *, may_apply):
            if failure == "uncertain":
                assert may_apply()
                self.applied.append(receipt)
                raise ConnectionError("response lost after submission")
            super().publication_submit(receipt, may_apply=may_apply)
            return {"state": "rejected", "phase": "rejected",
                    "detail": "rejected before creation"}

    async def scenario():
        service = Submission()
        ui = harness(tmp_path, service)
        identity = await sharing_card(ui)
        before = len(ui.edits)
        if failure == "edit":
            async def broken(**_payload):
                raise ConnectionError("display failed")
            ui.chat_update = broken
        await ui.click(identity, "confirm")
        assert service.applied == ([] if failure == "edit" else ["publication-receipt"])
        expected = {"edit": "failed", "uncertain": "uncertain", "rejected": "completed"}
        assert ui.record(identity)["state"] == expected[failure]
        if failure != "edit":
            assert len(ui.edits) == before + 2
            assert ("Uncertain" if failure == "uncertain" else "Failed") in ui.edits[-1]["text"]
            assert "Share My Skill" not in str(ui.edits[-1])
        await ui.click(identity, "confirm")
        assert service.applied == ([] if failure == "edit" else ["publication-receipt"])
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_share_hung_progress_is_bounded_without_publication(tmp_path, harness, monkeypatch):
    async def scenario():
        service = SharingService()
        ui = harness(tmp_path, service)
        identity = await sharing_card(ui)
        async def hanging(**_payload):
            await asyncio.Event().wait()
        ui.chat_update = hanging
        monkeypatch.setattr(native_progress, "PROGRESS_TIMEOUT", 0.02)
        await asyncio.wait_for(ui.click(identity, "confirm"), timeout=1)
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_share_slow_authorized_publication_is_not_abandoned(tmp_path, harness, monkeypatch):
    class Slow(SharingService):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def publication_submit(self, receipt, *, may_apply):
            self.started.set()
            assert self.release.wait(1)
            return super().publication_submit(receipt, may_apply=may_apply)

    async def scenario():
        service = Slow()
        ui = harness(tmp_path, service)
        identity = await sharing_card(ui)
        monkeypatch.setattr(native_progress, "PROGRESS_TIMEOUT", 0.02)
        from hermes_wisdom_plugin import native_runtime  # pylint: disable=import-outside-toplevel
        monkeypatch.setattr(native_runtime, "PROGRESS_TIMEOUT", 0.02)
        click = asyncio.create_task(ui.click(identity, "confirm"))
        try:
            assert await asyncio.to_thread(service.started.wait, 0.5)
            await asyncio.sleep(0.08)
            assert not click.done() and not service.applied
            assert ui.record(identity)["state"] == "applying"
        finally:
            service.release.set()
        await asyncio.wait_for(click, 1)
        assert service.applied == ["publication-receipt"]
        assert ui.record(identity)["state"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_install_progress_precedes_effect_and_disables_duplicate(tmp_path, harness):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        if harness is SlackHarness:
            await ui.runtime._send_notice(ui.caller, [{
                "category": "new_skill", "skill_id": "incident", "version": 2,
            }])
            identity = _record_id(ui.posts[-1], "Install Skill")
        else:
            record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
            identity = record["id"]
            ui.runtime.reviews.bind_message(identity, ui.caller, "123")

        original = ui.chat_update
        async def observe(**payload):
            if not service.applied:
                assert "Installing Incident response" in payload["text"]
                assert ui.record(identity)["state"] == "preparing"
                assert not any(block.get("elements") for block in payload.get("blocks", []))
                assert "wi:agent:" not in str(payload.get("reply_markup", ""))
                await ui.click(identity, "install")
                assert not service.applied
            return await original(**payload)
        ui.chat_update = observe
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        assert len(ui.edits) == 2
        assert "Installing" not in ui.edits[-1]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_progress_edit_failure_does_not_run_effect(tmp_path, harness):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        async def broken(**_payload):
            raise ConnectionError("display failed")
        ui.chat_update = broken
        await ui.click(identity, "install")
        await ui.click(identity, "install")
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_update_progress_and_stale_action(tmp_path, harness):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_update(service, ui.caller, "incident")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        await ui.click(identity, "install")
        assert not ui.edits and not service.applied
        assert ui.record(identity)["state"] == "pending"
        original = ui.chat_update
        async def observe(**payload):
            if not service.applied:
                assert "Updating Incident response" in payload["text"]
                assert ui.record(identity)["state"] == "preparing"
                assert not any(block.get("elements") for block in payload.get("blocks", []))
                assert "wi:agent:" not in str(payload.get("reply_markup", ""))
                await ui.click(identity, "update")
                assert not service.applied
            return await original(**payload)
        ui.chat_update = observe
        await ui.click(identity, "update")
        assert service.applied == ["exact-receipt"]
        assert ui.record(identity)["state"] == "completed"
        assert len(ui.edits) == 2
        assert "Updating" not in ui.edits[-1]["text"]
    asyncio.run(scenario())


def test_slack_rejected_progress_response_does_not_apply(tmp_path):
    async def scenario():
        service = NamedService()
        ui = SlackHarness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(identity, ui.caller, "card-1")
        async def rejected(**_payload):
            return {"ok": False, "error": "not_found"}
        ui.chat_update = rejected
        await ui.click(identity, "install")
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


def test_telegram_not_modified_progress_is_not_confirmed(tmp_path):
    async def scenario():
        service = NamedService()
        ui = TelegramHarness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(identity, ui.caller, "123")
        async def unchanged(**_payload):
            raise ValueError("Message is not modified")
        ui.chat_update = unchanged
        await ui.click(identity, "install")
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


def test_slack_failed_ack_never_claims_or_edits(tmp_path):
    async def scenario():
        service = NamedService()
        ui = SlackHarness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(identity, ui.caller, "card-1")
        async def failed_ack():
            raise ConnectionError("ack failed")
        assert callable(ui.handler)
        with pytest.raises(ConnectionError):
            await ui.handler(failed_ack, {}, {"value": callback_value(identity, "install")})
        assert not service.applied and not ui.edits
        assert ui.record(identity)["state"] == "pending"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_preparation_failure_replaces_progress_with_honest_terminal_card(tmp_path, harness):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        def fail(reference, **_kwargs):
            del reference
            raise ValueError("fresh preparation failed")
        service.install_plan = fail
        await ui.click(identity, "install")
        assert not service.applied
        assert ui.record(identity)["state"] == "failed"
        assert len(ui.edits) == 2
        assert "Installing" in ui.edits[0]["text"]
        assert "no files" in ui.edits[-1]["text"].lower()
        assert "Installing" not in ui.edits[-1]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("change", ["lease", "owner", "record"])
def test_change_before_progress_never_edits(tmp_path, harness, change):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        original = ui.runtime.reviews.act
        def revoke(*args, **kwargs):
            progress = kwargs["progress"]
            def before_edit(*progress_args):
                if change == "lease":
                    ui.runtime._leases[(ui.caller.platform, id(ui.adapter))] = object()
                elif change == "owner":
                    service.native_owner = lambda: owner(account_id="other")
                else:
                    claimed = ui.record(identity)
                    ui.runtime.reviews.state.compare_replace(
                        claimed, {**claimed, "state": "cancelled"},
                    )
                return progress(*progress_args)
            return original(*args, **{**kwargs, "progress": before_edit})
        ui.runtime.reviews.act = revoke
        await ui.click(identity, "install")
        assert not service.applied and not ui.edits
        assert ui.record(identity)["state"] == ("cancelled" if change == "record" else "failed")
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_owner_change_during_progress_prevents_effect_and_final_edit(tmp_path, harness):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        original = ui.chat_update
        async def change_owner(**payload):
            response = await original(**payload)
            service.native_owner = lambda: owner(account_id="other")
            return response
        ui.chat_update = change_owner
        await ui.click(identity, "install")
        assert not service.applied and len(ui.edits) == 1
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_hung_progress_edit_is_bounded_and_cannot_apply(tmp_path, harness, monkeypatch):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        started = asyncio.Event()
        async def hanging(**_payload):
            started.set()
            await asyncio.Event().wait()
        ui.chat_update = hanging
        monkeypatch.setattr(native_progress, "PROGRESS_TIMEOUT", 0.02)
        await asyncio.wait_for(ui.click(identity, "install"), timeout=1)
        assert started.is_set()
        assert not service.applied and not ui.edits
        assert ui.record(identity)["state"] == "failed"
    asyncio.run(scenario())
