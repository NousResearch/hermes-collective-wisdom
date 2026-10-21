"""Package path policy: transport hazards everywhere, platform rules only where declared."""

import json
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
def test_directories_hermes_excludes_from_skills_are_refused_by_name(
    tmp_path: Path, relative: str, directory: str, reason: str
):
    """Each refusal names the path, the directory and why Hermes skips it."""
    skill = _skill(tmp_path, {relative: b"content\n"})
    with pytest.raises(PackagePolicyError) as share:
        _prepare(skill, tmp_path)
    message = str(share.value)
    # The tree walk meets the directory itself first and names it.
    walked = relative[: relative.index(directory) + len(directory)]
    assert repr(walked) in message and repr(directory) in message and reason in message
    assert "not treat it as skill content" in message
    with pytest.raises(PackagePolicyError) as download:
        _download([relative])
    assert repr(relative) in str(download.value) and reason in str(download.value)


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
