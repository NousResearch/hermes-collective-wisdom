"""Local preflight facts, distinct from the Gateway's publication scan."""

import base64
import re
from typing import Any

from .credential_scan import CREDENTIAL_PATTERNS, scan_credential_bytes, scan_credentials


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
def model_disclosure_ready(
    files: list[tuple[str, str, bytes]], *editorial_fields: str,
) -> bool:
    """Credential preflight only; not a personal-information certification."""
    bodies = [body for _path, _mode, body in files]
    bodies.extend(field.encode("utf-8") for field in editorial_fields)

    text = []
    opaque = []
    for index, body in enumerate(bodies):
        try:
            text.append({"path": str(index), "content": body.decode("utf-8")})
        except UnicodeDecodeError:
            opaque.append({"path": str(index), "content_bytes": body})
    return not scan_credentials(text) and not scan_credential_bytes(opaque)


def _safe_text(value: object, *, limit: int) -> str:
    text = _CONTROL_RE.sub(" ", value if isinstance(value, str) else "")
    for _kind, pattern in CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return " ".join(text.split())[:limit]


def safe_scan_findings(scan: object) -> list[dict[str, Any]]:
    """Return bounded scanner facts without raw matches or credential values."""

    value = (
        scan.get("guard")
        if isinstance(scan, dict) and isinstance(scan.get("guard"), dict)
        else scan
    )
    findings = value.get("findings") if isinstance(value, dict) else None
    if not isinstance(findings, list):
        return []
    result: list[dict[str, Any]] = []
    for finding in findings[:200]:
        if not isinstance(finding, dict):
            continue
        line = finding.get("line")
        result.append({
            "severity": _safe_text(finding.get("severity"), limit=32),
            "category": _safe_text(
                finding.get("category") or finding.get("kind"), limit=64
            ),
            "pattern_id": _safe_text(
                finding.get("pattern_id") or finding.get("rule_id"), limit=128
            ),
            "file": _safe_text(
                finding.get("file") or finding.get("path"), limit=1024
            ),
            "line": (
                line
                if isinstance(line, int) and not isinstance(line, bool) and line > 0
                else None
            ),
            "description": _safe_text(
                finding.get("description") or finding.get("message"), limit=500
            ),
        })
    return result


def prepared_security_check(  # pylint: disable=too-many-locals
    files: list[dict[str, Any]], description: str, scan: dict[str, Any],
    *, include_gateway_pending: bool = True,
) -> dict[str, Any]:
    """Combine credential findings and a caller-supplied host guard result."""
    text_files = [
        {"path": item["path"], "content": item["content_utf8"]}
        for item in files if isinstance(item.get("content_utf8"), str)
    ]
    opaque_files = []
    for item in files:
        if "content_utf8" in item or not isinstance(item.get("content_base64"), str):
            continue
        try:
            body = base64.b64decode(item["content_base64"], validate=True)
        except (TypeError, ValueError):
            body = b""
        opaque_files.append({"path": item.get("path"), "content_bytes": body})
    findings = scan_credentials(
        text_files + [{"path": "Owner description", "content": description}]
    ) + scan_credential_bytes(opaque_files)
    rows = []
    for key, label, kinds in (
        ("private_keys", "Private keys", {"private_key"}),
        ("live_credentials", "Live credentials", {
            "aws_access_key", "github_token", "slack_token",
            "openai_style_key", "google_api_key",
        }),
        ("secret_assignments", "Secret-like assignments", {"assignment"}),
    ):
        count = sum(item["kind"] in kinds for item in findings)
        rows.append({
            "key": key, "label": label,
            "status": "blocked" if count else "pass",
            "finding_count": count,
            "details": ["Credential-shaped content detected; remove it before sharing."]
            if count else ["No issues detected by this local check."],
        })
    guard = scan.get("guard") or {}
    allowed = guard.get("allowed")
    guard_available = bool(guard) and guard.get(
        "available", allowed is not None
    ) is True
    guard_status = (
        "unavailable"
        if not guard_available or allowed not in {True, False, None}
        else "warning_required"
        if allowed is not True
        else "advisory"
        if guard.get("findings")
        else "pass"
    )
    rows.append({
        "key": "skills_guard", "label": "Harmful instruction patterns",
        "status": guard_status,
        "finding_count": len(guard.get("findings") or []),
        # Do not surface raw matches, filenames, or scanner reasons containing secrets.
        "details": ["The local skills guard checks known harmful instruction patterns."],
        "findings": safe_scan_findings({"guard": guard}),
    })
    for key, label in ((
        ("organization_policy", "Organization policy"),
        ("personal_information", "Personal information"),
    ) if include_gateway_pending else ()):
        rows.append({
            "key": key, "label": label, "status": "pending",
            "finding_count": 0,
            "details": [
                "Required Gateway check after you authorize upload; before publication."
            ],
        })
    blocked = any(row["status"] == "blocked" for row in rows)
    advisory = any(row["status"] == "advisory" for row in rows)
    warning_required = guard_status == "warning_required"
    unavailable = guard_status == "unavailable"
    local_status = (
        "blocked" if blocked else "unavailable" if unavailable
        else "warning_required" if warning_required else "advisory" if advisory else "pass"
    )
    return {
        "schema_version": 1,
        "source": "local_preflight",
        "status": (
            "pending"
            if include_gateway_pending and not blocked and not unavailable
            else local_status
        ),
        "local_status": local_status,
        "upload_allowed": not blocked and not unavailable and not warning_required,
        "warning_acceptance_required": warning_required,
        "opaque_file_count": sum("content_utf8" not in item for item in files),
        "opaque_scan_coverage": "ascii_credential_and_private_key_patterns",
        "summary": (
            "Local security checks found content that must be removed before sharing."
            if blocked else "Local security scanning could not complete. Try preparation again."
            if unavailable else "Local security checks have findings to review."
            if advisory or warning_required else "No issues detected by local security checks."
        ) + (" Required Gateway checks run after you authorize upload and before publication."
             if include_gateway_pending else ""),
        "checks": rows,
    }
