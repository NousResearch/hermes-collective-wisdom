"""Human-first, control-safe terminal presentation for Wisdom commands."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, cast

from .package import LEFT_OUT_LABEL, MAX_METADATA_BYTES, MAX_TREE_BYTES, PackagePolicyError

_SAFE_FINDING_FIELDS = (
    "severity", "category", "pattern_id", "file", "line", "description"
)
_INTERPRETERS = frozenset({
    "bash", "dash", "env", "fish", "node", "perl", "python", "python3",
    "ruby", "sh", "zsh",
})


def without_controls(value: object, *, preserve_layout: bool = False) -> str:
    """Replace active controls while optionally retaining newline/tab layout."""

    return "".join(
        character
        if preserve_layout and character in {"\n", "\t"}
        else " " if unicodedata.category(character) in {"Cc", "Cf"}
        else character
        for character in str(value)
    )


def text(value: object, *, limit: int = 512) -> str:
    """Return one bounded, whitespace-normalized terminal value."""

    return " ".join(without_controls(value).split())[:limit]


def path_text(value: object) -> str:
    """Return one complete package path, controls replaced and nothing truncated."""

    return without_controls(value)


def safe_scan_details(scan: object) -> list[dict[str, Any]]:
    """Project scanner evidence without ever exposing a raw matched substring."""

    findings: object = None
    try:
        from .local_security import safe_scan_findings  # pylint: disable=import-outside-toplevel

        findings = safe_scan_findings(scan)
    except (ImportError, AttributeError):
        current = scan
        if isinstance(current, dict) and isinstance(current.get("guard"), dict):
            current = current["guard"]
        if isinstance(current, dict):
            findings = current.get("findings")
    if not isinstance(findings, list):
        return []
    result: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        projected: dict[str, Any] = {}
        for key in _SAFE_FINDING_FIELDS:
            value = finding.get(key)
            if key == "line":
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    projected[key] = value
            elif isinstance(value, str) and value:
                projected[key] = text(value, limit=1024)
        if projected:
            result.append(projected)
    return result


def has_reviewable_warnings(value: object) -> bool:
    """Return whether a result requires explicit scanner warning acceptance."""

    data = value if isinstance(value, dict) else {}
    security = data.get("security_check")
    security = security if isinstance(security, dict) else {}
    if security.get("local_status") == "warning_required":
        return True
    scan = data.get("local_scan")
    scan = (
        scan.get("guard")
        if isinstance(scan, dict) and isinstance(scan.get("guard"), dict)
        else scan
    )
    return isinstance(scan, dict) and scan.get("allowed") is False


def render_findings(value: object) -> str:
    """Render complete safe finding facts, grouped by severity and category."""

    data = value if isinstance(value, dict) else {}
    scan = data.get("local_scan", data)
    details = safe_scan_details(scan)
    if not details:
        if has_reviewable_warnings(data):
            return "Scanner warnings: warning details are unavailable; acknowledgement is blocked."
        return "Local scanner warnings: none reported."
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for finding in details:
        key = (
            str(finding.get("severity") or "warning"),
            str(finding.get("category") or "other"),
        )
        grouped.setdefault(key, []).append(finding)
    lines = [f"Scanner findings ({len(details)})"]
    for (severity, category), values in grouped.items():
        lines.append(f"  {text(severity).upper()} / {text(category)}")
        for finding in values:
            rule = text(finding.get("pattern_id") or "unnamed rule")
            location = text(finding.get("file") or "package")
            if finding.get("line"):
                location += f":{finding['line']}"
            description = text(finding.get("description") or "No description", limit=1024)
            lines.append(f"    - {rule} at {location}: {description}")
    return "\n".join(lines)


def gateway_findings(value: object) -> list[dict[str, Any]]:
    """Safe projection of the Gateway's stored scan findings (path/detail shape)."""

    data = value if isinstance(value, dict) else {}
    server = data.get("server_checks")
    check = server.get("security_check") if isinstance(server, dict) else None
    findings = check.get("findings") if isinstance(check, dict) else None
    if not isinstance(findings, list):
        return []
    return safe_scan_details({"findings": [
        {**finding, "description": finding.get("description") or finding.get("detail")
         or finding.get("message")}
        for finding in findings if isinstance(finding, dict)
    ]})


def render_gateway_findings(value: object, *, limit: int | None = None) -> str:
    """Render Gateway findings; ``limit`` bounds a summary, never the full list."""

    details = gateway_findings(value)
    shown = details if limit is None else details[:limit]
    lines = []
    for finding in shown:
        location = text(finding.get("file") or "package")
        if finding.get("line"):
            location += f":{finding['line']}"
        label = text(finding.get("category") or "finding")
        severity = text(finding.get("severity") or "advisory")
        description = text(finding.get("description") or "No description", limit=300)
        lines.append(f"• {label} ({severity}) in {location}: {description}")
    if len(details) > len(shown):
        lines.append(f"…and {len(details) - len(shown)} more. View details lists every finding.")
    return "\n".join(lines)


def _file_line(number: int, item: dict[str, Any], path: str) -> str:
    """One reviewed file: whole path, executable mark and size."""

    mode = " [executable]" if item.get("mode") == "exec" else ""
    size = item.get("size")
    suffix = (f" — {size} bytes"
              if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else "")
    return f"  {number}. {path}{mode}{suffix}"


def render_inventory(value: object) -> str:
    """Render every reviewed file without dumping file bodies."""

    data = value if isinstance(value, dict) else {}
    files = data.get("files") if isinstance(data.get("files"), list) else []
    lines = [f"Files ({len(files)})"]
    # Paths print whole. The package walk already bounds every path it admits
    # or leaves out by MAX_METADATA_BYTES, so the listing uses that same budget
    # instead of cutting individual paths.
    budget = [MAX_METADATA_BYTES]

    def complete(path: str) -> str | None:
        budget[0] -= len(path.encode("utf-8")) + 128
        return path if budget[0] >= 0 else None

    hidden = 0
    for number, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            continue
        path = complete(path_text(item.get("path") or "unnamed"))
        if path is None:
            hidden += 1
        else:
            lines.append(_file_line(number, item, path))
    omitted = data.get("omitted_paths")
    if isinstance(omitted, list) and omitted:
        # Every path, one per line like the files above: an exact review never
        # truncates what it left out.
        lines.append(f"{LEFT_OUT_LABEL} ({len(omitted)}):")
        for raw in omitted:
            path = complete(path_text(raw))
            if path is None:
                hidden += 1
            else:
                lines.append(f"  - {path}")
    if hidden:
        lines.append(f"…and {hidden} more paths: the list exceeds the "
                     f"{MAX_METADATA_BYTES}-byte package path budget.")
    return "\n".join(lines)


def _owner_lines(data: dict[str, Any]) -> list[str]:
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    lines = []
    for key, label in (
        ("organization_id", "Organization"),
        ("account_id", "Owner account"),
        ("profile_name", "Profile"),
    ):
        value = owner.get(key)
        if isinstance(value, str) and value:
            lines.append(f"{label}: {text(value)}")
    return lines


def _list_lines(label: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    lines = [label + ":"]
    for item in value:
        if isinstance(item, (str, int)) and not isinstance(item, bool):
            lines.append("  - " + text(item, limit=1024))
        elif isinstance(item, dict):
            summary = item.get("description") or item.get("name") or item.get("command")
            if isinstance(summary, str):
                lines.append("  - " + text(summary, limit=1024))
    return lines


_REVIEW_TITLES = {
    "install": "Installation review",
    "update": "Update review",
    "publication": "Sharing review",
    "installed-setup": "Installed setup review",
}

_REVIEW_FACTS = (
    ("slug", "Skill"), ("skill_id", "Skill ID"),
    ("from_version", "Installed version"), ("version", "Version"),
    ("update_mode", "Update mode"), ("publication_mode", "Handling"),
    ("state", "State"), ("publication_state", "Publication state"),
    ("operation_id", "Operation"), ("draft_id", "Draft"),
    ("review_expires_at", "Review expires"),
)

_RECEIPT_COMMANDS = {
    "publication": "hermes wisdom publication submit ",
    "installed-setup": "hermes wisdom installed-setup apply ",
}


def _review_header_lines(kind: str, data: dict[str, Any]) -> list[str]:
    lines = [_REVIEW_TITLES.get(kind, "Collective Wisdom review")]
    for key, label in _REVIEW_FACTS:
        if key in {"skill_id", "operation_id", "draft_id", "review_expires_at"}:
            continue
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            lines.append(f"{label}: {text(value)}")
    description = data.get("description") or data.get("drafted_description")
    if isinstance(description, str) and description:
        lines.append("Description: " + text(description, limit=1024))
    return lines


def _compatibility_lines(data: dict[str, Any]) -> list[str]:
    compatibility = data.get("compatibility")
    lines: list[str] = []
    if isinstance(compatibility, dict) and compatibility:
        lines.append("Compatibility: " + text(compatibility.get("outcome") or "unknown"))
        for key, label in (
            ("satisfied", "Satisfied checks"),
            ("setup_actions", "Required setup actions"),
            ("limitations", "Known limitations"),
            ("blocked", "Needs attention"),
            ("notes", "Notes"),
        ):
            lines.extend(_list_lines(label, compatibility.get(key)))
    requirements = data.get("system_specification")
    if not isinstance(requirements, dict):
        manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else {}
        requirements = manifest.get("requirements")
    if isinstance(requirements, dict) and requirements:
        lines.append("Needed to use this skill: " + _requirements_summary(requirements))
    return lines


def _review_check_lines(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    server = data.get("server_checks")
    if isinstance(server, dict) and server:
        lines.append("Server checks:")
        for name, check in server.items():
            if isinstance(check, dict):
                state = check.get("status") or check.get("state") or "available"
                lines.append(f"  - {text(name)}: {text(state)}")
                if isinstance(check.get("findings"), list) and check["findings"]:
                    lines.append(render_findings({"findings": [
                        {**finding, "description": finding.get("description")
                         or finding.get("detail") or finding.get("message")}
                        for finding in check["findings"] if isinstance(finding, dict)
                    ]}))
    lines.append(render_findings(data))
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    acceptance = policy.get("warning_acceptance")
    if not isinstance(acceptance, dict):
        acceptance = data.get("warning_acceptance")
    if isinstance(acceptance, dict):
        state = "accepted" if acceptance.get("accepted") is True else "not accepted"
        lines.append("Warning acknowledgement: " + state)
    if data.get("modified") is True:
        preservation = data.get("preservation")
        preservation = preservation if isinstance(preservation, dict) else {}
        if data.get("local_baseline") == "unknown":
            lines.append(
                "Local published baseline: unknown; preservation required; action: "
                + text(preservation.get("action") or "blocked")
            )
        else:
            lines.append(
                "Local edits: detected; action: "
                + text(preservation.get("action") or "blocked")
            )
    if data.get("sensitive_expansion"):
        lines.extend(_list_lines("New sensitive capabilities", data["sensitive_expansion"]))
    return lines


def _setup_lines(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    step_value = data.get("step")
    step = step_value if isinstance(step_value, dict) else {}
    phase = step.get("phase")
    if isinstance(phase, str):
        lines.append("Setup phase: " + text(phase))
    index = step.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        lines.append(f"Setup step: {index + 1}")
    command = step.get("command")
    if isinstance(command, str) and command:
        lines.append(
            "Command (escaped literal; not executed by review): "
            + json.dumps(command, ensure_ascii=True)
        )
    prerequisites = data.get("prerequisites")
    if isinstance(prerequisites, list) and prerequisites:
        lines.append("Prerequisites:")
        for prerequisite in prerequisites:
            if isinstance(prerequisite, dict):
                name = text(prerequisite.get("name") or prerequisite.get("kind") or "check")
                status = text(prerequisite.get("status") or "unknown")
                lines.append(f"  - {name}: {status}")
    verification = data.get("verification")
    if isinstance(verification, dict):
        lines.append("Verification: " + text(verification.get("state") or "unknown"))
    return lines


def _decision_lines(kind: str, data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if data.get("allowed") is False:
        lines.append("Approval permission: blocked")
        lines.append("Approval status: blocked until the listed review actions are completed.")
    elif data.get("allowed") is True:
        lines.append("Approval permission: allowed")
        lines.append("Approval status: eligible for exact final confirmation.")
    if kind == "publication":
        raw_mode = data.get("publication_mode")
        mode = raw_mode.casefold() if isinstance(raw_mode, str) else ""
        consequence = {
            "open": "Open: confirmation may publish directly.",
            "moderated": "Moderated: confirmation submits for team moderation.",
            "managed": "Managed: confirmation follows organization-managed Gateway handling.",
        }.get(mode)
        if consequence is None:
            consequence = (
                "Gateway publication policy is unavailable or unrecognized; "
                "no publication route is claimed."
                if mode else
                "Gateway publication policy has not been checked yet; "
                "no publication route is claimed."
            )
        lines.append("Consequence: " + consequence)
        if data.get("network_submission") is False:
            lines.append("Nothing has been uploaded yet.")
        if isinstance(data.get("receipt"), str) and data.get("receipt"):
            lines.append("Next: hermes wisdom publication submit <local-skill-name>")
    return lines


def _compact_scanner_lines(data: dict[str, Any]) -> list[str]:
    """Report severity and bounded, sanitized examples."""
    details = safe_scan_details(data.get("local_scan"))
    if not details:
        return (["Scanner warnings: details unavailable; acknowledgement blocked."]
                if has_reviewable_warnings(data) else ["Scanner warnings: none reported."])
    counts = Counter(text(item.get("severity") or "warning").lower() for item in details)
    levels = ("critical", "high", "medium", "low", "warning", "advisory")
    totals = [f"{level}: {counts.pop(level)}" for level in levels if counts[level]]
    totals.extend(f"{name}: {count}" for name, count in sorted(counts.items()) if count)
    ordered = sorted(details, key=lambda item: levels.index(item.get("severity"))
                     if item.get("severity") in levels else len(levels))
    examples = dict.fromkeys(
        text(item.get("pattern_id") or item.get("category") or "finding", limit=60)
        + (": " + text(item["description"], limit=100)
           if item.get("description") else "") for item in ordered
    )
    return [f"Scanner warnings: {len(details)} ({', '.join(totals)})",
            "Examples: " + "; ".join(list(examples)[:3]),
            "Full scanner findings: review with --plan --json."]


def _compact_compat_lines(data: dict[str, Any]) -> list[str]:
    """Summarize compatibility actions without an unbounded list."""
    lines: list[str] = []
    compatibility = data.get("compatibility")
    if isinstance(compatibility, dict):
        lines.append("Compatibility: " + text(compatibility.get("outcome") or "unknown"))
        for key, label in (("blocked", "Needs attention"),
                           ("setup_actions", "Required setup actions"),
                           ("limitations", "Known limitations"),
                           ("notes", "Notes")):
            actions = compatibility.get(key)
            if isinstance(actions, list) and actions:
                lines.extend(_list_lines(label, actions[:2]))
                if len(actions) > 2:
                    lines.append(f"  - and {len(actions) - 2} more")
    return lines


def _compact_server_lines(data: dict[str, Any]) -> list[str]:
    """Summarize Gateway checks separately from local scanner findings."""
    lines: list[str] = []
    server = data.get("server_checks")
    if isinstance(server, dict):
        for name, check in server.items():
            if isinstance(check, dict):
                state = check.get("status") or check.get("state") or "available"
                findings = check.get("findings")
                suffix = (f" ({len(findings)} findings)"
                          if isinstance(findings, list) and findings else "")
                lines.append(f"{text(name)}: {text(state)}{suffix}")
    return lines


def _compact_approval_lines(data: dict[str, Any]) -> list[str]:
    """Retain state-changing review gates while hiding internal receipts."""
    lines: list[str] = []
    policy = data.get("policy")
    if not isinstance(policy, dict):
        policy = {}
    acceptance = policy.get("warning_acceptance") or data.get("warning_acceptance")
    if isinstance(acceptance, dict):
        state = "accepted" if acceptance.get("accepted") is True else "not accepted"
        lines.append("Warning acknowledgement: " + state)
    if data.get("modified") is True:
        preservation = data.get("preservation")
        preservation = preservation if isinstance(preservation, dict) else {}
        if data.get("local_baseline") == "unknown":
            lines.append("Local published baseline: unknown; preservation required; action: "
                         + text(preservation.get("action") or "blocked"))
        else:
            lines.append("Local edits: detected; action: "
                         + text(preservation.get("action") or "blocked"))
    if data.get("sensitive_expansion"):
        lines.extend(_list_lines("New sensitive capabilities", data["sensitive_expansion"]))
    if data.get("allowed") is False:
        lines.append("Approval permission: blocked")
    elif data.get("allowed") is True:
        lines.append("Approval permission: allowed")
    return lines


def _package_review(kind: str, data: dict[str, Any]) -> str:
    """Summarize install/update evidence without suppressing its severity."""
    lines = [_REVIEW_TITLES[kind]]
    for key, label in (("slug", "Skill"), ("from_version", "Installed version"),
                       ("version", "Version")):
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            lines.append(f"{label}: {text(value)}")
    lines.extend(_compact_compat_lines(data))
    files = data.get("files")
    lines.append(f"Files: {len(files) if isinstance(files, list) else 0}")
    lines.extend(_compact_server_lines(data))
    lines.extend(_compact_scanner_lines(data))
    commands = data.get("setup_commands")
    if isinstance(commands, list):
        for command in commands:
            if isinstance(command, str):
                lines.append("Setup command: " + json.dumps(command, ensure_ascii=True))
    lines.extend(_compact_approval_lines(data))
    return "\n".join(lines)


def render_action_review(kind: str, value: object) -> str:
    """Render approval-critical install/update/publication/setup facts."""

    data = value if isinstance(value, dict) else {}
    if kind in {"install", "update"}:
        return _package_review(kind, data)
    lines = _review_header_lines(kind, data)
    lines.extend(_compatibility_lines(data))
    lines.append(render_inventory(data))
    lines.extend(_review_check_lines(data))
    lines.extend(_setup_lines(data))
    lines.extend(_decision_lines(kind, data))
    return "\n".join(line for line in lines if line)


def _partial_setup_outcome(data: dict, name: str, kind: str) -> str | None:
    """Separate the file-write result from setup's independently tracked state."""
    setup = data.get("setup")
    if data.get("installed" if kind == "install" else "updated") is not True:
        return None
    verb = "installed" if kind == "install" else "updated"
    incomplete = f"{name or 'This skill'}: files {verb}; setup incomplete."
    if not isinstance(setup, dict) or setup.get("state") == "passed":
        if data.get("setup_required") is True and data.get("ready_to_use") is not True:
            if (isinstance(setup, dict) and setup.get("state") == "passed"
                    and data.get("setup_execution_authorized") is True):
                return (f"✅ {name or 'This skill'} {verb}\n"
                        "Setup ran successfully. Have your agent confirm the skill is ready "
                        "before using it.")
            return incomplete + " Check setup status before use."
        return None
    step = setup.get("step")
    position = f" at step {step}" if isinstance(step, int) and step > 0 else ""
    details = str(setup.get("detail") or "").splitlines()
    reason = details[0][:200] if details else ""
    detail = f": {reason}" if reason else ""
    return (f"{incomplete} Setup {text(setup.get('state') or 'unknown')}"
            f"{position}{detail}")


def render_operation_outcome(
    kind: str, value: object, *, skill_name: str | None = None
) -> str:
    """Render a terse success only for a completed file operation."""

    data = value if isinstance(value, dict) else {}
    completed = {
        "install": "installed",
        "update": "updated",
        "uninstall": "uninstalled",
    }
    key = completed.get(kind)
    raw_name = skill_name or data.get("skill_name") or data.get("slug")
    name = text(raw_name) if isinstance(raw_name, str) else ""
    complete = (
        data.get("pending") is not True and data.get("rejected") is not True
        and not data.get("error")
        and data.get("state") in (None, "completed", "done", "inactive")
    )
    partial_setup = (_partial_setup_outcome(data, name, kind)
                     if kind in {"install", "update"} and complete else None)
    if partial_setup is not None:
        return partial_setup
    if key and name and complete and data.get(key) is True:
        verb = "removed" if kind == "uninstall" else key
        return f"✅ {name} {verb}"
    if data.get("pending") is True:
        state = "pending"
    elif data.get("rejected") is True:
        state = "rejected"
    else:
        state = text(data.get("state") or "unknown")
    detail = data.get("detail") or data.get("error")
    return f"{name or 'This skill'}: {state}" + (
        f" — {text(detail, limit=1024)}" if detail else ""
    )


def _requirements_summary(requirements: dict[str, Any]) -> str:
    parts: list[str] = []
    hermes = requirements.get("hermes")
    if isinstance(hermes, dict) and isinstance(hermes.get("minimum_version"), str):
        parts.append("Hermes " + text(hermes["minimum_version"]) + "+")
    for key, label in (("platforms", "OS"), ("architectures", "CPU")):
        values = requirements.get(key)
        if isinstance(values, list):
            safe = [text(item) for item in values if isinstance(item, str)]
            if safe:
                parts.append(f"{label}: {', '.join(safe)}")
    runtime = requirements.get("runtime")
    if isinstance(runtime, dict):
        active = [text(key) for key, enabled in runtime.items() if enabled is True]
        if active:
            parts.append("runtime: " + ", ".join(active))
    for key, label in (
        ("tools", "tool"), ("plugins", "plugin"),
        ("credentials", "credential"), ("known_limitations", "limitation"),
    ):
        values = requirements.get(key)
        if isinstance(values, list) and values:
            parts.append(f"{len(values)} {label} item(s)")
    return "; ".join(parts) or "No special setup listed"


def _installation_status_text(value: object, slug: object = None) -> str:
    data = value if isinstance(value, dict) else {}
    status = data.get("installation_status")
    if isinstance(status, dict) and isinstance(status.get("text"), str):
        return text(status["text"]) + removed_install_hint(status, slug or data.get("slug"))
    return "Installation status unavailable"


def removed_install_hint(status: object, slug: object) -> str:
    """The terminal's way back for a skill whose files were removed outside Wisdom."""
    if (isinstance(status, dict) and status.get("state") == "removed"
            and isinstance(slug, str) and slug):
        return " · install again: hermes wisdom install " + shlex.quote(without_controls(slug))
    return ""


def _render_skill_detail(data: dict[str, Any]) -> str:
    """Show named catalog metadata and current installation state."""
    skill = cast(dict[str, Any], data["skill"]) if isinstance(data.get("skill"), dict) else {}
    raw_detail = data.get("latest_version_detail")
    latest_detail = cast(dict[str, Any], raw_detail) if isinstance(raw_detail, dict) else {}
    latest = (cast(dict[str, Any], latest_detail["version"])
              if isinstance(latest_detail.get("version"), dict) else {})
    lines = [text(skill.get("slug") or "Shared skill")]
    description = (
        latest.get("author_description")
        or skill.get("author_description")
        or "No description"
    )
    lines.append(text(description, limit=4096))
    versions = cast(list[Any], data["versions"]) if isinstance(data.get("versions"), list) else []
    numbers = [row.get("version") for row in versions if isinstance(row, dict)]
    numbers = [
        item for item in numbers
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]
    lines.append(f"Latest: v{max(numbers) if numbers else '?'}")
    lines.append(_installation_status_text(data, skill.get("slug")))
    for key, label in (("security_check", "Security"),
                       ("professionalism_check", "Professionalism")):
        check = latest.get(key)
        if isinstance(check, dict):
            lines.append(f"{label}: {text(check.get('status') or 'unavailable')}")
    requirements = latest.get("system_spec")
    if isinstance(requirements, dict):
        lines.append("Needed to use this skill: " + _requirements_summary(requirements))
    return "\n".join(lines)


def render_read_command(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    command: str | None, value: object
) -> str:
    """Render ordinary command families with useful rows and resumable actions."""

    data = value if isinstance(value, dict) else {}
    if command in {"list", "browse"}:
        rows = data.get("skills") if isinstance(data.get("skills"), list) else []
        lines = ["Shared skills", f"Results: {len(rows)}"]
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("slug") or "Unnamed skill"
            version = row.get("latest_version")
            suffix = (
                f" (v{version})"
                if isinstance(version, int) and not isinstance(version, bool) else ""
            )
            description = text(row.get("author_description") or "No description")
            lines.append(f"  - {text(name)}{suffix}: {description}")
            lines.append("    " + _installation_status_text(row))
        if not rows:
            lines.append("No matching shared skills.")
        return "\n".join(lines)
    if command == "show":
        return _render_skill_detail(data)
    if command == "versions":
        raw_rows = data.get("versions")
        rows = raw_rows if isinstance(raw_rows, list) else []
        status_source = next((row for row in rows if isinstance(row, dict)), {})
        lines = [
            "Published versions", _installation_status_text(status_source),
            f"Results: {len(rows)}",
        ]
        valid = [row for row in rows if isinstance(row, dict)]
        valid.sort(
            key=lambda row: row.get("version")
            if isinstance(row.get("version"), int) and not isinstance(row.get("version"), bool)
            else -1,
            reverse=True,
        )
        for row in valid:
            version = row.get("version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                continue
            state = text(row.get("published_at") or row.get("state") or "published")
            lines.append(f"  Version {version}: {state}")
            checks = [f"{label}: {text(row[key].get('status') or 'unavailable')}"
                      for key, label in (("security_check", "Security"),
                                         ("professionalism_check", "Professionalism"))
                      if isinstance(row.get(key), dict)]
            lines.extend(checks)
        if not valid:
            lines.append("No published versions.")
        return "\n".join(lines)
    if command == "installed":
        rows = value if isinstance(value, list) else data.get("installations")
        rows = rows if isinstance(rows, list) else []
        lines = ["Managed installations", f"Results: {len(rows)}"]
        for row in rows:
            if isinstance(row, dict):
                removed = (" · " + text(row.get("status_text") or "Removed")
                           if row.get("state") == "removed" else "")
                lines.append(
                    f"  - {text(row.get('slug') or 'Unnamed skill')}"
                    f" v{text(row.get('version') or '?')}{removed}"
                )
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(
            (text(command or "Collective Wisdom").title(), f"Results: {len(value)}")
        )
    lines = [text((command or "Collective Wisdom").replace("-", " ").title())]
    for key, label in (
        ("state", "State"), ("configured", "Configured"),
        ("delivery_mode", "Delivery mode"), ("queued", "Queued"),
        ("message", "Message"), ("slug", "Skill"), ("version", "Version"),
    ):
        item = data.get(key)
        if isinstance(item, (str, int, bool)):
            lines.append(f"{label}: {text(item)}")
    for key, values in data.items():
        if not isinstance(values, list):
            continue
        lines.append(f"{text(key.replace('_', ' ').title())}: {len(values)}")
        for row in values:
            if not isinstance(row, dict):
                continue
            summary = _safe_row_summary(row)
            if summary:
                lines.append("  - " + summary)
    return "\n".join(lines)


def _safe_row_summary(row: dict[str, Any]) -> str:
    """Render a bounded list row from explicitly non-secret presentation fields."""

    values = []
    for key in (
        "kind", "slug", "version", "state",
        "message", "advice", "occurred_at", "created_at",
    ):
        value = row.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            values.append(f"{key.replace('_', ' ')}={text(value)}")
    return "; ".join(values)


def review_identity(value: object) -> str:
    """Return a stable material-facts projection for display/review race checks."""

    data = value if isinstance(value, dict) else {}
    files = []
    for item in data.get("files", []) if isinstance(data.get("files"), list) else []:
        if isinstance(item, dict):
            files.append({key: item.get(key) for key in ("path", "mode", "hash", "size")})
    material = {
        "hashes": data.get("hashes"),
        "description": data.get("description") or data.get("drafted_description"),
        "system_specification": data.get("system_specification"),
        "files": files,
        "findings": safe_scan_details(data.get("local_scan")),
        "scan_fingerprint": hashlib.sha256(
            json.dumps(
                data.get("local_scan"), sort_keys=True, separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest(),
        "warning_required": has_reviewable_warnings(data),
        # Exact Gateway scan evidence for a retained operation (None for local drafts).
        "gateway_evidence": data.get("gateway_evidence_hash"),
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def render_file_preview(item: object) -> str:
    """Render escaped UTF-8 text or inert metadata for one explicitly selected file."""

    data = item if isinstance(item, dict) else {}
    path = text(data.get("path") or "unnamed")
    content = data.get("content_utf8")
    if not isinstance(content, str):
        size = data.get("size")
        if not isinstance(size, int):
            encoded = data.get("content_base64")
            size = "unknown" if not isinstance(encoded, str) else f"base64 length {len(encoded)}"
        return f"{path}\nBinary/opaque file ({size}); active preview is disabled."
    escaped = without_controls(content, preserve_layout=True)
    limit = 16_384
    suffix = (
        "\n[preview truncated; use prepared inspect --json for exact export]"
        if len(escaped) > limit else ""
    )
    return f"{path}\n---\n{escaped[:limit]}{suffix}\n---"


def edit_prepared(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    service: Any, draft_id: str
) -> object:
    """Edit a disposable UTF-8 copy, then CAS-save the complete unchanged snapshot."""

    payload = service.prepared_inspect(draft_id)
    if not isinstance(payload, dict):
        raise PackagePolicyError("prepared draft inspection is unavailable")
    files = payload.get("files")
    if not isinstance(files, list):
        raise PackagePolicyError("prepared draft file inventory is unavailable")
    editable = [
        (index, item) for index, item in enumerate(files)
        if isinstance(item, dict) and isinstance(item.get("content_utf8"), str)
    ]
    if not editable:
        raise PackagePolicyError("prepared draft has no UTF-8 file available for editing")
    selected_index, selected = next(
        ((index, item) for index, item in editable if item.get("path") == "SKILL.md"),
        editable[0],
    )
    raw_editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not raw_editor:
        raise PackagePolicyError("set VISUAL or EDITOR to a trusted text editor")
    try:
        command = shlex.split(raw_editor)
    except ValueError as exc:
        raise PackagePolicyError("VISUAL or EDITOR is not a valid command") from exc
    if not command:
        raise PackagePolicyError("set VISUAL or EDITOR to a trusted text editor")
    executable = Path(command[0]).name.casefold()
    if executable in _INTERPRETERS or executable.rstrip("0123456789.-") in _INTERPRETERS:
        raise PackagePolicyError("VISUAL or EDITOR must name a text editor, not an interpreter")
    inputs = {
        "draft_id": payload.get("draft_id"),
        "author_description": payload.get("author_description"),
        "files": files,
        "expected_hashes": payload.get("expected_hashes"),
    }
    if not isinstance(inputs["draft_id"], str) or not isinstance(inputs["author_description"], str):
        raise PackagePolicyError("prepared draft edit metadata is unavailable")
    if not isinstance(inputs["expected_hashes"], dict):
        raise PackagePolicyError("prepared draft edit hashes are unavailable")
    with tempfile.TemporaryDirectory(prefix="wisdom-edit-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        edit_path = directory / "editable.txt"
        edit_path.write_text(str(selected["content_utf8"]), encoding="utf-8")
        edit_path.chmod(0o600)
        try:
            completed = subprocess.run(  # nosec B603 - user-selected editor, no shell
                [*command, str(edit_path)], check=False
            )
        except OSError as exc:
            raise PackagePolicyError("the selected editor could not be started") from exc
        if completed.returncode != 0:
            raise PackagePolicyError("the selected editor exited without saving the draft")
        info = edit_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or edit_path.resolve().parent != directory.resolve()
            or info.st_size > MAX_TREE_BYTES
        ):
            raise PackagePolicyError("edited draft file is unsafe or too large")
        try:
            edited = edit_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PackagePolicyError("edited draft file must remain valid UTF-8 text") from exc
    replacement = dict(selected)
    replacement.pop("content_base64", None)
    replacement["content_utf8"] = edited
    saved_files = [dict(item) if isinstance(item, dict) else item for item in files]
    saved_files[selected_index] = replacement
    return service.prepared_save(
        inputs["draft_id"],
        author_description=inputs["author_description"],
        files=saved_files,
        expected_hashes=inputs["expected_hashes"],
    )
