"""HTTP confirmation exercises real receipts, files, SQLite, and replay handling."""

# The transport fixture acknowledges a disposable install; no production calls.
# pylint: disable=missing-function-docstring,too-few-public-methods,protected-access
# pylint: disable=redefined-outer-name,unused-import

import asyncio

import httpx
from fastapi import FastAPI

from test_install_execution import invocation_executor

from hermes_wisdom_plugin.desktop_api import build_router


def test_desktop_install_confirm_writes_reviewed_files_once(  # pylint: disable=too-many-locals
    invocation_executor,
):
    executor, store, client, _receipts, _plan, target, _authority = invocation_executor

    class Service:
        """Use the existing real install kernel through its actual public methods."""

        profile = ("default", executor.host.profile_home)

        def native_owner(self):
            return executor._owner()

        def install_plan(
            self, reference, *, update_mode=None, accept_warnings=False
        ):
            return executor.reviewer.plan(
                reference,
                update_mode=update_mode,
                accept_warnings=accept_warnings,
            )

        def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
            return executor.apply(
                receipt, accept_partial=accept_partial, may_apply=may_apply
            )

    app = FastAPI()
    app.include_router(build_router(Service))
    def request(path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                return await http.post(path, **kwargs)
        return asyncio.run(send())

    opened = request("/views")
    assert opened.status_code == 200
    view_id = opened.json()["view_id"]
    reviewed = request("/install/review", json={
        "view_id": view_id, "reference": "skill-1@v2", "update_mode": "MANUAL",
    })
    assert reviewed.status_code == 200
    review = reviewed.json()
    assert review["compatibility"]["outcome"] == "compatible"
    assert review["server_checks"]["security_check"]["state"] == "passed"
    assert not target.exists()
    assert not client.record_calls

    payload = {"view_id": view_id, "receipt": review["receipt"]}
    first = request("/install/confirm", json=payload)
    assert first.status_code == 200, first.text
    for path, _mode, data in client.files:
        assert (target / path).read_bytes() == data
    assert store.installation("skill-1")["content_hash"] == review["content_hash"]
    assert len(client.record_calls) == 1

    replay = request("/install/confirm", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(client.record_calls) == 1
