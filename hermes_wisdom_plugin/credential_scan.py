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


def scan_credentials(files: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Return credential-shaped findings; matches are never echoed back."""
    findings: list[dict[str, Any]] = []
    for item in files:
        for number, line in enumerate(item["content"].splitlines(), start=1):
            for kind, pattern in CREDENTIAL_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "kind": kind,
                        "file": item["path"],
                        "line": number,
                    })
    return findings
