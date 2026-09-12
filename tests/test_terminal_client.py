"""Explicit enrolment uses the bounded transport, never implicit credentials."""

import pytest
from test_client import Response, Session, _credentials

from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomError


def test_capabilities_are_authenticated_bounded_and_feature_checked():
    """Do not enable setup against an unrelated Sync server."""
    session = Session(Response(200, {"features": ["wisdom"]}))
    client = WisdomClient(_credentials, "org-1", session=session)
    assert client.capability().features == ["wisdom"]
    assert session.calls[0][1] == "https://gateway.example/v1/sync/capabilities"
    session.response = Response(200, {"features": []})
    with pytest.raises(WisdomAuthError):
        client.capability()
    session.response = Response(200, {"features": "wisdom"})
    with pytest.raises(WisdomError, match="schema"):
        client.capability()


def test_identity_registration_requires_dedicated_scope_provider():
    """Read-only callers cannot use the mutation helper."""
    session = Session(Response(201, {"installation_id": "hwi_test", "state": "active"}))
    client = WisdomClient(_credentials, "org-1", session=session)
    with pytest.raises(WisdomAuthError):
        client.register_identity("hwi_test")
    assert not session.calls


def test_identity_acknowledgement_is_exact_and_scope_checked_each_time():
    """A response for another identity cannot activate the pending setup."""
    calls = []

    def install_credentials():
        calls.append(True)
        return _credentials()

    session = Session(Response(201, {"installation_id": "hwi_test", "state": "active"}))
    client = WisdomClient(
        _credentials, "org-1", session=session,
        install_credential_provider=install_credentials,
    )
    assert client.register_identity("hwi_test").state == "active"
    method, url, options = session.calls[0]
    assert method == "POST"
    assert url.endswith("/v1/sync/wisdom/installation-identities")
    assert options["json"] == {"installation_id": "hwi_test"}
    assert options["allow_redirects"] is False
    session.response = Response(200, {"installation_id": "other", "state": "active"})
    with pytest.raises(WisdomError, match="different installation"):
        client.register_identity("hwi_test")
    assert len(calls) == 2


def test_close_prevents_further_network_calls():
    """Disposal makes an invocation transport unusable."""
    class CloseableSession(Session):
        """Record transport disposal."""
        closed = False

        def close(self):
            """Record closure."""
            self.closed = True

    session = CloseableSession()
    client = WisdomClient(_credentials, "org-1", session=session)
    client.close()
    assert session.closed
    with pytest.raises(WisdomError, match="closed"):
        client.list_skills()
    assert not session.calls
