"""Tests for the refresh-free Hermes account adapter."""

from __future__ import annotations

import sys
import secrets
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.account import (
    HermesAccountAdapter,
    _HostAccountSnapshot,
    _read_current_host_account,
    _validated_snapshot,
)
from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.errors import WisdomAuthError

NOW = 2_000.0


def snapshot(**overrides: object) -> _HostAccountSnapshot:
    """Build a valid host snapshot."""

    values = {
        "profile_name": "work",
        "profile_home": "/profiles/work",
        "account_id": "user-1",
        "organization_id": "org-1",
        "scopes": ("wisdom:read", "wisdom:publish"),
        "expires_at": NOW + 600,
        "not_before": 0.0,
        "base_url": "https://gateway.example",
        "portal_base_url": "https://portal.example",
        "access_token": "secret-token",
    }
    values.update(overrides)
    return _HostAccountSnapshot(**values)


class Reader:  # pylint: disable=too-few-public-methods
    """Mutable refresh-free host snapshot reader."""

    def __init__(self, value: _HostAccountSnapshot) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> _HostAccountSnapshot:
        self.calls += 1
        return self.value


def test_current_binds_profile_account_and_supplies_client_contract() -> None:
    """One valid snapshot binds identity and later reads supply current data."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)

    assert adapter.profile_name == "work"
    assert adapter.profile_home == "/profiles/work"
    assert adapter.account_id == "user-1"
    assert adapter.organization_id == "org-1"
    assert adapter.base_url == "https://gateway.example"
    assert adapter.portal_base_url == "https://portal.example"
    assert "secret-token" not in repr(adapter.__dict__["_binding"])
    assert not hasattr(adapter.__dict__["_binding"], "access_token")
    assert adapter.entitlement_claims().scopes == ["wisdom:read", "wisdom:publish"]
    credentials = adapter.credentials()
    assert credentials.base_url == "https://gateway.example"
    assert credentials.organization_id == "org-1"
    assert credentials.access_token == "secret-token"
    assert reader.calls == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_name", "other"),
        ("profile_home", "/profiles/other"),
        ("account_id", "user-2"),
        ("organization_id", "org-2"),
        ("base_url", "https://other-gateway.example"),
        ("portal_base_url", "https://other-portal.example"),
    ],
)
def test_profile_org_and_account_switches_fail_closed(field: str, value: str) -> None:
    """Pinned ownership rejects every profile, organization, or actor change."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)
    reader.value = replace(reader.value, **{field: value})

    assert adapter.entitlement_claims() is None
    with pytest.raises(WisdomAuthError, match="credentials are unavailable"):
        adapter.credentials()


def test_same_organization_actor_switch_is_denied() -> None:
    """Changing JWT subject is denied even when the organization is unchanged."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)
    reader.value = replace(reader.value, account_id="user-2", organization_id="org-1")

    assert adapter.entitlement_claims() is None
    with pytest.raises(WisdomAuthError):
        adapter.credentials()


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": NOW},
        {"not_before": NOW + 1},
    ],
)
def test_expiry_and_not_before_are_rechecked_without_refresh(changes: dict[str, float]) -> None:
    """Each adapter call rejects a newly stale or not-yet-valid snapshot."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)
    reader.value = replace(reader.value, **changes)

    assert adapter.entitlement_claims() is None
    with pytest.raises(WisdomAuthError):
        adapter.credentials()
    assert reader.calls == 3


def test_current_scope_removal_is_projected_for_entitlement_denial() -> None:
    """A current token with removed Wisdom scopes cannot retain old authority."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)
    reader.value = replace(reader.value, scopes=())

    claims = adapter.entitlement_claims()
    assert claims is not None
    assert claims.scopes == []
    with pytest.raises(WisdomAuthError):
        adapter.credentials()


def test_credentials_require_read_and_requested_scope() -> None:
    """Every credential request requires both baseline read and its requested capability."""

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)

    assert adapter.credentials(scope="wisdom:publish").access_token == "secret-token"
    reader.value = replace(reader.value, scopes=("wisdom:read",))
    with pytest.raises(WisdomAuthError):
        adapter.credentials(scope="wisdom:publish")
    reader.value = replace(reader.value, scopes=("wisdom:publish",))
    with pytest.raises(WisdomAuthError):
        adapter.credentials(scope="wisdom:publish")
    with pytest.raises(WisdomAuthError):
        adapter.credentials(scope="")


def test_scope_removal_blocks_client_before_network_io() -> None:
    """The credential provider itself gates requests after a scope change."""

    class NoNetworkSession:  # pylint: disable=too-few-public-methods
        """Record forbidden request attempts."""

        def __init__(self) -> None:
            self.calls = 0

        def request(self, *_args, **_kwargs):
            """Fail if the client reaches its transport."""

            self.calls += 1
            raise AssertionError("request must be gated before network I/O")

    reader = Reader(snapshot())
    adapter = HermesAccountAdapter.current(_read_snapshot=reader, _clock=lambda: NOW)
    session = NoNetworkSession()
    client = WisdomClient(
        adapter.credentials,
        adapter.organization_id,
        session=session,
    )
    reader.value = replace(reader.value, scopes=())

    with pytest.raises(WisdomAuthError, match="credentials are unavailable"):
        client.list_skills()
    assert session.calls == 0


def test_host_failures_deny_and_do_not_leak_details() -> None:
    """Host exceptions and bearers are absent from adapter errors and reprs."""

    secret = secrets.token_urlsafe(24)

    def fail() -> _HostAccountSnapshot:
        raise RuntimeError(f"host failed with {secret}")

    with pytest.raises(WisdomAuthError) as error:
        HermesAccountAdapter.current(_read_snapshot=fail, _clock=lambda: NOW)
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert secret not in repr(snapshot(access_token=secret))

    adapter = HermesAccountAdapter(snapshot(), fail, clock=lambda: NOW)
    assert adapter.entitlement_claims() is None
    with pytest.raises(WisdomAuthError) as request_error:
        adapter.credentials()
    assert secret not in str(request_error.value)
    assert request_error.value.__cause__ is None


def test_host_module_is_import_safe_without_hermes() -> None:
    """Importing the adapter itself does not import Hermes host modules."""

    assert "hermes_cli.auth" not in sys.modules
    assert "hermes_cli.auth_constants" not in sys.modules


def test_current_rejects_missing_host_api_shape(monkeypatch) -> None:
    """A missing reviewed host function fails closed without exposing import details."""

    def missing_api(_name: str):
        raise AttributeError("secret host layout detail")

    monkeypatch.setattr("hermes_wisdom_plugin.account.import_module", missing_api)

    with pytest.raises(WisdomAuthError, match="credentials are unavailable"):
        HermesAccountAdapter.current()


def _host_modules(provider_state, pool_status, calls):
    """Build the reviewed host API shape with deterministic local snapshots."""

    claims = {
        "provider-token": {
            "sub": "provider-user",
            "org_id": "provider-org",
            "exp": NOW + 600,
            "nbf": 0,
            "wisdom_scopes": ["wisdom:read"],
        },
        "pool-token": {
            "sub": "pool-user",
            "org_id": "pool-org",
            "exp": NOW + 600,
            "nbf": 0,
            "wisdom_scopes": ["wisdom:read"],
        },
    }

    def read_provider(_provider):
        calls.append("provider")
        return provider_state

    def read_pool_status():
        calls.append("pool")
        return pool_status

    return {
        "hermes_cli.auth": SimpleNamespace(
            get_provider_auth_state=read_provider,
            _nous_portal_base_url=lambda state: state.get("portal_base_url"),
        ),
        "hermes_cli.auth_constants": SimpleNamespace(
            _decode_jwt_claims=lambda token: claims.get(token, {})
        ),
        "hermes_cli.profiles": SimpleNamespace(get_active_profile_name=lambda: "work"),
        "hermes_constants": SimpleNamespace(get_hermes_home=lambda: "/profiles/work"),
        "hermes_cli.anon_auth": SimpleNamespace(is_guest_state=lambda _state: False),
        "hermes_cli.auth_nous": SimpleNamespace(
            get_nous_auth_status_local=read_pool_status
        ),
        "tools.skills_sync_client": SimpleNamespace(
            resolve_sync_base_url=lambda: "https://gateway.example"
        ),
    }


def _provider_state(token: str = "provider-token") -> dict[str, str]:
    """Return one valid persisted provider state."""

    return {
        "access_token": token,
        "user_id": "provider-user",
        "org_id": "provider-org",
        "portal_base_url": "https://provider-portal.example",
    }


def _pool_status(*, source: str = "pool:primary") -> dict[str, object]:
    """Return one valid refresh-free credential-pool projection."""

    return {
        "source": source,
        "access_token": "pool-token",
        "portal_base_url": "https://pool-portal.example",
    }


def test_present_provider_state_takes_precedence_over_pool(monkeypatch) -> None:
    """A present provider record is authoritative and avoids pool status reads."""

    calls = []
    modules = _host_modules(_provider_state(), _pool_status(), calls)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.account.import_module", lambda name: modules[name]
    )
    monkeypatch.setattr("hermes_wisdom_plugin.account.time.time", lambda: NOW)

    result = _read_current_host_account()

    assert calls == ["provider"]
    assert result.account_id == "provider-user"
    assert result.portal_base_url == "https://provider-portal.example"


def test_invalid_present_provider_state_never_falls_back_to_pool(monkeypatch) -> None:
    """An invalid present record denies instead of selecting another account."""

    calls = []
    modules = _host_modules({}, _pool_status(), calls)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.account.import_module", lambda name: modules[name]
    )
    monkeypatch.setattr("hermes_wisdom_plugin.account.time.time", lambda: NOW)

    with pytest.raises(RuntimeError):
        _read_current_host_account()
    assert calls == ["provider"]


def test_absent_provider_state_uses_refresh_free_pool_snapshot(monkeypatch) -> None:
    """Only an absent provider record permits the host's local pool fallback."""

    calls = []
    modules = _host_modules(None, _pool_status(), calls)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.account.import_module", lambda name: modules[name]
    )
    monkeypatch.setattr("hermes_wisdom_plugin.account.time.time", lambda: NOW)

    result = _read_current_host_account()

    assert calls == ["provider", "pool"]
    assert result.account_id == "pool-user"
    assert result.organization_id == "pool-org"
    assert result.portal_base_url == "https://pool-portal.example"


def test_pool_fallback_rejects_non_pool_provenance(monkeypatch) -> None:
    """A concurrent provider appearance cannot pass through the fallback path."""

    calls = []
    modules = _host_modules(None, _pool_status(source="auth_store_local"), calls)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.account.import_module", lambda name: modules[name]
    )

    with pytest.raises(RuntimeError, match="pool is unavailable"):
        _read_current_host_account()
    assert calls == ["provider", "pool"]


def test_core_validation_rejects_guest_missing_and_malformed_state() -> None:
    """Guest markers and malformed identity fields fail before projection."""

    state = {
        "access_token": "secret-token",
        "user_id": "user-1",
        "org_id": "org-1",
        "logged_in": False,
    }
    claims = {
        "sub": "user-1",
        "org_id": "org-1",
        "exp": NOW + 600,
        "nbf": 0,
        "wisdom_scopes": ["wisdom:read"],
    }
    valid = _validated_snapshot(
        "work",
        "/profiles/work",
        state,
        "secret-token",
        claims,
        "https://gateway.example",
        "https://portal.example",
        now=NOW,
    )
    assert valid.account_id == "user-1"

    invalid = [
        ({}, claims),
        (state, {}),
        (state, {**claims, "sub": ""}),
        (state, {**claims, "org_id": ""}),
        (state, {**claims, "exp": NOW}),
        (state, {**claims, "exp": True}),
        (state, {**claims, "nbf": NOW + 1}),
        (state, {**claims, "wisdom_scopes": "wisdom:read"}),
        ({**state, "user_id": "user-2"}, claims),
        ({**state, "org_id": "org-2"}, claims),
    ]
    for bad_state, bad_claims in invalid:
        with pytest.raises(RuntimeError):
            _validated_snapshot(
                "work",
                "/profiles/work",
                bad_state,
                bad_state.get("access_token"),
                bad_claims,
                "https://gateway.example",
                "https://portal.example",
                now=NOW,
            )

    with pytest.raises(RuntimeError):
        _validated_snapshot(
            "work",
            "/profiles/work",
            state,
            "secret-token",
            claims,
            "https://gateway.example",
            "https://portal.example",
            is_guest=True,
            now=NOW,
        )
