"""Native progress edge cases on registered callbacks."""
# pylint: disable=missing-function-docstring,protected-access,duplicate-code
# pylint: disable=too-many-locals
import asyncio
import threading

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_native_progress import NamedService
from hermes_wisdom_plugin import native_progress, native_runtime


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_blocked_progress_owner_keeps_loop_live_and_times_out(tmp_path, harness, monkeypatch):
    async def scenario():
        service = NamedService()
        ui = harness(tmp_path, service)
        record = ui.runtime.reviews.request_install(service, ui.caller, "incident@v2")
        identity = record["id"]
        ui.runtime.reviews.bind_message(
            identity, ui.caller, "card-1" if harness is SlackHarness else "123",
        )
        entered, release = threading.Event(), threading.Event()
        closed = threading.Event()
        original_owner = service.native_owner
        original_close = service.close
        def close():
            original_close()
            closed.set()
        service.close = close
        original_act = ui.runtime.reviews.act

        def block_on_progress(*args, **kwargs):
            progress = kwargs["progress"]

            def wrapped(*progress_args):
                def blocking_owner():
                    entered.set()
                    release.wait()
                    return original_owner()

                service.native_owner = blocking_owner
                return progress(*progress_args)

            return original_act(*args, **{**kwargs, "progress": wrapped})

        ui.runtime.reviews.act = block_on_progress
        monkeypatch.setattr(native_progress, "PROGRESS_TIMEOUT", 0.03)
        monkeypatch.setattr(native_runtime, "PROGRESS_TIMEOUT", 0.03)
        timer = threading.Timer(0.25, release.set)
        timer.start()
        try:
            started = asyncio.get_running_loop().time()
            click = asyncio.create_task(ui.click(identity, "install"))
            assert await asyncio.to_thread(entered.wait, 0.15)
            await asyncio.sleep(0.005)  # Adapter heartbeat while host auth is blocked.
            assert not release.is_set() and not click.done()
            await asyncio.wait_for(click, timeout=1)
            elapsed = asyncio.get_running_loop().time() - started
            assert entered.is_set()
            assert elapsed < 0.15, "native loop stalled on host owner lookup"
            assert not service.applied and not ui.edits
        finally:
            release.set()
            timer.join()
        assert await asyncio.to_thread(closed.wait, 1)
        assert ui.record(identity)["state"] == "failed"
        assert not service.applied and not ui.edits

    asyncio.run(scenario())


@pytest.mark.parametrize("harness", [SlackHarness, TelegramHarness])
def test_catalog_click_progress_on_registered_callback(tmp_path, harness):
    async def scenario():
        service = NamedService()
        service.show = lambda _target: {
            "skill": {"id": "incident", "slug": "Incident response", "state": "active"},
            "latest_version_detail": {"version": {"version": 2}},
            "installation_status": {"text": "Not installed", "state": "not_installed"},
        }
        ui = harness(tmp_path, service)
        identity = ui.runtime.begin_catalog(ui.caller, "show", "incident")["review_id"]
        await ui.flush()
        await ui.click(identity, "catalog.install.99")
        assert not service.applied and not ui.edits
        original = ui.chat_update
        async def observe(**payload):
            if not service.applied:
                claimed = ui.record(identity)
                assert claimed["operation"] == "install"
                assert claimed["plan"]["skill_id"] == "incident"
                assert claimed["plan"]["version"] == 2
                assert claimed["state"] == "preparing"
                await ui.click(identity, "catalog.install.0")
                assert not service.applied
            return await original(**payload)
        ui.chat_update = observe
        await ui.click(identity, "catalog.install.0")
        assert service.applied == ["exact-receipt"]
        assert len(ui.edits) == 2
        assert "Installing Incident response" in ui.edits[0]["text"]
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "catalog.install.0")
        assert len(ui.edits) == 2 and service.applied == ["exact-receipt"]

    asyncio.run(scenario())
