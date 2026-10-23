"""/wisdom uninstall opens an owner-bound confirmation card in Slack and Telegram DMs."""
# pylint: disable=missing-function-docstring,redefined-outer-name,unused-import
# pylint: disable=protected-access,missing-class-docstring

from __future__ import annotations

import asyncio
import shutil

import pytest

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_commands import _native_invocation
from test_install_execution import invocation_executor  # noqa: F401  (fixture)
from test_removed_outside_wisdom import _installed, _record

from hermes_wisdom_plugin.commands import run_slash
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.runtime import CommandRuntime


class _UninstallService:
    """The real executor behind the native uninstall card."""

    def __init__(self, executor):
        self.executor, self.applied = executor, 0

    def native_owner(self):
        return self.executor._owner()

    def list_installations(self):
        return self.executor.installed()

    def resolve_managed_skill(self, reference):
        for row in self.list_installations():
            if row.get("slug") == reference and row.get("state") in {"active", "removed"}:
                return row["skill_id"]
        raise WisdomNotFound("Managed skill not found")

    def uninstall(self, skill_id, *, may_apply):
        self.applied += 1
        return self.executor.uninstall(skill_id, may_apply=may_apply)

    @staticmethod
    def close():
        return None


def _card(ui):
    identity = ui.runtime.begin_uninstall(ui.caller, "managed-skill")["review_id"]
    return identity


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_dm_uninstall_card_confirms_once(invocation_executor, tmp_path, surface):
    executor, store, target = _installed(invocation_executor)
    service = _UninstallService(executor)

    async def scenario():
        ui = surface(tmp_path / "native", service)
        identity = _card(ui)
        await ui.flush()
        posted = str(ui.posts[-1])
        assert "Uninstall skill" in posted and "managed-skill" in posted and "v2" in posted
        assert "recoverable trash" in posted
        assert f"wi:agent:confirm:{identity}" in posted
        assert f"wi:agent:cancel:{identity}" in posted
        await ui.click(identity, "confirm")
        assert ui.record(identity)["state"] == "completed"
        assert ui.record(identity)["result"]["uninstalled"] is True
        await ui.click(identity, "confirm")

    asyncio.run(scenario())
    assert service.applied == 1
    assert not target.exists() and _record(store)["state"] == "inactive"


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_dm_uninstall_card_for_a_removed_skill_says_it_is_already_gone(
    invocation_executor, tmp_path, surface,
):
    executor, store, target = _installed(invocation_executor)
    shutil.rmtree(target)
    service = _UninstallService(executor)

    async def scenario():
        ui = surface(tmp_path / "native", service)
        identity = _card(ui)
        await ui.flush()
        posted = str(ui.posts[-1])
        assert "already removed from this computer" in posted
        assert "recoverable trash" not in posted
        await ui.click(identity, "confirm")
        assert ui.record(identity)["result"]["already_removed"] is True
        edited = str(ui.edits[-1])
        assert "It was already removed from this computer." in edited

    asyncio.run(scenario())
    assert service.applied == 1
    assert _record(store)["state"] == "inactive"
    assert executor.client.deactivate_calls == [("install-1", "skill-1")]
    assert store.pending_operations() == []


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_dm_uninstall_card_cancel_and_forged_clicks_change_nothing(
    invocation_executor, tmp_path, surface,
):
    executor, store, target = _installed(invocation_executor)
    service = _UninstallService(executor)
    stranger = ({"user": {"id": "U2"}} if surface is SlackHarness else {"actor_id": 99})

    async def scenario():
        ui = surface(tmp_path / "native", service)
        identity = _card(ui)
        await ui.flush()
        await ui.click(identity, "confirm", **stranger)
        assert ui.record(identity)["state"] == "pending"
        await ui.click(identity, "install")
        assert ui.record(identity)["state"] == "pending"
        await ui.click(identity, "cancel")
        assert ui.record(identity)["state"] == "cancelled"
        await ui.click(identity, "confirm")
        assert ui.record(identity)["state"] == "cancelled"

    asyncio.run(scenario())
    assert service.applied == 0
    assert target.is_dir() and _record(store)["state"] == "active"


def _runtime(tmp_path, monkeypatch, invocation):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: invocation)
    return runtime


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_uninstall_in_a_dm_opens_the_card(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, _native_invocation())
    calls = []

    def begin_uninstall(invocation, skill):
        calls.append((invocation.chat_type, skill))
        return {"state": "queued", "review_id": "review-1"}

    monkeypatch.setattr(runtime.native, "begin_uninstall", begin_uninstall)
    try:
        assert runtime.slash("uninstall managed-skill") == ""
        assert calls == [("private", "managed-skill")]
        assert runtime.slash("uninstall") == "Usage: /wisdom uninstall <skill-name>"
        assert len(calls) == 1
    finally:
        runtime.close()


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_uninstall_of_an_unmanaged_skill_says_so(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, _native_invocation("telegram"))

    def missing(*_args):
        raise WisdomNotFound("Managed skill not found")

    monkeypatch.setattr(runtime.native, "begin_uninstall", missing)
    try:
        assert runtime.slash("uninstall nope") == (
            "nope is not installed from Collective Wisdom on this computer. "
            "Nothing has been changed.")
    finally:
        runtime.close()


def test_shared_chats_keep_the_terminal_reply_and_help_lists_uninstall_as_private():
    assert "hermes wisdom uninstall <skill-name>" in run_slash(("uninstall", ["x"]), object())
    help_text = run_slash(("help", []), object())
    private = help_text.split("Terminal:")[0]
    terminal = help_text.split("Terminal:")[1]
    assert "/wisdom uninstall <skill>" in private
    assert "uninstall" not in terminal
