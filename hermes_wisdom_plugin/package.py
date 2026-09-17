"""Native Hermes skill package preparation and exact-byte verification."""

from __future__ import annotations

import json
import os
import platform
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
from .wire import ObjectSet, build_commit, build_tree


MAX_TREE_BYTES = 10 * 1024 * 1024
# Paths and canonical tree entries are bounded separately so empty files cannot
# turn validation/serialization into unbounded work. This is deliberately not a
# product file-count limit.
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_TREE_DEPTH = 16
MAX_SEGMENT_BYTES = 255
REQUIRED_ROOT_FILES = frozenset({"SKILL.md", "skill.manifest.json"})
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
BLOCKED_PATH_SEGMENTS = frozenset({
    ".circleci",
    ".git",
    ".github",
    ".gitlab",
    ".hooks",
    ".tox",
    ".venv",
    "__pycache__",
    "hooks",
    "node_modules",
    "venv",
})
_COMPILED_EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe")


class PackagePolicyError(ValueError):
    """Package bytes, modes, or paths violate the native package policy."""


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

    @property
    def hermes_version(self) -> str:
        """Return the current host version used in generated manifests."""

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


def infer_authoring_system_specification(  # pylint: disable=unused-argument
    source: Path,
    *,
    hermes_version: str,
    system_name: str | None = None,
    machine: str | None = None,
    frontmatter: dict[str, object] | None = None,
) -> SystemSpecification:
    """Build conservative owner-review defaults without exporting inventory.

    The authoring device establishes one known-good OS/architecture target.
    Skill-specific tool and plugin requirements come only from explicit
    frontmatter; the profile's complete enabled inventory is never consulted.
    """
    frontmatter = frontmatter or {}
    metadata = _hermes_metadata(frontmatter)

    if "platforms" in frontmatter:
        platforms = [
            _normalized_authoring_platform(item)
            for item in _declared_strings(frontmatter.get("platforms"))
        ]
    else:
        current_platform = _normalized_authoring_platform(
            system_name if system_name is not None else platform.system()
        )
        platforms = [current_platform] if current_platform else []

    architecture_source: object | None = None
    architecture_declared = False
    if "architectures" in frontmatter:
        architecture_source = frontmatter.get("architectures")
        architecture_declared = True
    elif "architectures" in metadata:
        architecture_source = metadata.get("architectures")
        architecture_declared = True
    if architecture_declared:
        architectures = [
            _normalized_authoring_architecture(item)
            for item in _declared_strings(architecture_source)
        ]
    else:
        current_architecture = _normalized_authoring_architecture(
            machine if machine is not None else platform.machine()
        )
        architectures = [current_architecture] if current_architecture else []

    tools = _tool_requirements(metadata)
    tool_names = {requirement.name for requirement in tools}
    return SystemSpecification(
        hermes=HermesRequirement(minimum_version=hermes_version),
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


def _segment_key(segment: str, *, path: str) -> str:
    if segment in ("", ".", ".."):
        raise PackagePolicyError(f"unsafe package path: {path!r}")
    if unicodedata.normalize("NFC", segment) != segment:
        raise PackagePolicyError(f"non-canonical package path: {path!r}")
    if segment.endswith((".", " ")):
        raise PackagePolicyError(f"unsafe install-target path: {path!r}")
    invalid_char = next(
        (
            char
            for char in segment
            if ord(char) < 0x20 or ord(char) == 0x7F or char in WINDOWS_INVALID_CHARS
        ),
        None,
    )
    if invalid_char is not None:
        raise PackagePolicyError(
            f"unsafe character {invalid_char!r} in package path: {path!r}"
        )
    if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
        raise PackagePolicyError(f"package path segment is too long: {path!r}")
    key = segment.casefold()
    if key.split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        raise PackagePolicyError(f"reserved install-target path: {path!r}")
    return key


def _validate_package_path(raw_path: str, *, source: str) -> tuple[PurePosixPath, str]:
    if (
        not raw_path
        or "\\" in raw_path
        or unicodedata.normalize("NFC", raw_path) != raw_path
        or PERCENT_ESCAPE_RE.search(raw_path)
    ):
        raise PackagePolicyError(f"non-canonical {source} path: {raw_path!r}")
    raw_parts = raw_path.split("/")
    if raw_path.startswith("/") or any(part in ("", ".", "..") for part in raw_parts):
        raise PackagePolicyError(f"unsafe {source} path: {raw_path!r}")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or not pure.parts:
        raise PackagePolicyError(f"unsafe {source} path: {raw_path!r}")
    keys = [_segment_key(part, path=raw_path) for part in pure.parts]
    if any(key in BLOCKED_PATH_SEGMENTS for key in keys):
        raise PackagePolicyError(f"active package directory is not allowed: {raw_path}")
    if len(pure.parts) - 1 > MAX_TREE_DEPTH:
        raise PackagePolicyError(
            f"package path exceeds maximum tree depth {MAX_TREE_DEPTH}: {raw_path}"
        )
    return pure, "/".join(keys)


def _validate_allowed_file(pure: PurePosixPath, *, path: str) -> None:
    if len(pure.parts) > 1 and pure.name in REQUIRED_ROOT_FILES:
        raise PackagePolicyError(f"required root file is misplaced: {path}")


def _validate_mode_and_bytes(mode: str, body: bytes, *, path: str) -> None:
    if not isinstance(mode, str) or not isinstance(body, bytes):
        raise PackagePolicyError(f"invalid package entry: {path}")
    if mode not in {"file", "exec"}:
        raise PackagePolicyError(f"unknown package mode: {path}")
    if body.startswith(_COMPILED_EXECUTABLE_MAGIC):
        raise PackagePolicyError(f"compiled executable content is not supported: {path}")


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
    root: Path, *, require_manifest: bool
) -> list[_SnapshotEntry]:
    """Admit metadata and payload before sorting or further processing."""

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise PackagePolicyError("skill root must be a regular directory") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise PackagePolicyError("skill root must be a regular directory")
    entries: list[_SnapshotEntry] = []
    collision_keys: set[str] = set()
    total = 0
    metadata_total = 0
    skill_body: bytes | None = None
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_TREE_DEPTH:
            raise PackagePolicyError(
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
                pure, key = _validate_package_path(rel, source="package")
                metadata_total += len(rel.encode("utf-8")) + 128
                if metadata_total > MAX_METADATA_BYTES:
                    raise PackagePolicyError(
                        f"package path metadata exceeds {MAX_METADATA_BYTES} bytes"
                    )
                if key in collision_keys:
                    raise PackagePolicyError(f"install-target path collision: {rel}")
                collision_keys.add(key)
                try:
                    metadata = item.stat(follow_symlinks=False)
                except OSError as exc:
                    raise PackagePolicyError("package tree changed while reading") from exc
                if stat.S_ISLNK(metadata.st_mode):
                    raise PackagePolicyError(f"symlinks are not supported: {rel}")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((path, depth + 1))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise PackagePolicyError(
                        f"special filesystem content is not supported: {rel}"
                    )
                _validate_allowed_file(pure, path=rel)
                if metadata.st_nlink != 1:
                    raise PackagePolicyError(f"hard-linked content is not supported: {rel}")
                if total + metadata.st_size > MAX_TREE_BYTES:
                    raise PackagePolicyError(
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
    if skill_body is None:
        raise PackagePolicyError("package requires exactly one root SKILL.md")
    if require_manifest and not any(
        entry.relative == "skill.manifest.json" for entry in entries
    ):
        raise PackagePolicyError(
            "package requires exactly one root skill.manifest.json"
        )
    try:
        skill_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagePolicyError("SKILL.md must be valid UTF-8") from exc
    return entries


def _validate_tree(root: Path, *, require_manifest: bool = True) -> list[Path]:
    return [entry.path for entry in _bounded_snapshot(root, require_manifest=require_manifest)]


def _read_regular_file(
    path: Path, *, display_path: str, max_bytes: int | None = None
) -> bytes:
    """Read one unchanged regular file without following a final symlink."""

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PackagePolicyError(f"special filesystem content is not supported: {display_path}")
    if before.st_nlink != 1:
        raise PackagePolicyError(f"hard-linked content is not supported: {display_path}")
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
            raise PackagePolicyError(f"file exceeds {max_bytes} bytes: {display_path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(max_bytes + 1 if max_bytes is not None else -1)
        if max_bytes is not None and len(body) > max_bytes:
            raise PackagePolicyError(f"file exceeds {max_bytes} bytes: {display_path}")
        after = path.lstat()
        if identity(after) != identity(opened):
            raise PackagePolicyError(f"source changed while reading: {display_path}")
        return body
    finally:
        os.close(descriptor)


def source_fingerprint(source: Path) -> str:
    """Hash the exact regular source files while rejecting unsafe entries."""

    if source.is_symlink() or not source.is_dir():
        raise PackagePolicyError("skill root must be a regular directory")
    records = [
        f"{entry.relative} {entry.mode} {sha256_address(entry.body)}\n"
        for entry in _bounded_snapshot(source, require_manifest=False)
    ]
    return sha256_address("".join(records).encode("utf-8"))


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
        raise PackagePolicyError("skill root must be a regular directory")
    source = source.resolve()
    source_snapshot = _bounded_snapshot(source, require_manifest=False)
    source_hash = sha256_address("".join(
        f"{entry.relative} {entry.mode} {sha256_address(entry.body)}\n"
        for entry in source_snapshot
    ).encode("utf-8"))
    target = overlay_root / source_hash.removeprefix("sha256:")
    if target.exists():
        raise PackagePolicyError("prepared overlay already exists; review the existing draft")
    target.mkdir(parents=True, mode=0o700)
    # Copy only validated allowlisted bytes. Validation happens first so an
    # unsupported referenced file never disappears silently from the package.
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
    if not (target / "skill.manifest.json").exists():
        generated = PackageManifest(
            name=source.name,
            requirements=infer_authoring_system_specification(
                source,
                hermes_version=host.hermes_version,
                frontmatter=_frontmatter(source, host),
            ),
        )
        (target / "skill.manifest.json").write_bytes(
            json.dumps(
                generated.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        (target / "skill.manifest.json").chmod(0o600)
    paths = _validate_tree(target)
    try:
        manifest, manifest_bytes = load_manifest(target / "skill.manifest.json")
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackagePolicyError("package manifest is invalid") from exc
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
        pure, key = _validate_package_path(raw_path, source="server")
        if key in keys:
            raise PackagePolicyError(f"install-target collision: {raw_path}")
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
    skill_body = next(body for path, _, body in files if path == "SKILL.md")
    try:
        skill_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagePolicyError("SKILL.md must be valid UTF-8") from exc
    return records, derive_content_hash(records)
