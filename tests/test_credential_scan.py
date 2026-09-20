"""Credential detector contracts extracted from the legacy share flow."""

from __future__ import annotations

import json

import pytest

from hermes_wisdom_plugin.credential_scan import scan_credential_bytes, scan_credentials


def scan_example(content: str, opaque: bool) -> list[dict]:
    """Exercise identical examples through both public scan surfaces."""
    if opaque:
        return scan_credential_bytes([{"path": "example", "content_bytes": content.encode()}])
    return scan_credentials([{"path": "example", "content": content}])


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("suffix", [
    "test", "testkey1234", "TESTkey1234", "dummy-key", "dummy1234",
    "example_key", "placeholder-key-1234", "test_key_1234", "test-key_1234",
])
def test_synthetic_key_classification_matches_for_text_and_bytes(opaque, suffix) -> None:
    """Opaque resources use the same bounded placeholder grammar, not a bypass."""
    key = "sk-" + "ant-api03-" + suffix
    assert not scan_example(f'api_key="{key}"', opaque)


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_synthetic_match_cannot_hide_another_credential_on_the_line(opaque, reverse) -> None:
    """Every match is considered, while output remains one finding per kind/line."""
    dummy = "sk-" + "ant-api03-testkey1234"
    credential = "sk-" + "a" * 32
    values = [dummy, credential, credential]
    if reverse:
        values.reverse()
    text = "safe\n" + ", ".join(f'api_key="{value}"' for value in values)
    findings = scan_example(text, opaque)
    assert findings == [
        {"kind": "openai_style_key", "file": "example", "line": 2},
        {"kind": "assignment", "file": "example", "line": 2},
    ]
    assert credential not in json.dumps(findings)


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("label", ["password", "passwd", "secret", "token"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_custom_secrets_are_not_exempted_by_a_dummy_key_value(opaque, label, quote) -> None:
    """User-chosen passwords and generic tokens can equal a dummy API key."""
    key = "sk-" + "ant-api03-testkey1234"
    kind = "openai_style_key" if quote else "assignment"
    assert {"kind": kind, "file": "example", "line": 1} in scan_example(
        f'{quote}{label}{quote}="{key}"', opaque
    )


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("prefix-", ""), ("", "-" + "a" * 32), ("", "9876"), ("", ".extra"),
     ("", "/extra"), ("", "+extra"), ("", "=extra"), ("", "é"), ("", "\\extra")],
)
def test_synthetic_prefix_of_a_larger_value_stays_blocked(opaque, prefix, suffix) -> None:
    """The entire delimited value must match; markers cannot bless attached data."""
    key = prefix + "sk-" + "ant-api03-testkey1234" + suffix
    assert scan_example(f'api_key="{key}"', opaque)


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("prefix", ["sk-proj-", "sk-ant-api04-", "ghp_", "xoxb-"])
def test_other_key_families_do_not_inherit_synthetic_exemptions(opaque, prefix) -> None:
    """A test marker inside any otherwise credential-shaped value still blocks."""
    assert scan_example(prefix + "test" + "a" * 32, opaque)


@pytest.mark.parametrize("suffix", ["test", "testkey1234"])
def test_credential_detector_allows_complete_synthetic_api_keys(suffix: str) -> None:
    """Development docs may use canonical dummy keys without a credential block."""
    key = "sk-" + "ant-api03-" + suffix
    text = f'client = anthropic.Anthropic(api_key="{key}")'

    assert not scan_credentials([{"path": "references/probe.md", "content": text}])


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_api_key_labels_recognize_synthetic_values(opaque, quote) -> None:
    """JSON-style API-key fields follow the same policy as keyword arguments."""
    key = "sk-" + "ant-api03-testkey1234"
    assert not scan_example(f'{quote}api_key{quote}: "{key}"', opaque)


@pytest.mark.parametrize("prefix,suffix", [(b"\xff", b""), (b"", b"\x80")])
def test_invalid_bytes_cannot_be_dropped_to_form_a_dummy_value(prefix, suffix) -> None:
    """Opaque matching is byte-preserving, not decode-with-ignore sanitization."""
    key = b"sk-" + b"ant-api03-testkey1234"
    findings = scan_credential_bytes([
        {"path": "opaque", "content_bytes": prefix + key + suffix},
    ])
    assert findings == [{"kind": "openai_style_key", "file": "opaque", "line": 1}]


def test_opaque_invalid_bytes_do_not_hide_other_lines() -> None:
    """ASCII fixtures remain recognized inside resources with undecodable bytes."""
    dummy = b"sk-" + b"ant-api03-testkey1234"
    real = b"sk-" + b"a" * 32
    body = b"\xff\x80\r\n" + dummy + b"\r\n" + real
    assert scan_credential_bytes([{"path": "opaque", "content_bytes": body}]) == [
        {"kind": "openai_style_key", "file": "opaque", "line": 3},
    ]


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
