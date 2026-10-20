"""Sharing names the exact reason a skill's own name or description cannot be shared."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_reshare_pending import Kernel
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.editorial import local_copy_problem
from hermes_wisdom_plugin.errors import ShareCopyError

_FIX = "Edit the skill's SKILL.md frontmatter, then retry."
_LONG = "x" * 839


@pytest.mark.parametrize(("name", "description", "expected"), [
    ("", "Review incident notes.", "Name is missing."),
    ("reviewed-skill", "", "Description is missing."),
    ("reviewed-skill", "   ", "Description is missing."),
    ("reviewed-skill", _LONG, "Description is 839 characters; the limit is 320."),
    ("n" * 101, "Review incident notes.", "Name is 101 characters; the limit is 100."),
    ("", _LONG, "Name is missing. Description is 839 characters; the limit is 320."),
    ("reviewed-skill", "x" * 320, None),
])
def test_copy_problem_names_the_measured_cause(name, description, expected):
    assert local_copy_problem(name, description) == expected


def _write_skill(kernel, frontmatter: dict[str, str]) -> None:
    lines = "".join(f"{key}: {value}\n" for key, value in frontmatter.items())
    (kernel.source / "SKILL.md").write_text(f"---\n{lines}---\n# Incident review\n",
                                            encoding="utf-8")
    kernel.reviewer.host.frontmatter = dict(frontmatter)


_CASES = {
    "missing-name": ({"description": "Review incident notes."}, "Name is missing."),
    "missing-description": ({"name": "reviewed-skill"}, "Description is missing."),
    "over-limit": ({"name": "reviewed-skill", "description": _LONG},
                   "Description is 839 characters; the limit is 320."),
}


def _share(kernel, *flags):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return run_cli(parser.parse_args(["share", "reviewed-skill", *flags]), kernel)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_terminal_share_reports_the_exact_problem_and_uploads_nothing(tmp_path, capsys, case):
    frontmatter, problem = _CASES[case]
    kernel = Kernel(tmp_path)
    _write_skill(kernel, frontmatter)
    assert _share(kernel, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] in {"copy_incomplete", "not_shared"}
    assert result["detail"] == f"{problem} {_FIX}"
    assert not kernel.client.state.calls


@pytest.mark.parametrize("case", ["missing-name", "over-limit"])
def test_publication_review_refuses_copy_that_cannot_be_shared(tmp_path, case):
    frontmatter, problem = _CASES[case]
    kernel = Kernel(tmp_path)
    _write_skill(kernel, frontmatter)
    prepared = kernel.suggest("reviewed-skill")
    assert prepared["editorial_status"] == "incomplete"
    assert prepared["editorial_problem"] == f"{problem} {_FIX}"
    with pytest.raises(ShareCopyError, match=problem):
        kernel.publication_review(prepared["draft_id"])
    assert not kernel.client.state.calls


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_native_share_card_names_the_exact_problem(tmp_path, surface, case):
    frontmatter, problem = _CASES[case]

    async def scenario():
        kernel = Kernel(tmp_path)
        _write_skill(kernel, frontmatter)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        view = ui.view(identity)
        assert view.title == "Skill needs attention"
        assert view.items[0].detail == f"{problem} {_FIX}"
        assert "Add a name and a description" not in str(ui.posts[-1])
        assert problem in str(ui.posts[-1])
        assert [action.action for action in view.actions] == ["suggest.refresh", "suggest.keep"]
        # Fixing SKILL.md and pressing Retry prepares the current source again.
        _write_skill(kernel, {"name": "reviewed-skill", "description": "Review incident notes."})
        await ui.click(identity, "suggest.refresh")
        assert ui.view(identity).title == "📤 Share skill"
        assert not kernel.client.state.calls["publish"]
        ui.runtime.close()
    asyncio.run(scenario())
