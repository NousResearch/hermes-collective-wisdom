"""Refresh-free adapter for the current Hermes Nous account."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path

from .client import WisdomGatewayCredentials
from .entitlement import DEFAULT_SCOPE, EntitlementClaims
from .errors import WisdomAuthError

_UNAVAILABLE = "Collective Wisdom credentials are unavailable"


@dataclass(frozen=True, slots=True)
class _HostAccountSnapshot:  # pylint: disable=too-many-instance-attributes
    """Validated host state; the bearer is deliberately absent from its repr."""

    profile_name: str
    profile_home: str
    account_id: str
    organization_id: str
    scopes: tuple[str, ...]
    expires_at: float
    not_before: float
    base_url: str
    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AccountBinding:
    """Non-secret ownership and destination retained for the adapter lifetime."""

    profile_name: str
    profile_home: str
    account_id: str
    organization_id: str
    base_url: str


SnapshotReader = Callable[[], _HostAccountSnapshot]


class HermesAccountAdapter:
    """Bind one Hermes profile/account and recheck it before every use."""

    def __init__(
        self,
        snapshot: _HostAccountSnapshot,
        read_snapshot: SnapshotReader,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(snapshot, _HostAccountSnapshot)
            or not callable(read_snapshot)
            or not callable(clock)
        ):
            raise TypeError("a validated host account snapshot and reader are required")
        self._binding = _AccountBinding(
            profile_name=snapshot.profile_name,
            profile_home=snapshot.profile_home,
            account_id=snapshot.account_id,
            organization_id=snapshot.organization_id,
            base_url=snapshot.base_url,
        )
        self._read_snapshot = read_snapshot
        self._clock = clock

    @classmethod
    def current(
        cls,
        *,
        _read_snapshot: SnapshotReader | None = None,
        _clock: Callable[[], float] = time.time,
    ) -> HermesAccountAdapter:
        """Bind the current valid Hermes account without refreshing credentials."""

        reader = _read_snapshot or _read_current_host_account
        try:
            snapshot = reader()
            _require_current_snapshot(snapshot, _clock())
            return cls(snapshot, reader, clock=_clock)
        except Exception:  # pylint: disable=broad-exception-caught
            raise WisdomAuthError(_UNAVAILABLE) from None

    @property
    def profile_name(self) -> str:
        """Return the bound non-secret profile name."""

        return self._binding.profile_name

    @property
    def profile_home(self) -> str:
        """Return the bound profile home."""

        return self._binding.profile_home

    @property
    def account_id(self) -> str:
        """Return the bound JWT subject."""

        return self._binding.account_id

    @property
    def organization_id(self) -> str:
        """Return the bound organization identifier."""

        return self._binding.organization_id

    def _current_bound_snapshot(self) -> _HostAccountSnapshot:
        snapshot = self._read_snapshot()
        _require_current_snapshot(snapshot, self._clock())
        expected = self._binding
        if (
            snapshot.profile_name != expected.profile_name
            or snapshot.profile_home != expected.profile_home
            or snapshot.account_id != expected.account_id
            or snapshot.organization_id != expected.organization_id
            or snapshot.base_url != expected.base_url
        ):
            raise WisdomAuthError(_UNAVAILABLE)
        return snapshot

    def entitlement_claims(self) -> EntitlementClaims | None:
        """Return current credential-free claims, or deny on any host failure."""

        try:
            snapshot = self._current_bound_snapshot()
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        return EntitlementClaims(
            org_id=snapshot.organization_id,
            scopes=list(snapshot.scopes),
            expires_at=snapshot.expires_at,
            not_before=snapshot.not_before,
        )

    def credentials(self) -> WisdomGatewayCredentials:
        """Return one current bearer snapshot for a Gateway request."""

        try:
            snapshot = self._current_bound_snapshot()
            if DEFAULT_SCOPE not in snapshot.scopes:
                raise WisdomAuthError(_UNAVAILABLE)
            return WisdomGatewayCredentials(
                base_url=snapshot.base_url,
                access_token=snapshot.access_token,
                organization_id=snapshot.organization_id,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            raise WisdomAuthError(_UNAVAILABLE) from None


def _read_current_host_account() -> _HostAccountSnapshot:
    """Read current Hermes state through the pinned, refresh-free host APIs."""

    get_provider_auth_state = import_module("hermes_cli.auth").get_provider_auth_state
    decode_jwt_claims = getattr(
        import_module("hermes_cli.auth_constants"), "_decode_jwt_claims"
    )
    get_active_profile_name = import_module("hermes_cli.profiles").get_active_profile_name
    get_hermes_home = import_module("hermes_constants").get_hermes_home
    is_guest_state = import_module("hermes_cli.anon_auth").is_guest_state
    resolve_sync_base_url = import_module(
        "tools.skills_sync_client"
    ).resolve_sync_base_url

    profile_name = get_active_profile_name()
    profile_home = _resolved_home(get_hermes_home())
    state = get_provider_auth_state("nous")
    if not isinstance(state, Mapping):
        raise RuntimeError("Nous credentials are missing")
    token = state.get("access_token")
    claims = decode_jwt_claims(token)
    base_url = resolve_sync_base_url()

    if profile_name != get_active_profile_name() or profile_home != _resolved_home(
        get_hermes_home()
    ):
        raise RuntimeError("Hermes profile changed during credential read")
    return _validated_snapshot(
        profile_name,
        profile_home,
        state,
        token,
        claims,
        base_url,
        is_guest=is_guest_state(state),
    )


def _resolved_home(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise RuntimeError("Hermes profile home is unavailable")
    return str(Path(value).expanduser().resolve(strict=False))


def _validated_snapshot(  # pylint: disable=too-many-arguments,too-many-locals,too-many-positional-arguments
    profile_name: object,
    profile_home: object,
    state: Mapping[object, object],
    token: object,
    claims: object,
    base_url: object,
    *,
    is_guest: bool = False,
    now: float | None = None,
) -> _HostAccountSnapshot:
    current_time = time.time() if now is None else now
    if not _nonblank(profile_name) or not _nonblank(profile_home):
        raise RuntimeError("Hermes profile binding is unavailable")
    if is_guest is True:
        raise RuntimeError("guest credentials are unavailable")
    if not _nonblank(token) or not isinstance(claims, Mapping):
        raise RuntimeError("Nous credentials are malformed")

    account_id = claims.get("sub")
    organization_id = claims.get("org_id")
    expires_at = _finite_number(claims.get("exp"))
    not_before = _finite_number(claims.get("nbf", 0))
    scopes = claims.get("wisdom_scopes")
    identity_valid = _nonblank(account_id) and _nonblank(organization_id)
    timing_valid = bool(
        expires_at is not None
        and not_before is not None
        and expires_at > current_time >= not_before
    )
    scopes_valid = isinstance(scopes, list) and all(
        isinstance(scope, str) for scope in scopes
    )
    if not identity_valid or not timing_valid or not scopes_valid:
        raise RuntimeError("Nous credentials are invalid")

    stored_account = state.get("user_id")
    stored_org = state.get("org_id")
    if (_nonblank(stored_account) and stored_account != account_id) or (
        _nonblank(stored_org) and stored_org != organization_id
    ):
        raise RuntimeError("Nous account binding is inconsistent")
    if not _nonblank(base_url):
        raise RuntimeError("Nous Gateway route is unavailable")

    return _HostAccountSnapshot(
        profile_name=profile_name,
        profile_home=profile_home,
        account_id=account_id,
        organization_id=organization_id,
        scopes=tuple(scopes),
        expires_at=expires_at,
        not_before=not_before,
        base_url=base_url,
        access_token=token,
    )


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_current_snapshot(snapshot: object, now: object) -> None:
    if not isinstance(snapshot, _HostAccountSnapshot):
        raise RuntimeError("Hermes account snapshot is invalid")
    current_time = _finite_number(now)
    if (
        current_time is None
        or snapshot.expires_at <= current_time
        or snapshot.not_before > current_time
        or not all(isinstance(scope, str) for scope in snapshot.scopes)
    ):
        raise RuntimeError("Hermes account snapshot is not current")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
