"""Every chat table cell stays on one row and renders literally."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import pytest

from test_commands import _Service, run_slash

from hermes_wisdom_plugin.top_command import render_page

_BREAKS = "\n| 999 | injected |\r\u2028| 998 |\u2029| 997 |\x85| 996 |"
_MARKUP = [
    "[click](https://evil.example) **admin** @channel",
    "<https://evil.example|label> <!here> <@U123> _italic_ ~~gone~~",
    "<b>html</b> *em* www.evil.example #general",
]


def _top(name: str) -> dict:
    return {"state": "ok", "page": 1, "per_page": 10, "pages": 1, "total": 2, "items": [
        {"name": name, "uses": 3, "edits": 1, "last_used": None, "shared": True},
        {"name": "plain-skill", "uses": 0, "edits": 0, "last_used": None, "shared": False},
    ]}


@pytest.mark.parametrize("markdown", [True, False])
def test_top_skill_name_with_line_breaks_stays_one_physical_row(markdown):
    output = render_page(_top("evil" + _BREAKS), next_command="n {page}",
                         previous_command="p {page}", markdown=markdown)
    lines = output.splitlines()
    assert not any(separator in output for separator in "\r\u2028\u2029\x85")
    rows = [line for line in lines if "evil" in line]
    assert len(rows) == 1, output
    assert not any(line.lstrip("| ").startswith(("999", "998", "997", "996")) for line in lines)
    assert sum("plain-skill" in line for line in lines) == 1
    if markdown:
        # Six columns: the name's pipes are escaped inside its code span.
        assert rows[0].replace("\\|", "").count("|") == 7
        assert "| `evil \\| 999 \\| injected \\|" in rows[0]


@pytest.mark.parametrize("markup", _MARKUP)
def test_browse_catalog_text_cells_render_literally(markup):
    service = _Service()
    service.rows[0]["author_description"] = markup
    service.rows[0]["slug"] = "*bold*_skill_[x](https://evil.example)"
    lines = run_slash("browse --page 1", service).splitlines()
    row = lines[4]
    cells = row.removeprefix("| ").removesuffix(" |").split(" | ")
    assert len(cells) == 5, row
    # Free catalog text is a single code span: no link, emphasis, autolink,
    # HTML or mention is interpreted on any Markdown surface.
    assert cells[1] == "`*bold*_skill_[x](https://evil.example)`"
    assert cells[4] == f"`{markup.replace('|', chr(92) + '|')}`"
    for cell in (cells[1], cells[4]):
        assert cell.count("`") == 2 and cell.startswith("`") and cell.endswith("`")


def test_browse_description_with_line_breaks_stays_one_row():
    service = _Service()
    service.rows[0]["author_description"] = "first" + _BREAKS
    lines = run_slash("browse --page 1", service).splitlines()
    assert sum(line.startswith("| 1 |") for line in lines) == 1
    assert not any(line.startswith(("| 999", "| 998", "| 997", "| 996")) for line in lines)
