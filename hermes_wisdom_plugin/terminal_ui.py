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
from pathlib import Path
from typing import Any

from .package import MAX_TREE_BYTES, PackagePolicyError

_SAFE_FINDING_FIELDS = (
    "severity", "category", "pattern_id", "file", "line", "description"
)
_INTERPRETERS = frozenset({
    "bash", "dash", "env", "fish", "node", "perl", "python", "python3",
    "ruby", "sh", "zsh",
})


def without_controls(value: object) -> str:
    """Replace terminal control characters while preserving ordinary Unicode."""

    return "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in str(value)
    )


def text(value: object, *, limit: int = 512) -> str:
    """Return one bounded, whitespace-normalized terminal value."""

    return " ".join(without_controls(value).split())[:limit]


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
        return "Scanner warnings: none reported."
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


def render_inventory(value: object) -> str:
    """Render every reviewed file without dumping file bodies."""

    data = value if isinstance(value, dict) else {}
    files = data.get("files") if isinstance(data.get("files"), list) else []
    lines = [f"Files ({len(files)})"]
    for number, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            continue
        path = text(item.get("path") or "unnamed")
        mode = " [executable]" if item.get("mode") == "exec" else ""
        facts = []
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            facts.append(f"{size} bytes")
        digest = item.get("hash")
        if isinstance(digest, str) and digest:
            facts.append(text(digest, limit=96))
        suffix = " — " + ", ".join(facts) if facts else ""
        lines.append(f"  {number}. {path}{mode}{suffix}")
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
)

_RECEIPT_COMMANDS = {
    "install": "hermes wisdom install --apply-receipt ",
    "update": "hermes wisdom update --apply-receipt ",
    "publication": "hermes wisdom publication submit ",
    "installed-setup": "hermes wisdom installed-setup apply ",
}


def _review_header_lines(kind: str, data: dict[str, Any]) -> list[str]:
    lines = [_REVIEW_TITLES.get(kind, "Collective Wisdom review"), *_owner_lines(data)]
    for key, label in _REVIEW_FACTS:
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
            ("blocked", "Blocking requirements"),
        ):
            lines.extend(_list_lines(label, compatibility.get(key)))
    requirements = data.get("system_specification")
    if not isinstance(requirements, dict):
        manifest = data.get("manifest") if isinstance(data.get("manifest"), dict) else {}
        requirements = manifest.get("requirements")
    if isinstance(requirements, dict) and requirements:
        lines.append("Requirements: " + _requirements_summary(requirements))
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
        lines.append("Approval status: blocked until the listed review actions are completed.")
    elif data.get("allowed") is True:
        lines.append("Approval status: eligible for exact final confirmation.")
    if kind in {"install", "update"}:
        lines.append(
            "Consequence: writes the reviewed package files; setup commands are not run."
        )
    elif kind == "publication":
        mode = text(data.get("publication_mode") or "server policy")
        consequence = (
            "publishes directly to the organization catalog"
            if mode.casefold() == "direct"
            else "submits to organization moderation before publication"
        )
        lines.append("Consequence: " + consequence + ".")
        if data.get("network_submission") is False:
            lines.append("Nothing has been uploaded yet.")
    receipt = data.get("receipt")
    command = _RECEIPT_COMMANDS.get(kind)
    if isinstance(receipt, str) and receipt and command is not None:
        lines.append("Next: " + command + shlex.quote(without_controls(receipt)))
    return lines


def render_action_review(kind: str, value: object) -> str:
    """Render approval-critical install/update/publication/setup facts."""

    data = value if isinstance(value, dict) else {}
    lines = _review_header_lines(kind, data)
    lines.extend(_compatibility_lines(data))
    lines.append(render_inventory(data))
    lines.extend(_review_check_lines(data))
    lines.extend(_setup_lines(data))
    lines.extend(_decision_lines(kind, data))
    return "\n".join(line for line in lines if line)


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
            parts.append(f"{len(values)} {label} requirement(s)")
    return "; ".join(parts) or "declared (inspect JSON for structured details)"


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
            name = row.get("slug") or row.get("id") or "Unnamed skill"
            version = row.get("latest_version")
            suffix = (
                f" (v{version})"
                if isinstance(version, int) and not isinstance(version, bool) else ""
            )
            description = text(row.get("author_description") or "No description")
            lines.append(f"  - {text(name)}{suffix}: {description}")
        if not rows:
            lines.append("No matching shared skills.")
        return "\n".join(lines)
    if command == "show":
        skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
        latest_detail = data.get("latest_version_detail")
        latest_detail = latest_detail if isinstance(latest_detail, dict) else {}
        latest = (
            latest_detail.get("version")
            if isinstance(latest_detail.get("version"), dict) else {}
        )
        lines = [text(skill.get("slug") or skill.get("id") or "Shared skill")]
        description = (
            latest.get("author_description")
            or skill.get("author_description")
            or "No description"
        )
        lines.append(text(description))
        versions = data.get("versions") if isinstance(data.get("versions"), list) else []
        numbers = [row.get("version") for row in versions if isinstance(row, dict)]
        numbers = [
            item for item in numbers
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        ]
        lines.append(f"Latest: v{max(numbers) if numbers else '?'}")
        requirements = latest.get("system_spec")
        if isinstance(requirements, dict):
            lines.append("Requirements: " + _requirements_summary(requirements))
        return "\n".join(lines)
    if command == "versions":
        rows = data.get("versions") if isinstance(data.get("versions"), list) else []
        lines = ["Published versions", f"Results: {len(rows)}"]
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
        return "\n".join(lines)
    if command == "installed":
        rows = value if isinstance(value, list) else data.get("installations")
        rows = rows if isinstance(rows, list) else []
        lines = ["Managed installations", f"Results: {len(rows)}"]
        for row in rows:
            if isinstance(row, dict):
                lines.append(
                    f"  - {text(row.get('slug') or row.get('skill_id') or 'unknown')}"
                    f" v{text(row.get('version') or '?')}"
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
        ("message", "Message"), ("skill_id", "Skill"), ("version", "Version"),
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
        "kind", "id", "operation_id", "skill_id", "slug", "version", "state",
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
    escaped = without_controls(content)
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
