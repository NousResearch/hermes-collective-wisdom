"""Restored journeys through real registered native callbacks; no live services."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
import asyncio

from native_test_support import owner
from restoration_support import SlackHarness


class Service:
    def __init__(self):
        self.applied = []

    def native_owner(self):
        return owner()

    def close(self):
        return None

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
