"""Native Hermes package preparation and verification tests."""

import json
import os
from pathlib import Path

import pytest

import hermes_wisdom_plugin.package as package_module
from hermes_wisdom_plugin.package import (
    PackageHostProvider,
    PackagePolicyError,
    PackageSecurityDecision,
    infer_authoring_system_specification,
    verify_content_files,
)


class _Host(PackageHostProvider):
    """Deterministic supported-host seam for package kernel tests."""

    hermes_version = "9.8.7"

    def __init__(
        self,
        *,
        allowed: bool | None = True,
        frontmatter: dict[str, object] | None = None,
    ) -> None:
        self.allowed = allowed
        self.frontmatter = frontmatter or {}
        self.checked: list[Path] = []

    def parse_skill_frontmatter(self, _source: Path) -> dict[str, object]:
        """Return the host-parsed fixture metadata."""

        return self.frontmatter

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Return a controlled host-owned security verdict."""

        self.checked.append(overlay)
        return PackageSecurityDecision(allowed=self.allowed)


class _MalformedSecurityHost(_Host):
    """Host fixture returning a value outside the provider contract."""

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Return a malformed result after recording the guarded overlay."""

        self.checked.append(overlay)


class _RaisingSecurityHost(_Host):
    """Host fixture whose security provider becomes unavailable."""

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Raise a provider failure after recording the guarded overlay."""

        self.checked.append(overlay)
        raise RuntimeError("provider failed")


def prepare_package(*args, host: PackageHostProvider | None = None, **kwargs):
    """Call production preparation with an explicit passing test provider."""

    return package_module.prepare_package(*args, host=host or _Host(), **kwargs)


def make_skill(root: Path) -> Path:
    """Create a minimal instruction-only skill fixture."""

    skill = root / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test.\n---\n\n# Test\n", encoding="utf-8"
    )
    refs = skill / "refs"
    refs.mkdir()
    (refs / "notes.txt").write_text("Exact notes.\n", encoding="utf-8")
    return skill


def test_preparation_creates_only_instruction_overlay_and_hashes(tmp_path: Path):
    """Preparation copies allowlisted bytes and derives package hashes."""

    skill = make_skill(tmp_path)
    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="  <b>Does</b> the useful thing. \r\n",
        owner="owner",
        installation_id="installation-123456",
    )
    assert package.description == "Does the useful thing."
    assert {item.path for item in package.files} == {
        "SKILL.md",
        "refs/notes.txt",
        "skill.manifest.json",
    }
    manifest = json.loads(
        (package.overlay / "skill.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["requirements"]["tools"] == []
    assert manifest["requirements"]["hermes"]["minimum_version"] == _Host.hermes_version
    assert len(manifest["requirements"]["platforms"]) == 1
    assert len(manifest["requirements"]["architectures"]) == 1
    assert package.content_hash.startswith("sha256:")


def test_authoring_defaults_normalize_device_and_explicit_skill_requirements(
    tmp_path: Path,
):
    """Generated requirements use explicit metadata and normalized host facts."""

    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text(
        """---
name: my-skill
description: Test.
metadata:
  hermes:
    requires_toolsets: [terminal, browser]
    requires_tools: [execute_code, terminal]
    requires_plugins:
      - local/example-plugin
      - id: local/optional-plugin
        minimum_version: 2.4.0
        required: false
---

# Test
""",
        encoding="utf-8",
    )

    specification = infer_authoring_system_specification(
        skill,
        hermes_version="9.8.7",
        system_name="Darwin",
        machine="aarch64",
        frontmatter={
            "name": "my-skill",
            "description": "Test.",
            "metadata": {
                "hermes": {
                    "requires_toolsets": ["terminal", "browser"],
                    "requires_tools": ["execute_code", "terminal"],
                    "requires_plugins": [
                        "local/example-plugin",
                        {
                            "id": "local/optional-plugin",
                            "minimum_version": "2.4.0",
                            "required": False,
                        },
                    ],
                }
            },
        },
    )

    assert specification.hermes.minimum_version == "9.8.7"
    assert specification.platforms == ["macOS"]
    assert specification.architectures == ["arm64"]
    assert [requirement.name for requirement in specification.tools] == [
        "terminal",
        "browser",
        "execute_code",
    ]
    assert all(requirement.auto_install is False for requirement in specification.tools)
    runtime = getattr(specification, "runtime")
    assert runtime.shell is True
    assert runtime.browser is True
    assert runtime.code is True
    assert [requirement.model_dump() for requirement in specification.plugins] == [
        {
            "id": "local/example-plugin",
            "minimum_version": None,
            "required": True,
        },
        {
            "id": "local/optional-plugin",
            "minimum_version": "2.4.0",
            "required": False,
        },
    ]


def test_explicit_portability_declarations_override_device_defaults(tmp_path: Path):
    """Explicit portability declarations override authoring-device defaults."""

    skill = make_skill(tmp_path)
    (skill / "SKILL.md").write_text(
        """---
name: my-skill
description: Test.
platforms: [linux, windows]
architectures: []
---

# Test
""",
        encoding="utf-8",
    )

    specification = infer_authoring_system_specification(
        skill,
        hermes_version="1.2.3",
        system_name="Darwin",
        machine="arm64",
        frontmatter={
            "name": "my-skill",
            "description": "Test.",
            "platforms": ["linux", "windows"],
            "architectures": [],
        },
    )

    assert specification.platforms == ["Linux", "Windows"]
    assert specification.architectures == []


def test_existing_manifest_is_preserved_instead_of_reinferred(tmp_path: Path):
    """Existing manifest bytes remain exact instead of being regenerated."""

    skill = make_skill(tmp_path)
    manifest = {
        "schema_version": 1,
        "name": "my-skill",
        "requirements": {
            "hermes": {"minimum_version": "0.3.0"},
            "platforms": [],
            "architectures": [],
            "model": {"capabilities": [], "minimum_context_window": None},
            "tools": [],
            "plugins": [],
            "credentials": [],
            "connections": [],
            "filesystem": {"read": [], "write": []},
            "network": {"destinations": []},
            "runtime": {
                "shell": False,
                "browser": False,
                "code": False,
                "sandbox": True,
            },
            "hardware": [],
            "known_limitations": [],
        },
    }
    original = json.dumps(manifest, separators=(",", ":")).encode()
    (skill / "skill.manifest.json").write_bytes(original)

    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )

    assert (package.overlay / "skill.manifest.json").read_bytes() == original


@pytest.mark.parametrize("extension", [".txt", ".md", ".rst", ".adoc", ".asciidoc"])
def test_allowlisted_inert_text_references_are_accepted(tmp_path: Path, extension: str):
    """Every explicitly allowlisted inert text extension can be packaged."""

    skill = make_skill(tmp_path)
    (skill / "assets" / f"guide{extension}").parent.mkdir(exist_ok=True)
    (skill / "assets" / f"guide{extension}").write_text(
        "Static reference material.\n", encoding="utf-8"
    )
    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )
    assert f"assets/guide{extension}" in {item.path for item in package.files}


def test_native_skill_round_trips_scripts_resources_and_modes(tmp_path: Path):
    """Native package files keep exact bytes and normalized executable intent."""

    skill = make_skill(tmp_path)
    skill_md = skill / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nRun scripts/run.sh.\n", encoding="utf-8")
    fixtures = {
        ".gitignore": b"*.pyc\n",
        "references/config.yaml": b"enabled: true\n",
        "scripts/run.sh": b"#!/bin/sh\nprintf sentinel\n",
        "templates/report.docx": b"PK\x03\x04opaque-template",
        "assets/logo.png": b"\x89PNG\r\n\x1a\nopaque",
        "pyproject.toml": b"[project]\nname='fixture'\n",
    }
    for relative, body in fixtures.items():
        target = skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (skill / "scripts/run.sh").chmod(0o755)

    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )
    raw = package_module.read_validated_package_files(package.overlay)
    by_path = {path: (mode, body) for path, mode, body in raw}
    for relative, body in fixtures.items():
        assert by_path[relative][1] == body
    assert by_path["scripts/run.sh"][0] == "exec"
    assert by_path["assets/logo.png"][0] == "file"
    assert (package.overlay / "scripts/run.sh").stat().st_mode & 0o111
    assert (package.overlay / "assets/logo.png").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "relative,content",
    [
        ("scripts/run.sh", "echo nope"),
        ("templates/active.md", "active"),
        ("package.json", "{}"),
        ("refs/package.json", "{}"),
        ("refs/run.sh", "echo nope"),
        ("refs/template.j2", "{{ active }}"),
        ("refs/config.yaml", "active: true"),
        ("assets/page.html", "<script>active</script>"),
        ("assets/logo.svg", "<svg></svg>"),
        ("assets/archive.zip", "not really an archive"),
        ("refs/README", "unknown extension"),
    ],
)
def test_ordinary_native_supporting_files_are_not_silently_omitted(
    tmp_path: Path, relative: str, content: str
):
    """Ordinary source, config, dependency, and template files are complete."""

    skill = make_skill(tmp_path)
    target = skill / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    package = prepare_package(
        skill,
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )
    assert (package.overlay / relative).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("relative", ["refs/.github/workflows/note.md", "assets/hooks/run"])
def test_active_vcs_and_hook_metadata_remains_blocked(tmp_path: Path, relative: str):
    """Native support does not admit VCS automation metadata or hook directories."""
    skill = make_skill(tmp_path)
    target = skill / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unsafe metadata", encoding="utf-8")
    with pytest.raises(PackagePolicyError, match="active package directory"):
        prepare_package(
            skill, overlay_root=tmp_path / "overlays",
            author_description="A valid description.", owner="owner",
            installation_id="installation-123456",
        )


def test_download_rejects_hostile_paths_but_accepts_native_modes_and_binary():
    """Downloads reject traversal while preserving native opaque resources."""

    manifest = b'{"schema_version":1}'
    base = [("SKILL.md", "file", b"# test"), ("skill.manifest.json", "file", manifest)]
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [("../escape", "file", b"x")])
    records, _ = verify_content_files(base + [
        ("scripts/run", "exec", b"#!/bin/sh\necho safe-to-package\n"),
        ("assets/image.png", "file", b"\x89PNG\x00\xff"),
    ])
    assert [(item.path, item.mode) for item in records[-2:]] == [
        ("scripts/run", "exec"), ("assets/image.png", "file")
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.txt",
        "refs//empty.txt",
        "refs/./dot.txt",
        "refs/../escape.txt",
        "refs/%2e%2e/escape.txt",
        "refs/%252e%252e/escape.txt",
        "refs/%2fescape.txt",
        "refs\\escape.txt",
        "refs/control\x00.txt",
        "refs/control\x1f.txt",
        "refs/control\x7f.txt",
        "refs/trailing.txt.",
        "refs/trailing.txt ",
        "refs/NUL.txt",
        "refs/com1.notes.txt",
        "refs/bad:name.txt",
        "refs/cafe\u0301.txt",

    ],
)
def test_download_rejects_nonportable_or_noncanonical_paths(path: str):
    """Downloaded paths must be portable and canonically encoded."""

    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [(path, "file", b"reference")])


@pytest.mark.parametrize("path", ["refs/SKILL.md", "assets/skill.manifest.json"])
def test_download_rejects_misplaced_required_root_files(path: str):
    """Root identity files cannot be shadowed in supporting directories."""

    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [(path, "file", b"apparently harmless")])


def test_download_rejects_compiled_executable_but_not_opaque_resource():
    """General native binaries are unsupported regardless of declared file mode."""
    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    with pytest.raises(PackagePolicyError, match="compiled executable"):
        verify_content_files(base + [("scripts/tool", "exec", b"\x7fELFpayload")])
    with pytest.raises(PackagePolicyError, match="compiled executable"):
        verify_content_files(
            base + [("assets/tool-reference.bin", "file", b"\x7fELFpayload")]
        )
    records, _ = verify_content_files(base + [("assets/image.png", "file", b"\x89PNG")])
    assert records[-1].mode == "file"


@pytest.mark.parametrize(
    "entry",
    [
        ("refs/value", None, b"x"),
        ("refs/value", "file", "not-bytes"),
        (None, "file", b"x"),
        ("refs/value", "file"),
    ],
)
def test_download_malformed_entries_raise_typed_policy_errors(entry):
    """Malformed wire records fail with the package policy error contract."""
    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    with pytest.raises(PackagePolicyError):
        verify_content_files(base + [entry])


def test_source_metadata_budget_stops_before_reading_entire_empty_tree(
    tmp_path: Path, monkeypatch
):
    """Metadata admission stops a huge empty tree before payload reads fan out."""
    skill = make_skill(tmp_path)
    for number in range(100):
        (skill / "refs" / f"empty-{number}.txt").touch()
    reads = 0
    original = package_module._read_regular_file  # pylint: disable=protected-access

    def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(package_module, "MAX_METADATA_BYTES", 600)
    monkeypatch.setattr(package_module, "_read_regular_file", counted)
    with pytest.raises(PackagePolicyError, match="metadata"):
        package_module.source_fingerprint(skill)
    assert reads < 10


def test_download_rejects_install_target_collisions():
    """Case-folding install-target collisions fail closed."""

    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    with pytest.raises(PackagePolicyError, match="collision"):
        verify_content_files(
            base
            + [
                ("refs/Notes.txt", "file", b"one"),
                ("refs/notes.txt", "file", b"two"),
            ]
        )


def test_download_uses_aggregate_payload_and_metadata_caps(monkeypatch):
    """Many files and one large file are accepted within independent budgets."""

    base = [
        ("SKILL.md", "file", b"# test"),
        ("skill.manifest.json", "file", b'{"schema_version":1}'),
    ]
    many = base + [(f"references/{number}.txt", "file", b"") for number in range(80)]
    records, _ = verify_content_files(many)
    assert len(records) == 82

    large = b"x" * (512 * 1024)
    records, _ = verify_content_files(base + [("references/large.txt", "file", large)])
    assert records[-1].path == "references/large.txt"

    monkeypatch.setattr(package_module, "MAX_TREE_BYTES", 32)
    with pytest.raises(PackagePolicyError, match="total bytes"):
        verify_content_files(base + [("refs/total.txt", "file", b"x" * 16)])


def test_local_preparation_enforces_aggregate_size_cap(tmp_path: Path, monkeypatch):
    """Local source trees cannot exceed the aggregate payload bound."""

    skill = make_skill(tmp_path)
    monkeypatch.setattr(package_module, "MAX_TREE_BYTES", 64)
    (skill / "refs" / "large.txt").write_bytes(b"x" * 65)
    with pytest.raises(PackagePolicyError, match="total bytes"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_local_preparation_rejects_duplicate_manifest_keys(tmp_path: Path):
    """Ambiguous duplicate manifest keys fail preparation."""

    skill = make_skill(tmp_path)
    manifest = (
        b'{"schema_version":1,"schema_version":1,"name":"duplicate","requirements":{}}'
    )
    (skill / "skill.manifest.json").write_bytes(manifest)
    with pytest.raises(PackagePolicyError, match="manifest is invalid"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_local_preparation_rejects_hard_links(tmp_path: Path):
    """Hard-linked source content cannot enter a package."""

    skill = make_skill(tmp_path)
    os.link(skill / "refs" / "notes.txt", skill / "refs" / "alias.txt")
    with pytest.raises(PackagePolicyError, match="hard-linked"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFO support")
def test_local_preparation_rejects_special_files(tmp_path: Path):
    """Special filesystem nodes cannot enter a package."""

    skill = make_skill(tmp_path)
    os.mkfifo(skill / "refs" / "special.txt")
    with pytest.raises(PackagePolicyError, match="special filesystem"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_mode_only_change_changes_source_fingerprint(tmp_path: Path):
    """Changing executable intent invalidates exact-package review identity."""

    skill = make_skill(tmp_path)
    script = skill / "run"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o600)
    before = package_module.source_fingerprint(skill)
    script.chmod(0o700)
    assert package_module.source_fingerprint(skill) != before


@pytest.mark.parametrize("allowed", [False])
def test_preparation_preserves_blocked_host_security_as_reviewable_warning(
    tmp_path: Path, allowed: bool | None
):
    """A scanner denial is evidence and does not prevent a local draft snapshot."""

    skill = make_skill(tmp_path)
    original = (skill / "SKILL.md").read_bytes()
    package = prepare_package(
        skill,
        host=_Host(allowed=allowed),
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )
    assert package.overlay.is_dir()
    assert (skill / "SKILL.md").read_bytes() == original


def test_repeated_preparation_never_deletes_an_edited_overlay(tmp_path: Path):
    """The package kernel refuses to replace a previously prepared private overlay."""

    skill = make_skill(tmp_path)
    overlays = tmp_path / "overlays"
    package = prepare_package(
        skill,
        overlay_root=overlays,
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )
    overlay_skill = package.overlay / "SKILL.md"
    overlay_skill.write_text("owner-edited private overlay\n", encoding="utf-8")

    with pytest.raises(PackagePolicyError, match="existing draft"):
        prepare_package(
            skill,
            overlay_root=overlays,
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )
    assert overlay_skill.read_text(encoding="utf-8") == "owner-edited private overlay\n"


def test_source_race_is_rejected_instead_of_mixing_file_versions(
    tmp_path: Path, monkeypatch
):
    """A source mutation during snapshotting cannot produce a mixed package."""

    skill = make_skill(tmp_path)
    original_reader = package_module._read_regular_file  # pylint: disable=protected-access
    changed = False

    def racing_reader(
        path: Path, *, display_path: str, max_bytes: int | None = None
    ) -> bytes:
        nonlocal changed
        body = original_reader(
            path, display_path=display_path, max_bytes=max_bytes
        )
        if not changed and path == skill / "SKILL.md":
            changed = True
            path.write_text("changed during preparation\n", encoding="utf-8")
        return body

    monkeypatch.setattr(package_module, "_read_regular_file", racing_reader)
    with pytest.raises(PackagePolicyError, match="source changed"):
        prepare_package(
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


@pytest.mark.parametrize("host", [_MalformedSecurityHost(), _RaisingSecurityHost()])
def test_malformed_or_raising_security_provider_fails_with_policy_error(
    tmp_path: Path, host: PackageHostProvider
):
    """Provider bugs remain typed fail-closed package errors."""

    skill = make_skill(tmp_path)
    with pytest.raises(PackagePolicyError, match="security guard"):
        prepare_package(
            skill,
            host=host,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_preparation_requires_explicit_host_provider(tmp_path: Path):
    """Package preparation has no implicit or private host fallback."""

    skill = make_skill(tmp_path)
    with pytest.raises(TypeError, match="host"):
        package_module.prepare_package(  # pylint: disable=missing-kwoa
            skill,
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
        )


def test_editorial_application_is_an_explicit_plugin_owned_callback(tmp_path: Path):
    """Editorial business logic is delegated separately from the host API."""

    skill = make_skill(tmp_path)
    calls: list[tuple[Path, str | None, str | None]] = []

    def apply_editorial(
        overlay: Path,
        *,
        editorial_name: str | None,
        editorial_description: str | None,
    ) -> None:
        calls.append((overlay, editorial_name, editorial_description))

    package = prepare_package(
        skill,
        host=_Host(),
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
        editorial_name="Reviewed name",
        editorial_description="Reviewed description",
        editorial_applier=apply_editorial,
    )

    assert calls == [
        (package.overlay, "Reviewed name", "Reviewed description")
    ]


def test_editorial_values_require_plugin_owned_applier(tmp_path: Path):
    """Editorial values cannot silently disappear when the plugin hook is absent."""

    skill = make_skill(tmp_path)
    with pytest.raises(PackagePolicyError, match="editorial metadata applier"):
        prepare_package(
            skill,
            host=_Host(),
            overlay_root=tmp_path / "overlays",
            author_description="A valid description.",
            owner="owner",
            installation_id="installation-123456",
            editorial_name="Reviewed name",
        )


def test_host_security_guard_receives_final_exact_overlay(tmp_path: Path):
    """The host guard sees generated manifest and every exact staged file."""

    skill = make_skill(tmp_path)
    host = _Host()
    package = prepare_package(
        skill,
        host=host,
        overlay_root=tmp_path / "overlays",
        author_description="A valid description.",
        owner="owner",
        installation_id="installation-123456",
    )

    assert host.checked == [package.overlay]
    assert (host.checked[0] / "skill.manifest.json").is_file()
    guarded_files = {
        path.relative_to(package.overlay).as_posix()
        for path in host.checked[0].rglob("*")
        if path.is_file()
    }
    assert guarded_files == {
        "SKILL.md",
        "refs/notes.txt",
        "skill.manifest.json",
    }
