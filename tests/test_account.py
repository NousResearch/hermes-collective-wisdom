"""Tests for the refresh-free Hermes account adapter."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from hermes_wisdom_plugin.account import (
    HermesAccountAdapter,
    _HostAccountSnapshot,
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
    assert "secret-token" not in repr(adapter.__dict__["_binding"])
    assert not hasattr(adapter.__dict__["_binding"], "access_token")
    assert adapter.entitlement_claims().scopes == ["wisdom:read", "wisdom:publish"]
    credentials = adapter.credentials()
    assert credentials.base_url == "https://gateway.example"
    assert credentials.organization_id == "org-1"
    assert credentials.access_token == "secret-token"
    assert reader.calls == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_name", "other"),
        ("profile_home", "/profiles/other"),
        ("account_id", "user-2"),
        ("organization_id", "org-2"),
        ("base_url", "https://other-gateway.example"),
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

    secret = "credential-super-secret"

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
            is_guest=True,
            now=NOW,
        )
