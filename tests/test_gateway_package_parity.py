"""Sharing refuses, before any upload, every package Gateway's validator refuses.

The lists and limits below are copied from Team Gateway's package validator
and Wisdom package policy at revision ba2b5ae9. Gateway answers a package that
breaks one of them with HTTP 422 ``instruction_only_violation`` and no draft,
so the plugin must refuse the same package locally with a reason naming the
file. When Gateway changes a list, update hermes_wisdom_plugin/package.py and
this file together.
"""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_package_path_policy import (
    _case_sensitive, _prepare as _prepare_declared, _skill,
)
from test_reshare_pending import Kernel

import hermes_wisdom_plugin.package as package_module
from hermes_wisdom_plugin.package import (
    PackagePolicyError,
    SkillRefusal,
    verify_content_files,
)

GATEWAY_REVISION = "ba2b5ae9"
# Gateway package validator: BLOCKED_PATH_SEGMENTS (matched case-folded, any segment).
GATEWAY_BLOCKED_PATH_SEGMENTS = frozenset({
    ".circleci", ".git", ".github", ".gitlab", ".hooks", ".tox", ".venv",
    "__pycache__", "hooks", "node_modules", "venv",
})
# Gateway package validator: WINDOWS_RESERVED_NAMES (case-folded segment before the first dot).
GATEWAY_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
# Gateway package validator: validateSegment: /[<>:"|?*]/u.
GATEWAY_INVALID_CHARS = frozenset('<>:"|?*')
# Gateway package validator: COMPILED_EXECUTABLE_MAGIC.
GATEWAY_COMPILED_EXECUTABLE_MAGIC = frozenset({
    b"\x7fELF", b"MZ", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    b"\x00asm",
})
# Gateway Wisdom package policy limits.
GATEWAY_MAX_TOTAL_BYTES = 10 * 1024 * 1024
GATEWAY_MAX_METADATA_BYTES = 2 * 1024 * 1024
GATEWAY_MAX_TREE_DEPTH = 16
GATEWAY_MAX_SEGMENT_BYTES = 255
GATEWAY_ENTRY_METADATA_OVERHEAD = 128
GATEWAY_REQUIRED_ROOT_FILES = frozenset({"SKILL.md", "skill.manifest.json"})


def test_mirrored_lists_and_limits_match_gateway():
    assert GATEWAY_REVISION == "ba2b5ae9"
    assert set(package_module.BLOCKED_PATH_SEGMENTS) == GATEWAY_BLOCKED_PATH_SEGMENTS
    assert package_module.WINDOWS_RESERVED_NAMES == GATEWAY_WINDOWS_RESERVED_NAMES
    # ':' keeps its own Hermes-specific reason; the refused set is the same.
    assert package_module.WINDOWS_INVALID_CHARS | {":"} == GATEWAY_INVALID_CHARS
    magic = package_module._COMPILED_EXECUTABLE_MAGIC  # pylint: disable=protected-access
    assert set(magic) == GATEWAY_COMPILED_EXECUTABLE_MAGIC
    assert package_module.MAX_TREE_BYTES == GATEWAY_MAX_TOTAL_BYTES
    assert package_module.MAX_METADATA_BYTES == GATEWAY_MAX_METADATA_BYTES
    assert package_module.MAX_TREE_DEPTH == GATEWAY_MAX_TREE_DEPTH
    assert package_module.MAX_SEGMENT_BYTES == GATEWAY_MAX_SEGMENT_BYTES
    assert package_module.REQUIRED_ROOT_FILES == GATEWAY_REQUIRED_ROOT_FILES


_MANIFEST = json.dumps({"schema_version": 1}).encode()


def _verify(entries: list[tuple[str, str, bytes]]):
    base = [("SKILL.md", "file", b"# test"), ("skill.manifest.json", "file", _MANIFEST)]
    return verify_content_files(base + entries)


def _refused(path: str, rule: str, body: bytes = b"x") -> None:
    with pytest.raises(SkillRefusal) as refused:
        _verify([(path, "file", body)])
    assert rule in str(refused.value)


# One case per Gateway rule in validatePath / validateSegment, each with the
# local reason it must produce.
PATH_RULES = [
    ("validatePath empty", "", "empty path"),
    ("validatePath absolute", "/abs.md", "absolute path"),
    ("validatePath backslash", "refs\\a.md", "backslash separator"),
    ("validatePath NFC", "cafe\u0301.md", "canonical (NFC)"),
    ("validatePath depth", "/".join(["d"] * 17) + "/f.md", "maximum tree depth 16"),
    ("validateSegment empty", "refs//a.md", "path traversal"),
    ("validateSegment dot", "refs/./a.md", "path traversal"),
    ("validateSegment dot-dot", "refs/../a.md", "path traversal"),
    ("validateSegment trailing dot", "refs/a.md.", "ends in '.' or space"),
    ("validateSegment trailing space", "refs/a.md ", "ends in '.' or space"),
    ("validateSegment percent", "refs/%41.md", "percent-escape"),
    ("validateSegment lone surrogate", "refs/a\udc80.md", "not valid UTF-8"),
    ("validateSegment control", "refs/a\x01.md", "control character U+0001"),
    ("validateSegment DEL", "refs/a\x7f.md", "control character U+007F"),
    ("validateSegment segment bytes", "refs/" + "é" * 128, "exceeds 255 bytes"),
    *[(f"validateSegment char {char!r}", f"refs/a{char}b.md",
       "alternate data stream" if char == ":" else f"{char!r} can't be stored on Windows")
      for char in sorted(GATEWAY_INVALID_CHARS)],
]


@pytest.mark.parametrize("_rule,path,reason", PATH_RULES, ids=[rule[0] for rule in PATH_RULES])
def test_each_gateway_path_rule_is_refused_locally(_rule: str, path: str, reason: str):
    _refused(path, reason)


@pytest.mark.parametrize("name", sorted(GATEWAY_WINDOWS_RESERVED_NAMES))
def test_each_gateway_windows_reserved_name_is_refused_locally(name: str):
    for spelling in (name, name.upper() + ".txt", f"{name}.tar.gz"):
        _refused(f"refs/{spelling}", f"reserved Windows device name {name!r}")


@pytest.mark.parametrize("name", sorted(GATEWAY_BLOCKED_PATH_SEGMENTS))
def test_each_gateway_blocked_segment_is_refused_locally_in_any_spelling(name: str):
    for directory in (name, name.upper()):
        with pytest.raises(SkillRefusal) as refused:
            _verify([(f"refs/{directory}/notes.md", "file", b"x")])
        message = str(refused.value)
        assert f"package path 'refs/{directory}/notes.md' is refused: " in message
        assert f"directory {directory!r}" in message or f"named {directory!r}" in message
    # The last segment is checked too: Gateway refuses a file with the name.
    with pytest.raises(SkillRefusal, match=f"file name {name!r}|files named {name!r}"):
        _verify([(f"refs/{name}", "file", b"x")])


def test_gateway_case_fold_collision_is_refused_locally():
    with pytest.raises(SkillRefusal, match="differs from 'refs/Notes.md' only by letter case"):
        _verify([("refs/Notes.md", "file", b"a"), ("refs/notes.md", "file", b"b")])
    # Gateway keys file paths only: different files under case-variant folders pass.
    _verify([("Refs/a.md", "file", b"a"), ("refs/b.md", "file", b"b")])


@pytest.mark.parametrize("magic", sorted(GATEWAY_COMPILED_EXECUTABLE_MAGIC))
def test_each_gateway_compiled_executable_magic_is_refused_locally(magic: bytes):
    with pytest.raises(SkillRefusal, match="compiled executable content is not supported"):
        _verify([("refs/blob.bin", "file", magic + b"\x00rest")])


def test_gateway_root_file_rules_are_refused_locally():
    with pytest.raises(SkillRefusal, match="required root file is misplaced"):
        _verify([("refs/SKILL.md", "file", b"x")])
    with pytest.raises(PackagePolicyError, match="not a complete Wisdom package"):
        verify_content_files([("skill.manifest.json", "file", _MANIFEST)])
    with pytest.raises(PackagePolicyError, match="not a complete Wisdom package"):
        verify_content_files([("SKILL.md", "file", b"# test")])
    with pytest.raises(SkillRefusal, match="SKILL.md must be valid UTF-8"):
        verify_content_files([("SKILL.md", "file", b"\xff"),
                              ("skill.manifest.json", "file", _MANIFEST)])


def test_gateway_size_limits_are_refused_locally():
    with pytest.raises(PackagePolicyError, match="exceeds 10485760 total bytes"):
        _verify([("refs/big.md", "file", b"x" * GATEWAY_MAX_TOTAL_BYTES)])
    name = "n" * 200
    count = GATEWAY_MAX_METADATA_BYTES // (len(name) + 20 + GATEWAY_ENTRY_METADATA_OVERHEAD) + 2
    with pytest.raises(PackagePolicyError, match="path metadata exceeds 2097152 bytes"):
        _verify([(f"refs/{index:015d}{name}", "file", b"") for index in range(count)])


def _prepare(skill: Path, root: Path):
    return _prepare_declared(skill, root, None)


SHARE_REFUSALS = [
    ("hooks/notes.md", "package path 'hooks' is refused: folders named 'hooks' can't be "
     "shared; Gateway treats them as active package directories (hook scripts). Rename it"),
    (".gitlab/x", "package path '.gitlab' is refused: folders named '.gitlab' can't be "
     "shared; Gateway treats them as active package directories (repository CI and "
     "automation metadata). Rename it"),
    (".hooks/x", "package path '.hooks' is refused: folders named '.hooks' can't be "
     "shared; Gateway treats them as active package directories (hook scripts). Rename it"),
    (".circleci/config.yml", "package path '.circleci' is refused: folders named "
     "'.circleci' can't be shared; Gateway treats them as active package directories "
     "(CI configuration). Rename it"),
    ("CON.txt", "package path 'CON.txt' is refused: 'CON.txt' is the reserved Windows "
     "device name 'con'; Gateway refuses it in every package"),
    ("aux.md", "package path 'aux.md' is refused: 'aux.md' is the reserved Windows device "
     "name 'aux'; Gateway refuses it in every package"),
    ("Hooks/x", "package path 'Hooks' is refused: directory 'Hooks' matches 'hooks' (hook "
     "scripts) except for letter case; Gateway refuses every spelling, and a "
     "case-folding filesystem treats it as 'hooks'"),
]


@pytest.mark.parametrize("relative,expected", SHARE_REFUSALS)
def test_share_refuses_gateway_refused_paths_by_name_before_any_overlay(
    tmp_path: Path, relative: str, expected: str
):
    if relative == "Hooks/x" and not _case_sensitive(tmp_path):
        pytest.skip("needs a case-sensitive filesystem")
    skill = _skill(tmp_path, {relative: b"notes\n", "scripts/hello.py": b"print(1)\n"})
    with pytest.raises(SkillRefusal) as refused:
        _prepare(skill, tmp_path)
    assert str(refused.value) == expected
    assert not (tmp_path / "overlays").exists()


def test_generated_dirs_fixture_shares_once_hooks_is_renamed(tmp_path: Path):
    """The live cw-test-generated-dirs shape: generated trees left out, hooks refused."""
    generated = {
        "scripts/hello.py": b"print('hello')\n",
        "scripts/__pycache__/hello.cpython-311.pyc": b"\x00bytecode",
        "node_modules/left-pad/index.js": b"module.exports = 1;\n",
        ".github/workflows/ci.yml": b"on: push\n",
        ".pytest_cache/v/cache/nodeids": b"[]\n",
    }
    (tmp_path / "live").mkdir()
    with pytest.raises(SkillRefusal, match="folders named 'hooks' can't be shared"):
        _prepare(_skill(tmp_path / "live", {**generated, "hooks/notes.md": b"n\n"}),
                 tmp_path / "live")
    package = _prepare(_skill(tmp_path, {**generated, "notes/notes.md": b"n\n"}), tmp_path)
    assert package.omitted_paths == (
        ".github/", ".pytest_cache/", "node_modules/", "scripts/__pycache__/",
    )
    shared = package_module.read_validated_package_files(package.overlay)
    assert [path for path, _mode, _body in shared] == [
        "SKILL.md", "notes/notes.md", "scripts/hello.py", "skill.manifest.json",
    ]
    verify_content_files(shared)


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_share_of_a_hooks_folder_names_the_rule_without_any_upload(tmp_path, surface):
    async def scenario():
        kernel = Kernel(tmp_path)
        (kernel.source / "hooks").mkdir()
        (kernel.source / "hooks" / "notes.md").write_text("notes\n", encoding="utf-8")
        ui = surface(tmp_path, kernel)
        queued = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")
        await ui.flush()
        view = ui.view(queued["review_id"])
        assert view.title == "Skill needs attention"
        assert [(item.title, item.detail) for item in view.items] == [
            ("Not shared", SHARE_REFUSALS[0][1]),
        ]
        assert not kernel.client.state.calls
        ui.runtime.close()
    asyncio.run(scenario())
