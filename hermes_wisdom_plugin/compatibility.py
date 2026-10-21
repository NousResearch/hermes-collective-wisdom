"""Deterministic local compatibility evaluation for Wisdom packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from packaging.version import InvalidVersion, Version

from .contract import SystemSpecification


CompatibilityOutcome = Literal[
    "compatible", "compatible_after_setup", "partial", "blocked_pending_action"
]


@dataclass(frozen=True)
class LocalCapabilities:  # pylint: disable=too-many-instance-attributes
    """Host-supplied facts used by deterministic compatibility evaluation."""

    hermes_version: str
    os: str
    architecture: str
    model_capabilities: frozenset[str] = frozenset()
    context_window: int | None = None
    enabled_tools: dict[str, str | None] = field(default_factory=dict)
    plugins: dict[str, str | None] = field(default_factory=dict)
    credentials: frozenset[str] = frozenset()
    connections: frozenset[str] = frozenset()
    filesystem_readable: frozenset[str] = frozenset()
    filesystem_writable: frozenset[str] = frozenset()
    network_access: bool | None = None
    runtime: dict[str, bool] = field(default_factory=dict)
    hardware: frozenset[str] = frozenset()
    is_admin: bool = False


@dataclass(frozen=True)
class CompatibilityResult:  # pylint: disable=too-many-instance-attributes
    """Deterministic compatibility outcome and its categorized reasons.

    ``notes`` are informational facts (for example a recorded architecture
    that differs from this machine). They never change the outcome.
    """

    outcome: CompatibilityOutcome
    satisfied: tuple[str, ...]
    setup_actions: tuple[str, ...]
    limitations: tuple[str, ...]
    blocked: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillDeclarations:
    """Requirements the package's own SKILL.md frontmatter declares.

    Hermes semantics: a skill without ``platforms:`` runs everywhere, and
    there is no architecture gating. Only these declarations block install;
    other recorded manifest facts are notes.
    """

    platforms: tuple[str, ...] = ()
    minimum_hermes: str | None = None


def skill_declarations(frontmatter: object) -> SkillDeclarations:
    """Read the gating declarations from parsed SKILL.md frontmatter."""
    if not isinstance(frontmatter, dict):
        return SkillDeclarations()
    data = {str(key): value for key, value in frontmatter.items()}
    raw = data.get("platforms")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    platforms = tuple(dict.fromkeys(
        item.strip() for item in values if isinstance(item, str) and item.strip()
    ))
    metadata = data.get("metadata")
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
    minimum = hermes.get("minimum_version") if isinstance(hermes, dict) else None
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        minimum = str(minimum)
    return SkillDeclarations(
        platforms=platforms,
        minimum_hermes=minimum.strip() if isinstance(minimum, str) and minimum.strip() else None,
    )


_OS_NAMES = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}


def _os_label(value: str) -> str:
    normalized = _normalized_os(value)
    return _OS_NAMES.get(normalized, value.strip() or "unknown")


def _normalized_os(value: str) -> str:
    lowered = value.strip().lower()
    return {"macos": "darwin", "mac": "darwin", "win32": "windows"}.get(
        lowered, lowered
    )


def _normalized_architecture(value: str) -> str:
    lowered = value.strip().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }.get(lowered, lowered)


def _version_at_least(actual: str | None, minimum: str | None) -> bool:
    if minimum is None:
        return actual is not None
    if actual is None:
        return False
    try:
        return Version(actual) >= Version(minimum)
    except InvalidVersion:
        return actual == minimum


def evaluate(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    spec: SystemSpecification, local: LocalCapabilities,
    declared: SkillDeclarations | None = None,
) -> CompatibilityResult:
    """Evaluate requirements against trusted host-supplied facts.

    Only the package SKILL.md's own declarations (``declared``) gate OS and
    Hermes version, matching Hermes skill loading. Recorded manifest
    platforms, architectures and Hermes floors the skill did not declare
    (older releases pinned the sharer's machine) become notes.
    """

    declared = declared or SkillDeclarations()
    satisfied: list[str] = []
    setup: list[str] = []
    partial: list[str] = list(spec.known_limitations)
    blocked: list[str] = []
    notes: list[str] = []
    if declared.minimum_hermes is not None:
        if _version_at_least(local.hermes_version, declared.minimum_hermes):
            satisfied.append(f"Hermes >= {declared.minimum_hermes}")
        else:
            blocked.append(f"requires Hermes {declared.minimum_hermes} or newer; "
                           f"this machine runs Hermes {local.hermes_version or 'unknown'}")
    elif _version_at_least(local.hermes_version, spec.hermes.minimum_version):
        satisfied.append(f"Hermes >= {spec.hermes.minimum_version}")
    else:
        notes.append(f"Shared with Hermes >= {spec.hermes.minimum_version}; "
                     f"this machine runs Hermes {local.hermes_version or 'unknown'}")
    local_os = _normalized_os(local.os)
    if declared.platforms:
        if local_os not in {_normalized_os(item) for item in declared.platforms}:
            blocked.append(f"requires {' or '.join(_os_label(item) for item in declared.platforms)}"
                           f"; this machine is {_os_label(local.os)}")
    elif spec.platforms and local_os not in {_normalized_os(item) for item in spec.platforms}:
        notes.append(f"Shared with platforms: {', '.join(spec.platforms)}; "
                     f"this machine is {_os_label(local.os)}")
    if spec.architectures and _normalized_architecture(local.architecture) not in {
        _normalized_architecture(item) for item in spec.architectures
    }:
        notes.append(f"Shared with architectures: {', '.join(spec.architectures)}; "
                     f"this machine is {local.architecture or 'unknown'}")
    missing_caps = sorted(set(spec.model.capabilities) - set(local.model_capabilities))
    if missing_caps:
        partial.append("model capabilities: " + ", ".join(missing_caps))
    if spec.model.minimum_context_window and (
        local.context_window is None
        or local.context_window < spec.model.minimum_context_window
    ):
        partial.append(f"model context window >= {spec.model.minimum_context_window}")
    for requirement in spec.tools:
        present = requirement.name in local.enabled_tools
        actual = local.enabled_tools.get(requirement.name)
        label = requirement.name + (
            f">={requirement.minimum_version}" if requirement.minimum_version else ""
        )
        if requirement.requires_admin and not local.is_admin:
            blocked.append(f"administrator approval for tool {label}")
        elif not present or (
            requirement.minimum_version is not None
            and not _version_at_least(actual, requirement.minimum_version)
        ):
            setup.append(f"enable tool {label}")
    for requirement in spec.plugins:
        present = requirement.id in local.plugins
        actual = local.plugins.get(requirement.id)
        if not present or (
            requirement.minimum_version is not None
            and not _version_at_least(actual, requirement.minimum_version)
        ):
            (setup if requirement.required else partial).append(
                f"install plugin {requirement.id}"
            )
    setup.extend(
        f"configure credential {item}"
        for item in spec.credentials
        if item not in local.credentials
    )
    setup.extend(
        f"connect {item}" for item in spec.connections if item not in local.connections
    )
    setup.extend(
        f"grant filesystem read access to {item}"
        for item in spec.filesystem.read
        if item not in local.filesystem_readable
    )
    setup.extend(
        f"grant filesystem write access to {item}"
        for item in spec.filesystem.write
        if item not in local.filesystem_writable
    )
    if spec.network.destinations and local.network_access is not True:
        setup.extend(
            f"allow network access to {item}" for item in spec.network.destinations
        )
    elif spec.network.destinations:
        satisfied.append("network egress is enabled")
    for capability, required in spec.runtime.model_dump().items():
        if required and not local.runtime.get(capability, False):
            blocked.append(f"runtime capability {capability}")
    missing_hw = sorted(set(spec.hardware) - set(local.hardware))
    blocked.extend(f"hardware {item}" for item in missing_hw)
    if blocked:
        outcome: CompatibilityOutcome = "blocked_pending_action"
    elif setup:
        outcome = "compatible_after_setup"
    elif partial:
        outcome = "partial"
    else:
        outcome = "compatible"
    return CompatibilityResult(
        outcome, tuple(satisfied), tuple(setup), tuple(partial), tuple(blocked),
        tuple(notes),
    )
