"""Credential-shaped content detection for local Wisdom preflight."""

from __future__ import annotations

import re
from typing import Any

CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9_\-/+]{16,}"
        ),
    ),
)
BYTE_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple(
    (
        kind,
        re.compile(pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE),
    )
    for kind, pattern in CREDENTIAL_PATTERNS
)


# Only a finite family of public example values, never arbitrary "test" substrings.
# Keep broad detection separate: presentation redaction remains conservative.
_SYNTHETIC_KEY = (
    r"sk-(?:ant-api03-)?(?:test|dummy|example|placeholder)"
    r"(?:[-_]?key)?(?:[-_]?1234)?"
)
_SYNTHETIC_PATTERNS = {
    "openai_style_key": re.compile(_SYNTHETIC_KEY, re.IGNORECASE | re.ASCII),
    "assignment": re.compile(
        r"api[_-]?key\s*[:=]\s*['\"]?" + _SYNTHETIC_KEY,
        re.IGNORECASE | re.ASCII,
    ),
}

_CUSTOM_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:secret|token|password|passwd)\b['\"]?\s*[:=]\s*['\"]?\Z",
    re.IGNORECASE | re.ASCII,
)


def _synthetic_match(kind: str, match: re.Match[str] | re.Match[bytes]) -> bool:
    """Recognize complete dummy API keys, not prefixes of ambiguous credentials."""
    pattern = _SYNTHETIC_PATTERNS.get(kind)
    if pattern is None:
        return False
    value = match.group()
    before = match.string[max(0, match.start() - 1):match.start()]
    after = match.string[match.end():match.end() + 1]
    # A public dummy API-key value can still be a real user-chosen password.
    prefix = match.string[:match.start()]
    value, before, after, prefix = (
        part.decode("latin-1") if isinstance(part, bytes) else part
        for part in (value, before, after, prefix)
    )
    return (
        pattern.fullmatch(value) is not None
        and before in " \t\r\n\"'`([{=:,;"
        and after in " \t\r\n\"'`)]},;"
        and not _CUSTOM_SECRET_ASSIGNMENT.search(prefix)
    )


def scan_credentials(files: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Return credential-shaped findings; matches are never echoed back."""
    findings: list[dict[str, Any]] = []
    for item in files:
        for number, line in enumerate(item["content"].splitlines(), start=1):
            for kind, pattern in CREDENTIAL_PATTERNS:
                if any(not _synthetic_match(kind, match) for match in pattern.finditer(line)):
                    findings.append({
                        "kind": kind,
                        "file": item["path"],
                        "line": number,
                    })
    return findings


def scan_credential_bytes(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan opaque bytes for the ASCII credential forms the text scanner knows."""

    findings: list[dict[str, Any]] = []
    for item in files:
        body = item.get("content_bytes")
        path = item.get("path")
        if not isinstance(body, bytes) or not isinstance(path, str):
            continue
        for number, line in enumerate(body.splitlines(), start=1):
            for kind, pattern in BYTE_CREDENTIAL_PATTERNS:
                if any(not _synthetic_match(kind, match) for match in pattern.finditer(line)):
                    findings.append({"kind": kind, "file": path, "line": number})
    return findings
