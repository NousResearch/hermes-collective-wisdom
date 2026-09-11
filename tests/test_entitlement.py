"""Collective Wisdom entitlement boundary contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_wisdom_plugin.entitlement import (
    EntitlementClaims,
    EntitlementRequiredError,
    LocalWorkGates,
    current_entitlement,
    is_entitled,
    local_work_allowed,
    require_entitlement,
)

NOW = 2_000.0


class ClaimsReader:
    """Mutable host-reader stand-in used to prove every check observes fresh state."""

    def __init__(self, claims: object) -> None:
        self.claims = claims
        self.read_count = 0
        self.refresh_count = 0
        self.host_admin = False

    def __call__(self) -> EntitlementClaims | None:
        self.read_count += 1
        if self.claims is None or isinstance(self.claims, EntitlementClaims):
            return self.claims
        return self.claims  # type: ignore[return-value]

    def refresh(self) -> None:
        """Represent a forbidden host operation that this boundary cannot request."""
        self.refresh_count += 1
        raise AssertionError("entitlement reads must not refresh credentials")


def valid_claims(**overrides: object) -> EntitlementClaims:
    """Build a credential-free claim projection with optional malformed values."""
    values: dict[str, object] = {
        "org_id": "team-example",
        "scopes": ["wisdom:read", "wisdom:publish"],
        "expires_at": NOW + 600,
        "not_before": 0,
    }
    values.update(overrides)
    return EntitlementClaims(**values)


def test_ordinary_member_is_entitled_without_global_admin() -> None:
    """Ordinary Wisdom scopes work without a global administrator claim."""
    reader = ClaimsReader(valid_claims())

    assert is_entitled(reader, now=NOW)
    assert is_entitled(reader, "team-example", now=NOW)
    assert not is_entitled(reader, "other-team", now=NOW)
    assert not is_entitled(reader, scope="wisdom:admin", now=NOW)

    entitlement = current_entitlement(reader, now=NOW)
    assert entitlement is not None
    assert entitlement.org_id == "team-example"
    assert entitlement.scopes == ("wisdom:read", "wisdom:publish")
    assert not hasattr(entitlement, "access_token")


@pytest.mark.parametrize(
    "claims",
    [
        None,
        object(),
        valid_claims(expires_at=0),
        valid_claims(expires_at=None),
        valid_claims(expires_at=True),
        valid_claims(expires_at="9999999999"),
        valid_claims(expires_at=float("inf")),
        valid_claims(expires_at=float("nan")),
        valid_claims(not_before=NOW + 99_999),
        valid_claims(not_before=True),
        valid_claims(org_id=None),
        valid_claims(org_id=""),
        valid_claims(org_id="   "),
        valid_claims(scopes=None),
        valid_claims(scopes=[]),
        valid_claims(scopes="wisdom:read"),
        valid_claims(scopes=[1, "wisdom:read"]),
        valid_claims(scopes=["wisdom:admin"]),
    ],
    ids=[
        "missing",
        "wrong-boundary-type",
        "expired",
        "missing-expiry",
        "boolean-expiry",
        "string-expiry",
        "infinite-expiry",
        "nan-expiry",
        "not-yet-valid",
        "boolean-not-before",
        "missing-org",
        "empty-org",
        "blank-org",
        "missing-scopes",
        "empty-scopes",
        "string-scopes",
        "non-string-scope",
        "scope-mismatch",
    ],
)
def test_missing_malformed_expired_or_scope_mismatch_fails_closed(claims: object) -> None:
    """Every absent, invalid, stale, or mismatched input denies access."""
    reader = ClaimsReader(claims)

    assert not is_entitled(reader, now=NOW)
    with pytest.raises(EntitlementRequiredError, match="unavailable"):
        require_entitlement(reader, now=NOW)


def test_host_admin_or_nous_org_does_not_bypass_scope() -> None:
    """Neither a privileged host flag nor an organization name grants access."""
    reader = ClaimsReader(valid_claims(org_id="nous", scopes=["wisdom:admin"]))
    reader.host_admin = True

    assert not is_entitled(reader, now=NOW)
    assert not is_entitled(reader, "nous", now=NOW)


def test_claim_replacement_and_reader_failure_have_no_positive_cache() -> None:
    """Each decision rereads claims and reader failures deny availability."""
    reader = ClaimsReader(valid_claims())
    assert is_entitled(reader, now=NOW)

    reader.claims = None
    assert not is_entitled(reader, now=NOW)
    reader.claims = valid_claims(org_id="second-team")
    assert is_entitled(reader, "second-team", now=NOW)
    assert not is_entitled(reader, "team-example", now=NOW)
    assert reader.read_count == 4

    def failing_reader() -> EntitlementClaims:
        raise RuntimeError("host auth snapshot unavailable")

    assert not is_entitled(failing_reader, now=NOW)


def test_entitlement_read_has_no_refresh_path() -> None:
    """The entitlement boundary uses only the reader's local snapshot method."""
    reader = ClaimsReader(valid_claims())

    assert is_entitled(reader, now=NOW)
    assert reader.read_count == 1
    assert reader.refresh_count == 0


def test_expiry_and_not_before_are_rechecked_at_each_operation() -> None:
    """Expiry and not-before bounds are evaluated on every operation."""
    reader = ClaimsReader(valid_claims(expires_at=NOW))

    assert is_entitled(reader, now=NOW - 1)
    assert not is_entitled(reader, now=NOW)
    reader.claims = valid_claims(not_before=NOW + 1)
    assert not is_entitled(reader, now=NOW)
    assert is_entitled(reader, now=NOW + 1)


def test_local_work_requires_each_distinct_gate_and_matching_org() -> None:
    """Local work needs opt-in, disclosure, installation, and matching scope."""
    reader = ClaimsReader(valid_claims())
    gates = LocalWorkGates(
        feature_opted_in=True,
        disclosure_acknowledged=True,
        installation_registered=True,
        active_org_id="team-example",
    )

    assert local_work_allowed(reader, gates, now=NOW)
    assert not local_work_allowed(reader, replace(gates, feature_opted_in=False), now=NOW)
    assert not local_work_allowed(
        reader, replace(gates, disclosure_acknowledged=False), now=NOW
    )
    assert not local_work_allowed(reader, replace(gates, installation_registered=False), now=NOW)
    assert not local_work_allowed(reader, replace(gates, active_org_id=None), now=NOW)
    assert not local_work_allowed(reader, replace(gates, active_org_id="other-team"), now=NOW)

    reader.claims = valid_claims(scopes=[])
    assert not local_work_allowed(reader, gates, now=NOW)


@pytest.mark.parametrize(
    "gates",
    [
        LocalWorkGates("yes", True, True, "team-example"),  # type: ignore[arg-type]
        LocalWorkGates(True, "accepted", True, "team-example"),  # type: ignore[arg-type]
        LocalWorkGates(True, True, "yes", "team-example"),  # type: ignore[arg-type]
    ],
)
def test_local_gate_values_must_be_explicit_booleans(gates: LocalWorkGates) -> None:
    """Truthy strings cannot impersonate explicit local gate decisions."""
    assert not local_work_allowed(ClaimsReader(valid_claims()), gates, now=NOW)
