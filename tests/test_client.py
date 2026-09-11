"""Read-only Collective Wisdom Gateway client tests."""

import json

import pytest

from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)


class Response:  # pylint: disable=too-few-public-methods
    """Minimal requests response fixture."""

    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""

    def json(self):
        """Return the configured JSON body."""

        return self._body


class Session:  # pylint: disable=too-few-public-methods
    """Recording requests session fixture."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        """Record and return one configured response."""

        self.calls.append((method, url, kwargs))
        return self.response


def client(response):
    """Build a client against the recording session."""

    return WisdomClient(
        "https://gateway.example",
        "test-token",
        "org-1",
        timeout=7,
        session=Session(response),
    )


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


def test_browse_preserves_cursor_and_schema_validation():
    """Cursor pages preserve opaque values and reject malformed rows."""
    value = client(
        Response(200, {"skills": [_summary()], "next_cursor": "cursor-2"})
    )

    result = value.list_skills(cursor="cursor-1")

    assert result.next_cursor == "cursor-2"
    assert result.skills[0].slug == "shared-skill"
    assert value.session.calls == [
        (
            "GET",
            "https://gateway.example/v1/sync/wisdom/skills",
            {"params": {"cursor": "cursor-1"}, "timeout": 7},
        )
    ]
    value.session.response = Response(
        200, {"skills": [_summary(install_count="three")], "next_cursor": None}
    )
    with pytest.raises(WisdomError, match="schema validation"):
        value.list_skills()


def test_detail_version_and_content_paths_are_quoted_and_read_only():
    """Discovery endpoints quote identifiers and issue only GET requests."""
    value = client(Response(200, {"skill": {}, "versions": []}))
    value.skill("skill/one")
    value.session.response = Response(200, {"skill": {}, "version": {}})
    value.version("skill/one", 2)
    value.session.response = Response(
        200, {"commit": "sha256:a", "content_hash": "sha256:b", "files": []}
    )
    value.content(
        "skill/one",
        2,
        installation_id="installation-1",
        takedown_generation=7,
    )
    value.raw_copy("skill/one", 2)

    assert [call[0] for call in value.session.calls] == ["GET"] * 4
    assert [call[1].split("/wisdom/")[-1] for call in value.session.calls] == [
        "skills/skill%2Fone",
        "skills/skill%2Fone/versions/2",
        "skills/skill%2Fone/versions/2/content",
        "skills/skill%2Fone/versions/2/raw",
    ]
    assert value.session.calls[2][2]["params"] == {
        "installation_id": "installation-1",
        "takedown_generation": 7,
    }


@pytest.mark.parametrize(
    ("status", "error_type", "exit_code"),
    [
        (401, WisdomAuthError, 3),
        (403, WisdomAuthError, 3),
        (404, WisdomNotFound, 4),
        (409, WisdomConflict, 5),
        (422, WisdomValidationError, 6),
        (503, WisdomError, 8),
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
