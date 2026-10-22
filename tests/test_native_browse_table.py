"""/wisdom browse native cards render the listing as a table on Slack and Telegram."""
# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio
from dataclasses import replace

import pytest

from native_test_support import owner
from restoration_support import SlackHarness, TelegramHarness
from hermes_wisdom_plugin.errors import StaleControlError, WisdomValidationError
from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.native_slack import blocks, table_block
from hermes_wisdom_plugin.native_telegram import keyboard, plain_html, rich_html

_PORTAL = "https://portal.example/orgs/acme/wisdom/skills/"


def _rows():
    return [
        {"id": "id-a", "slug": "code-review", "latest_version": 2,
         "author_description": "Review pull requests\nwith a checklist.",
         "installation_status": {"state": "installed", "text": "Installed v2",
                                 "installed_version": 2, "latest_version": 2},
         "portal_url": _PORTAL + "id-a"},
        {"id": "id-b", "slug": "merge-fixer", "latest_version": 1,
         "author_description": "Resolve <merge> & conflicts " + "x" * 120,
         "installation_status": {"state": "not_installed", "text": "Not installed"}},
    ]


def _view(rows=None, state="pending"):
    return project_view({"operation": "catalog", "state": state, "plan": {
        "command": "browse", "target": "", "data": _rows() if rows is None else rows,
    }})


def _cell_text(cell):
    return "".join(element["text"] for element in cell["elements"][0]["elements"])


def test_slack_browse_is_one_table_block_with_install_only_for_missing_rows():
    rendered = blocks(_view(), "card-1")

    assert [block["type"] for block in rendered] == [
        "header", "section", "table", "actions", "actions",
    ]
    table = rendered[2]
    grid = [[_cell_text(cell) for cell in row] for row in table["rows"]]
    assert grid[0] == ["Skill", "Version", "Installed", "Description"]
    assert all(cell["elements"][0]["elements"][0]["style"] == {"bold": True}
               for cell in table["rows"][0])
    assert grid[1] == ["code-review", "v2", "Yes", "Review pull requests with a checklist."]
    assert grid[2][:3] == ["merge-fixer", "v1", "No"]
    assert len(grid[2][3]) == 80 and grid[2][3].endswith("…")
    installs = rendered[3]["elements"]
    assert [button["value"] for button in installs] == ["wi:agent:catalog.install.1:card-1"]
    assert installs[0]["text"]["text"].startswith("Install merge-fix")
    assert installs[0]["style"] == "primary"
    links = rendered[4]["elements"]
    assert [button["url"] for button in links] == [_PORTAL + "id-a"]
    assert "value" not in links[0]


def test_slack_browse_falls_back_to_items_when_the_table_is_unsafe():
    view = _view()
    assert view.table is not None and table_block(view) is not None
    too_many = replace(view, table=replace(
        view.table, columns=tuple(f"c{index}" for index in range(21)),
    ))
    assert table_block(too_many) is None
    rendered = blocks(too_many, "card-1")
    assert "table" not in [block["type"] for block in rendered]
    item_buttons = [element.get("value") for block in rendered if block["type"] == "actions"
                    for element in block["elements"]]
    assert "wi:agent:catalog.install.1:card-1" in item_buttons


def test_show_and_versions_cards_keep_the_item_layout():
    for command in ("show", "versions"):
        row = _rows()[1]
        data = ({"skill": {"id": row["id"], "slug": row["slug"]},
                 "latest_version_detail": {"version": {"version": 1}},
                 "installation_status": row["installation_status"]}
                if command == "show" else {"versions": [dict(row, version=1)]})
        view = project_view({"operation": "catalog", "state": "pending",
                             "plan": {"command": command, "slug": row["slug"], "data": data}})
        assert view.table is None
        assert "table" not in [block["type"] for block in blocks(view, "card-1")]


def test_telegram_rich_browse_is_an_html_table_with_escaped_cells():
    rendered = rich_html(_view(), "card-1")

    assert rendered.startswith(
        "<h5>Collective Wisdom catalog</h5><p>Browse: All skills · 2 skills, newest first</p>")
    assert ("<table bordered><tr><th>Skill</th><th>Version</th><th>Installed</th>"
            "<th>Description</th></tr>") in rendered
    assert ("<tr><td>code-review</td><td>v2</td><td>Yes</td>"
            "<td>Review pull requests with a checklist.</td></tr>") in rendered
    assert "<td>Resolve &lt;merge&gt; &amp; conflicts x" in rendered
    assert "<merge>" not in rendered
    assert rendered.count('type="callback_data"') == 1
    assert 'data="wi:agent:catalog.install.1:card-1"' in rendered
    assert f'url="{_PORTAL}id-a"' in rendered


def test_telegram_plain_browse_is_one_compact_block_per_skill():
    rendered = plain_html(_view())

    assert rendered.splitlines()[:8] == [
        "<b>Collective Wisdom catalog</b>",
        "",
        "Browse: All skills · 2 skills, newest first",
        "",
        "<b>code-review</b> · v2 · Installed v2",
        "Review pull requests with a checklist.",
        "",
        "<b>merge-fixer</b> · v1 · Not installed",
    ]
    assert rendered.splitlines()[8].startswith("Resolve &lt;merge&gt; &amp; conflicts x")


def test_telegram_keyboard_uses_table_controls_once():
    pytest.importorskip("telegram")
    markup = keyboard(_view(), "card-1")
    data = [button.callback_data for row in markup.inline_keyboard for button in row
            if button.callback_data]
    assert data == ["wi:agent:catalog.install.1:card-1"]


def test_dismissed_browse_card_has_no_controls():
    view = _view(state="cancelled")
    assert view.table is not None and view.table.actions == []
    assert "callback_data" not in rich_html(view, "card-1")


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_browse_table_install_runs_once_and_refuses_stale_or_forged(tmp_path, surface):
    class Service:
        applied = 0

        def native_owner(self):
            return owner()

        def search_skills(self, _query=None):
            return _rows()

        def install_plan(self, reference, **_kwargs):
            assert reference == "id-b@v1"
            return {"operation": "install", "owner": self.native_owner(),
                    "skill_id": "id-b", "slug": "merge-fixer", "version": 1,
                    "receipt": "private-receipt", "allowed": True}

        def native_install_inspect(self, _receipt):
            return [{"path": "SKILL.md", "content": "inert fixture"}]

        def install_apply(self, _receipt, *, may_apply, accept_partial=False):
            assert may_apply() and not accept_partial
            self.applied += 1
            return {"installed": True, "skill_id": "id-b", "version": 1}

        def close(self):
            return None

    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_catalog(ui.caller, "browse")["review_id"]
        await ui.flush()
        posted = str(ui.posts[-1])
        assert "catalog.install.1" in posted
        assert "catalog.install.0" not in posted
        with pytest.raises((StaleControlError, WisdomValidationError)):
            ui.runtime.reviews.act(identity, "catalog.install.0", service,
                                   replace(ui.caller, message_id=ui.record(identity)["message_id"]))
        assert ui.record(identity)["state"] == "pending"
        await ui.click(identity, "catalog.install.1")
        assert service.applied == 1
        assert ui.record(identity)["state"] == "completed"
        await ui.click(identity, "catalog.install.1")
        assert service.applied == 1

    asyncio.run(scenario())


def _many(count):
    return [{"id": f"id-{index}", "slug": f"skill-{index}", "latest_version": 1,
             "author_description": f"Skill number {index}",
             "installation_status": {"state": "not_installed", "text": "Not installed"}}
            for index in range(count)]


def _paged_view(count, page):
    return project_view({"operation": "catalog", "state": "pending", "catalog_page": page,
                         "plan": {"command": "browse", "target": "", "data": _many(count)}})


def _offered(view):
    return [action.action for action in view.navigation_actions + view.table.actions
            if action.action]


def test_browse_pages_through_every_skill_with_absolute_install_indices():
    first, last = _paged_view(13, 0), _paged_view(13, 2)

    assert first.summary == "Browse: All skills · 13 skills, newest first · page 1 of 3"
    assert [row.name for row in first.table.rows] == [f"skill-{index}" for index in range(5)]
    assert _offered(first) == ["catalog.page.1"] + [f"catalog.install.{i}" for i in range(5)]
    assert last.summary.endswith("page 3 of 3")
    assert [row.name for row in last.table.rows] == ["skill-10", "skill-11", "skill-12"]
    assert _offered(last) == ["catalog.page.1", "catalog.install.10",
                              "catalog.install.11", "catalog.install.12"]
    assert _offered(_paged_view(13, 1))[:2] == ["catalog.page.0", "catalog.page.2"]
    assert _paged_view(5, 0).navigation_actions == []
    assert _paged_view(13, 99).summary.endswith("page 3 of 3")


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_browse_next_page_reaches_and_installs_a_skill_past_the_first_five(tmp_path, surface):
    class Service:
        applied = []

        def native_owner(self):
            return owner()

        def search_skills(self, _query=None):
            return _many(7)

        def install_plan(self, reference, **_kwargs):
            assert reference == "id-6@v1"
            return {"operation": "install", "owner": self.native_owner(),
                    "skill_id": "id-6", "slug": "skill-6", "version": 1,
                    "receipt": "private-receipt", "allowed": True}

        def native_install_inspect(self, _receipt):
            return [{"path": "SKILL.md", "content": "inert fixture"}]

        def install_apply(self, _receipt, *, may_apply, accept_partial=False):
            assert may_apply() and not accept_partial
            self.applied.append("id-6")
            return {"installed": True, "skill_id": "id-6", "version": 1}

        def close(self):
            return None

    async def scenario():
        service = Service()
        ui = surface(tmp_path, service)
        identity = ui.runtime.begin_catalog(ui.caller, "browse")["review_id"]
        await ui.flush()
        assert "catalog.install.6" not in str(ui.posts[-1])
        with pytest.raises((StaleControlError, WisdomValidationError)):
            ui.runtime.reviews.act(identity, "catalog.install.6", service,
                                   replace(ui.caller, message_id=ui.record(identity)["message_id"]))
        await ui.click(identity, "catalog.page.1")
        view = ui.view(identity)
        assert view.summary.endswith("page 2 of 2")
        assert [row.name for row in view.table.rows] == ["skill-5", "skill-6"]
        with pytest.raises((StaleControlError, WisdomValidationError)):
            ui.runtime.reviews.act(identity, "catalog.install.0", service,
                                   replace(ui.caller, message_id=ui.record(identity)["message_id"]))
        await ui.click(identity, "catalog.install.6")
        assert service.applied == ["id-6"]
        assert ui.record(identity)["state"] == "completed"

    asyncio.run(scenario())
