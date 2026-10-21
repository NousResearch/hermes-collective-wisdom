"""Setup commands too long for one card are paged in the chat; Run setup needs every page."""
# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_native_paged_findings import _controls, _labels
from test_restored_native_flow import Service, _clean_install_plan

COMMANDS = [f"printf 'step {number:02d} " + "x" * 220 + "'" for number in range(1, 16)]


class LongSetup(Service):
    def install_plan(self, reference, **_kwargs):
        return {**_clean_install_plan(self, reference), "setup_commands": list(COMMANDS)}

    def install_apply(self, receipt, *, may_apply, **_kwargs):
        return self._effect(receipt, may_apply)

    def install_setup_apply(self, receipt, *, may_apply):
        return {**self._effect("setup:" + receipt, may_apply),
                "setup": {"state": "passed", "steps": len(COMMANDS)}}

    def _effect(self, applied, may_apply):
        assert may_apply()
        self.applied.append(applied)
        return {"installed": True, "setup_required": True, "ready_to_use": None}


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_long_setup_is_paged_then_runs_once(tmp_path, surface):
    async def scenario():
        service = LongSetup()
        ui = surface(tmp_path, service)
        queued = await asyncio.to_thread(ui.runtime.install_direct, ui.caller, "incident")
        await ui.flush()
        identity = queued["review_id"]
        assert "Install Skill" in _labels(ui, ui.posts[-1])
        assert "15 commands. You see every command" in ui.view(identity).to_text()
        await ui.click(identity, "install")
        assert service.applied == ["exact-receipt"]

        card = ui.view(identity)
        assert card.title == "Setup needed" and "terminal" not in card.to_text().lower()
        assert _controls(card) == [("Not now", "setup.defer"), ("View all commands", "paged.0")]
        assert "View all commands lists all 15 commands." in card.notice
        assert "Run setup" not in _labels(ui, ui.edits[-1])
        await ui.click(identity, "setup.run")  # forged before the commands were shown
        assert service.applied == ["exact-receipt"]

        await ui.click(identity, "paged.0")
        pages = []
        while True:
            view = ui.view(identity)
            pages.append(view.items[0].detail)
            if not any(action.label == "Next" for action in view.actions):
                break
            assert "Run setup" not in _labels(ui, ui.edits[-1])
            await ui.next_page(identity)
        assert len(pages) >= 3 and "\n".join(pages) == "\n".join(COMMANDS)
        assert "Run setup" in _labels(ui, ui.edits[-1])
        await ui.click(identity, "paged.back")
        assert _controls(ui.view(identity))[0] == ("Run setup", "setup.run")
        assert "Run setup" in _labels(ui, ui.edits[-1])

        await ui.click(identity, "setup.run")
        await ui.click(identity, "setup.run")
        assert service.applied == ["exact-receipt", "setup:exact-receipt"]
        ui.runtime.close()
    asyncio.run(scenario())
