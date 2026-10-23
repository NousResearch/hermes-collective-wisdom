"""Managed skills whose files were deleted or moved outside Wisdom."""
# pylint: disable=missing-function-docstring,redefined-outer-name,unused-import
# pylint: disable=protected-access,missing-class-docstring,too-few-public-methods

from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from test_install_execution import invocation_executor  # noqa: F401  (fixture)
from restoration_support import SlackHarness, TelegramHarness
from hermes_wisdom_plugin import commands, terminal_presentation
from hermes_wisdom_plugin.catalog_status import CatalogInstallationStatus
from hermes_wisdom_plugin.managed_target import RemovedOutsideWisdom, removed_outside_wisdom
from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import keyboard, rich_html
from hermes_wisdom_plugin.package import PackagePolicyError, SkillRefusal
from hermes_wisdom_plugin.terminal_ui import render_operation_outcome, render_read_command
from hermes_wisdom_plugin.update_review import UpdateReviewer


def _installed(invocation_executor):
    executor, store, client, _receipts, plan, target, _authority = invocation_executor
    client.deactivate_calls = []

    def deactivate_install(installation_id, skill_id):
        client.deactivate_calls.append((installation_id, skill_id))
        if getattr(client, "offline", False):
            raise RuntimeError("offline")
        client.installation_rows = [
            row for row in client.installation_rows if row["skill_id"] != skill_id]
        return {"state": "inactive"}

    client.deactivate_install = deactivate_install
    executor.apply(plan["receipt"])
    assert target.is_dir()
    return executor, store, target


def _removed(invocation_executor):
    executor, store, target = _installed(invocation_executor)
    shutil.rmtree(target)
    return executor, store, target


def _record(store):
    return store.installation("skill-1")


def test_only_an_absent_target_counts_as_removed(invocation_executor):
    executor, store, target = _installed(invocation_executor)
    owner = executor._owner()
    assert removed_outside_wisdom(executor.host, _record(store), owner) is False

    shutil.rmtree(target)
    assert removed_outside_wisdom(executor.host, _record(store), owner) is True
    assert removed_outside_wisdom(
        executor.host, {**_record(store), "state": "inactive"}, owner) is False

    target.write_text("planted file\n", encoding="utf-8")
    assert removed_outside_wisdom(executor.host, _record(store), owner) is False
    target.unlink()
    elsewhere = target.parent / "elsewhere"
    elsewhere.mkdir()
    target.symlink_to(elsewhere, target_is_directory=True)
    assert removed_outside_wisdom(executor.host, _record(store), owner) is False


def test_installed_flags_a_removed_skill_instead_of_active(invocation_executor):
    executor, store, _target = _removed(invocation_executor)

    rows = executor.installed()

    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "removed"
    assert row["removed_outside_wisdom"] is True
    assert row["status_text"] == "Removed from this computer outside Wisdom"
    assert row["modified"] is False
    assert _record(store)["state"] == "active", "a read never rewrites the stored record"

    text = render_read_command("installed", {"installations": rows})
    assert "managed-skill v2 · Removed from this computer outside Wisdom" in text
    buffer = io.StringIO()
    console = Console(file=buffer, width=300, color_system=None)
    terminal_presentation.render_terminal("installed", {"installations": rows}, console, text)
    assert "Removed from this computer outside Wisdom" in buffer.getvalue()
    assert "active" not in buffer.getvalue()


def test_a_present_skill_is_still_listed_active(invocation_executor):
    executor, _store, _target = _installed(invocation_executor)
    row = executor.installed()[0]
    assert row["state"] == "active" and "removed_outside_wisdom" not in row


def _catalog_row(invocation_executor):
    executor, store, _target = _removed(invocation_executor)
    owner = executor._owner()
    status = CatalogInstallationStatus(store, owner, executor.host.profile_home)
    return {
        "id": "skill-1", "slug": "managed-skill", "latest_version": 2,
        "author_description": "A managed skill",
        "installation_status": status.for_skill("skill-1", "managed-skill", 2),
    }


def test_catalog_status_says_removed_not_unavailable(invocation_executor):
    row = _catalog_row(invocation_executor)
    assert row["installation_status"] == {
        "state": "removed", "text": "Removed from this computer",
        "installed_version": 2, "latest_version": 2,
    }
    text = render_read_command("browse", {"skills": [row]})
    assert "Removed from this computer · install again: hermes wisdom install managed-skill" in text
    assert "unavailable" not in text


def _browse(row):
    return project_view({"operation": "catalog", "state": "pending", "plan": {
        "command": "browse", "target": "", "data": [row],
    }})


def _cells(rendered):
    table = next(block for block in rendered if block["type"] == "table")
    return [["".join(element["text"] for element in cell["elements"][0]["elements"])
             for cell in line] for line in table["rows"]]


def test_slack_browse_renders_removed_and_offers_reinstall(invocation_executor):
    rendered = blocks(_browse(_catalog_row(invocation_executor)), "card-1")

    assert _cells(rendered)[1][:3] == ["managed-skill", "v2", "Removed"]
    buttons = [element for block in rendered if block["type"] == "actions"
               for element in block["elements"]]
    install = [button for button in buttons
               if button.get("value") == "wi:agent:catalog.install.0:card-1"]
    assert len(install) == 1
    assert install[0]["text"]["text"].startswith("Reinstall")


def test_telegram_browse_renders_removed_and_offers_reinstall(invocation_executor):
    view = _browse(_catalog_row(invocation_executor))
    rendered = rich_html(view, "card-1")

    assert "<td>managed-skill</td><td>v2</td><td>Removed</td>" in rendered
    assert 'data="wi:agent:catalog.install.0:card-1"' in rendered
    pytest.importorskip("telegram")
    data = [button.callback_data for line in keyboard(view, "card-1").inline_keyboard
            for button in line if button.callback_data]
    assert data == ["wi:agent:catalog.install.0:card-1"]


def test_show_card_offers_reinstall_for_a_removed_skill(invocation_executor):
    row = _catalog_row(invocation_executor)
    view = project_view({"operation": "catalog", "state": "pending", "plan": {
        "command": "show", "slug": "managed-skill", "data": {
            "skill": {"id": "skill-1", "slug": "managed-skill"},
            "latest_version_detail": {"version": {"version": 2}},
            "installation_status": row["installation_status"],
        },
    }})

    assert "Removed from this computer" in view.items[0].detail
    assert [(action.label, action.action) for action in view.items[0].actions] == [
        ("Reinstall", "catalog.install.0")]
    rendered = str(blocks(view, "card-1"))
    assert "wi:agent:catalog.install.0:card-1" in rendered


def test_managed_name_resolution_still_finds_a_removed_skill():
    class Service:
        @staticmethod
        def list_installations():
            return [{"skill_id": "skill-1", "slug": "managed-skill", "state": "removed"}]

    assert commands._managed_reference(Service(), "managed-skill") == "skill-1"


# Reinstall ------------------------------------------------------------------

def _reinstall(executor):
    return executor.reviewer.plan("skill-1@v2", update_mode="AUTO_WITH_NOTICE")


def test_a_removed_skill_reinstalls_once_over_its_stale_record(invocation_executor):
    executor, store, target = _removed(invocation_executor)
    client = executor.client
    before = len(client.record_calls)

    plan = _reinstall(executor)
    result = executor.apply(plan["receipt"])

    assert result["installed"] is True and target.is_dir()
    assert _record(store)["state"] == "active"
    assert removed_outside_wisdom(executor.host, _record(store), executor._owner()) is False
    assert len(client.record_calls) == before + 1
    assert store.pending_operations() == []
    assert executor.apply(plan["receipt"]) == result
    assert len(client.record_calls) == before + 1


def test_a_present_skill_is_still_refused_as_already_managed(invocation_executor):
    executor, _store, _target = _installed(invocation_executor)
    with pytest.raises(PackagePolicyError, match="already managed; use the update workflow"):
        _reinstall(executor)
    with pytest.raises(PackagePolicyError, match="already managed; use the update workflow"):
        executor.reviewer.plan("managed-skill")


def test_a_planted_file_at_a_removed_target_still_blocks_reinstall(invocation_executor):
    executor, _store, target = _removed(invocation_executor)
    plan = _reinstall(executor)
    target.write_text("planted\n", encoding="utf-8")
    with pytest.raises(PackagePolicyError):
        executor.apply(plan["receipt"])
    assert target.read_text(encoding="utf-8") == "planted\n"


def test_an_uninstalled_skill_installs_again(invocation_executor):
    executor, store, target = _installed(invocation_executor)
    executor.uninstall("skill-1")
    assert not target.exists() and _record(store)["state"] == "inactive"

    plan = _reinstall(executor)
    executor.apply(plan["receipt"])

    assert target.is_dir() and _record(store)["state"] == "active"


class _CatalogService:
    """Real reviewer and executor behind the native catalog controls."""

    def __init__(self, executor, store):
        self.executor, self.store, self.applied = executor, store, 0

    def native_owner(self):
        return self.executor._owner()

    def search_skills(self, _query=None):
        status = CatalogInstallationStatus(
            self.store, self.native_owner(), self.executor.host.profile_home)
        return [{
            "id": "skill-1", "slug": "managed-skill", "latest_version": 2,
            "author_description": "A managed skill",
            "installation_status": status.for_skill("skill-1", "managed-skill", 2),
        }]

    def install_plan(self, reference, **kwargs):
        return self.executor.reviewer.plan(reference, **kwargs)

    @staticmethod
    def native_install_inspect(_receipt):
        return [{"path": "SKILL.md", "content": "inert fixture"}]

    def install_apply(self, receipt, *, may_apply, accept_partial=False):
        self.applied += 1
        return self.executor.apply(receipt, accept_partial=accept_partial, may_apply=may_apply)

    @staticmethod
    def close():
        return None


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_reinstall_from_the_browse_card_applies_once(invocation_executor, tmp_path, surface):
    executor, store, target = _removed(invocation_executor)
    service = _CatalogService(executor, store)
    before = len(executor.client.record_calls)

    async def scenario():
        ui = surface(tmp_path / "native", service)
        identity = ui.runtime.begin_catalog(ui.caller, "browse")["review_id"]
        await ui.flush()
        assert "catalog.install.0" in str(ui.posts[-1])
        await ui.click(identity, "catalog.install.0")
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "catalog.install.0")

    asyncio.run(scenario())
    assert service.applied == 1
    assert target.is_dir() and _record(store)["state"] == "active"
    assert len(executor.client.record_calls) == before + 1


# Check and update ------------------------------------------------------------

def _updates(executor):
    return UpdateReviewer(executor.client, executor.store, executor.receipts,
                          executor.host, executor.reviewer.owner)


def test_check_reports_a_removed_skill_plainly(invocation_executor):
    executor, _store, _target = _removed(invocation_executor)

    result = _updates(executor).check("skill-1")

    assert result == {
        "skill_id": "skill-1", "slug": "managed-skill", "state": "removed",
        "reason": "Removed from this computer outside Wisdom",
        "installed_version": 2, "latest_version": None,
    }
    text = commands._human_render("check", result)
    assert text == ("Update check\nSkill: managed-skill\n"
                    "State: Removed from this computer outside Wisdom\n"
                    "Next step: hermes wisdom install managed-skill")


def test_update_of_a_removed_skill_names_the_reinstall(invocation_executor):
    executor, _store, _target = _removed(invocation_executor)

    with pytest.raises(RemovedOutsideWisdom) as raised:
        _updates(executor).plan("skill-1")

    assert not isinstance(raised.value, SkillRefusal)
    assert str(raised.value) == (
        "managed-skill was removed from this computer outside Wisdom; "
        "install it again with: hermes wisdom install managed-skill")
    assert "regular directory" not in str(raised.value)


class _UpdateService(_CatalogService):
    def update_plan(self, skill_id, **_kwargs):
        return _updates(self.executor).plan(skill_id)

    def show(self, _target):
        row = self.search_skills()[0]
        return {"skill": {"id": "skill-1", "slug": "managed-skill"},
                "latest_version_detail": {"version": {"version": 2}},
                "installation_status": row["installation_status"]}


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_chat_update_of_a_removed_skill_offers_reinstall(invocation_executor, tmp_path, surface):
    executor, store, target = _removed(invocation_executor)
    service = _UpdateService(executor, store)

    async def scenario():
        ui = surface(tmp_path / "native", service)
        identity = ui.runtime.begin_update(ui.caller, "skill-1")["review_id"]
        await ui.flush()
        posted = str(ui.posts[-1])
        assert "Removed from this computer" in posted
        assert "regular directory" not in posted
        assert "catalog.install.0" in posted
        await ui.click(identity, "catalog.install.0")
        await ui.click(identity, "catalog.install.0")

    asyncio.run(scenario())
    assert service.applied == 1
    assert target.is_dir() and _record(store)["state"] == "active"


# Uninstall -------------------------------------------------------------------

def test_uninstall_of_a_removed_skill_completes_without_bytes(invocation_executor):
    executor, store, target = _removed(invocation_executor)
    client = executor.client

    result = executor.uninstall("skill-1")

    assert result == {
        "uninstalled": True, "skill_id": "skill-1", "slug": "managed-skill",
        "state": "inactive", "recoverable_path": None, "already_removed": True,
    }
    assert _record(store)["state"] == "inactive"
    assert client.deactivate_calls == [("install-1", "skill-1")]
    assert store.pending_operations() == []
    assert not target.exists()
    assert render_operation_outcome("uninstall", result) == (
        "✅ managed-skill removed. It was already removed from this computer.")

    assert executor.uninstall("skill-1") == result
    assert client.deactivate_calls == [("install-1", "skill-1")]


def test_uninstall_of_a_removed_skill_resumes_after_gateway_failure(invocation_executor):
    executor, store, _target = _removed(invocation_executor)
    client = executor.client
    client.offline = True

    pending = executor.uninstall("skill-1")

    assert pending.get("pending") is True
    assert len(store.pending_operations("skill-1")) == 1
    client.offline = False
    result = executor.uninstall("skill-1")
    assert result["uninstalled"] is True and result["already_removed"] is True
    assert store.pending_operations() == []
    assert _record(store)["state"] == "inactive"


def test_uninstall_refuses_a_file_planted_at_a_removed_target(invocation_executor):
    executor, store, target = _removed(invocation_executor)
    target.write_text("planted\n", encoding="utf-8")

    with pytest.raises(PackagePolicyError):
        executor.uninstall("skill-1")

    assert store.pending_operations() == []
    assert _record(store)["state"] == "active"
    assert target.read_text(encoding="utf-8") == "planted\n"


def test_uninstall_of_a_present_skill_still_moves_it_to_trash(invocation_executor):
    executor, store, target = _installed(invocation_executor)

    result = executor.uninstall("skill-1")

    assert "already_removed" not in result
    assert result["recoverable_path"] and (Path(result["recoverable_path"]) / "SKILL.md").is_file()
    assert not target.exists() and _record(store)["state"] == "inactive"
    assert render_operation_outcome("uninstall", result) == "✅ managed-skill removed"
