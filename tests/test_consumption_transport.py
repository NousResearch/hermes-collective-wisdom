"""Transport and terminal journey for shared consumption controls."""
# pylint: disable=missing-function-docstring,import-outside-toplevel

from __future__ import annotations

from datetime import datetime, timezone

from test_client import Response, Session

from hermes_wisdom_plugin.client import WisdomClient, WisdomGatewayCredentials


def _credentials():
    return WisdomGatewayCredentials("https://gateway.example", "token", "org-1")


def test_shared_transport_uses_exact_gateway_routes_and_request_seam():
    session = Session(Response(200, {
        "events": [{
            "event_id": "event-1", "kind": "new", "skill_id": "skill-1",
            "version": 1, "takedown_generation": 0, "installation_id": None,
            "update_mode": None, "occurred_at": datetime.now(timezone.utc).isoformat(),
        }],
        "next_cursor": "next", "has_more": False,
    }))
    client = WisdomClient(_credentials, "org-1", owner_user_id="user-1", session=session)
    assert client.feed(None, installation_id="hwi_test").events[0].event_id == "event-1"

    session.response = Response(200, {
        "org_id": "org-1", "muted": False, "duration": None,
        "muted_until": None, "forever": False, "revision": 4,
        "mutation_id": None, "updated_at": None,
    })
    assert client.recommendation_mute().revision == 4

    key = "sha256:" + "a" * 64
    session.response = Response(200, {
        "org_id": "org-1", "suppressions": [],
    })
    assert client.recommendation_suppressions([key]) == []

    assert [(call[0], call[1]) for call in session.calls] == [
        ("GET", "https://gateway.example/v1/sync/wisdom/feed"),
        ("GET", "https://gateway.example/v1/sync/wisdom/agent-led/mute"),
        ("POST", "https://gateway.example/v1/sync/wisdom/agent-led/suppressions/check"),
    ]
    assert session.calls[0][2]["params"] == {"installation_id": "hwi_test"}
    assert session.calls[1][2]["params"] == {"include_revision": "1"}
    assert session.calls[2][2]["json"] == {"keys": [key]}


def test_terminal_parser_exposes_explicit_consumption_controls():
    import argparse

    from hermes_wisdom_plugin.commands import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser)
    assert parser.parse_args(["notifications", "--mark-seen"]).wisdom_command == "notifications"
    assert parser.parse_args(["mute", "1w"]).mute_duration == "1w"
    assert parser.parse_args(["sync", "retry"]).sync_action == "retry"
    assert parser.parse_args(["inbox"]).wisdom_command == "inbox"
    assert parser.parse_args(["maintenance", "--refresh-policy"]).refresh_policy is True
