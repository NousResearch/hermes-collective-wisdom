"""Sharing a skill whose scripts were run: generated trees are left out and listed."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import asyncio
import json

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_reshare_pending import Kernel
from test_share_copy import _share

from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.terminal_ui import render_inventory

_LEFT_OUT = ("Left out (Hermes doesn't load these): node_modules/, "
             "scripts/__pycache__/, scripts/stale.pyc")
_TERMINAL_LEFT_OUT = ("Left out (Hermes doesn't load these) (3):\n  - node_modules/\n"
                      "  - scripts/__pycache__/\n  - scripts/stale.pyc")


def _run_scripts(kernel) -> None:
    """What running a skill's script and installing its JS dependencies leave behind."""
    scripts = kernel.source / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "fetch.py").write_bytes(b"print('fetch')\n")
    (scripts / "__pycache__").mkdir()
    (scripts / "__pycache__" / "fetch.cpython-311.pyc").write_bytes(b"\x00bytecode")
    (scripts / "stale.pyc").write_bytes(b"\x00bytecode")
    (kernel.source / "node_modules" / "left-pad").mkdir(parents=True)
    (kernel.source / "node_modules" / "left-pad" / "index.js").write_bytes(b"module.exports=1\n")


def _uploaded_paths(kernel) -> dict[str, bytes]:
    """Every file path and body in the exact uploaded commit tree."""
    objects = kernel.client.state.uploaded_objects
    commit = json.loads(objects[kernel.client.state.drafts["draft-1"].draftCommit][1])
    found: dict[str, bytes] = {}

    def walk(tree: str, prefix: str) -> None:
        for entry in json.loads(objects[tree][1])["entries"]:
            if entry["kind"] == "tree":
                walk(entry["hash"], prefix + entry["name"] + "/")
            else:
                found[prefix + entry["name"]] = objects[entry["hash"]][1]

    walk(commit["tree"], "")
    return found


def _expected_upload(kernel) -> set[str]:
    return {
        path.relative_to(kernel.source).as_posix()
        for path in kernel.source.rglob("*")
        if path.is_file() and not any(
            part in {"__pycache__", "node_modules"} for part in path.parts
        ) and path.suffix != ".pyc"
    } | {"skill.manifest.json"}


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_share_leaves_generated_trees_out_lists_them_and_publishes_the_rest(
    tmp_path, surface,
):
    async def scenario():
        kernel = Kernel(tmp_path)
        _run_scripts(kernel)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        assert ui.view(identity).title == "📤 Share skill"
        await ui.click(identity, "details")
        details = ui.view(identity).to_text()
        assert _LEFT_OUT in details
        assert "File: scripts/__pycache__" not in details
        await ui.click(identity, "back")
        await ui.click(identity, "confirm")
        assert ui.record(identity)["state"] == "completed"
        assert kernel.client.state.calls["publish"] == 1
        uploaded = _uploaded_paths(kernel)
        assert set(uploaded) == _expected_upload(kernel)
        assert uploaded["scripts/fetch.py"] == b"print('fetch')\n"
        ui.runtime.close()
    asyncio.run(scenario())


def test_terminal_share_lists_what_it_left_out_and_publishes_the_rest(tmp_path, capsys):
    kernel = Kernel(tmp_path)
    _run_scripts(kernel)
    assert _share(kernel, "--yes") == 0
    assert _TERMINAL_LEFT_OUT in capsys.readouterr().out
    assert set(_uploaded_paths(kernel)) == _expected_upload(kernel)


def test_terminal_inventory_lists_every_path_left_out_however_long():
    """An exact review never truncates what it left out."""
    omitted = [f"scripts/generated-{number:03d}/__pycache__/" for number in range(300)]
    rendered = render_inventory({"files": [], "omitted_paths": omitted})
    assert len(rendered) > 4096
    lines = rendered.splitlines()
    assert lines[1] == "Left out (Hermes doesn't load these) (300):"
    assert lines[2:] == [f"  - {path}" for path in omitted]


def test_terminal_share_prints_a_long_valid_path_whole(tmp_path, capsys):
    """Each segment is valid (200 < 255 bytes); the full path exceeds 512 characters."""
    kernel = Kernel(tmp_path)
    deep = "/".join(letter * 200 for letter in "abc")
    (kernel.source / deep / "__pycache__").mkdir(parents=True)
    (kernel.source / deep / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00bytecode")
    (kernel.source / deep / "notes.md").write_bytes(b"notes\n")
    assert _share(kernel, "--yes") == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(deep + "/__pycache__/") > 512
    assert f"  - {deep}/__pycache__/" in lines
    assert any(line.endswith(f". {deep}/notes.md — 6 bytes") for line in lines)
    assert f"{deep}/notes.md" in _uploaded_paths(kernel)


def test_terminal_inventory_is_bounded_by_the_package_path_budget():
    """Whole paths, never cut; the existing metadata budget bounds the listing."""
    path = "/".join(["s" * 250] * 16)
    omitted = [f"{path}/{number:05d}/" for number in range(600)]
    rendered = render_inventory({"files": [], "omitted_paths": omitted})
    shown = [line[4:] for line in rendered.splitlines() if line.startswith("  - ")]
    assert shown == omitted[:len(shown)] and 0 < len(shown) < len(omitted)
    assert rendered.splitlines()[-1] == (
        f"…and {len(omitted) - len(shown)} more paths: the list exceeds the "
        "2097152-byte package path budget.")


def test_new_generated_output_after_review_requires_a_fresh_review(tmp_path):
    """The omission list is bound: what the card listed is what was left out."""
    kernel = Kernel(tmp_path)
    _run_scripts(kernel)
    prepared = kernel.suggest("reviewed-skill")
    review = kernel.publication_review(prepared["draft_id"])
    (kernel.source / ".pytest_cache").mkdir()
    with pytest.raises(WisdomConflict, match="changed after review"):
        kernel.publication_submit(review["receipt"])
    assert not kernel.client.state.calls["upload"]
