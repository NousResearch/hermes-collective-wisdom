"""Install-scoped Collective Wisdom client contract tests."""

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=too-few-public-methods,unused-argument

from __future__ import annotations

import base64
import json

import pytest

from hermes_wisdom_plugin.client import WisdomClient, WisdomGatewayCredentials
from hermes_wisdom_plugin.contract import ContentFile, derive_content_hash, sha256_address
from hermes_wisdom_plugin.errors import WisdomError, WisdomValidationError


class Response:
    def __init__(self, body: dict):
        self.status_code = 200
        self.body = json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}

    def iter_content(self, chunk_size: int):
        yield self.body

    def close(self):
        return None


class Session:
    def __init__(self, responses: list[dict]):
        self.responses = [Response(item) for item in responses]
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _credentials(token: str) -> WisdomGatewayCredentials:
    return WisdomGatewayCredentials("https://gateway.example", token, "org-1")


def _content_response() -> dict:
    bodies = {
        "SKILL.md": b"# Shared skill\n",
        "skill.manifest.json": json.dumps(
            {
                "schema_version": 1,
                "name": "Shared skill",
                "requirements": {"hermes": {"minimum_version": "0.1.0"}},
            },
            separators=(",", ":"),
        ).encode(),
    }
    records = [
        ContentFile(path=path, mode="file", hash=sha256_address(body))
        for path, body in bodies.items()
    ]
    return {
        "commit": "sha256:" + "c" * 64,
        "content_hash": derive_content_hash(records),
        "files": [
            {
                "path": path,
                "mode": "file",
                "hash": sha256_address(body),
                "content_base64": base64.b64encode(body).decode(),
            }
            for path, body in bodies.items()
        ],
    }


def test_install_operations_use_fresh_install_credentials_and_validate_wire_data():
    install_calls = []

    def install_credentials():
        install_calls.append(True)
        return _credentials(f"install-{len(install_calls)}")

    session = Session([
        _content_response(),
        {"installed_version": 2, "effective_update_mode": "MANUAL"},
        {"installations": []},
        {"installation_id": "install/1", "skill_id": "skill/1", "state": "inactive"},
    ])
    client = WisdomClient(
        lambda: _credentials("read"),
        "org-1",
        session=session,
        install_credential_provider=install_credentials,
    )

    _, files = client.content(
        "skill/1", 2, installation_id="install/1", takedown_generation=3
    )
    ack = client.record_install(
        skill_id="skill/1",
        installation_id="install/1",
        version=2,
        takedown_generation=3,
        update_mode="MANUAL",
    )
    assert client.installations("install/1") == []
    deactivated = client.deactivate_install("install/1", "skill/1")

    assert [path for path, _, _ in files] == ["SKILL.md", "skill.manifest.json"]
    assert ack.installed_version == 2
    assert deactivated.state == "inactive"
    assert install_calls == [True, True, True, True]
    assert [call[2]["headers"]["Authorization"] for call in session.calls] == [
        "Bearer install-1", "Bearer install-2", "Bearer install-3", "Bearer install-4"
    ]
    assert session.calls[0][2]["params"] == {
        "installation_id": "install/1",
        "takedown_generation": "3",
    }
    assert session.calls[1][0] == "POST"
    assert session.calls[3][0] == "DELETE"
    assert [call[2]["timeout"] for call in session.calls] == [(30, 120), 30, 30, 30]


def test_content_tolerates_additive_fields_but_rejects_invalid_base64():
    response = _content_response()
    response["files"][0]["unexpected"] = True
    client = WisdomClient(
        lambda: _credentials("read"),
        "org-1",
        session=Session([response]),
        install_credential_provider=lambda: _credentials("install"),
    )
    _, files = client.content("skill", 1, installation_id="install", takedown_generation=0)
    assert len(files) == 2

    malformed = _content_response()
    malformed["files"][0]["content_base64"] = "%%%"
    client.session = Session([malformed])
    with pytest.raises(WisdomValidationError, match="malformed base64"):
        client.content("skill", 1, installation_id="install", takedown_generation=0)


def test_install_acknowledgements_must_match_requests():
    client = WisdomClient(
        lambda: _credentials("read"),
        "org-1",
        session=Session([
            {"installed_version": 3, "effective_update_mode": "MANUAL"},
            {"installation_id": "other", "skill_id": "skill", "state": "inactive"},
        ]),
        install_credential_provider=lambda: _credentials("install"),
    )
    with pytest.raises(WisdomError, match="different installed version"):
        client.record_install(
            skill_id="skill", installation_id="install", version=2,
            takedown_generation=0, update_mode=None,
        )
    with pytest.raises(WisdomError, match="different installation"):
        client.deactivate_install("install", "skill")
