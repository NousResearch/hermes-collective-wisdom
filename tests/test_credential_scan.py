"""Credential detector contracts extracted from the legacy share flow."""

from __future__ import annotations

import json

import pytest

from hermes_wisdom_plugin.credential_scan import scan_credentials


def test_credential_detector_allows_long_skill_names() -> None:
    """Long skill identifiers that begin with letters `sk` are not API keys."""
    text = "Use skill-qualification-test and skill-publishing-flow-test."

    assert not scan_credentials([{"path": "SKILL.md", "content": text}])


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("sk-" + "a" * 32, "openai_style_key"),
        ("ghp_" + "a" * 32, "github_token"),
        ("gho_" + "a" * 32, "github_token"),
        ("xoxb-" + "a" * 32, "slack_token"),
        ("AKIA" + "A" * 16, "aws_access_key"),
        ("AIza" + "a" * 32, "google_api_key"),
    ],
)
def test_credential_detector_finds_key_shapes_without_echoing_values(
    content: str, kind: str
) -> None:
    """Known key forms produce location-only findings without secret values."""
    findings = scan_credentials(
        [{"path": "SKILL.md", "content": f"safe first line\n{content}\nsafe last line"}]
    )

    assert findings == [{"kind": kind, "file": "SKILL.md", "line": 2}]
    assert content not in json.dumps(findings)


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("-----BEGIN " + "PRIVATE KEY" + "-----", "private_key"),
        ("password=" + "a" * 24, "assignment"),
    ],
)
def test_credential_detector_preserves_non_token_patterns(content: str, kind: str) -> None:
    """Private-key headers and secret assignments retain their source classifications."""
    assert scan_credentials([{"path": "SKILL.md", "content": content}]) == [
        {"kind": kind, "file": "SKILL.md", "line": 1}
    ]
