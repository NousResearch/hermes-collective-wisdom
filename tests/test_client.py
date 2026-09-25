"""Read-only Collective Wisdom Gateway client tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from hermes_wisdom_plugin.client import (
    MAX_RESPONSE_BYTES,
    WisdomClient,
    WisdomGatewayCredentials,
    WisdomOrganizationContext,
)
from hermes_wisdom_plugin.errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomUnavailable,
    WisdomValidationError,
)


class Response:  # pylint: disable=too-few-public-methods
    """Minimal streamed requests response fixture."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        status: int,
        body=None,
        *,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        stream_error: Exception | None = None,
    ):
        self.status_code = status
        self.raw = raw if raw is not None else (
            json.dumps(body).encode("utf-8") if body is not None else b""
        )
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        }
        self.chunks = chunks
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size: int):
        """Yield configured decoded response chunks."""

        if self.stream_error is not None:
            raise self.stream_error
        chunks = self.chunks
        if chunks is None:
            chunks = [
                self.raw[offset : offset + chunk_size]
                for offset in range(0, len(self.raw), chunk_size)
            ]
        yield from chunks

    def close(self):
        """Record deterministic response cleanup."""

        self.closed = True


class Session:  # pylint: disable=too-few-public-methods
    """Recording requests session fixture."""

    def __init__(self, response=None, *, request_error: Exception | None = None):
        self.response = response
        self.request_error = request_error
        self.calls = []

    def request(self, method, url, **kwargs):
        """Record and return one configured response."""

        self.calls.append((method, url, kwargs))
        if self.request_error is not None:
            raise self.request_error
        return self.response


def _credentials(**overrides) -> WisdomGatewayCredentials:
    values = {
        "base_url": "https://gateway.example",
        "access_token": "test-token",
        "organization_id": "org-1",
    }
    values.update(overrides)
    return WisdomGatewayCredentials(**values)


def client(response=None, **kwargs):
    """Build a client with a recording provider and session."""

    provider_calls = []

    def provider():
        provider_calls.append(True)
        return _credentials()

    session = kwargs.pop("session", Session(response))
    value = WisdomClient(
        provider,
        "org-1",
        timeout=7,
        session=session,
        **kwargs,
    )
    value.provider_calls = provider_calls
    return value


def _summary(**overrides):
    value = {
        "id": "skill-1",
        "slug": "shared-skill",
        "state": "active",
        "created_by_user_id": "user-1",
        "latest_version": 2,
        "author_description": "Does a task.",
        "verified_facts": None,
        "explanation": None,
        "install_count": 3,
        "takedown_generation": 0,
        "system_spec": None,
    }
    value.update(overrides)
    return value


def test_credentials_are_lazy_explicit_and_project_only_non_secret_org_context():
    """Construction performs no auth read and organization context omits the bearer."""

    value = client(Response(200, {"skills": [], "next_cursor": None}))

    assert not value.provider_calls
    assert value.organization_context() == WisdomOrganizationContext("org-1")
    assert not value.provider_calls
    assert "test-token" not in repr(_credentials())
    assert value.session.calls == []


def test_each_request_uses_one_current_snapshot_without_redirects():
    """Requests use explicit auth headers and refuse transport redirects."""

    value = client(Response(200, {"skills": [], "next_cursor": None}))

    value.list_skills()

    assert value.provider_calls == [True]
    request = value.session.calls[0][2]
    assert request["headers"] == {
        "Authorization": "Bearer test-token",
        "Accept": "application/json",
    }
    assert request["timeout"] == 7.0
    assert request["stream"] is True
    assert request["allow_redirects"] is False


def test_real_requests_session_reads_loopback_then_blocks_organization_change():
    """The default transport sends one valid request and fences later org drift."""

    received = []
    response_body = json.dumps(
        {"skills": [_summary()], "next_cursor": None}
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        """Serve one deterministic Wisdom discovery response."""

        def do_GET(self):  # pylint: disable=invalid-name
            """Record request metadata and return a valid discovery page."""

            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_request(self, code="-", size="-"):
            """Keep the focused test output quiet."""

            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    credentials = [
        _credentials(base_url=f"http://127.0.0.1:{server.server_port}")
    ]
    value = WisdomClient(lambda: credentials[0], "org-1", timeout=2)
    try:
        result = value.list_skills(limit=1)
        request_count = len(received)
        credentials[0] = _credentials(
            base_url=f"http://127.0.0.1:{server.server_port}",
            organization_id="other-org",
        )
        with pytest.raises(WisdomAuthError):
            value.list_skills(limit=1)
    finally:
        value.session.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert result.skills[0].slug == "shared-skill"
    assert received == [
        {
            "path": "/v1/sync/wisdom/skills?limit=1",
            "authorization": "Bearer test-token",
            "accept": "application/json",
        }
    ]
    assert len(received) == request_count


@pytest.mark.parametrize(
    "provided",
    [
        None,
        _credentials(base_url=""),
        _credentials(base_url="https://user:pass@gateway.example"),
        _credentials(base_url="https://gateway.example?token=leak"),
        _credentials(access_token=""),
        _credentials(organization_id=""),
        _credentials(organization_id="other-org"),
    ],
)
def test_invalid_or_changed_credentials_fail_closed_before_http(provided):
    """Malformed or cross-organization snapshots never reach transport."""

    session = Session(Response(200, {"skills": [], "next_cursor": None}))
    value = WisdomClient(lambda: provided, "org-1", session=session)

    with pytest.raises(WisdomAuthError, match="credentials are unavailable"):
        value.list_skills()

    assert not session.calls


def test_provider_failures_do_not_expose_exception_messages():
    """Credential reader failures retain a generic public message."""

    def fail():
        raise RuntimeError("private refresh token contents")

    session = Session(Response(200, {"skills": [], "next_cursor": None}))
    with pytest.raises(WisdomAuthError) as raised:
        WisdomClient(fail, "org-1", session=session).list_skills()
    assert "private refresh" not in str(raised.value)
    assert not session.calls


def test_browse_preserves_cursor_limit_empty_page_and_strict_schema():
    """Cursor pages preserve opaque values and reject coerced rows."""

    response = Response(200, {"skills": [_summary()], "next_cursor": "cursor-2"})
    value = client(response)

    result = value.list_skills(cursor="cursor-1", limit=25)

    assert result.next_cursor == "cursor-2"
    assert result.skills[0].slug == "shared-skill"
    assert value.session.calls[0][0:2] == (
        "GET",
        "https://gateway.example/v1/sync/wisdom/skills",
    )
    assert value.session.calls[0][2]["params"] == {
        "cursor": "cursor-1",
        "limit": "25",
    }
    assert response.closed is True

    value.session.response = Response(200, {"skills": [], "next_cursor": None})
    assert value.list_skills().skills == []
    value.session.response = Response(
        200, {"skills": [_summary(install_count="3")], "next_cursor": None}
    )
    with pytest.raises(WisdomError, match="schema validation"):
        value.list_skills()


def test_system_spec_retains_shared_contract_validation():
    """Discovery does not weaken system specifications to arbitrary dictionaries."""

    value = client(
        Response(
            200,
            {
                "skills": [_summary(system_spec={"schema_version": "1"})],
                "next_cursor": None,
            },
        )
    )

    with pytest.raises(WisdomError, match="schema validation"):
        value.list_skills()


def test_detail_and_version_paths_are_quoted_and_read_only():
    """Metadata endpoints quote identifiers and issue only GET requests."""

    value = client(Response(200, {"skill": {}, "versions": []}))
    value.skill("skill/one")
    value.session.response = Response(200, {"skill": {}, "version": {}})
    value.version("skill/one", 2)

    assert [call[0] for call in value.session.calls] == ["GET", "GET"]
    assert [call[1].split("/wisdom/")[-1] for call in value.session.calls] == [
        "skills/skill%2Fone",
        "skills/skill%2Fone/versions/2",
    ]
    assert all(call[2]["params"] is None for call in value.session.calls)


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        ("skill", ("",), {}),
        ("skill", ("a" * 513,), {}),
        ("skill", ("bad\x00id",), {}),
        ("version", ("skill-1", 0), {}),
        ("version", ("skill-1", True), {}),
        ("list_skills", (), {"cursor": ""}),
        ("list_skills", (), {"cursor": "c" * 4097}),
        ("list_skills", (), {"limit": 0}),
        ("list_skills", (), {"limit": True}),
        ("list_skills", (), {"limit": 101}),
    ],
)
def test_invalid_discovery_requests_fail_before_credentials_and_http(
    operation, args, kwargs
):
    """Invalid identifiers, versions, cursors, and limits stop locally."""

    value = client(Response(200, {}))

    with pytest.raises(WisdomValidationError):
        getattr(value, operation)(*args, **kwargs)

    assert value.session.calls == []
    assert not value.provider_calls


@pytest.mark.parametrize(
    ("status", "error_type", "exit_code"),
    [
        (401, WisdomAuthError, 3),
        (403, WisdomAuthError, 3),
        (404, WisdomNotFound, 4),
        (409, WisdomConflict, 5),
        (422, WisdomValidationError, 6),
        (502, WisdomUnavailable, 8),
        (503, WisdomUnavailable, 8),
        (504, WisdomUnavailable, 8),
    ],
)
def test_http_failures_preserve_status_code_and_exit_contract(
    status, error_type, exit_code
):
    """HTTP errors retain the shared status, code, and process-exit contract."""

    value = client(Response(status, {"error": "stable_error_code"}))

    with pytest.raises(error_type) as raised:
        value.list_skills()

    assert raised.value.status == status
    assert raised.value.code == "stable_error_code"
    assert raised.value.exit_code == exit_code
    if isinstance(raised.value, WisdomUnavailable):
        assert str(raised.value) == "Collective Wisdom is temporarily unavailable."


@pytest.mark.parametrize(
    "response",
    [
        Response(200, raw=b"not-json"),
        Response(200, body={}, headers={"Content-Type": "text/html"}),
        Response(200, body={}, headers={"Content-Length": "invalid"}),
        Response(200, body={}, headers={"Content-Length": "-1"}),
    ],
)
def test_malformed_json_and_headers_fail_closed_and_close_response(response):
    """Malformed response framing never reaches schema parsing."""

    value = client(response)

    with pytest.raises(WisdomError, match="malformed response|invalid response headers"):
        value.list_skills()

    assert response.closed is True


@pytest.mark.parametrize("response", [
    Response(503, raw=b"<html>upstream unavailable</html>",
             headers={"Content-Type": "text/html"}),
    Response(502, raw=b""),
])
def test_proxy_outage_without_json_still_uses_transient_error(response):
    """A proxy's HTML/empty outage response is still a transient failure."""
    value = client(response)
    with pytest.raises(WisdomUnavailable, match="temporarily unavailable") as raised:
        value.list_skills()
    assert raised.value.status == response.status_code
    assert response.closed is True


def test_declared_and_streamed_response_limits_are_enforced():
    """Declared and decoded streamed bytes obey the configured ceiling."""

    declared = Response(
        200,
        body={},
        headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
    )
    value = client(declared)
    with pytest.raises(WisdomError, match="exceeded size limit"):
        value.list_skills()
    assert declared.closed is True

    streamed = Response(200, chunks=[b"{" + b" " * 7, b" " * 8])
    value = client(streamed, max_response_bytes=15)
    with pytest.raises(WisdomError, match="exceeded size limit"):
        value.list_skills()
    assert streamed.closed is True


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (requests.Timeout("slow"), "temporarily unavailable"),
        (requests.ConnectionError("offline"), "temporarily unavailable"),
    ],
)
def test_transport_failures_are_stable_and_do_not_echo_details(error, message):
    """Transport exceptions map to stable client messages."""

    value = client(session=Session(request_error=error))

    with pytest.raises(WisdomUnavailable, match=message) as raised:
        value.list_skills()
    assert "slow" not in str(raised.value)
    assert "offline" not in str(raised.value)
    assert raised.value.code == "gateway_unavailable"
    assert raised.value.exit_code == 8


def test_stream_timeout_closes_response():
    """A timeout while consuming the body still releases the response."""

    response = Response(200, stream_error=requests.Timeout("slow body"))
    value = client(response)

    with pytest.raises(WisdomUnavailable, match="temporarily unavailable"):
        value.list_skills()

    assert response.closed is True


@pytest.mark.parametrize(
    ("timeout", "max_bytes"),
    [(0, MAX_RESPONSE_BYTES), (float("nan"), MAX_RESPONSE_BYTES), (True, 1), (1, 0)],
)
def test_client_bounds_are_validated_at_construction(timeout, max_bytes):
    """Invalid bounds are rejected without reading credentials."""

    with pytest.raises(ValueError):
        WisdomClient(
            _credentials,
            "org-1",
            timeout=timeout,
            max_response_bytes=max_bytes,
        )
