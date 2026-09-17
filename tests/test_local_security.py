"""Pure local preflight aggregation contracts."""

import base64
import json

import pytest

from hermes_wisdom_plugin.local_security import prepared_security_check, safe_scan_findings


@pytest.mark.parametrize("in_description", [False, True])
@pytest.mark.parametrize(
    ("content", "key"),
    [
        ("-----BEGIN " + "PRIVATE KEY" + "-----", "private_keys"),
        ("ghp_" + "a" * 36, "live_credentials"),
        ("password=" + "a" * 24, "secret_assignments"),
    ],
)
def test_local_scan_blocks_credentials_without_echoing_them(
    content: str, key: str, in_description: bool
) -> None:
    """Credentials in files or descriptions block upload without value disclosure."""
    result = prepared_security_check(
        [{"path": "SKILL.md", "content_utf8": "Safe" if in_description else content}],
        content if in_description else "Safe",
        {"guard": {"allowed": True, "findings": []}},
    )

    assert result["status"] == "blocked" and result["upload_allowed"] is False
    row = next(row for row in result["checks"] if row["key"] == key)
    assert row["status"] == "blocked" and row["finding_count"] == 1
    assert content not in json.dumps(result)


@pytest.mark.parametrize(
    ("allowed", "status", "local_status"),
    [(False, "pending", "warning_required"), (None, "pending", "warning_required")],
)
def test_local_guard_failure_is_not_a_pass(
    allowed: bool | None, status: str, local_status: str
) -> None:
    """A completed ask/deny scan needs consent; missing scan evidence is unavailable."""
    result = prepared_security_check(
        [], "Safe", {"guard": {"available": True, "allowed": allowed}}
    )

    assert result["upload_allowed"] is False
    assert result["status"] == status
    assert result["local_status"] == local_status


def test_missing_host_guard_is_unavailable_not_pass() -> None:
    """Omitting the host-owned guard result fails closed."""
    result = prepared_security_check([], "Safe", {})

    assert result["status"] == "unavailable"
    assert result["local_status"] == "unavailable"
    assert result["upload_allowed"] is False


def test_explicitly_unavailable_host_guard_is_not_acknowledgeable() -> None:
    """An absent scanner result cannot be converted into a warning approval."""
    result = prepared_security_check(
        [], "Safe", {"guard": {"available": False, "allowed": None}}
    )
    assert result["local_status"] == "unavailable"
    assert result["warning_acceptance_required"] is False


def test_local_checklist_keeps_gateway_checks_pending() -> None:
    """A clean local result does not claim deferred Gateway checks have run."""
    result = prepared_security_check([], "Safe", {"guard": {"allowed": True}})
    rows = {row["key"]: row for row in result["checks"]}

    assert result["local_status"] == "pass"
    assert result["status"] == "pending"
    assert result["upload_allowed"] is True
    assert rows["private_keys"]["status"] == "pass"
    assert rows["organization_policy"]["status"] == "pending"
    assert rows["personal_information"]["status"] == "pending"
    assert rows["organization_policy"]["details"] == [
        "Required Gateway check after you authorize upload; before publication."
    ]
    assert "No issues detected by local security checks." in result["summary"]
    assert "Required Gateway checks run after" in result["summary"]


def test_advisory_host_findings_do_not_become_a_pass() -> None:
    """Known non-blocking host findings remain advisory in local status."""
    result = prepared_security_check(
        [], "Safe", {"guard": {"allowed": True, "findings": [{"kind": "review"}]}}
    )

    assert result["local_status"] == "advisory"
    assert result["status"] == "pending"
    assert result["upload_allowed"] is True
    guard = next(row for row in result["checks"] if row["key"] == "skills_guard")
    assert guard["status"] == "advisory" and guard["finding_count"] == 1


@pytest.mark.parametrize("path", ["assets/image.png", "references/guide.pdf", "templates/a.docx"])
def test_opaque_resources_receive_byte_credential_coverage(path: str) -> None:
    """Known opaque resources get bounded byte-pattern credential coverage."""
    benign = b"\x89opaque\xffresource"
    clean = prepared_security_check(
        [{"path": path, "content_base64": base64.b64encode(benign).decode()}],
        "Safe", {"guard": {"allowed": True, "findings": []}},
    )
    assert clean["local_status"] == "pass"
    assert clean["opaque_scan_coverage"] == "ascii_credential_and_private_key_patterns"

    secret = b"\xffprefix\npassword=" + b"a" * 24
    blocked = prepared_security_check(
        [{"path": path, "content_base64": base64.b64encode(secret).decode()}],
        "Safe", {"guard": {"allowed": True, "findings": []}},
    )
    assert blocked["local_status"] == "blocked"
    assert b"a" * 24 not in json.dumps(blocked).encode()


def test_safe_scan_findings_redacts_values_controls_and_raw_matches() -> None:
    """The presentation projection omits raw matches and credential values."""
    token = "ghp_" + "a" * 36
    projected = safe_scan_findings({"guard": {"allowed": False, "findings": [{
        "severity": "high\x1b[31m", "category": "credential",
        "pattern_id": "rule-1", "file": f"refs/{token}.txt",
        "line": 7, "description": f"found {token}", "match": token,
    }]}})
    assert projected == [{
        "severity": "high [31m", "category": "credential", "pattern_id": "rule-1",
        "file": "refs/[REDACTED].txt", "line": 7,
        "description": "found [REDACTED]",
    }]
    assert token not in json.dumps(projected)
