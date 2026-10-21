"""Package path policy: transport hazards everywhere, platform rules only where declared."""

import json
import os
import re
from pathlib import Path

import pytest

import hermes_wisdom_plugin.package as package_module
from hermes_wisdom_plugin.package import (
    PackageHostProvider,
    PackagePolicyError,
    PackageSecurityDecision,
    verify_content_files,
)
from hermes_wisdom_plugin.package_application import stage_package, tree_hashes


class _Host(PackageHostProvider):
    """Supported-host seam returning fixed frontmatter and a passing guard."""

    def __init__(self, frontmatter: dict[str, object] | None = None) -> None:
        self.frontmatter = frontmatter or {}

    def parse_skill_frontmatter(self, _source: Path) -> dict[str, object]:
        """Return the fixture frontmatter."""
        return self.frontmatter

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Allow every overlay."""
        return PackageSecurityDecision(allowed=True)


def _skill(root: Path, files: dict[str, bytes]) -> Path:
    skill = root / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test.\n---\n\n# Test\n", encoding="utf-8"
    )
    for relative, body in files.items():
        target = skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return skill


def _prepare(skill: Path, root: Path, platforms: list[str] | None = None):
    frontmatter: dict[str, object] = {"platforms": platforms} if platforms is not None else {}
    return package_module.prepare_package(
        skill, host=_Host(frontmatter), overlay_root=root / "overlays",
        author_description="A valid description.", owner="owner",
        installation_id="installation-123456",
    )


def _download(paths: list[str], platforms: list[str] | None = None):
    manifest: dict[str, object] = {"schema_version": 1}
    if platforms is not None:
        manifest["requirements"] = {"platforms": platforms}
    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", json.dumps(manifest).encode()),
    ]
    return verify_content_files(base + [(path, "file", b"x") for path in paths])


INERT = {
    "assets/hooks/run": b"#!/bin/sh\necho inert\n",
    "refs/.hooks/pre-commit": b"inert hook text\n",
    "refs/.gitlab/ci.yml": b"stages: [docs]\n",
    "refs/.circleci/config.yml": b"version: 2\n",
    "refs/bad?name.txt": b"question mark\n",
    "refs/trailing.txt.": b"trailing dot\n",
    "refs/NUL.txt": b"device name elsewhere\n",
    "refs/Notes.txt": b"upper\n",
    "refs/notes.txt": b"lower\n",
}


@pytest.mark.parametrize("platforms", [["linux"], None])
def test_inert_repository_names_and_linux_paths_share_byte_for_byte(
    tmp_path: Path, platforms
):
    """Names with no meaning to Hermes inside a skill are ordinary content."""
    package = _prepare(_skill(tmp_path, INERT), tmp_path, platforms)
    raw = package_module.read_validated_package_files(package.overlay)
    by_path = {path: body for path, _mode, body in raw}
    for relative, body in INERT.items():
        assert by_path[relative] == body
    assert set(INERT) <= {item.path for item in package.files}
    _download(list(INERT), platforms)


GENERATED = {
    "refs/.git/config": "refs/.git/",
    "refs/.github/workflows/note.md": "refs/.github/",
    "node_modules/left-pad/index.js": "node_modules/",
    "scripts/.venv/pyvenv.cfg": "scripts/.venv/",
    "venv/bin/activate": "venv/",
    "scripts/__pycache__/x.cpython-311.pyc": "scripts/__pycache__/",
    ".tox/py311/log.txt": ".tox/",
    "lib/site-packages/pkg/__init__.py": "lib/site-packages/",
    ".pytest_cache/v/cache/nodeids": ".pytest_cache/",
    "scripts/helper.pyc": "scripts/helper.pyc",
    "scripts/legacy.pyo": "scripts/legacy.pyo",
}


def test_generated_paths_hermes_does_not_load_are_left_out_and_listed(tmp_path: Path):
    """Share skips what Hermes skill discovery skips, and names every path it left out."""
    files = {**dict.fromkeys(GENERATED, b"generated\n"), "scripts/run.py": b"print(1)\n"}
    package = _prepare(_skill(tmp_path, files), tmp_path)
    assert package.omitted_paths == tuple(sorted(set(GENERATED.values())))
    shared = {path for path, _mode, _body in
              package_module.read_validated_package_files(package.overlay)}
    assert shared == {"SKILL.md", "skill.manifest.json", "scripts/run.py"}
    assert {item.path for item in package.files} == shared
    assert package_module.left_out_line({"omitted_paths": list(package.omitted_paths)}) == (
        "Left out (Hermes doesn't load these): " + ", ".join(package.omitted_paths)
    )


def test_every_name_an_installer_refuses_is_one_sharing_leaves_out():
    """A package Hermes builds can never carry a path its installers refuse."""
    assert set(package_module.BLOCKED_PATH_SEGMENTS) <= package_module.GENERATED_SKILL_DIRS


def test_source_hash_binds_what_was_left_out(tmp_path: Path):
    """Unchanged with nothing left out; a new generated path changes it."""
    skill = _skill(tmp_path, {"scripts/run.py": b"print(1)\n"})
    plain = package_module.source_fingerprint(skill)
    records = "".join(
        f"{path} file {package_module.sha256_address(body)}\n"
        for path, body in sorted({
            "SKILL.md": (skill / "SKILL.md").read_bytes(), "scripts/run.py": b"print(1)\n",
        }.items())
    )
    assert plain == package_module.sha256_address(records.encode("utf-8"))
    (skill / "scripts" / "__pycache__").mkdir()
    (skill / "scripts" / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00")
    assert package_module.source_fingerprint(skill) != plain
    assert _prepare(skill, tmp_path).omitted_paths == ("scripts/__pycache__/",)


@pytest.mark.parametrize("name", ["link.pyc", "evil\x01.pyc"])
def test_real_hazards_named_like_bytecode_stay_refused(tmp_path: Path, name: str):
    """Only regular, valid bytecode files are left out; symlinks and bad names refuse."""
    skill = _skill(tmp_path, {})
    if name == "link.pyc":
        (skill / name).symlink_to(skill / "SKILL.md")
        rule = "symlinks are not supported: link.pyc"
    else:
        (skill / name).write_bytes(b"x")
        rule = "control character U+0001"
    with pytest.raises(PackagePolicyError, match=re.escape(rule)):
        _prepare(skill, tmp_path)


def test_symlink_inside_a_shared_directory_stays_refused(tmp_path: Path):
    """Leaving generated paths out never relaxes the symlink refusal."""
    skill = _skill(tmp_path, {"scripts/run.py": b"print(1)\n"})
    (skill / "scripts" / "link").symlink_to(skill / "SKILL.md")
    with pytest.raises(PackagePolicyError, match="symlinks are not supported: scripts/link"):
        _prepare(skill, tmp_path)


@pytest.mark.parametrize("relative", ["__pycache__", "scripts/__pycache__", ".git", "node_modules"])
def test_a_symlink_named_like_a_generated_directory_is_refused_not_left_out(
    tmp_path: Path, relative: str
):
    """Only a real directory is left out by name; a symlink keeps its refusal."""
    skill = _skill(tmp_path, {"scripts/run.py": b"print(1)\n"})
    (skill / relative).symlink_to(skill / "SKILL.md")
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path)
    assert str(share.value) == f"symlinks are not supported: {relative}"
    with pytest.raises(PackagePolicyError, match=re.escape(relative)):
        package_module.source_fingerprint(skill)


def test_a_directory_symlink_named_like_a_generated_directory_is_refused(tmp_path: Path):
    """A link to a real directory is still a link: refused, never followed or left out."""
    skill = _skill(tmp_path, {"refs/notes.md": b"notes\n"})
    (skill / "scripts").mkdir()
    (skill / "scripts" / "__pycache__").symlink_to(skill / "refs", target_is_directory=True)
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path)
    assert str(share.value) == "symlinks are not supported: scripts/__pycache__"


def test_a_fifo_named_like_a_generated_directory_is_refused(tmp_path: Path):
    """Special filesystem content keeps its refusal whatever it is called."""
    skill = _skill(tmp_path, {})
    os.mkfifo(skill / ".git")
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path)
    assert str(share.value) == "special filesystem content is not supported: .git"


def test_a_regular_file_named_like_a_generated_directory_meets_the_normal_rules(
    tmp_path: Path,
):
    """Never left out: an install-refused name refuses; any other name is shared."""
    skill = _skill(tmp_path, {"node_modules": b"not a directory\n"})
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path)
    assert str(share.value).startswith(
        "package path 'node_modules' is refused: directory 'node_modules' is an "
        "installed dependency tree"
    )
    (tmp_path / "other").mkdir()
    other = _skill(tmp_path / "other", {".pytest_cache": b"a file\n", "refs/.nox": b"x\n"})
    package = _prepare(other, tmp_path / "other")
    assert not package.omitted_paths
    shared = {path: body for path, _mode, body in
              package_module.read_validated_package_files(package.overlay)}
    assert shared[".pytest_cache"] == b"a file\n" and shared["refs/.nox"] == b"x\n"


@pytest.mark.parametrize(
    "relative,directory,reason",
    [
        ("refs/.git/config", ".git", "version-control metadata"),
        ("refs/.github/workflows/note.md", ".github", "repository CI"),
        ("node_modules/left-pad/index.js", "node_modules", "installed dependency tree"),
        ("scripts/.venv/pyvenv.cfg", ".venv", "Python virtual environment"),
        ("venv/bin/activate", "venv", "Python virtual environment"),
        ("scripts/__pycache__/x.pyc", "__pycache__", "generated Python bytecode"),
        (".tox/py311/log.txt", ".tox", "generated test environment"),
    ],
)
def test_downloaded_package_with_a_generated_directory_is_refused_by_name(
    relative: str, directory: str, reason: str
):
    """A Hermes-built package never has one, so a download that does is refused exactly."""
    with pytest.raises(PackagePolicyError) as download:
        _download([relative])
    assert str(download.value) == (
        f"package path {relative!r} is refused: directory {directory!r} is "
        f"{package_module.BLOCKED_PATH_SEGMENTS[directory]}; Hermes does not treat it as "
        "skill content, so it would not reach the agents that install this skill"
    )
    assert reason in str(download.value)


@pytest.mark.parametrize(
    "relative,rule",
    [
        ("refs/bad?name.txt", "cannot be stored on Windows"),
        ("refs/trailing.txt.", "ends in '.' or space"),
        ("refs/com1.notes.txt", "reserved Windows device name"),
    ],
)
def test_declared_windows_refuses_windows_names_before_writing_an_overlay(
    tmp_path: Path, relative: str, rule: str
):
    """Windows rules apply when the skill declares Windows, naming path and rule."""
    skill = _skill(tmp_path, {relative: b"x"})
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path, ["windows"])
    assert repr(relative) in str(share.value) and rule in str(share.value)
    assert not (tmp_path / "overlays").exists()
    with pytest.raises(PackagePolicyError, match=rule):
        _download([relative], ["Windows"])


@pytest.mark.parametrize("platforms", [["windows"], ["macos"], ["linux", "macos"]])
def test_case_fold_collisions_are_refused_only_for_case_folding_platforms(
    tmp_path: Path, platforms
):
    """A declared case-folding platform cannot hold Notes.txt and notes.txt."""
    files = {"refs/Notes.txt": b"one", "refs/notes.txt": b"two"}
    with pytest.raises(PackagePolicyError, match="only by letter case"):
        _prepare(_skill(tmp_path, files), tmp_path, platforms)
    with pytest.raises(PackagePolicyError, match="only by letter case"):
        _download(list(files), platforms)


@pytest.mark.parametrize(
    "path,rule",
    [
        ("refs/../escape.txt", "path traversal"),
        ("/absolute.txt", "absolute path"),
        ("refs/control\x01.txt", "control character U+0001"),
        ("refs/bad:name.txt", "alternate data stream"),
        ("refs/cafe\u0301.txt", "canonical (NFC)"),
        ("refs\\escape.txt", "backslash"),
        ("refs/%2e%2e/escape.txt", "percent-escape"),
    ],
)
def test_transport_hazards_stay_refused_on_every_platform(path: str, rule: str):
    """Undeclared and Linux-only packages still refuse transport hazards."""
    for platforms in (None, ["linux"]):
        with pytest.raises(PackagePolicyError) as refused:
            _download([path], platforms)
        assert repr(path) in str(refused.value) and rule in str(refused.value)


def test_installer_names_a_collision_the_target_filesystem_creates(tmp_path: Path):
    """Two entries landing on one file fail with the exact path, leaving no staging."""
    files = [
        ("SKILL.md", "file", b"# test"),
        ("refs/notes.txt", "file", b"one"),
        ("refs/notes.txt", "file", b"two"),
    ]
    staging = tmp_path / "staging"
    with pytest.raises(PackagePolicyError, match="'refs/notes.txt' is refused: it collides"):
        stage_package(staging, files, {"never": "matches"})
    assert tree_hashes(staging) is None
