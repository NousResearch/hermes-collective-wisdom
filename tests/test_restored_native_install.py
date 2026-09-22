"""Native callbacks drive real package review/inspection/application offline."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
# pylint: disable=protected-access,redefined-outer-name,unused-import
import asyncio
import base64
import json

from test_install_execution import invocation_executor
from restoration_support import SlackHarness
from review_test_support import _content
from hermes_wisdom_plugin.runtime import InvocationService


def test_native_inspection_and_install_use_exact_real_package(invocation_executor, tmp_path):
    executor, store, client, _receipts, _plan, target, _authority = invocation_executor
    client.files.extend([
        ("scripts/check.sh", "exec", b"#!/bin/sh\n# inspected, never executed\n"),
        ("asset.bin", "file", b"\x00\xff\x01"),
    ])
    client.version_value["content_hash"] = _content(client.files).content_hash

    class Service:
        def native_owner(self):
            return executor._owner()

        def close(self):
            pass

        def check_owner(self):
            return executor._owner()

        def install_plan(self, reference, **kwargs):
            return executor.reviewer.plan(reference, **kwargs)

        def native_install_inspect(self, receipt):
            return InvocationService._native_package_inspect(
                self, executor.reviewer, receipt, "Install",
            )

        def install_apply(self, receipt, **kwargs):
            return executor.apply(receipt, **kwargs)

    async def scenario():
        ui = SlackHarness(tmp_path / "native", Service())
        response = ui.runtime.begin(ui.caller, "skill-1@v2")
        await ui.flush()
        identity = response["review_id"]
        original = ui.record(identity)["plan"]
        await ui.click(identity, "details")
        chunks = []
        while True:
            view = ui.view(identity)
            chunks.append(view.items[0].detail)
            following = next((a for a in view.actions if a.label == "Next"), None)
            if following is None:
                break
            await ui.click(identity, following.action)
        inspected = json.loads("".join(chunks))
        assert inspected["review"] == original
        assert "never executed" in str(inspected["package_files"])
        assert base64.b64encode(b"\x00\xff\x01").decode() in str(inspected["package_files"])
        assert ui.record(identity)["plan"] == original
        assert not target.exists()
        assert not client.record_calls
        await ui.click(identity, "back")
        await ui.click(identity, "install")
        assert ui.record(identity)["state"] == "completed"
        for path, _mode, data in client.files:
            assert (target / path).read_bytes() == data
        assert len(client.record_calls) == 1
        assert store.installation("skill-1")["content_hash"] == original["content_hash"]
        assert ui.view(identity).to_text() == "✅ managed-skill installed"
        await ui.click(identity, "install")
        assert len(client.record_calls) == 1
    asyncio.run(scenario())
