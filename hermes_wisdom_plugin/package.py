"""Instruction-only package preparation and exact-byte verification."""

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


MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024
MAX_TREE_BYTES = 512 * 1024
MAX_TREE_DEPTH = 3
MAX_SEGMENT_BYTES = 255
ALLOWED_ROOT_FILES = frozenset({"SKILL.md", "skill.manifest.json"})
ALLOWED_SUPPORT_DIRS = frozenset({"refs", "assets"})
ALLOWED_SUPPORT_EXTENSIONS = frozenset({".txt", ".md", ".rst", ".adoc", ".asciidoc"})
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
    "scripts",
    "templates",
    "venv",
})
FORBIDDEN_REFERENCE_RE = re.compile(
    r"(?i)(?:^|[\s\[(`'\"])(?:scripts?|templates?)/[^\s)`'\"]+"
)
PACKAGE_MANAGER_FILES = frozenset({
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
})


class PackagePolicyError(ValueError):
    """Package bytes or paths violate the instruction-only policy."""


@dataclass(frozen=True)
class PackageSecurityDecision:
    """Host-owned guard result for the exact prepared overlay."""

    allowed: bool | None


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
    if security.allowed is not True:
        state = "blocked" if security.allowed is False else "unavailable"
        raise PackagePolicyError(f"host package security guard is {state}")


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
    if len(pure.parts) == 1:
        if pure.name not in ALLOWED_ROOT_FILES:
            raise PackagePolicyError(
                f"unsupported root file {path}; create an explicit instruction-only fork"
            )
        return
    if pure.parts[0] not in ALLOWED_SUPPORT_DIRS:
        raise PackagePolicyError(f"unsupported package path: {path}")
    if pure.name in ALLOWED_ROOT_FILES:
        raise PackagePolicyError(f"required root file is misplaced: {path}")
    if pure.suffix.casefold() not in ALLOWED_SUPPORT_EXTENSIONS:
        raise PackagePolicyError(
            f"unsupported active or unknown file type {path}; "
            "create an explicit instruction-only fork"
        )


def _validate_text_bytes(body: bytes, *, path: str) -> None:
    if b"\x00" in body:
        raise PackagePolicyError(f"NUL/binary content is not allowed: {path}")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagePolicyError(f"binary content is not allowed: {path}") from exc
    if body.startswith(b"#!"):
        raise PackagePolicyError(f"executable shebang content is not allowed: {path}")


def _safe_relative(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    _validate_package_path(rel, source="package")
    return rel


def _validate_tree(  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    root: Path, *, require_manifest: bool = True
) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise PackagePolicyError("skill root must be a regular directory")
    entries: list[Path] = []
    collision_keys: set[str] = set()
    total = 0
    file_count = 0
    skill_body: bytes | None = None
    for path in sorted(root.rglob("*")):
        rel = _safe_relative(path, root)
        if path.is_symlink():
            raise PackagePolicyError(f"symlinks are not supported: {rel}")
        if not path.is_file() and not path.is_dir():
            raise PackagePolicyError(
                f"special filesystem content is not supported: {rel}"
            )
        pure, key = _validate_package_path(rel, source="package")
        parts = pure.parts
        if len(parts) == 1:
            if path.is_file() and parts[0] not in ALLOWED_ROOT_FILES:
                raise PackagePolicyError(
                    f"unsupported root file {rel}; create an explicit instruction-only fork"
                )
            if path.is_dir() and parts[0] not in ALLOWED_SUPPORT_DIRS:
                raise PackagePolicyError(
                    f"unsupported directory {rel}; scripts/templates cannot be silently omitted"
                )
        elif parts[0] not in ALLOWED_SUPPORT_DIRS:
            raise PackagePolicyError(f"unsupported package path: {rel}")
        if key in collision_keys:
            raise PackagePolicyError(f"install-target path collision: {rel}")
        collision_keys.add(key)
        if path.is_file():
            _validate_allowed_file(pure, path=rel)
            if path.stat().st_nlink != 1:
                raise PackagePolicyError(f"hard-linked content is not supported: {rel}")
            if path.name in PACKAGE_MANAGER_FILES:
                raise PackagePolicyError(
                    f"package-manager manifest is not allowed: {rel}"
                )
            body = _read_regular_file(
                path, display_path=rel, max_bytes=MAX_FILE_BYTES
            )
            file_count += 1
            if file_count > MAX_FILES:
                raise PackagePolicyError(f"package exceeds {MAX_FILES} files")
            size = len(body)
            if size > MAX_FILE_BYTES:
                raise PackagePolicyError(f"file exceeds {MAX_FILE_BYTES} bytes: {rel}")
            total += size
            if total > MAX_TREE_BYTES:
                raise PackagePolicyError(
                    f"package exceeds {MAX_TREE_BYTES} total bytes"
                )
            mode = path.lstat().st_mode
            if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise PackagePolicyError(f"executable content is not allowed: {rel}")
            _validate_text_bytes(body, path=rel)
            if rel == "SKILL.md":
                skill_body = body
            entries.append(path)
    if not (root / "SKILL.md").is_file():
        raise PackagePolicyError("package requires exactly one root SKILL.md")
    if require_manifest and not (root / "skill.manifest.json").is_file():
        raise PackagePolicyError(
            "package requires exactly one root skill.manifest.json"
        )
    if skill_body is None:
        raise PackagePolicyError("package requires exactly one root SKILL.md")
    skill_text = skill_body.decode("utf-8")
    if FORBIDDEN_REFERENCE_RE.search(skill_text):
        raise PackagePolicyError(
            "SKILL.md references active scripts/templates; create an explicit instruction-only fork"
        )
    return entries


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
    records: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise PackagePolicyError(
                f"symlinks are not supported: {path.relative_to(source).as_posix()}"
            )
        if path.is_file():
            relative = path.relative_to(source).as_posix()
            records.append(
                f"{relative} {sha256_address(_read_regular_file(path, display_path=relative))}\n"
            )
        elif not path.is_dir():
            raise PackagePolicyError(
                "special filesystem content is not supported: "
                f"{path.relative_to(source).as_posix()}"
            )
    return sha256_address("".join(records).encode("utf-8"))


def read_validated_package_files(root: Path) -> list[tuple[str, str, bytes]]:
    """Read one complete safe package without hiding unsafe overlay entries."""

    paths = _validate_tree(root)
    files = [
        (
            _safe_relative(path, root),
            "file",
            _read_regular_file(
                path,
                display_path=_safe_relative(path, root),
                max_bytes=MAX_FILE_BYTES,
            ),
        )
        for path in paths
    ]
    after = [_safe_relative(path, root) for path in _validate_tree(root)]
    if after != [path for path, _mode, _body in files]:
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
    source_hash = source_fingerprint(source)
    target = overlay_root / source_hash.removeprefix("sha256:")
    if target.exists():
        raise PackagePolicyError("prepared overlay already exists; review the existing draft")
    target.mkdir(parents=True, mode=0o700)
    # Copy only validated allowlisted bytes. Validation happens first so an
    # unsupported referenced file never disappears silently from the package.
    _validate_tree(source, require_manifest=False)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            relative = path.relative_to(source).as_posix()
            dest.write_bytes(_read_regular_file(path, display_path=relative))
            dest.chmod(0o600)
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
            mode="file",
            hash=sha256_address(path.read_bytes()),
        )
        for path in paths
    ]
    content_hash = derive_content_hash(files)
    objects = ObjectSet()
    tree = build_tree(target, objects, max_object_bytes=MAX_FILE_BYTES)
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
    if len(files) > MAX_FILES:
        raise PackagePolicyError(f"download exceeds {MAX_FILES} files")
    records: list[ContentFile] = []
    keys: set[str] = set()
    total = 0
    for raw_path, mode, body in files:
        pure, key = _validate_package_path(raw_path, source="server")
        if key in keys:
            raise PackagePolicyError(f"install-target collision: {raw_path}")
        keys.add(key)
        if mode != "file":
            raise PackagePolicyError(f"executable/unknown mode rejected: {raw_path}")
        _validate_allowed_file(pure, path=raw_path)
        if pure.name in PACKAGE_MANAGER_FILES:
            raise PackagePolicyError(
                f"package-manager manifest is not allowed: {raw_path}"
            )
        if len(body) > MAX_FILE_BYTES:
            raise PackagePolicyError(f"file exceeds {MAX_FILE_BYTES} bytes: {raw_path}")
        total += len(body)
        if total > MAX_TREE_BYTES:
            raise PackagePolicyError(f"download exceeds {MAX_TREE_BYTES} total bytes")
        _validate_text_bytes(body, path=raw_path)
        records.append(
            ContentFile(path=raw_path, mode="file", hash=sha256_address(body))
        )
    required = {record.path for record in records}
    if "SKILL.md" not in required or (
        require_manifest and "skill.manifest.json" not in required
    ):
        raise PackagePolicyError("download is not a complete Wisdom package")
    skill_body = next(body for path, _, body in files if path == "SKILL.md")
    if FORBIDDEN_REFERENCE_RE.search(skill_body.decode("utf-8")):
        raise PackagePolicyError("SKILL.md references unsupported active content")
    return records, derive_content_hash(records)
