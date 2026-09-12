"""Bounded CLI and shared-chat-safe text commands for Collective Wisdom.

The CLI dispatch shape is extracted from ``hermes_cli/subcommands/wisdom.py``
and the read-only projections from ``gateway/wisdom_command.py``, both at
``0924f7946b937c2ed02d083fc26752d577c54b3b``. Host registration, service
construction, native callbacks, and all mutations remain outside this module.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)
from .package import PackagePolicyError


PAGE_SIZE = 5
_MAX_PAGE = 10_000
_SLASH_COMMANDS = frozenset(
    {"setup", "status", "list", "browse", "show", "versions", "help"}
)
_CHECK_LABELS = (
    ("security_check", "Security check"),
    ("professionalism_check", "Professionalism check"),
)
_CHECK_STATES = {
    "pass": "Pass",
    "advisory": "Advisory",
    "blocked": "Blocked",
    "pending": "Pending",
    "retry": "Pending",
    "running": "Pending",
    "unavailable": "Unavailable",
}
_PUBLIC_STATUS_STATES = {
    "active": "ready",
    "configured": "ready",
    "ready": "ready",
    "not_configured": "not configured",
    "degraded": "attention required",
    "unavailable": "unavailable",
}
_CLI_COMMANDS = ("setup", "status", "list", "browse", "show", "versions", "help")


class _SlashUsageError(ValueError):
    """A locally generated, safe-to-render slash usage error."""


def build_parser(parser: argparse.ArgumentParser) -> None:
    """Populate an already-created ``hermes wisdom`` argument parser."""

    parser.set_defaults(_wisdom_parser=parser)
    commands = parser.add_subparsers(dest="wisdom_command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )
        return command

    setup = add("setup", "Validate entitlement and initialize this profile")
    setup.add_argument(
        "--accept-disclosure",
        action="store_true",
        help="Accept the local telemetry and owner-private draft disclosure",
    )
    add("status", "Show local and Gateway Wisdom status")
    add("list", "List published Collective Wisdom skills")
    browse = add("browse", "Search the organization catalog by keyword")
    browse.add_argument("query", nargs="?")
    show = add("show", "Show one published skill")
    show.add_argument("skill_id")
    versions = add("versions", "List published versions")
    versions.add_argument("skill_id")
    add("help", "Show Collective Wisdom command help")


def run_cli(  # pylint: disable=too-many-branches,too-many-return-statements
    args: argparse.Namespace, service: object
) -> int:
    """Run one parsed terminal command against an injected service."""

    command = getattr(args, "wisdom_command", None)
    as_json = bool(getattr(args, "json", False))
    try:
        if command == "setup":
            accepted = bool(args.accept_disclosure)
            if not accepted:
                if not sys.stdin.isatty():
                    raise PackagePolicyError(
                        "noninteractive setup requires --accept-disclosure"
                    )
                # The onboarding slice owns the canonical disclosure text.
                from .onboarding import (  # pylint: disable=import-error,import-outside-toplevel
                    WISDOM_DISCLOSURE,
                )

                print(WISDOM_DISCLOSURE)
                answer = input("Enable Collective Wisdom for this profile? [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    return 7
                accepted = True
            result = service.setup(accept_disclosure=accepted)
        elif command == "status":
            result = service.status()
        elif command == "list":
            service.require_setup()
            result = service.list_skills()
        elif command == "browse":
            service.require_setup()
            result = {"skills": service.search_skills(args.query)}
        elif command == "show":
            service.require_setup()
            result = service.show(args.skill_id)
        elif command == "versions":
            service.require_setup()
            result = {"versions": service.versions(args.skill_id)}
        elif command == "help":
            if as_json:
                _emit({"commands": list(_CLI_COMMANDS)}, as_json=True)
            else:
                getattr(args, "_wisdom_parser").print_help()
            return 0
        else:
            getattr(args, "_wisdom_parser").print_help()
            return 2
        _emit(result, as_json=as_json)
        return 0
    except WisdomError as exc:
        _emit(
            {"ok": False, "error": str(exc), "category": exc.exit_code},
            as_json=as_json,
        )
        return exc.exit_code
    except PackagePolicyError as exc:
        _emit({"ok": False, "error": str(exc), "category": 6}, as_json=as_json)
        return 6
    except KeyboardInterrupt:
        return 7
    except Exception:  # pylint: disable=broad-exception-caught
        _emit(
            {
                "ok": False,
                "error": "Collective Wisdom is temporarily unavailable.",
                "category": 8,
            },
            as_json=as_json,
        )
        return 8


def run_slash(  # pylint: disable=too-many-branches,too-many-return-statements
    raw_args: str, service: object
) -> str:
    """Synchronously render one mutation-free, shared-chat-safe slash view."""

    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        return "Could not parse /wisdom arguments."
    try:
        command = parts[0].casefold() if parts else "help"
        args = parts[1:]
        if command not in _SLASH_COMMANDS:
            return _unsupported_command()
        if command == "list":
            command = "browse"
        if command == "help":
            if args:
                raise _SlashUsageError("Usage: /wisdom help")
            return _slash_help()
        if command == "setup":
            return (
                "Set up Collective Wisdom\n\n"
                "Run `hermes wisdom setup` in your terminal to review the disclosure "
                "and configure this profile."
            )
        if command == "status":
            if args:
                raise _SlashUsageError("Usage: /wisdom status")
            return _render_status(service.status())
        if command == "browse":
            query, page = _paged_arguments(
                args, "Usage: /wisdom browse [query] [--page N]"
            )
            service.require_setup()
            return _render_browse(service.search_skills(query or None), query, page)
        if command == "show":
            if len(args) != 1:
                raise _SlashUsageError("Usage: /wisdom show <skill>")
            service.require_setup()
            return _render_show(service.resolve_skill(args[0]))
        if command == "versions":
            reference, page = _version_arguments(args)
            service.require_setup()
            return _render_versions(service.resolve_skill(reference), page)
    except _SlashUsageError as exc:
        return str(exc)
    except (WisdomNotFound, WisdomAuthError, WisdomConflict, WisdomValidationError) as exc:
        return _safe_service_error(exc)
    except Exception:  # pylint: disable=broad-exception-caught
        return "Collective Wisdom is temporarily unavailable. Try again shortly."
    return _unsupported_command()


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        )
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    else:
        print(value)


def _slash_help() -> str:
    return "\n".join(
        (
            "Collective Wisdom commands",
            "",
            "/wisdom browse [query]",
            "/wisdom show <skill>",
            "/wisdom versions <skill>",
            "/wisdom status",
            "/wisdom setup",
            "/wisdom help",
        )
    )


def _unsupported_command() -> str:
    return "Unsupported Collective Wisdom command.\n\n" + _slash_help()


def _paged_arguments(args: list[str], usage: str) -> tuple[str, int]:
    values: list[str] = []
    page: int | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--page":
            if page is not None or index + 1 >= len(args):
                raise _SlashUsageError(usage)
            page = _page_number(args[index + 1], usage)
            index += 2
            continue
        if value.startswith("--"):
            raise _SlashUsageError(usage)
        values.append(value)
        index += 1
    return " ".join(values).strip(), page or 1


def _version_arguments(args: list[str]) -> tuple[str, int]:
    usage = "Usage: /wisdom versions <skill> [--page N]"
    values, page = _paged_arguments(args, usage)
    references = shlex.split(values)
    if len(references) != 1:
        raise _SlashUsageError(usage)
    return references[0], page


def _page_number(value: str, usage: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise _SlashUsageError(usage) from exc
    if not 1 <= page <= _MAX_PAGE:
        raise _SlashUsageError(usage)
    return page


def _render_status(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    configured = data.get("configured") is True
    state = _PUBLIC_STATUS_STATES.get(
        str(data.get("state") or "").casefold(),
        "ready" if configured else "not configured",
    )
    if not configured and state == "ready":
        state = "not configured"
    return "\n".join(
        (
            "Collective Wisdom status",
            "",
            f"Setup: {'ready' if configured else 'required'}",
            f"State: {state}",
        )
    )


def _render_browse(rows: object, query: str, page: int) -> str:
    skills = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    start = (page - 1) * PAGE_SIZE
    selected = skills[start : start + PAGE_SIZE]
    heading = f'Results for "{_text(query)}"' if query else "Skills published by your team"
    lines = ["Shared skills", "", f"{heading} - page {page}"]
    for item in selected:
        slug = _text(item.get("slug") or item.get("id") or "Unnamed skill")
        version = _positive_version(item.get("latest_version"))
        description = _text(item.get("author_description") or "No description")
        lines.extend(
            (
                "",
                slug,
                f"v{version or '?'} - {description}",
                _review_states(item),
                f"Show: {_slash_command('show', slug)}",
            )
        )
    if not selected:
        lines.extend(("", "No matching shared skills."))
    lines.extend(_browse_navigation(query, page, start + PAGE_SIZE < len(skills)))
    return "\n".join(lines)


def _browse_navigation(query: str, page: int, has_next: bool) -> list[str]:
    lines: list[str] = []
    if page > 1:
        lines.extend(("", f"Back: {_paged_command('browse', query, page - 1)}"))
    if has_next:
        if not lines:
            lines.append("")
        lines.append(f"Next: {_paged_command('browse', query, page + 1)}")
    return lines


def _render_show(detail: object) -> str:
    data = detail if isinstance(detail, dict) else {}
    skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
    latest_detail = data.get("latest_version_detail")
    latest_detail = latest_detail if isinstance(latest_detail, dict) else {}
    latest = latest_detail.get("version")
    latest = latest if isinstance(latest, dict) else {}
    versions = data.get("versions") if isinstance(data.get("versions"), list) else []
    version_numbers = [
        version
        for item in versions
        if isinstance(item, dict)
        if (version := _positive_version(item.get("version"))) is not None
        and item.get("state") in (None, "published")
    ]
    slug = _text(skill.get("slug") or skill.get("id") or "Shared skill")
    description = _text(
        latest.get("author_description")
        or skill.get("author_description")
        or "No description"
    )
    lines = [
        slug,
        "",
        description,
        f"Latest: v{max(version_numbers) if version_numbers else '?'}",
        f"Requirements: {_requirements_summary(latest.get('system_spec'))}",
        _review_states(latest),
    ]
    portal_url = _safe_url(data.get("portal_url"))
    if portal_url:
        lines.append(f"View in Portal: {portal_url}")
    lines.append(f"Versions: {_slash_command('versions', slug)}")
    lines.append(f"Back: {_slash_command_without_argument('browse')}")
    return "\n".join(lines)


def _render_versions(  # pylint: disable=too-many-locals
    detail: object, page: int
) -> str:
    data = detail if isinstance(detail, dict) else {}
    skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
    slug = _text(skill.get("slug") or skill.get("id") or "Shared skill")
    values = data.get("versions") if isinstance(data.get("versions"), list) else []
    versions = sorted(
        (
            item
            for item in values
            if isinstance(item, dict)
            and _positive_version(item.get("version")) is not None
            and item.get("state") in (None, "published")
        ),
        key=lambda item: int(item["version"]),
        reverse=True,
    )
    start = (page - 1) * PAGE_SIZE
    selected = versions[start : start + PAGE_SIZE]
    lines = [f"{slug} versions", "", f"Published versions - page {page}"]
    base_url = _safe_url(data.get("portal_url"))
    for item in selected:
        number = int(item["version"])
        published = _text(
            item.get("published_at")
            or item.get("created_at")
            or "Immutable published version"
        )
        lines.extend(("", f"Version {number}", published, _review_states(item)))
        if isinstance(item.get("system_spec"), dict):
            lines.append(f"Requirements: {_requirements_summary(item['system_spec'])}")
        version_url = _version_url(base_url, number)
        if version_url:
            lines.append(f"View in Portal: {version_url}")
    if not selected:
        lines.extend(("", "No published versions."))
    has_next = start + PAGE_SIZE < len(versions)
    lines.extend(("", f"Back: {_slash_command('show', slug)}"))
    if page > 1:
        lines.append(f"Previous: {_paged_command('versions', slug, page - 1)}")
    if has_next:
        lines.append(f"Next: {_paged_command('versions', slug, page + 1)}")
    return "\n".join(lines)


def _requirements_summary(value: object) -> str:
    specification = value if isinstance(value, dict) else {}
    requirements = specification.get("requirements")
    requirements = requirements if isinstance(requirements, dict) else specification
    hermes = requirements.get("hermes")
    hermes = hermes if isinstance(hermes, dict) else {}
    runtime = requirements.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    parts: list[str] = []
    minimum = hermes.get("minimum_version")
    if isinstance(minimum, str) and minimum:
        parts.append(f"Hermes {_text(minimum)}+")
    for field, label in (("platforms", "OS"), ("architectures", "CPU")):
        values = requirements.get(field)
        if isinstance(values, list):
            safe_values = [_text(item) for item in values if isinstance(item, str)]
            if safe_values:
                parts.append(f"{label}: {', '.join(safe_values)}")
    enabled_runtime = _enabled_runtime(runtime)
    if enabled_runtime:
        parts.append("runtime: " + ", ".join(enabled_runtime))
    tools = requirements.get("tools")
    plugins = requirements.get("plugins")
    if isinstance(tools, list) and tools:
        parts.append(f"{len(tools)} tool requirement(s)")
    if isinstance(plugins, list) and plugins:
        parts.append(f"{len(plugins)} plugin requirement(s)")
    model_summary = _model_requirement_summary(requirements.get("model"))
    if model_summary:
        parts.append(model_summary)
    parts.extend(_opaque_requirement_summaries(requirements))
    return "; ".join(parts) or "No special requirements declared"


def _enabled_runtime(runtime: dict[str, Any]) -> list[str]:
    return [
        name for name in ("shell", "browser", "code") if runtime.get(name) is True
    ]


def _model_requirement_summary(value: object) -> str | None:
    model = value if isinstance(value, dict) else {}
    capabilities = _safe_list_count(model.get("capabilities"))
    context_declared = (
        isinstance(model.get("minimum_context_window"), int)
        and not isinstance(model.get("minimum_context_window"), bool)
        and model["minimum_context_window"] > 0
    )
    if capabilities or context_declared:
        qualifiers = []
        if capabilities:
            qualifiers.append(f"{capabilities} capability requirement(s)")
        if context_declared:
            qualifiers.append("context minimum declared")
        return "model: " + ", ".join(qualifiers)
    return None


def _opaque_requirement_summaries(requirements: dict[str, Any]) -> list[str]:
    """Count sensitive requirements without rendering their values."""

    summaries: list[str] = []
    for field, label in (
        ("credentials", "credential"),
        ("connections", "connection"),
        ("hardware", "hardware"),
    ):
        count = _safe_list_count(requirements.get(field))
        if count:
            summaries.append(f"{count} {label} requirement(s)")
    limitations = _safe_list_count(requirements.get("known_limitations"))
    if limitations:
        summaries.append(f"{limitations} known limitation(s)")
    filesystem = requirements.get("filesystem")
    filesystem = filesystem if isinstance(filesystem, dict) else {}
    readable = _safe_list_count(filesystem.get("read"))
    writable = _safe_list_count(filesystem.get("write"))
    if readable or writable:
        summaries.append(f"filesystem: {readable} read, {writable} write location(s)")
    network = requirements.get("network")
    network = network if isinstance(network, dict) else {}
    destinations = _safe_list_count(network.get("destinations"))
    if destinations:
        summaries.append(f"{destinations} network destination requirement(s)")
    return summaries


def _safe_list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _review_states(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    lines = []
    for key, label in _CHECK_LABELS:
        check = data.get(key)
        check = check if isinstance(check, dict) else {}
        state = _CHECK_STATES.get(str(check.get("status") or "").casefold(), "Unavailable")
        lines.append(f"{label}: {state}")
    return " - ".join(lines)


def _safe_service_error(exc: Exception) -> str:
    if isinstance(exc, WisdomNotFound):
        return "That Collective Wisdom item was not found."
    if isinstance(exc, WisdomAuthError):
        return "Sign in again or ask your team administrator for Collective Wisdom access."
    if isinstance(exc, WisdomConflict):
        return "The Collective Wisdom item changed. Open it again to see its current state."
    return "The Collective Wisdom request was invalid."


def _positive_version(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _text(value: object, *, limit: int = 512) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


def _slash_command(command: str, argument: str) -> str:
    return "/wisdom " + shlex.join((command, argument))


def _slash_command_without_argument(command: str) -> str:
    return "/wisdom " + shlex.join((command,))


def _paged_command(command: str, argument: str, page: int) -> str:
    parts = [command]
    if argument:
        parts.append(argument)
    parts.extend(("--page", str(page)))
    return "/wisdom " + shlex.join(parts)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return value


def _version_url(base_url: str | None, version: int) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["version"] = str(version)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
