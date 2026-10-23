"""Managed skills whose files were deleted or moved outside Wisdom."""
# pylint: disable=missing-function-docstring,redefined-outer-name,unused-import
# pylint: disable=protected-access,missing-class-docstring,too-few-public-methods

from __future__ import annotations

import io
import shutil

import pytest
from rich.console import Console

from test_install_execution import invocation_executor  # noqa: F401  (fixture)
from hermes_wisdom_plugin import commands, terminal_presentation
from hermes_wisdom_plugin.catalog_status import CatalogInstallationStatus
from hermes_wisdom_plugin.managed_target import removed_outside_wisdom
from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import keyboard, rich_html
from hermes_wisdom_plugin.terminal_ui import render_read_command


def _installed(invocation_executor):
    executor, store, _client, _receipts, plan, target, _authority = invocation_executor
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
