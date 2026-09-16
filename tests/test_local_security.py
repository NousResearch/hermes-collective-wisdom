"""Pure local preflight aggregation contracts."""

from __future__ import annotations

import json

import pytest

from hermes_wisdom_plugin.local_security import prepared_security_check


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


@pytest.mark.parametrize(("allowed", "status"), [(False, "blocked"), (None, "unavailable")])
def test_local_guard_failure_is_not_a_pass(allowed: bool | None, status: str) -> None:
    """A blocked or unknown host guard result cannot authorize upload."""
    result = prepared_security_check([], "Safe", {"guard": {"allowed": allowed}})

    assert result["upload_allowed"] is False
    assert result["status"] == status
    assert result["local_status"] == status


def test_missing_host_guard_is_unavailable_not_pass() -> None:
    """Omitting the host-owned guard result fails closed."""
    result = prepared_security_check([], "Safe", {})

    assert result["status"] == "unavailable"
    assert result["local_status"] == "unavailable"
    assert result["upload_allowed"] is False


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
