"""Read-only Hermes host adapter for manual Wisdom install review.

Security and target decisions bind narrowly to the audited bf51 Hermes host;
this module deliberately does not import the destructive Hub installer API.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .compatibility import LocalCapabilities
from .contract import SystemSpecification
from .package import PackagePolicyError, PackageSecurityDecision


def _distribution_version(name: str) -> str | None:
    candidates = (name, name.replace("_", "-"), name.rsplit("/", 1)[-1])
    for candidate in dict.fromkeys(candidates):
        try:
            return importlib.metadata.version(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _path_capabilities(paths: list[str]) -> tuple[frozenset[str], frozenset[str]]:
    readable: set[str] = set()
    writable: set[str] = set()
    for raw in paths:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if "$" in expanded:
            continue
        candidate = Path(expanded)
        if candidate.exists() and os.access(candidate, os.R_OK):
            readable.add(raw)
        writable_target = candidate
        while (
            not writable_target.exists() and writable_target != writable_target.parent
        ):
            writable_target = writable_target.parent
        if writable_target.exists() and os.access(writable_target, os.W_OK):
            writable.add(raw)
    return frozenset(readable), frozenset(writable)


def _is_admin() -> bool:
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:  # pragma: no cover - exercised by the Windows CI lane
        import ctypes  # pylint: disable=import-outside-toplevel

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pylint: disable=broad-exception-caught  # pragma: no cover
        return False


class InstallationHost:
    """Fail-closed, profile-bound host facts needed by manual review."""

    def __init__(self, profile_home: Path) -> None:
        if not isinstance(profile_home, Path):
            raise TypeError("profile_home must be a Path")
        self.profile_home = profile_home.expanduser().resolve()

    @property
    def skills_root(self) -> Path:
        """Return this reviewer's profile-scoped skills root."""

        return self.profile_home / "skills"

    def parse_skill_frontmatter(self, source: Path) -> dict[str, object]:
        """Parse one local SKILL.md through the supported host utility."""

        self._check_profile()
        skill_md = source / "SKILL.md"
        if source.is_symlink() or skill_md.is_symlink():
            raise PackagePolicyError("local skill source is unsafe")
        try:
            parse = getattr(self._module("agent.skill_utils"), "parse_frontmatter")
            frontmatter, _body = parse(skill_md.read_text(encoding="utf-8"))
        except PackagePolicyError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("local skill frontmatter is unavailable") from exc
        if not isinstance(frontmatter, dict):
            raise PackagePolicyError("local skill frontmatter is unavailable")
        self._check_profile()
        return {str(key): value for key, value in frontmatter.items()}

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        """Adapt the host scan to the package kernel's fail-closed protocol."""

        scan = self.scan(overlay)
        allowed = scan.get("allowed")
        if allowed is not None and not isinstance(allowed, bool):
            raise PackagePolicyError("Hermes host skill scan returned an invalid result")
        findings = scan.get("findings")
        if not isinstance(findings, list) or any(
            not isinstance(item, dict) for item in findings
        ):
            raise PackagePolicyError("Hermes host skill scan returned an invalid result")
        return PackageSecurityDecision(
            allowed=allowed,
            available=True,
            verdict=str(scan["verdict"]),
            summary=str(scan["summary"]),
            findings=tuple(dict(item) for item in findings),
        )

    @staticmethod
    def _module(name: str) -> Any:
        try:
            return importlib.import_module(name)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("required Hermes host coupling is unavailable") from exc

    def _check_profile(self) -> None:
        constants = self._module("hermes_constants")
        get_home = getattr(constants, "get_hermes_home", None)
        if not callable(get_home):
            raise PackagePolicyError("required Hermes host coupling is unavailable")
        try:
            active_home = Path(get_home()).expanduser().resolve()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("active Hermes profile is unavailable") from exc
        if active_home != self.profile_home:
            raise PackagePolicyError("active Hermes profile changed during install review")

    # Keep the extracted static detector together while adapting its host boundary.
    # pylint: disable=too-many-statements,too-many-nested-blocks
    def capabilities(  # pylint: disable=too-many-locals,too-many-branches
        self, spec: SystemSpecification
    ) -> LocalCapabilities:  # pylint: disable=too-many-statements,too-many-nested-blocks
        """Read only declared local facts; never execute commands or probe networks."""

        if not isinstance(spec, SystemSpecification):
            raise TypeError("spec must be a SystemSpecification")
        self._check_profile()
        try:
            cli = self._module("hermes_cli")
            config_api = self._module("hermes_cli.config")
            tools_api = self._module("hermes_cli.tools_config")
            toolsets = self._module("toolsets")
            load_config = getattr(config_api, "load_config_readonly")
            get_env_value = getattr(config_api, "get_env_value")
            summarize_toolsets = getattr(tools_api, "_platform_toolset_summary")
            resolve_toolset = getattr(toolsets, "resolve_toolset")
            config = load_config()
            if config is None:
                config = {}
            if not isinstance(config, dict):
                raise TypeError("host config is not a mapping")

            enabled_toolsets = set(
                summarize_toolsets(config, ["cli"]).get("cli", set())
            )
            enabled_names = set(enabled_toolsets)
            for toolset in enabled_toolsets:
                enabled_names.update(
                    resolve_toolset(toolset, include_registry=False)
                )

            enabled_tools: dict[str, str | None] = {}
            for requirement in spec.tools:
                if requirement.name in enabled_names or shutil.which(requirement.name):
                    enabled_tools[requirement.name] = _distribution_version(
                        requirement.name
                    )

            configured_plugins = config.get("plugins")
            configured_plugins = (
                configured_plugins if isinstance(configured_plugins, dict) else {}
            )
            enabled_plugin_names = {
                item
                for item in configured_plugins.get("enabled", [])
                if isinstance(item, str)
            }
            plugins = {
                requirement.id: _distribution_version(requirement.id)
                for requirement in spec.plugins
                if requirement.id in enabled_plugin_names
            }

            credentials = frozenset(
                name for name in spec.credentials if get_env_value(name)
            )
            connections: set[str] = set()
            messaging_env = {
                "telegram": "TELEGRAM_BOT_TOKEN",
                "discord": "DISCORD_BOT_TOKEN",
                "slack": "SLACK_BOT_TOKEN",
                "whatsapp": "WHATSAPP_ENABLED",
            }
            for name in spec.connections:
                env_name = messaging_env.get(name.casefold())
                if env_name and get_env_value(env_name):
                    connections.add(name)
            mcp_servers = config.get("mcp_servers")
            if isinstance(mcp_servers, dict):
                connections.update(
                    str(name)
                    for name, value in mcp_servers.items()
                    if not isinstance(value, dict) or value.get("enabled", True) is not False
                )

            model_capabilities: set[str] = set()
            context_window: int | None = None
            model_config = config.get("model")
            if isinstance(model_config, dict):
                configured_context = model_config.get("context_length")
                if (
                    isinstance(configured_context, int)
                    and not isinstance(configured_context, bool)
                    and configured_context > 0
                ):
                    context_window = configured_context
                provider = str(model_config.get("provider") or "")
                model = str(
                    model_config.get("default") or model_config.get("name") or ""
                )
                if provider and model and (
                    spec.model.capabilities or spec.model.minimum_context_window
                ):
                    models = self._module("agent.models_dev")
                    get_model_capabilities = getattr(
                        models, "get_model_capabilities"
                    )
                    model_info = get_model_capabilities(
                        provider, model, allow_network=False
                    )
                    if model_info:
                        for field, capability in (
                            ("supports_tools", "tools"),
                            ("supports_vision", "vision"),
                            ("supports_reasoning", "reasoning"),
                        ):
                            if getattr(model_info, field, False):
                                model_capabilities.add(capability)
                        context_window = context_window or getattr(
                            model_info, "context_window", None
                        )

            required_paths = [*spec.filesystem.read, *spec.filesystem.write]
            filesystem_readable, filesystem_writable = _path_capabilities(
                required_paths
            )
            terminal = config.get("terminal")
            terminal = terminal if isinstance(terminal, dict) else {}
            backend = str(terminal.get("backend") or "local").casefold()
            network_access = not (
                backend == "docker" and not terminal.get("docker_network", True)
            )
            normalized_os = platform.system().strip().casefold()
            normalized_os = {
                "macos": "darwin",
                "mac": "darwin",
                "win32": "windows",
            }.get(normalized_os, normalized_os)
            architecture = platform.machine().strip().casefold()
            architecture = {
                "amd64": "x86_64",
                "x64": "x86_64",
                "aarch64": "arm64",
            }.get(architecture, architecture)
            result = LocalCapabilities(
                hermes_version=str(getattr(cli, "__version__")),
                os=normalized_os,
                architecture=architecture,
                model_capabilities=frozenset(model_capabilities),
                context_window=context_window,
                enabled_tools=enabled_tools,
                plugins=plugins,
                credentials=credentials,
                connections=frozenset(connections),
                filesystem_readable=filesystem_readable,
                filesystem_writable=filesystem_writable,
                network_access=network_access,
                runtime={
                    "shell": shutil.which("sh") is not None
                    or platform.system() == "Windows",
                    "browser": "browser" in enabled_toolsets,
                    "code": "code_execution" in enabled_toolsets,
                    "sandbox": True,
                },
                hardware=frozenset({"cpu", architecture}),
                is_admin=_is_admin(),
            )
        except PackagePolicyError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError(
                "Hermes host capability detection is unavailable"
            ) from exc
        self._check_profile()
        return result

    def scan(self, directory: Path) -> dict[str, object]:
        """Run the actual host community scan and non-force install policy."""

        if not isinstance(directory, Path):
            raise TypeError("directory must be a Path")
        self._check_profile()
        try:
            guard = self._module("tools.skills_guard")
            scan_skill = getattr(guard, "scan_skill")
            should_allow = getattr(guard, "should_allow_install")
            result = scan_skill(directory, source="community")
            allowed, reason = should_allow(result, force=False)
            if allowed is not None and not isinstance(allowed, bool):
                raise TypeError("invalid host policy decision")
            verdict = getattr(result, "verdict")
            findings = getattr(result, "findings")
            if not isinstance(verdict, str) or not isinstance(findings, list):
                raise TypeError("invalid host scan result")
            serialized = []
            for finding in findings:
                if is_dataclass(finding) and not isinstance(finding, type):
                    serialized.append(asdict(finding))
                elif isinstance(finding, dict):
                    serialized.append(dict(finding))
                else:
                    raise TypeError("invalid host scan finding")
            if not isinstance(reason, str):
                raise TypeError("invalid host policy summary")
            decision = {
                "available": True,
                "allowed": allowed,
                "verdict": verdict,
                "summary": reason,
                "findings": serialized,
            }
        except PackagePolicyError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("Hermes host skill scan is unavailable") from exc
        self._check_profile()
        return decision

    def validate_target(self, path: Path) -> None:
        """Validate one contained skills target using host-owned redirect guards."""

        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        self._check_profile()
        root = self.skills_root.resolve()
        candidate = path.expanduser().resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise PackagePolicyError("install target must be contained by skills")
        try:
            models = self._module("tools.skills_hub_models")
            installer = self._module("tools.skills_hub_install")
            validate_name = getattr(models, "_validate_skill_name")
            resolve_target = getattr(installer, "_resolve_lock_install_path")
            check_target = getattr(installer, "_check_install_target")
            if validate_name(path.name) != path.name:
                raise ValueError("host normalized the requested skill name")
            relative = path.relative_to(self.skills_root).as_posix()
            guarded = resolve_target(relative, path.name)
            if guarded.resolve() != candidate:
                raise ValueError("host resolved a different install target")
            check_target(guarded)
        except PackagePolicyError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("Hermes host rejected the install target") from exc
        self._check_profile()
