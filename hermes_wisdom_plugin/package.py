"""Native Hermes skill package preparation and exact-byte verification."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .contract import (
    ContentFile,
    FilesystemRequirement,
    HermesRequirement,
    ModelRequirement,
    NetworkRequirement,
    PackageManifest,
    PluginRequirement,
    RuntimeRequirement,
    SystemSpecification,
    ToolRequirement,
    author_description_hash,
    derive_content_hash,
    load_manifest,
    sanitize_author_description,
    sha256_address,
)
from .setup_document import SETUP_PATH, SetupGuidanceError, check_shared_setup_document
from .wire import ObjectSet, build_commit, build_tree


MAX_TREE_BYTES = 10 * 1024 * 1024
# Paths and canonical tree entries are bounded separately so empty files cannot
# turn validation/serialization into unbounded work. This is deliberately not a
# product file-count limit.
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_TREE_DEPTH = 16
MAX_SEGMENT_BYTES = 255
REQUIRED_ROOT_FILES = frozenset({"SKILL.md", "skill.manifest.json"})
# Gateway refuses every package path with one of these rules, whatever the
# skill declares, so sharing refuses them locally first with a reason naming
# the path. Mirrors gateway-gateway src/sync/packageValidator.ts
# (``WINDOWS_RESERVED_NAMES``, ``validateSegment``); see
# tests/test_gateway_package_parity.py for the pinned revision.
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
# Characters a Windows filesystem cannot store. ``:`` has its own reason
# (Hermes skill installs refuse it too: Windows drive or alternate data stream).
WINDOWS_INVALID_CHARS = frozenset('<>"|?*')
PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
# Directory names Hermes does not treat as skill content (``EXCLUDED_SKILL_DIRS``
# in agent/skill_utils.py): skill discovery prunes them. Sharing leaves them
# out of the package, like the Hermes skills hub ("Dotfiles, bytecode and
# __pycache__ never ship in a bundle", tools/skills_hub_github.py), and lists
# what it left out. Running a skill's script must not make it unshareable.
# Names match exactly, as Hermes skill discovery does (case-sensitive), and only
# for real directories. A case variant of a Gateway-refused name is refused by
# name instead (``_check_blocked_segment``); any other case variant is shared.
GENERATED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
# Python bytecode files, matched like the Hermes skills hub
# (``base.endswith(".pyc")``). ``.pyo`` is ordinary content there, and Python 3
# never writes one (PEP 488), so it is ordinary content here too.
GENERATED_FILE_SUFFIXES = (".pyc",)
LEFT_OUT_LABEL = "Left out (Hermes doesn't load these)"
# Gateway refuses a package with any of these names, case-folded, in any path
# segment ("prohibited active package directory": ``BLOCKED_PATH_SEGMENTS`` in
# gateway-gateway src/sync/packageValidator.ts). Those Hermes also skips
# (GENERATED_SKILL_DIRS) are left out of a share when they are real
# directories; every other occurrence, including the names Hermes loads
# (hooks, .hooks, .gitlab, .circleci) and any case variant, is refused locally
# by name before upload. Each entry says why.
BLOCKED_PATH_SEGMENTS = {
    ".circleci": "CI configuration",
    ".git": "version-control metadata, which can carry hooks and configuration",
    ".github": "repository CI and automation metadata",
    ".gitlab": "repository CI and automation metadata",
    ".hooks": "hook scripts",
    ".tox": "a generated test environment",
    ".venv": "a Python virtual environment",
    "__pycache__": "generated Python bytecode",
    "hooks": "hook scripts",
    "node_modules": "an installed dependency tree",
    "venv": "a Python virtual environment",
}
# plugin.yaml ``requires_hermes``: the floor written when a skill declares none.
PLUGIN_HERMES_FLOOR = "0.21.5"
# Gateway ``COMPILED_EXECUTABLE_MAGIC``: ELF, PE, Mach-O (thin and universal,
# both byte orders; CAFEBABE also covers Java classes) and WebAssembly.
_COMPILED_EXECUTABLE_MAGIC = (
    b"\x7fELF", b"MZ", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    b"\x00asm",
)


class PackagePolicyError(ValueError):
    """Package bytes, modes, or paths violate the native package policy."""


class SkillRefusal(PackagePolicyError):
    """The skill's own files or declarations break a package rule.

    Raised only with plugin-authored text naming the rule and the skill's own
    path, so a chat card may show it: the owner can fix it in the skill's
    folder. Every other failure, including a plain ``PackagePolicyError`` or
    any Gateway-supplied text, keeps the generic reply.
    """


@dataclass(frozen=True)
class PackageSecurityDecision:
    """Host-owned guard result for the exact prepared overlay."""

    allowed: bool | None
    available: bool = True
    verdict: str | None = None
    summary: str | None = None
    findings: tuple[dict[str, object], ...] = ()


class PackageHostProvider(Protocol):
    """Supported host capabilities required during package preparation."""

    def parse_skill_frontmatter(self, source: Path) -> dict[str, object]:
        """Parse source SKILL.md metadata through the supported host API."""

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Run the host-owned guard against the exact prepared overlay."""


class EditorialMetadataApplier(Protocol):  # pylint: disable=too-few-public-methods
    """Plugin-owned callback for applying reviewed overlay presentation data."""

    def __call__(
        self,
        overlay: Path,
        *,
        editorial_name: str | None,
        editorial_description: str | None,
    ) -> None:
        """Apply reviewed editorial values without changing source bytes."""


def _require_host_security(
    host: PackageHostProvider, overlay: Path
) -> None:
    """Convert every invalid or unavailable guard outcome to a policy error."""

    try:
        security = host.check_package_security(overlay)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise PackagePolicyError("host package security guard is unavailable") from exc
    if not isinstance(security, PackageSecurityDecision):
        raise PackagePolicyError("host package security guard returned an invalid result")
    # A negative scanner verdict is retained and presented by the contribution
    # workflow; it is not a structural packaging veto. Unavailable scanning still
    # fails closed because there is no evidence for a human to review.
    if not isinstance(security.available, bool):
        raise PackagePolicyError("host package security guard returned an invalid result")
    if security.allowed is not None and not isinstance(security.allowed, bool):
        raise PackagePolicyError("host package security guard returned an invalid result")
    if not security.available:
        raise PackagePolicyError("host package security guard is unavailable")


@dataclass(frozen=True)
class PreparedPackage:  # pylint: disable=too-many-instance-attributes
    """Validated owner-private package overlay and its exact wire objects."""

    source: Path
    overlay: Path
    source_hash: str
    content_hash: str
    description: str
    description_hash: str
    manifest_hash: str
    manifest: PackageManifest
    files: list[ContentFile]
    objects: ObjectSet
    commit: str
    # Generated source paths left out of the package (directories end in '/').
    omitted_paths: tuple[str, ...] = ()


def left_out_line(value: object) -> str | None:
    """The one display line naming generated paths a share left out, if any."""
    paths = value.get("omitted_paths") if isinstance(value, dict) else None
    if not isinstance(paths, list) or not paths:
        return None
    return LEFT_OUT_LABEL + ": " + ", ".join(str(path) for path in paths)


def _declared_strings(value: object) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    else:
        return []
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))[:64]


def _normalized_authoring_platform(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized in {"darwin", "mac", "macos", "osx"}:
        return "macOS"
    if normalized.startswith("linux"):
        return "Linux"
    if normalized.startswith("win"):
        return "Windows"
    return value.strip()


def _normalized_authoring_architecture(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    if normalized in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    return value.strip()


def _frontmatter(
    source: Path, host: PackageHostProvider
) -> dict[str, object]:
    try:
        parsed = host.parse_skill_frontmatter(source)
    except Exception:  # pylint: disable=broad-exception-caught
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hermes_metadata(frontmatter: dict[str, object]) -> dict[str, object]:
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    typed_metadata: dict[str, object] = {
        str(key): value for key, value in metadata.items()
    }
    hermes = typed_metadata.get("hermes")
    if not isinstance(hermes, dict):
        return {}
    return {str(key): value for key, value in hermes.items()}


def _tool_requirements(metadata: dict[str, object]) -> list[ToolRequirement]:
    # A toolset is a valid compatibility target too: local capability detection
    # includes both enabled toolset names and their concrete tools.
    names = [
        *_declared_strings(metadata.get("requires_toolsets")),
        *_declared_strings(metadata.get("requires_tools")),
    ]
    return [
        ToolRequirement(name=name, auto_install=False) for name in dict.fromkeys(names)
    ][:64]


def _plugin_requirements(metadata: dict[str, object]) -> list[PluginRequirement]:
    raw = metadata.get("requires_plugins")
    values = raw if isinstance(raw, list) else [raw] if raw is not None else []
    requirements: list[PluginRequirement] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str):
            plugin_id = item.strip()
            minimum_version = None
            required = True
        elif isinstance(item, dict):
            plugin: dict[str, object] = {str(key): value for key, value in item.items()}
            plugin_id = str(plugin.get("id") or "").strip()
            raw_version = plugin.get("minimum_version")
            minimum_version = (
                str(raw_version).strip() if raw_version is not None else None
            )
            required = plugin.get("required", True) is not False
        else:
            continue
        if not plugin_id or plugin_id in seen:
            continue
        seen.add(plugin_id)
        requirements.append(
            PluginRequirement(
                id=plugin_id,
                minimum_version=minimum_version or None,
                required=required,
            )
        )
        if len(requirements) == 64:
            break
    return requirements


def declared_minimum_hermes(frontmatter: dict[str, object]) -> str | None:
    """The skill's own Hermes floor (``metadata.hermes.minimum_version``), if any."""
    value = _hermes_metadata(frontmatter).get("minimum_version")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def infer_authoring_system_specification(
    source: Path,  # pylint: disable=unused-argument
    *,
    frontmatter: dict[str, object] | None = None,
) -> SystemSpecification:
    """Record only what the skill declares; never the sharer's machine or inventory.

    Hermes treats a skill without ``platforms:`` as portable and has no
    architecture gating, so an absent declaration stays absent. The manifest
    contract requires a Hermes floor: an undeclared one is the plugin's own
    floor, which every host able to install the package already meets.
    Tool and plugin requirements come only from explicit frontmatter.
    """
    frontmatter = frontmatter or {}
    metadata = _hermes_metadata(frontmatter)

    platforms = [
        _normalized_authoring_platform(item)
        for item in _declared_strings(frontmatter.get("platforms"))
    ]
    architecture_source = (
        frontmatter.get("architectures") if "architectures" in frontmatter
        else metadata.get("architectures")
    )
    architectures = [
        _normalized_authoring_architecture(item)
        for item in _declared_strings(architecture_source)
    ]

    tools = _tool_requirements(metadata)
    tool_names = {requirement.name for requirement in tools}
    return SystemSpecification(
        hermes=HermesRequirement(
            minimum_version=declared_minimum_hermes(frontmatter) or PLUGIN_HERMES_FLOOR,
        ),
        platforms=platforms,
        architectures=architectures,
        model=ModelRequirement(),
        tools=tools,
        plugins=_plugin_requirements(metadata),
        filesystem=FilesystemRequirement(),
        network=NetworkRequirement(),
        runtime=RuntimeRequirement(
            shell=bool(tool_names & {"shell", "terminal"}),
            browser=bool(tool_names & {"browser", "computer", "computer_use"}),
            code=bool(tool_names & {"code", "code_execution", "execute_code"}),
        ),
    )


def _refuse(source: str, path: str, rule: str) -> SkillRefusal:
    return SkillRefusal(f"{source} path {path!r} is refused: {rule}")


def _check_segment(segment: str, *, path: str, source: str) -> None:
    """Gateway's per-segment rules, on every package whatever it declares.

    Mirrors ``validateSegment`` in gateway-gateway src/sync/packageValidator.ts
    (blocked names are ``_check_blocked_segment``), each with its own reason.
    """

    control = next(
        (char for char in segment if ord(char) < 0x20 or ord(char) == 0x7F), None
    )
    if control is not None:
        raise _refuse(source, path, f"control character U+{ord(control):04X}")
    if ":" in segment:
        raise _refuse(
            source, path,
            "':' (Hermes skill installs refuse it: Windows drive or alternate data stream)",
        )
    if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
        raise _refuse(source, path, f"a path segment exceeds {MAX_SEGMENT_BYTES} bytes")
    invalid = next((char for char in segment if char in WINDOWS_INVALID_CHARS), None)
    if invalid is not None:
        raise _refuse(
            source, path,
            f"{invalid!r} can't be stored on Windows; Gateway refuses it in every package",
        )
    if segment.endswith((".", " ")):
        raise _refuse(
            source, path,
            "a name ends in '.' or space, which Windows strips; Gateway refuses it "
            "in every package",
        )
    device = segment.casefold().split(".", 1)[0]
    if device in WINDOWS_RESERVED_NAMES:
        raise _refuse(
            source, path,
            f"{segment!r} is the reserved Windows device name {device!r}; Gateway "
            "refuses it in every package",
        )


def _check_blocked_segment(
    segment: str, *, path: str, source: str, kind: str,
) -> None:
    """Names Gateway refuses in any segment and spelling ("active package directory").

    ``kind`` is the real entry type of this segment: "directory" or "file".
    A real directory Hermes skips is left out of a share before this runs.
    """

    folded = segment.casefold()
    reason = BLOCKED_PATH_SEGMENTS.get(folded)
    if reason is None:
        return
    # The reasons describe directories, so a file only carries the name.
    entry = f"directory {segment!r}" if kind == "directory" else f"file name {segment!r}"
    if segment != folded:
        # Hermes itself matches these names exactly, but Gateway refuses any
        # spelling, and a case-folding filesystem would treat this one as the
        # name itself. Refuse it by name; never leave it out silently.
        raise _refuse(
            source, path,
            f"{entry} matches {folded!r} ({reason}) except for letter case; Gateway "
            f"refuses every spelling, and a case-folding filesystem treats it as {folded!r}",
        )
    if folded not in GENERATED_SKILL_DIRS:
        # Hermes loads this name as skill content, but Gateway never accepts it.
        noun = "folders" if kind == "directory" else "files"
        raise _refuse(
            source, path,
            f"{noun} named {segment!r} can't be shared; Gateway treats them as active "
            f"package directories ({reason}). Rename it",
        )
    is_reason = reason if kind == "directory" else "reserved for " + reason
    raise _refuse(
        source, path,
        f"{entry} is {is_reason}; Hermes does not treat it as skill "
        "content, so it would not reach the agents that install this skill",
    )


def _validate_package_path(
    raw_path: str, *, source: str, blocked: bool = True
) -> tuple[PurePosixPath, str]:
    """Check one package path against Gateway's path rules.

    Returns the parsed path and its exact key for duplicate detection.
    Case-fold collisions span paths: see ``check_case_fold_collisions``.
    ``blocked=False`` leaves the blocked name rule to a caller that must first
    check the entry's type.
    """
    if not raw_path:
        raise _refuse(source, raw_path, "empty path")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in raw_path):
        # The filesystem name holds bytes that are not UTF-8 (Python keeps
        # them as lone surrogates); Gateway refuses a lone surrogate.
        raise _refuse(source, raw_path, "the name is not valid UTF-8")
    if "\\" in raw_path:
        raise _refuse(source, raw_path, "backslash separator ('/' is the only separator)")
    if unicodedata.normalize("NFC", raw_path) != raw_path:
        raise _refuse(source, raw_path, "Unicode is not in canonical (NFC) form")
    if PERCENT_ESCAPE_RE.search(raw_path):
        raise _refuse(source, raw_path, "percent-escape sequence")
    if raw_path.startswith("/"):
        raise _refuse(source, raw_path, "absolute path")
    if any(part in ("", ".", "..") for part in raw_path.split("/")):
        raise _refuse(source, raw_path, "empty, '.' or '..' segment (path traversal)")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts:
        raise _refuse(source, raw_path, "absolute path")
    for index, part in enumerate(pure.parts):
        _check_segment(part, path=raw_path, source=source)
        if blocked:
            # Every caller passes a file path: only its last segment is a file.
            _check_blocked_segment(
                part, path=raw_path, source=source,
                kind="file" if index == len(pure.parts) - 1 else "directory",
            )
    if len(pure.parts) - 1 > MAX_TREE_DEPTH:
        raise _refuse(source, raw_path, f"deeper than the maximum tree depth {MAX_TREE_DEPTH}")
    return pure, raw_path


def check_case_fold_collisions(paths: list[str], *, source: str) -> None:
    """Refuse two file paths that differ only by letter case, as Gateway does.

    Gateway keys every file path by its case-folded segments
    (``wisdomInstallTargetCaseFold``, aligned with ``str.casefold``) whatever
    the skill declares. On a case-folding filesystem the installer also names
    a collision exactly (``stage_package``).
    """
    seen: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        if key in seen:
            raise _refuse(
                source, path, f"differs from {seen[key]!r} only by letter case; Gateway "
                "refuses two paths that would be one file on a case-folding filesystem",
            )
        seen[key] = path


def _validate_allowed_file(pure: PurePosixPath, *, path: str) -> None:
    if len(pure.parts) > 1 and pure.name in REQUIRED_ROOT_FILES:
        raise SkillRefusal(f"required root file is misplaced: {path}")


def _validate_mode_and_bytes(mode: str, body: bytes, *, path: str) -> None:
    if not isinstance(mode, str) or not isinstance(body, bytes):
        raise PackagePolicyError(f"invalid package entry: {path}")
    if mode not in {"file", "exec"}:
        raise PackagePolicyError(f"unknown package mode: {path}")
    if body.startswith(_COMPILED_EXECUTABLE_MAGIC):
        raise SkillRefusal(f"compiled executable content is not supported: {path}")


def _normalized_mode(metadata: int) -> str:
    return "exec" if metadata & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else "file"


def _safe_relative(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    _validate_package_path(rel, source="package")
    return rel


@dataclass(frozen=True)
class _SnapshotEntry:
    """One admitted regular file from a bounded no-follow tree walk."""

    path: Path
    relative: str
    mode: str
    body: bytes


def _bounded_snapshot(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    root: Path, *, require_manifest: bool, omitted: list[str] | None = None
) -> list[_SnapshotEntry]:
    """Admit metadata and payload before sorting or further processing.

    A package walk (``omitted is None``) refuses generated paths. A source walk
    leaves real generated directories (lstat: never a symlink, never descended)
    and regular bytecode files out and appends each one's path to ``omitted``;
    any other entry with such a name meets every other rule.
    """

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise SkillRefusal("skill root must be a regular directory") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise SkillRefusal("skill root must be a regular directory")
    entries: list[_SnapshotEntry] = []
    total = 0
    metadata_total = 0
    skill_body: bytes | None = None
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_TREE_DEPTH:
            raise SkillRefusal(
                f"package path exceeds maximum tree depth {MAX_TREE_DEPTH}"
            )
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            raise PackagePolicyError("package tree changed while reading") from exc
        with iterator:
            for item in iterator:
                path = Path(item.path)
                rel = path.relative_to(root).as_posix()
                # Validate first: a name that is not UTF-8 cannot be measured.
                pure, _key = _validate_package_path(rel, source="package", blocked=False)
                metadata_total += len(rel.encode("utf-8")) + 128
                if metadata_total > MAX_METADATA_BYTES:
                    raise SkillRefusal(
                        f"package path metadata exceeds {MAX_METADATA_BYTES} bytes"
                    )
                try:
                    metadata = item.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PackagePolicyError("package tree changed while reading") from exc
                if (omitted is not None and stat.S_ISDIR(metadata.st_mode)
                        and item.name in GENERATED_SKILL_DIRS):
                    # A real directory Hermes prunes by name; never descended.
                    # Anything else with such a name meets the rules below.
                    omitted.append(rel + "/")
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise SkillRefusal(f"symlinks are not supported: {rel}")
                if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                    raise SkillRefusal(
                        f"special filesystem content is not supported: {rel}"
                    )
                for index, part in enumerate(pure.parts):
                    # Earlier segments are directories; the last is this entry.
                    final = index == len(pure.parts) - 1
                    _check_blocked_segment(
                        part, path=rel, source="package",
                        kind="file" if final and stat.S_ISREG(metadata.st_mode)
                        else "directory",
                    )
                if (omitted is not None and stat.S_ISREG(metadata.st_mode)
                        and item.name.endswith(GENERATED_FILE_SUFFIXES)):
                    omitted.append(rel)
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((path, depth + 1))
                    continue
                _validate_allowed_file(pure, path=rel)
                if metadata.st_nlink != 1:
                    raise SkillRefusal(f"hard-linked content is not supported: {rel}")
                if total + metadata.st_size > MAX_TREE_BYTES:
                    raise SkillRefusal(
                        f"package exceeds {MAX_TREE_BYTES} total bytes"
                    )
                body = _read_regular_file(
                    path, display_path=rel, max_bytes=MAX_TREE_BYTES - total
                )
                total += len(body)
                mode = _normalized_mode(metadata.st_mode)
                _validate_mode_and_bytes(mode, body, path=rel)
                if rel == "SKILL.md":
                    skill_body = body
                entries.append(_SnapshotEntry(path, rel, mode, body))
    entries.sort(key=lambda entry: entry.relative)
    check_case_fold_collisions([entry.relative for entry in entries], source="package")
    if skill_body is None:
        raise SkillRefusal("package requires exactly one root SKILL.md")
    if require_manifest and not any(
        entry.relative == "skill.manifest.json" for entry in entries
    ):
        raise SkillRefusal(
            "package requires exactly one root skill.manifest.json"
        )
    try:
        skill_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillRefusal("SKILL.md must be valid UTF-8") from exc
    return entries


def _validate_tree(root: Path, *, require_manifest: bool = True) -> list[Path]:
    return [entry.path for entry in _bounded_snapshot(root, require_manifest=require_manifest)]


def _read_regular_file(
    path: Path, *, display_path: str, max_bytes: int | None = None
) -> bytes:
    """Read one unchanged regular file without following a final symlink."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SkillRefusal(f"special filesystem content is not supported: {display_path}")
    if before.st_nlink != 1:
        raise SkillRefusal(f"hard-linked content is not supported: {display_path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PackagePolicyError(f"source changed while reading: {display_path}") from exc
    try:
        opened = os.fstat(descriptor)
        def identity(value):
            """Return the stable stat fields required for this open/read check."""

            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
            )
        if identity(opened) != identity(before):
            raise PackagePolicyError(f"source changed while reading: {display_path}")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise SkillRefusal(f"file exceeds {max_bytes} bytes: {display_path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(max_bytes + 1 if max_bytes is not None else -1)
        if max_bytes is not None and len(body) > max_bytes:
            raise SkillRefusal(f"file exceeds {max_bytes} bytes: {display_path}")
        after = path.lstat()
        if identity(after) != identity(opened):
            raise PackagePolicyError(f"source changed while reading: {display_path}")
        return body
    finally:
        os.close(descriptor)


def _source_snapshot(source: Path) -> tuple[list[_SnapshotEntry], tuple[str, ...]]:
    """Source files to share and the generated paths left out, both sorted."""
    omitted: list[str] = []
    entries = _bounded_snapshot(source, require_manifest=False, omitted=omitted)
    return entries, tuple(sorted(omitted))


def _source_hash(entries: list[_SnapshotEntry], omitted: tuple[str, ...]) -> str:
    """Bind the shared bytes and the list of what was left out.

    The hash is unchanged when nothing is left out. No admitted path holds a
    NUL or newline, so the omission section cannot read as a file record.
    """
    records = "".join(
        f"{entry.relative} {entry.mode} {sha256_address(entry.body)}\n" for entry in entries
    )
    if omitted:
        records += "\0omitted\n" + "".join(f"{path}\n" for path in omitted)
    return sha256_address(records.encode("utf-8"))


def source_fingerprint(source: Path) -> str:
    """Hash the exact regular source files while rejecting unsafe entries."""

    if source.is_symlink() or not source.is_dir():
        raise SkillRefusal("skill root must be a regular directory")
    return _source_hash(*_source_snapshot(source))


def read_validated_source_files(source: Path) -> list[tuple[str, str, bytes]]:
    """Read every shared source byte for local preflight without requiring a manifest."""
    first = [(e.relative, e.mode, e.body) for e in _source_snapshot(source)[0]]
    second = [(e.relative, e.mode, e.body) for e in _source_snapshot(source)[0]]
    if first != second:
        raise PackagePolicyError("source changed during local preflight")
    return first


def read_validated_package_files(root: Path) -> list[tuple[str, str, bytes]]:
    """Read one complete safe package without hiding unsafe overlay entries."""

    snapshot = _bounded_snapshot(root, require_manifest=True)
    files = [
        (entry.relative, entry.mode, entry.body) for entry in snapshot
    ]
    after = _bounded_snapshot(root, require_manifest=True)
    if [(item.relative, item.mode, item.body) for item in after] != files:
        raise PackagePolicyError("prepared package changed while reading")
    return files


def prepare_package(  # pylint: disable=too-many-arguments,too-many-locals
    source: Path,
    *,
    host: PackageHostProvider,
    overlay_root: Path,
    author_description: str,
    owner: str,
    installation_id: str,
    editorial_name: str | None = None,
    editorial_description: str | None = None,
    editorial_applier: EditorialMetadataApplier | None = None,
) -> PreparedPackage:
    """Create a guarded private overlay and its canonical wire objects."""

    if source.is_symlink():
        raise SkillRefusal("skill root must be a regular directory")
    source = source.resolve()
    source_snapshot, omitted_paths = _source_snapshot(source)
    source_hash = _source_hash(source_snapshot, omitted_paths)
    target = overlay_root / source_hash.removeprefix("sha256:")
    if target.exists():
        raise PackagePolicyError("prepared overlay already exists; review the existing draft")
    authored_manifest = next(
        (e.body for e in source_snapshot if e.relative == "skill.manifest.json"), None
    )
    generated_manifest = None if authored_manifest is not None else json.dumps(
        PackageManifest(
            name=source.name,
            requirements=infer_authoring_system_specification(
                source,
                frontmatter=_frontmatter(source, host),
            ),
        ).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    # Every Gateway path rule, including case-fold collisions, already ran on
    # the source snapshot, so no refused path reaches an overlay.
    # Invalid setup guidance is refused here, naming the field, rather than
    # breaking every later install review.
    for entry in source_snapshot:
        if entry.relative == SETUP_PATH:
            try:
                check_shared_setup_document(entry.body)
            except SetupGuidanceError as exc:
                raise SkillRefusal(str(exc)) from exc
    target.mkdir(parents=True, mode=0o700)
    # Copy only validated allowlisted bytes. Validation happens first so an
    # unsupported referenced file never disappears silently from the package;
    # generated paths left out are named in ``omitted_paths``.
    for entry in source_snapshot:
        dest = target.joinpath(*PurePosixPath(entry.relative).parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(entry.body)
        dest.chmod(0o700 if entry.mode == "exec" else 0o600)
    if source_fingerprint(source) != source_hash:
        raise PackagePolicyError("source changed while the review overlay was being prepared")
    if editorial_name is not None or editorial_description is not None:
        if editorial_applier is None:
            raise PackagePolicyError("plugin editorial metadata applier is required")
        editorial_applier(
            target,
            editorial_name=editorial_name,
            editorial_description=editorial_description,
        )
    if generated_manifest is not None and not (target / "skill.manifest.json").exists():
        (target / "skill.manifest.json").write_bytes(generated_manifest)
        (target / "skill.manifest.json").chmod(0o600)
    paths = _validate_tree(target)
    try:
        manifest, manifest_bytes = load_manifest(target / "skill.manifest.json")
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillRefusal("package manifest is invalid") from exc
    _require_host_security(host, target)
    description = sanitize_author_description(author_description)
    files = [
        ContentFile(
            path=_safe_relative(path, target),
            mode=_normalized_mode(path.lstat().st_mode),
            hash=sha256_address(path.read_bytes()),
        )
        for path in paths
    ]
    content_hash = derive_content_hash(files)
    objects = ObjectSet()
    tree = build_tree(target, objects, max_object_bytes=MAX_TREE_BYTES)
    commit = build_commit(
        tree,
        [],
        owner=owner,
        device=installation_id,
        message="Collective Wisdom owner-private draft",
        objects=objects,
    )
    return PreparedPackage(
        source=source,
        overlay=target,
        source_hash=source_hash,
        content_hash=content_hash,
        description=description,
        description_hash=author_description_hash(description),
        manifest_hash=sha256_address(manifest_bytes),
        manifest=manifest,
        files=files,
        objects=objects,
        commit=commit,
        omitted_paths=omitted_paths,
    )


def verify_content_files(
    files: list[tuple[str, str, bytes]],
    *,
    require_manifest: bool = True,
) -> tuple[list[ContentFile], str]:
    """Validate downloaded paths/modes/blobs and derive the content hash."""
    records: list[ContentFile] = []
    keys: set[str] = set()
    total = 0
    metadata_total = 0
    for entry in files:
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise PackagePolicyError("download contains a malformed package entry")
        raw_path, mode, body = entry
        if not isinstance(raw_path, str):
            raise PackagePolicyError("download contains an invalid package path")
        if not isinstance(mode, str) or not isinstance(body, bytes):
            raise PackagePolicyError(f"invalid package entry: {raw_path}")
        pure, key = _validate_package_path(raw_path, source="package")
        if key in keys:
            raise _refuse("package", raw_path, "duplicate path")
        keys.add(key)
        metadata_total += len(raw_path.encode("utf-8")) + 128
        if metadata_total > MAX_METADATA_BYTES:
            raise PackagePolicyError(
                f"download path metadata exceeds {MAX_METADATA_BYTES} bytes"
            )
        _validate_allowed_file(pure, path=raw_path)
        total += len(body)
        if total > MAX_TREE_BYTES:
            raise PackagePolicyError(f"download exceeds {MAX_TREE_BYTES} total bytes")
        _validate_mode_and_bytes(mode, body, path=raw_path)
        records.append(
            ContentFile(path=raw_path, mode=mode, hash=sha256_address(body))
        )
    required = {record.path for record in records}
    if "SKILL.md" not in required or (
        require_manifest and "skill.manifest.json" not in required
    ):
        raise PackagePolicyError("download is not a complete Wisdom package")
    check_case_fold_collisions([record.path for record in records], source="package")
    skill_body = next(body for path, _, body in files if path == "SKILL.md")
    try:
        skill_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillRefusal("SKILL.md must be valid UTF-8") from exc
    return records, derive_content_hash(records)
