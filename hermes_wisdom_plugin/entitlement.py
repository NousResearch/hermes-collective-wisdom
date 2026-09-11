"""Credential-free Collective Wisdom entitlement and local-work gates.

The host owns authentication, profile selection, and decoding its local token.
This module receives only the narrow Wisdom claim projection needed for an
availability decision. The projection is advisory: remote services still
authorize every protected request.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_SCOPE = "wisdom:read"
DENIED_MESSAGE = "Collective Wisdom is unavailable for the current account and team"


@dataclass(frozen=True, slots=True)
class EntitlementClaims:
    """Untrusted, credential-free claims projected by the authenticated host."""

    org_id: object = None
    scopes: object = None
    expires_at: object = None
    not_before: object = 0


@dataclass(frozen=True, slots=True)
class Entitlement:
    """Validated local availability hint safe to retain for one operation."""

    org_id: str
    scopes: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class LocalWorkGates:
    """Non-secret local gates supplied by the profile-owned workflow layer."""

    feature_opted_in: bool
    disclosure_acknowledged: bool
    installation_registered: bool
    active_org_id: str | None


EntitlementClaimsReader = Callable[[], EntitlementClaims | None]


class EntitlementRequiredError(PermissionError):
    """Raised when a protected Wisdom operation lacks current entitlement."""


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validate_claims(claims: object, now: object) -> Entitlement | None:
    if not isinstance(claims, EntitlementClaims):
        return None

    current_time = _finite_number(now)
    expires_at = _finite_number(claims.expires_at)
    not_before = _finite_number(claims.not_before)
    if (
        current_time is None
        or expires_at is None
        or expires_at <= current_time
        or not_before is None
        or not_before > current_time
    ):
        return None

    org_id = claims.org_id
    scopes = claims.scopes
    if not isinstance(org_id, str) or not org_id.strip() or not isinstance(scopes, list):
        return None
    if not all(isinstance(scope, str) for scope in scopes):
        return None

    return Entitlement(org_id=org_id, scopes=tuple(scopes), expires_at=expires_at)


def current_entitlement(
    read_claims: EntitlementClaimsReader,
    *,
    now: float | None = None,
) -> Entitlement | None:
    """Read and validate current claims without refreshing or caching credentials."""
    try:
        claims = read_claims()
        return _validate_claims(claims, time.time() if now is None else now)
    except Exception:  # pylint: disable=broad-exception-caught
        # A host-reader failure is an availability denial, not a recovery trigger.
        return None


def is_entitled(
    read_claims: EntitlementClaimsReader,
    org_id: str | None = None,
    *,
    scope: str = DEFAULT_SCOPE,
    now: float | None = None,
) -> bool:
    """Return whether current claims grant the requested organization scope."""
    entitlement = current_entitlement(read_claims, now=now)
    return bool(
        entitlement is not None
        and scope in entitlement.scopes
        and (org_id is None or org_id == entitlement.org_id)
    )


def require_entitlement(
    read_claims: EntitlementClaimsReader,
    org_id: str | None = None,
    *,
    scope: str = DEFAULT_SCOPE,
    now: float | None = None,
) -> None:
    """Reject a protected operation without current matching entitlement."""
    if not is_entitled(read_claims, org_id, scope=scope, now=now):
        raise EntitlementRequiredError(DENIED_MESSAGE)


def local_work_allowed(
    read_claims: EntitlementClaimsReader,
    gates: LocalWorkGates,
    *,
    scope: str = DEFAULT_SCOPE,
    now: float | None = None,
) -> bool:
    """Require distinct local opt-in, disclosure, installation, and entitlement gates."""
    try:
        org_id = gates.active_org_id
        return bool(
            gates.feature_opted_in is True
            and gates.disclosure_acknowledged is True
            and gates.installation_registered is True
            and isinstance(org_id, str)
            and org_id.strip()
            and is_entitled(read_claims, org_id, scope=scope, now=now)
        )
    except (AttributeError, TypeError):
        return False
