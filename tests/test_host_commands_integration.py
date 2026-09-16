"""Clean-host integration probe for native Wisdom CLI and TUI commands."""
# The isolated host subprocess deliberately carries its own wire fixtures.
# pylint: disable=duplicate-code,too-many-lines

from __future__ import annotations

from pathlib import Path

from host_probe_support import (
    immutable_host_checkout,
    isolated_host_environment,
    run_host_python,
    write_no_auth_refresh_sitecustomize,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"
PLUGIN_ID = "hermes-collective-wisdom"
PROBE = r'''
import base64
import asyncio
import contextlib
import hashlib
import io
import importlib
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PLUGIN_ID = "hermes-collective-wisdom"
PROJECT_ROOT = Path(os.environ["WISDOM_PLUGIN_ROOT"]).resolve()
HOME = Path(os.environ["HERMES_HOME"]).resolve()
REQUESTS = []
REQUEST_LOCK = threading.Lock()
ACTIVE_TOKEN = None
EXPECTED_HASHES = None
EXPECTED_SYSTEM_SPEC = None
EXPECTED_INSTALLATION_ID = None
DRAFT = None
MUTE_REVISION = 0
REMOTE_INSTALLED_VERSION = 1

UPDATE_REQUIREMENTS = {"hermes": {"minimum_version": "0.1.0"}}
UPDATE_MANIFEST = json.dumps({
    "schema_version": 1, "name": "release-notes",
    "requirements": UPDATE_REQUIREMENTS,
}, sort_keys=True, separators=(",", ":")).encode()
UPDATE_FILES = {
    1: [("SKILL.md", "file", b"# Release notes v1\n"),
        ("skill.manifest.json", "file", UPDATE_MANIFEST)],
    2: [("SKILL.md", "file", b"# Release notes v2\n"),
        ("skill.manifest.json", "file", UPDATE_MANIFEST)],
}


def wire_hash(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


def content_hash(files):
    manifest = "".join(sorted(
        f"{path} {mode} {wire_hash(body)}\n" for path, mode, body in files
    )).encode()
    return wire_hash(manifest)


def content_response(version):
    files = UPDATE_FILES[version]
    return {
        "commit": "sha256:" + ("c" if version == 1 else "d") * 64,
        "content_hash": content_hash(files),
        "files": [{
            "path": path, "mode": mode, "hash": wire_hash(body),
            "content_base64": base64.b64encode(body).decode(),
        } for path, mode, body in files],
    }

SKILL = {
    "id": "skill-1",
    "slug": "release-notes",
    "state": "active",
    "created_by_user_id": "publisher-1",
    "latest_version": 2,
    "author_description": "Draft clear release notes.",
    "verified_facts": None,
    "explanation": None,
    "install_count": 3,
    "takedown_generation": 0,
    "system_spec": None,
    "security_check": {"state": "pass"},
    "professionalism_check": {"state": "pass"},
}
VERSIONS = [
    {"version": 1, "state": "published", "published_at": "2026-09-01T00:00:00Z"},
    {"version": 2, "state": "published", "published_at": "2026-09-02T00:00:00Z"},
]


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _record(self, method, body=None):
        authorized = bool(
            ACTIVE_TOKEN
            and self.headers.get("Authorization") == f"Bearer {ACTIVE_TOKEN}"
        )
        entry = {
            "method": method,
            "path": urlsplit(self.path).path,
            "body": body,
            "authorized": authorized,
        }
        with REQUEST_LOCK:
            REQUESTS.append(entry)
        return entry["path"]

    def do_GET(self):
        path = self._record("GET")
        if path == "/v1/sync/capabilities":
            self._reply(200, {"features": ["wisdom"]})
        elif path == "/v1/sync/wisdom/skills":
            self._reply(200, {"skills": [SKILL], "next_cursor": None})
        elif path == "/v1/sync/wisdom/skills/skill-1":
            self._reply(200, {"skill": SKILL, "versions": VERSIONS})
        elif path == "/v1/sync/wisdom/skills/skill-1/versions/2":
            self._reply(
                200,
                {
                    "skill": SKILL,
                    "version": {
                        **VERSIONS[1],
                        "author_description": "Draft clear release notes.",
                        "security_check": {"state": "pass"},
                        "professionalism_check": {"state": "pass"},
                        "published_by_user_id": "publisher-1",
                        "system_spec": UPDATE_REQUIREMENTS,
                        "content_hash": content_hash(UPDATE_FILES[2]),
                        "commit": "sha256:" + "d" * 64,
                        "package_manifest_hash": wire_hash(UPDATE_MANIFEST),
                    },
                },
            )
        elif path == "/v1/sync/wisdom/skills/skill-1/versions/2/content":
            self._reply(200, content_response(2))
        elif path.startswith("/v1/sync/wisdom/installations/"):
            assert path == f"/v1/sync/wisdom/installations/{EXPECTED_INSTALLATION_ID}"
            self._reply(200, {"installations": [{
                "skill_id": "skill-1",
                "installed_version": REMOTE_INSTALLED_VERSION,
                "latest_version": 2,
                "update_mode": "AUTO_WITH_NOTICE",
                "skill_state": "active",
                "takedown_generation": 0,
            }]})
        elif path == "/v1/sync/wisdom/feed":
            self._reply(200, {
                "events": [{
                    "event_id": "feed-event-1", "kind": "new",
                    "skill_id": "skill-1", "version": 2, "takedown_generation": 0,
                    "installation_id": None, "update_mode": None,
                    "occurred_at": "2026-09-14T00:00:00Z",
                }],
                "next_cursor": "feed-next", "has_more": False,
            })
        elif path == "/v1/sync/wisdom/agent-led/mute":
            self._reply(200, {
                "org_id": "team-1", "muted": MUTE_REVISION > 0,
                "duration": "1_week" if MUTE_REVISION > 0 else None,
                "muted_until": None, "forever": False, "revision": MUTE_REVISION,
                "mutation_id": None, "updated_at": None,
            })
        elif path == "/v1/sync/wisdom/agent-led/policy":
            self._reply(200, {
                "org_id": "team-1",
                "usage_evidence_window_days": 7,
                "min_aggregate_invocations": 3,
                "consecutive_day_usage_counts": True,
                "repeated_edits_count": True,
                "max_recommendations_per_user_per_week": 3,
                "publication_mode": "moderated",
                "install_popularity_threshold": 10,
                "notification_defaults": {
                    "skill_ready_to_share": True,
                    "teammate_published": True,
                    "update_available": True,
                },
                "manager_review_email_cadence": "daily",
                "not_now_suppression_days": 30,
                "version": 7,
                "updated_by_user_id": "manager-1",
            })
        elif path == "/v1/sync/wisdom/drafts":
            self._reply(200, {"drafts": [DRAFT] if DRAFT else []})
        elif DRAFT and path == f"/v1/sync/wisdom/drafts/{DRAFT['id']}":
            self._reply(200, {
                "draft": DRAFT,
                "effective_policy": {
                    "publication_policy": "moderated", "policy_version": 7,
                },
            })
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        global DRAFT, REMOTE_INSTALLED_VERSION
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
            body = {"multipart_bytes": len(raw)}
        else:
            body = json.loads(raw or b"{}")
        path = self._record("POST", body)
        if path == "/v1/sync/wisdom/installation-identities":
            self._reply(
                200,
                {"installation_id": body.get("installation_id"), "state": "active"},
            )
        elif path == "/v1/sync/wisdom/agent-led/suppressions/check":
            assert isinstance(body.get("keys"), list) and body["keys"]
            assert all(key.startswith("sha256:") for key in body["keys"])
            self._reply(200, {"org_id": "team-1", "suppressions": []})
        elif path == "/v1/sync/wisdom/installations":
            assert body == {
                "skill_id": "skill-1",
                "installation_id": EXPECTED_INSTALLATION_ID,
                "version": 2,
                "takedown_generation": 0,
                "update_mode": "AUTO_WITH_NOTICE",
            }
            REMOTE_INSTALLED_VERSION = 2
            self._reply(200, {
                "installed_version": 2,
                "effective_update_mode": "AUTO_WITH_NOTICE",
            })
        elif path == "/v1/sync/objects":
            assert body["multipart_bytes"] > 0
            self._reply(200, {"stored": 4, "already_present": 0})
        elif path == "/v1/sync/wisdom/drafts":
            assert EXPECTED_HASHES and EXPECTED_SYSTEM_SPEC
            DRAFT = {
                "id": "draft-remote-1", "orgId": "team-1", "ownerUserId": "member-1",
                "slug": body["slug"], "state": "ready", "draftCommit": body["draft_commit"],
                "contentHash": body["content_hash"],
                "authorDescription": body["author_description"],
                "authorDescriptionHash": EXPECTED_HASHES["author_description"],
                "authorDescriptionSchemaVersion": 1,
                "proposalN": None,
                "packageManifestHash": EXPECTED_HASHES["package_manifest"],
                "packageManifestSchemaVersion": 1,
                "systemSpec": EXPECTED_SYSTEM_SPEC,
                "scan": {"verdict": "pass"},
                "scanVerdict": "pass",
                "security_check": {
                    "schema_version": 1,
                    "status": "pass",
                    "summary": "No security concerns detected.",
                    "checks": [
                        {
                            "key": key, "label": label, "status": "pass",
                            "finding_count": 0, "details": [],
                        }
                        for key, label in (
                            ("private_keys", "Private keys"),
                            ("live_credentials", "Live credentials"),
                            ("organization_policy", "Organization policy"),
                            ("personal_information", "Personal information"),
                            ("secret_like_assignments", "Secret-like assignments"),
                        )
                    ],
                },
                "professionalism_check": body["professionalism_review"],
                "explanation": None,
                "explanationModel": None,
                "ownerApprovedAt": None,
                "moderationNote": None,
                "moderationDeciderUserId": None,
                "moderationDecidedAt": None,
                "createdAt": "2026-09-14T00:00:00Z", "updatedAt": "2026-09-14T00:00:00Z",
                "supersedesDraftId": None,
            }
            self._reply(201, {"draft": DRAFT})
        elif DRAFT and path == f"/v1/sync/wisdom/drafts/{DRAFT['id']}/approve":
            assert body == {
                "content_hash": EXPECTED_HASHES["content"],
                "author_description_hash": EXPECTED_HASHES["author_description"],
                "package_manifest_hash": EXPECTED_HASHES["package_manifest"],
            }
            DRAFT = {
                **DRAFT, "state": "owner_approved",
                "ownerApprovedAt": "2026-09-14T00:00:01Z",
                "updatedAt": "2026-09-14T00:00:01Z",
            }
            self._reply(200, {"draft": DRAFT})
        elif DRAFT and path == f"/v1/sync/wisdom/drafts/{DRAFT['id']}/publish":
            assert body == {"content_hash": EXPECTED_HASHES["content"], "base_commit": None}
            DRAFT = {
                **DRAFT, "state": "pending_moderation",
                "proposalN": 11,
                "updatedAt": "2026-09-14T00:00:02Z",
            }
            self._reply(202, {
                "proposal_id": 11,
                "state": "pending_moderation",
                "publication_outcome": "pending_review",
                "org_id": "team-1",
                "org_name": "Fixture Team",
                "review_url": None,
                "user_message": "Submitted for manager review; it is not yet available to the organization.",
            })
        elif DRAFT and path == f"/v1/sync/wisdom/drafts/{DRAFT['id']}/decline":
            DRAFT = {**DRAFT, "state": "declined", "updatedAt": "2026-09-14T00:00:03Z"}
            self._reply(200, {"state": "declined"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_PUT(self):
        global MUTE_REVISION
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        path = self._record("PUT", body)
        if path == "/v1/sync/wisdom/agent-led/mute":
            assert body["duration"] == "1_week"
            assert body["expected_revision"] == MUTE_REVISION
            requested = datetime.fromisoformat(body["requested_at"].replace("Z", "+00:00"))
            MUTE_REVISION += 1
            self._reply(200, {
                "org_id": "team-1", "muted": True, "duration": "1_week",
                "muted_until": (requested + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                "forever": False, "revision": MUTE_REVISION,
                "mutation_id": body["mutation_id"], "updated_at": body["requested_at"],
            })
        else:
            self._reply(404, {"error": "not_found"})

    def log_message(self, _format, *_args):
        return


def request_snapshot():
    with REQUEST_LOCK:
        return list(REQUESTS)


def state_snapshot(root):
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def encode_segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def persist_login(auth, gateway_url):
    global ACTIVE_TOKEN
    claims = {
        "sub": "member-1",
        "org_id": "team-1",
        "exp": time.time() + 600,
        "nbf": 0,
        "scope": "inference:invoke",
        "wisdom_scopes": ["wisdom:read", "wisdom:install", "wisdom:publish"],
    }
    ACTIVE_TOKEN = (
        encode_segment({"alg": "none"}) + "." + encode_segment(claims) + ".fixture"
    )
    auth._save_auth_store(
        {
            "providers": {
                "nous": {
                    "access_token": ACTIVE_TOKEN,
                    "refresh_token": "must-never-be-used",
                    "user_id": "member-1",
                    "org_id": "team-1",
                    "portal_base_url": "https://portal.example",
                }
            }
        }
    )
    os.environ["HERMES_SYNC_BASE_URL"] = gateway_url


def parse_json(output):
    value = json.loads(output)
    assert isinstance(value, (dict, list))
    return value


def contains(value, expected):
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(contains(item, expected) for item in value)
    return False


def registration_generation(manager):
    assert set(manager._cli_commands).intersection({"wisdom"}) == {"wisdom"}, manager._plugins
    assert set(manager._plugin_commands).intersection({"wisdom"}) == {"wisdom"}, manager._plugins
    registrations = [
        item
        for item in manager._ownership_ledger.get(PLUGIN_ID, ())
        if item.active
    ]
    keys = [(item.kind, item.key) for item in registrations]
    assert keys.count(("cli_command", "wisdom")) == 1
    assert keys.count(("command", "wisdom")) == 1
    assert sum(kind == "on_unload" for kind, _key in keys) == 1
    assert keys.count(("hook", "pre_gateway_dispatch")) == 1
    assert keys.count(("hook", "pre_llm_call")) == 1
    assert keys.count(("hook", "on_skill_lifecycle")) == 1
    assert keys.count(("hook", "on_session_end")) == 1
    assert keys.count(("auxiliary_task", "hermes_collective_wisdom_refinement")) == 1
    tool_names = {"wisdom_inbox", "wisdom_inspect", "present_wisdom_consent"}
    assert {key for kind, key in keys if kind == "tool"} == tool_names
    # Retain the generic host policy and reject duplicate registrations on reload.
    assert sum(kind == "tool_override_policy" for kind, _key in keys) == 1
    assert len(keys) == len(set(keys)) == 9 + len(tool_names), keys
    return manager._plugin_commands["wisdom"]["handler"].__self__


def run_cli(main, parser, subparsers, *arguments):
    args = main._parse_cli_args(parser, subparsers, ["wisdom", *arguments])
    assert callable(args.func)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = args.func(args)
        except SystemExit as exc:
            code = exc.code
    output = stdout.getvalue().strip()
    assert ACTIVE_TOKEN is None or ACTIVE_TOKEN not in output
    assert ACTIVE_TOKEN is None or ACTIVE_TOKEN not in stderr.getvalue()
    return code, output


def run_slash(server_module, command, request_id):
    response = server_module.handle_request(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "slash.exec",
            "params": {"session_id": "wisdom-probe", "command": command},
        }
    )
    assert response["id"] == request_id
    assert "error" not in response
    output = response["result"]["output"]
    assert isinstance(output, str) and output
    assert ACTIVE_TOKEN is None or ACTIVE_TOKEN not in output
    return output


gateway = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
gateway.daemon_threads = True
gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
gateway_thread.start()
gateway_url = f"http://127.0.0.1:{gateway.server_port}"
os.environ["HERMES_SYNC_BASE_URL"] = gateway_url

manager = None
server_module = None
try:
    from hermes_cli import auth

    assert Path(auth.__file__).resolve().is_relative_to(
        Path(os.environ["WISDOM_AGENT_DIR"]).resolve()
    )
    assert auth.resolve_nous_runtime_credentials.__name__ == "forbidden_refresh"
    auth._save_auth_store({"providers": {}})

    # Force the real parser's plugin-discovery branch instead of registering by hand.
    sys.argv[:] = ["hermes", "wisdom", "help"]
    from hermes_cli import main
    from hermes_cli.plugins import get_plugin_manager

    parser, subparsers = main._build_cli_parser()
    manager = get_plugin_manager()
    runtime = registration_generation(manager)
    loaded = manager._plugins[PLUGIN_ID]
    assert loaded.enabled is True
    assert loaded.module.__name__.startswith("hermes_plugins.")
    assert Path(loaded.module.__file__).resolve() == PROJECT_ROOT / "__init__.py"
    assert not (runtime.root / "wisdom.db").exists()
    assert request_snapshot() == []

    help_stdout = io.StringIO()
    with contextlib.redirect_stdout(help_stdout):
        try:
            main._parse_cli_args(parser, subparsers, ["wisdom", "--help"])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("argparse help did not exit")
    help_text = help_stdout.getvalue()
    for command in ("setup", "status", "list", "browse", "show", "versions"):
        assert command in help_text

    code, command_help = run_cli(main, parser, subparsers, "help")
    assert code == 0
    assert "Collective Wisdom" in command_help
    assert request_snapshot() == []
    assert not (runtime.root / "wisdom.db").exists()

    from tui_gateway import server as server_module

    server_module._sessions["wisdom-probe"] = {
        "session_key": "wisdom-probe",
        "profile_home": str(HOME),
        "agent": None,
    }
    slash_help = run_slash(server_module, "/wisdom help", 1)
    assert "/wisdom browse" in slash_help
    setup_requests = len(request_snapshot())
    slash_setup = run_slash(
        server_module, "/wisdom setup --accept-disclosure", 2
    )
    assert "hermes wisdom setup" in slash_setup
    assert len(request_snapshot()) == setup_requests == 0
    assert not (runtime.root / "wisdom.db").exists()

    code, denied = run_cli(main, parser, subparsers, "list", "--json")
    assert code not in (None, 0)
    denied_payload = parse_json(denied)
    assert denied_payload["ok"] is False
    assert request_snapshot() == []
    assert not (runtime.root / "wisdom.db").exists()

    persist_login(auth, gateway_url)
    code, setup_output = run_cli(
        main,
        parser,
        subparsers,
        "setup",
        "--accept-disclosure",
        "--json",
    )
    assert code == 0
    setup_payload = parse_json(setup_output)
    assert contains(setup_payload, "active")
    setup_calls = request_snapshot()
    assert [(item["method"], item["path"]) for item in setup_calls] == [
        ("GET", "/v1/sync/capabilities"),
        ("POST", "/v1/sync/wisdom/installation-identities"),
    ]
    assert all(item["authorized"] for item in setup_calls)
    installation_id = setup_calls[1]["body"]["installation_id"]
    EXPECTED_INSTALLATION_ID = installation_id
    assert installation_id.startswith("hwi_")
    assert contains(setup_payload, installation_id)
    assert (runtime.root / "wisdom.db").is_file()

    # Exercise every new command family through the real registered host parser
    # and the requests HTTP seam. Passive reads/status must not add requests.
    command_start = len(request_snapshot())
    code, maintenance_output = run_cli(
        main, parser, subparsers, "maintenance", "--json",
    )
    assert code == 0, maintenance_output
    assert parse_json(maintenance_output)["metadata"]["enriched"] == 1
    maintenance_calls = [
        (item["method"], item["path"])
        for item in request_snapshot()[command_start:]
    ]
    assert maintenance_calls == [
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
        ("GET", "/v1/sync/wisdom/feed"),
        ("GET", "/v1/sync/wisdom/skills/skill-1/versions/2"),
        ("GET", "/v1/sync/wisdom/agent-led/mute"),
        ("POST", "/v1/sync/wisdom/agent-led/suppressions/check"),
    ], maintenance_calls

    passive_start = len(request_snapshot())
    code, notifications_output = run_cli(
        main, parser, subparsers, "notifications", "--json",
    )
    assert code == 0, notifications_output
    assert parse_json(notifications_output)["events"][0]["event_id"] == "feed-event-1"
    code, inbox_output = run_cli(main, parser, subparsers, "inbox", "--json")
    assert code == 0, inbox_output
    inbox_payload = parse_json(inbox_output)
    assert inbox_payload["mode"] == "agent"
    assert [row["reference"]["event_id"] for row in inbox_payload["assessments"]] == [
        "feed-event-1"
    ]
    assert inbox_payload["interactions"] == []
    assert inbox_payload["installed_setup"] == []

    # Invoke the actual pinned-host registration with one admitted private route.
    from hermes_wisdom_plugin.native_context import NativeInvocation
    from tools.registry import registry

    admitted = NativeInvocation(
        platform="slack", profile="wisdom-probe", chat_type="dm", chat_id="D1",
        actor_id="U1", actor_name="Shannon", thread_id="T1", scope_id="W1",
        message_id="message-1", session_key="slack:W1:D1:U1", adapter=object(),
        host_session_id="host-session-1", task_id="host-task-1", turn_id="turn-1",
    )
    runtime.native.session_context = lambda: {
        "platform": "slack", "profile": "wisdom-probe", "chat_id": "D1",
        "user_id": "U1", "thread_id": "T1", "scope_id": "W1",
        "message_id": "message-1", "session_key": "slack:W1:D1:U1",
        "session_id": "host-session-1",
    }
    runtime.native._tool_invocations[(  # pylint: disable=protected-access
        "host-task-1", "host-session-1"
    )] = (float("inf"), admitted)
    inbox_tool = registry.get_entry("wisdom_inbox", scope=manager.scope_key)
    assert inbox_tool is not None
    tool_payload = parse_json(inbox_tool.handler(
        {}, task_id="host-task-1", session_id="host-session-1"
    ))
    assert tool_payload == inbox_payload
    assert request_snapshot()[passive_start:] == []
    code, mute_status_output = run_cli(
        main, parser, subparsers, "mute", "status", "--json",
    )
    assert code == 0, mute_status_output
    code, sync_status_output = run_cli(
        main, parser, subparsers, "sync", "status", "--json",
    )
    assert code == 0, sync_status_output
    assert request_snapshot()[passive_start:] == []

    mute_start = len(request_snapshot())
    code, mute_output = run_cli(main, parser, subparsers, "mute", "1w", "--json")
    assert code == 0, mute_output
    assert parse_json(mute_output)["mute"]["duration"] == "1_week"
    assert [(item["method"], item["path"]) for item in request_snapshot()[mute_start:]] == [
        ("GET", "/v1/sync/wisdom/agent-led/mute"),
        ("PUT", "/v1/sync/wisdom/agent-led/mute"),
        ("GET", "/v1/sync/wisdom/agent-led/mute"),
    ]
    retry_start = len(request_snapshot())
    code, retry_output = run_cli(main, parser, subparsers, "sync", "retry", "--json")
    assert code == 0, retry_output
    assert request_snapshot()[retry_start:] == []

    # Seed a real managed v1 tree and drive policy-authorized v2 replacement
    # through the registered host parser/controller and recording HTTP boundary.
    seed_service = runtime._service()
    package_name = seed_service.__class__.__module__.rsplit(".", 1)[0]
    contract_module = importlib.import_module(package_name + ".contract")
    owner = seed_service._installation_owner()
    managed_target = (
        seed_service.profile[1] / "skills" / "_wisdom"
        / contract_module.org_directory_name(owner["organization_id"])
        / "release-notes"
    )
    managed_target.mkdir(parents=True)
    for file_path, _mode, body in UPDATE_FILES[1]:
        destination = managed_target / file_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
    seed_service._onboarding.store.record_install({
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "release-notes", "version": 1,
        "content_hash": content_hash(UPDATE_FILES[1]),
        "baseline": {path: wire_hash(body) for path, _mode, body in UPDATE_FILES[1]},
        "target_path": str(managed_target), "update_mode": "AUTO_WITH_NOTICE",
        "state": "active", "owner": owner,
    })
    seed_service.close()
    runtime._services.discard(seed_service)
    automatic_start = len(request_snapshot())
    code, automatic_output = run_cli(
        main, parser, subparsers,
        "maintenance", "--apply-automatic", "--json",
    )
    assert code == 0, automatic_output
    automatic = parse_json(automatic_output)["automatic_updates"]
    assert automatic["results"][0]["state"] == "updated", automatic
    assert automatic["results"][0]["result"]["effective_update_mode"] == "AUTO_WITH_NOTICE"
    assert managed_target.joinpath("SKILL.md").read_bytes() == b"# Release notes v2\n"
    automatic_calls = [
        (item["method"], item["path"])
        for item in request_snapshot()[automatic_start:]
    ]
    assert automatic_calls == [
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
        ("GET", "/v1/sync/wisdom/feed"),
        ("GET", f"/v1/sync/wisdom/installations/{installation_id}"),
        ("GET", f"/v1/sync/wisdom/installations/{installation_id}"),
        ("GET", "/v1/sync/wisdom/skills/skill-1"),
        ("GET", "/v1/sync/wisdom/skills/skill-1/versions/2"),
        ("GET", "/v1/sync/wisdom/skills/skill-1/versions/2/content"),
        ("GET", f"/v1/sync/wisdom/installations/{installation_id}"),
        ("GET", "/v1/sync/wisdom/skills/skill-1"),
        ("GET", "/v1/sync/wisdom/skills/skill-1/versions/2"),
        ("GET", "/v1/sync/wisdom/skills/skill-1/versions/2/content"),
        ("GET", f"/v1/sync/wisdom/installations/{installation_id}"),
        ("POST", "/v1/sync/wisdom/installations"),
    ], automatic_calls
    local_notice_start = len(request_snapshot())
    code, updated_notifications = run_cli(
        main, parser, subparsers, "notifications", "--json",
    )
    assert code == 0, updated_notifications
    assert any(
        event["event_id"].startswith("automatic-update:")
        and event["category"] == "updated"
        for event in parse_json(updated_notifications)["events"]
    )
    assert request_snapshot()[local_notice_start:] == []

    # Prove the public host tool dispatch -> skill_usage fire-site -> registered
    # on_skill_lifecycle hook -> plugin database path in this disposable profile.
    lifecycle_skill = HOME / "skills" / "lifecycle-proof"
    lifecycle_skill.mkdir(parents=True)
    (lifecycle_skill / "SKILL.md").write_text(
        "---\nname: lifecycle-proof\ndescription: Prove lifecycle hooks.\n---\n# Lifecycle\n",
        encoding="utf-8",
    )
    from model_tools import handle_function_call

    viewed = parse_json(handle_function_call(
        "skill_view", {"name": "lifecycle-proof"},
        task_id="lifecycle-task", session_id="lifecycle-session",
    ))
    assert viewed["success"] is True
    with sqlite3.connect(runtime.root / "wisdom.db") as lifecycle_db:
        assert lifecycle_db.execute("SELECT SUM(use_count) FROM usage_day").fetchone()[0] == 1

    managed = parse_json(handle_function_call(
        "skill_manage", {
            "action": "patch", "name": "lifecycle-proof",
            "old_string": "# Lifecycle", "new_string": "# Lifecycle updated",
        }, task_id="mutation-task", session_id="mutation-session",
    ))
    if managed.get("staged"):
        from tools import write_approval
        from tools.skill_manager_tool import apply_skill_pending

        pending = write_approval.get_pending(write_approval.SKILLS, managed["pending_id"])
        assert pending is not None
        applied = parse_json(apply_skill_pending(pending["payload"]))
        assert applied["success"] is True
        assert write_approval.discard_pending(write_approval.SKILLS, managed["pending_id"])
    else:
        assert managed["success"] is True
    with sqlite3.connect(runtime.root / "wisdom.db") as lifecycle_db:
        assert lifecycle_db.execute("SELECT COUNT(*) FROM refinement").fetchone()[0] == 1

    weekly_requests = len(request_snapshot())
    code, weekly_output = run_cli(
        main, parser, subparsers, "review-week", "--dry-run", "--json",
    )
    assert code == 0, weekly_output
    weekly = parse_json(weekly_output)
    assert weekly["dry_run"] is True and weekly["queued"] is False
    assert [(item["method"], item["path"]) for item in request_snapshot()[weekly_requests:]] == [
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
    ]

    # Keep the host's real PluginLlm -> call_llm -> configured default-model
    # route. Replace only its OpenAI/httpx network client with a recording
    # transport; no model or Gateway receives the private package.
    import agent.process_bootstrap as process_bootstrap

    model_requests = []

    def recording_model_response(request):
        payload = json.loads(request.content)
        model_requests.append({"url": str(request.url), "payload": payload})
        schema_name = ""
        for message in payload["messages"]:
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if isinstance(content, str) and "Schema name:" in content:
                schema_name = content.split("Schema name:", 1)[1].splitlines()[0].strip()
        if schema_name == "wisdom_editorial_metadata":
            result = {
                "editorial_name": "Incident Review",
                "editorial_description": "Review incident notes with a consistent checklist.",
            }
        elif schema_name == "collective_wisdom.refinement":
            result = {"classification": "meaningful"}
        elif schema_name == "collective_wisdom.recommendations":
            request_input = next(
                part["text"]
                for message in payload["messages"]
                for part in (
                    message.get("content", [])
                    if isinstance(message.get("content"), list)
                    else []
                )
                if isinstance(part, dict) and "untrusted_evidence" in part.get("text", "")
            )
            evidence = json.loads(request_input)["untrusted_evidence"]
            result = {"advice": [{
                "assessment_id": item["assessment_id"],
                "relevance": "recommend",
                "title": "Release Notes",
                "explanation": "Supports a recurring release workflow.",
            } for item in evidence]}
        else:
            assert schema_name == "wisdom_professionalism_review", schema_name
            result = {
                "status": "pass",
                "summary": "No professionalism concerns detected.",
                "checks": [
                    {"key": key, "status": "pass", "finding_count": 0, "details": []}
                    for key in (
                        "profanity_or_abuse", "hate_or_harassment",
                        "sexual_or_graphic_language", "manipulative_or_spam",
                    )
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "recorded", "object": "chat.completion", "created": 1,
                "model": "configured-review-model",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(result)},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def recording_http_client(_base_url, *, async_mode=False, verify=True):
        del verify
        assert async_mode is False
        return httpx.Client(transport=httpx.MockTransport(recording_model_response))

    assert runtime.llm._sync_caller is None
    process_bootstrap.build_keepalive_http_client = recording_http_client

    # Older hosts have no local presentation capability: hooks must not assess.
    # Exercise the assessment kernel explicitly to retain real PluginLLM transport
    # coverage; the separate presentation-host probe proves automatic delivery.
    def assess_with_pinned_transport(session_id, history):
        service = runtime._service()
        try:
            bridge = importlib.import_module(package_name + ".mediation")
            bridge.QueuedAssessment(
                service, service._installation_owner(), runtime.llm,
                get_delivery_mode=lambda: "agent",
            ).run(
                session_key="local:" + session_id, session_id=session_id,
                platform="local", actor_id=owner["account_id"],
                model="configured-review-model", history=history,
                address={"chat_id": "terminal"},
            )
        finally:
            service.close()
    from agent.auxiliary_client import reset_runtime_main, set_runtime_main
    from hermes_cli.lifecycle import invoke_hook
    MUTE_REVISION = 0
    invoke_hook(
        "pre_llm_call", platform="cli", session_id="assessment-session",
        task_id="assessment-task", turn_id="assessment-turn-missing-runtime",
        model="configured-review-model", parent_session_id="", sender_id="",
        conversation_history=[{"role": "user", "content": "private missing route"}],
    )
    assert model_requests == []
    with sqlite3.connect(runtime.root / "wisdom.db") as db:
        db.execute(
            "UPDATE wisdom_shared_assessment SET available_at=0 "
            "WHERE state='pending'"
        )
    wrong_route = set_runtime_main(
        "custom:recording", "configured-review-model",
        base_url="https://recording.invalid/v1", api_key="recording-key",
        api_mode="chat_completions", session_id="different-session",
    )
    try:
        invoke_hook(
            "pre_llm_call", platform="cli", session_id="assessment-session",
            task_id="assessment-task", turn_id="assessment-turn-wrong-runtime",
            model="configured-review-model", parent_session_id="", sender_id="",
            conversation_history=[{"role": "user", "content": "private wrong route"}],
        )
    finally:
        reset_runtime_main(wrong_route)
    assert model_requests == []
    with sqlite3.connect(runtime.root / "wisdom.db") as db:
        db.execute(
            "UPDATE wisdom_shared_assessment SET available_at=0 "
            "WHERE state='pending'"
        )
    route_token = set_runtime_main(
        "custom:recording", "configured-review-model",
        base_url="https://recording.invalid/v1", api_key="recording-key",
        api_mode="chat_completions", session_id="assessment-session",
    )
    try:
        invoke_hook(
            "pre_llm_call", platform="cli", session_id="assessment-session",
            task_id="assessment-task", turn_id="assessment-turn",
            model="configured-review-model", parent_session_id="", sender_id="",
            conversation_history=[
                {"role": "system", "content": "private system"},
                {"role": "tool", "content": "private tool result"},
                {"role": "user", "content": "We prepare releases every week."},
            ],
        )
        assert model_requests == []
        assess_with_pinned_transport("assessment-session", [
            {"role": "system", "content": "private system"},
            {"role": "tool", "content": "private tool result"},
            {"role": "user", "content": "We prepare releases every week."},
        ])
    finally:
        reset_runtime_main(route_token)
    assert len(model_requests) == 1
    assessment_payload = model_requests[0]["payload"]
    assert assessment_payload["model"] == "configured-review-model"
    assert "tools" not in assessment_payload
    assessment_text = "\n".join(
        part.get("text", "")
        for message in assessment_payload["messages"]
        for part in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(part, dict)
    )
    assessment_input = next(
        json.loads(line) for line in assessment_text.splitlines()
        if line.startswith("{") and "untrusted_evidence" in line
    )
    assert assessment_input["conversation_excerpts"] == [
        {"role": "user", "content": "We prepare releases every week."}
    ]
    assert "private system" not in assessment_text
    assert "private tool result" not in assessment_text
    assessment_service = runtime._service()
    try:
        assessed_inbox = assessment_service.inbox()
    finally:
        assessment_service.close()
    assessed = assessed_inbox["assessments"][0]
    assert assessed["state"] == "ready"
    assert assessed["advice"]["provenance"] == {
        "kind": "agent_assessed", "provider": "custom:recording",
        "model": "configured-review-model",
    }
    model_requests.clear()

    with sqlite3.connect(runtime.root / "wisdom.db") as db:
        db.execute(
            "UPDATE wisdom_shared_assessment SET state='pending',advice_json=NULL,"
            "attempts=0,available_at=0,lease_token=NULL,lease_until=NULL "
            "WHERE id=?",
            (assessed["id"],),
        )
    tui_route = set_runtime_main(
        "custom:recording", "configured-review-model",
        base_url="https://recording.invalid/v1", api_key="recording-key",
        api_mode="chat_completions", session_id="tui-assessment-session",
    )
    try:
        invoke_hook(
            "pre_llm_call", platform="tui", session_id="tui-assessment-session",
            task_id="tui-assessment-task", turn_id="tui-assessment-turn",
            model="configured-review-model", parent_session_id="", sender_id="",
            conversation_history=[
                {"role": "user", "content": "The TUI also prepares weekly releases."},
            ],
        )
        assert model_requests == []
        assess_with_pinned_transport("tui-assessment-session", [
            {"role": "user", "content": "The TUI also prepares weekly releases."},
        ])
    finally:
        reset_runtime_main(tui_route)
    assert len(model_requests) == 1
    assert model_requests[0]["payload"]["model"] == "configured-review-model"
    model_requests.clear()

    # Drive the actual pinned host lifecycle choke points. The mutation hook only
    # records the exact ambiguous snapshot; model work starts at turn completion.
    from tools.skill_usage import bump_patch, bump_use

    refinement_skill = HOME / "skills" / "refinement-probe"
    refinement_skill.mkdir(parents=True)
    refinement_before = (
        "---\nname: refinement-probe\n---\n# Review incidents\n\n"
        "```python\ndef owner(record):\n    return record.reporter\n```\n"
    )
    refinement_after = refinement_before.replace("record.reporter", "record.remediation_owner")
    (refinement_skill / "SKILL.md").write_text(refinement_before, encoding="utf-8")
    invoke_hook(
        "pre_llm_call", platform="cli", session_id="refinement-session",
        task_id="refinement-task", turn_id="refinement-turn", model="configured-review-model",
        parent_session_id="", sender_id="",
    )
    bump_use(
        "refinement-probe", task_id="refinement-task", session_id="refinement-session"
    )
    (refinement_skill / "SKILL.md").write_text(refinement_after, encoding="utf-8")
    bump_patch(
        "refinement-probe", task_id="refinement-task", session_id="refinement-session"
    )
    assert model_requests == []
    invoke_hook(
        "on_session_end", platform="cli", session_id="refinement-session",
        task_id="refinement-task", turn_id="refinement-turn", completed=True,
        failed=False, interrupted=False, turn_exit_reason="text_response(stop)",
        model="configured-review-model",
    )
    assert len(model_requests) == 1
    refinement_payload = model_requests[0]["payload"]
    assert refinement_payload["model"] == "configured-review-model"
    assert "tools" not in refinement_payload
    refinement_inputs = []
    for message in refinement_payload["messages"]:
        content = message.get("content")
        parts = content if isinstance(content, list) else [{"text": content}]
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if not isinstance(text, str):
                continue
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "structural_diff" in candidate:
                refinement_inputs.append(candidate)
    assert refinement_inputs == [{
        "before": refinement_before,
        "after": refinement_after,
        "structural_diff": {"added": [], "removed": [], "changed": ["SKILL.md"]},
    }]
    with sqlite3.connect(runtime.root / "wisdom.db") as db:
        refinement_row = db.execute(
            "SELECT r.classification,r.structural_json FROM refinement r "
            "JOIN local_skill s ON s.id=r.skill_id WHERE s.canonical_path=?",
            (str(refinement_skill.resolve()),),
        ).fetchone()
        assert refinement_row[0] == "meaningful", refinement_row
        assert db.execute("SELECT COUNT(*) FROM stability_job").fetchone()[0] == 1
    model_requests.clear()

    local_only_skill = HOME / "skills" / "local-only-review"
    local_only_skill.mkdir(parents=True)
    local_only_bytes = (
        "---\nname: local-only-review\ndescription: Keep this review local.\n---\n"
        "# Local only\nDo not disclose this package.\n"
    ).encode()
    (local_only_skill / "SKILL.md").write_bytes(local_only_bytes)
    requests_before_contribution = request_snapshot()
    code, local_only_output = run_cli(
        main, parser, subparsers, "suggest", "local-only-review", "--json",
    )
    assert code == 0, local_only_output
    assert parse_json(local_only_output)["professionalism_check"]["status"] == "unavailable"
    assert model_requests == []
    assert request_snapshot() == requests_before_contribution
    assert (local_only_skill / "SKILL.md").read_bytes() == local_only_bytes

    local_skill = HOME / "skills" / "incident-review"
    local_skill.mkdir(parents=True)
    original_local = (
        "---\nname: incident-review\ndescription: Review incident notes.\n---\n"
        "# Incident review\nUse the checklist.\n"
    ).encode()
    (local_skill / "SKILL.md").write_bytes(original_local)
    code, contribution_output = run_cli(
        main, parser, subparsers, "suggest", "incident-review",
        "--allow-model-review", "--json",
    )
    assert code == 0, contribution_output
    contribution = parse_json(contribution_output)
    assert contribution["state"] == "prepared"
    assert contribution["network_submission"] is False
    assert contribution["professionalism_check"]["status"] == "pass"
    assert contribution["professionalism_check"]["provenance"] == {
        "kind": "agent_assessed", "provider": "custom:recording",
        "model": "configured-review-model",
    }
    assert len(model_requests) == 2
    assert all(item["url"] == "https://recording.invalid/v1/chat/completions" for item in model_requests)
    assert all(item["payload"]["model"] == "configured-review-model" for item in model_requests)
    assert all("tools" not in item["payload"] for item in model_requests)
    professionalism_input = next(
        part["text"]
        for message in model_requests[1]["payload"]["messages"]
        for part in message.get("content", [])
        if isinstance(part, dict) and "untrusted_instruction_package" in part.get("text", "")
    )
    disclosed = json.loads(professionalism_input)
    assert disclosed == {
        "untrusted_instruction_package": [
            {"path": item["path"], "content_utf8": item["content_utf8"]}
            for item in contribution["files"]
        ],
        "untrusted_owner_description": "Review incident notes.",
    }
    assert request_snapshot() == requests_before_contribution
    assert (local_skill / "SKILL.md").read_bytes() == original_local
    assert Path(contribution["overlay_path"]).is_relative_to(runtime.root)
    assert contribution["publisher_evidence"] is None

    # Exercise the real registered terminal adapter through inspect -> edit -> save
    # -> local recheck, then explicitly authorized model re-review. The existing
    # transport records the default PluginLlm path without replacing _sync_caller.
    code, inspect_output = run_cli(
        main, parser, subparsers, "prepared", "inspect",
        contribution["draft_id"], "--json",
    )
    assert code == 0, inspect_output
    editable = parse_json(inspect_output)
    assert set(editable) == {
        "draft_id", "author_description", "files", "expected_hashes"
    }
    editable["author_description"] = "Owner-approved incident review copy."
    for item in editable["files"]:
        if item["path"] == "SKILL.md":
            item["content_utf8"] = item["content_utf8"].replace(
                "Use the checklist.", "Use the owner-reviewed checklist."
            )
    payload_path = HOME / "prepared-edit.json"
    payload_path.write_text(json.dumps(editable), encoding="utf-8")
    requests_before_save = len(model_requests)
    code, save_output = run_cli(
        main, parser, subparsers, "prepared", "save", str(payload_path), "--json",
    )
    assert code == 0, save_output
    saved = parse_json(save_output)
    assert saved["hashes"] != contribution["hashes"]
    assert saved["professionalism_check"]["status"] == "unavailable"
    assert len(model_requests) == requests_before_save
    assert (local_skill / "SKILL.md").read_bytes() == original_local

    code, recheck_output = run_cli(
        main, parser, subparsers, "prepared", "review",
        contribution["draft_id"], "--json",
    )
    assert code == 0, recheck_output
    assert parse_json(recheck_output)["professionalism_check"]["status"] == "unavailable"
    assert len(model_requests) == requests_before_save
    code, reviewed_output = run_cli(
        main, parser, subparsers, "prepared", "review",
        contribution["draft_id"], "--allow-model-review", "--json",
    )
    assert code == 0, reviewed_output
    reviewed_edit = parse_json(reviewed_output)
    assert reviewed_edit["professionalism_check"]["status"] == "pass"
    assert reviewed_edit["professionalism_check"]["content_hash"] == saved["hashes"]["content"]
    assert len(model_requests) == requests_before_save + 1
    assert request_snapshot() == requests_before_contribution

    # Use the real registered host command path for the final local review and the
    # only explicit send confirmation. The recording server implements the pinned
    # Gateway route/state contract while retaining every emitted request.
    publication_requests = len(request_snapshot())
    code, publication_review_output = run_cli(
        main, parser, subparsers, "publication", "review",
        contribution["draft_id"], "--json",
    )
    assert code == 0, publication_review_output
    publication_review = parse_json(publication_review_output)
    assert publication_review["network_submission"] is False
    assert publication_review["publication_mode"] == "moderated"
    assert publication_review["hashes"] == reviewed_edit["hashes"]
    assert publication_review["professionalism_check"]["status"] == "pass"
    assert [(item["method"], item["path"]) for item in request_snapshot()[publication_requests:]] == [
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
    ]
    EXPECTED_HASHES = publication_review["hashes"]
    EXPECTED_SYSTEM_SPEC = publication_review["system_specification"]
    code, publication_submit_output = run_cli(
        main, parser, subparsers, "publication", "submit",
        publication_review["receipt"], "--json",
    )
    assert code == 0, publication_submit_output
    publication_result = parse_json(publication_submit_output)
    assert publication_result["state"] == "done"
    assert publication_result["publication_state"] == "pending_moderation"
    publication_operation = publication_result["operation_id"]
    publication_calls = request_snapshot()[publication_requests + 1:]
    assert [(item["method"], item["path"]) for item in publication_calls] == [
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
        ("GET", "/v1/sync/wisdom/agent-led/policy"),
        ("POST", "/v1/sync/objects"),
        ("POST", "/v1/sync/wisdom/drafts"),
        ("GET", "/v1/sync/wisdom/drafts/draft-remote-1"),
        ("POST", "/v1/sync/wisdom/drafts/draft-remote-1/approve"),
        ("POST", "/v1/sync/wisdom/drafts/draft-remote-1/publish"),
    ]
    assert all(item["authorized"] for item in publication_calls)
    assert (local_skill / "SKILL.md").read_bytes() == original_local

    requests_before_status = request_snapshot()
    code, status_output = run_cli(main, parser, subparsers, "status", "--json")
    assert code == 0
    status_payload = parse_json(status_output)
    assert status_payload["configured"] is True
    assert contains(status_payload, installation_id)
    assert request_snapshot() == requests_before_status

    cli_cases = (
        (("list", "--json"), "skill-1"),
        (("browse", "release", "--json"), "release-notes"),
        (("show", "skill-1", "--json"), "release-notes"),
        (("versions", "skill-1", "--json"), 2),
    )
    for arguments, expected in cli_cases:
        code, output = run_cli(main, parser, subparsers, *arguments)
        assert code == 0
        assert contains(parse_json(output), expected)

    slash_requests = len(request_snapshot())
    slash_browse = run_slash(server_module, "/wisdom browse release", 3)
    assert "release-notes" in slash_browse
    slash_show = run_slash(server_module, "/wisdom show skill-1", 4)
    assert "release-notes" in slash_show
    slash_versions = run_slash(server_module, "/wisdom versions skill-1", 5)
    assert "Version 2" in slash_versions
    assert len(request_snapshot()) > slash_requests
    assert not runtime._children

    # A host ContextVar keeps the parent bound while the child must receive the
    # captured profile explicitly rather than inheriting an unrelated ambient env.
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    ambient_home = os.environ["HERMES_HOME"]
    other_profile = HOME.parent / "empty-ambient-profile"
    other_profile.mkdir()
    profile_token = set_hermes_home_override(HOME)
    os.environ["HERMES_HOME"] = str(other_profile)
    context_requests = len(request_snapshot())
    try:
        assert get_hermes_home().resolve() == HOME
        state = auth.get_provider_auth_state("nous") or {}
        assert state.get("access_token") == ACTIVE_TOKEN, "host credential context was lost"
        context_browse = run_slash(server_module, "/wisdom browse release", 6)
        assert "release-notes" in context_browse, context_browse
        assert len(request_snapshot()) > context_requests
        from hermes_cli.plugins import resolve_plugin_command_result

        async def resolve_from_running_loop():
            handler = manager._plugin_commands["wisdom"]["handler"]
            return resolve_plugin_command_result(handler("browse release"))

        # The host's running-loop resolver uses a helper thread. The plugin must
        # capture the calling ContextVar before returning its awaitable.
        threaded_browse = asyncio.run(resolve_from_running_loop())
        assert "release-notes" in threaded_browse, threaded_browse
    finally:
        os.environ["HERMES_HOME"] = ambient_home
        reset_hermes_home_override(profile_token)
    assert get_hermes_home().resolve() == HOME
    assert not list(other_profile.rglob("nous_auth.json"))
    assert not list(other_profile.rglob("wisdom.db"))
    assert not runtime._children

    post_count = sum(item["method"] == "POST" for item in request_snapshot())
    state_before_slash_setup = state_snapshot(runtime.root)
    slash_setup = run_slash(
        server_module, "/wisdom setup --accept-disclosure", 7
    )
    assert "hermes wisdom setup" in slash_setup
    assert sum(item["method"] == "POST" for item in request_snapshot()) == post_count
    # The snapshot comparison above checks the relevant boundary directly:
    # slash setup must not add a POST, regardless of earlier assessment calls.
    assert state_snapshot(runtime.root) == state_before_slash_setup

    first_runtime = runtime
    DRAFT = {
        **DRAFT,
        "state": "changes_requested",
        "moderationNote": "Clarify the owner-reviewed checklist.",
        "moderationDeciderUserId": "manager-1",
        "moderationDecidedAt": "2026-09-14T00:00:04Z",
        "updatedAt": "2026-09-14T00:00:04Z",
    }
    manager.discover_and_load(force=True)
    runtime = registration_generation(manager)
    parser, subparsers = main._build_cli_parser()
    assert runtime is not first_runtime
    assert first_runtime._closed is True
    assert not first_runtime._children
    code, durable_output = run_cli(
        main, parser, subparsers, "publication", "status",
        publication_operation, "--json",
    )
    assert code == 0, durable_output
    assert parse_json(durable_output)["publication_state"] == "pending_moderation"
    code, reopened_output = run_cli(
        main, parser, subparsers, "publication", "reopen",
        publication_operation, "--json",
    )
    assert code == 0, reopened_output
    assert parse_json(reopened_output)["publication_state"] == "changes_requested"

    assert manager.unload(PLUGIN_ID) is True
    assert "wisdom" not in manager._cli_commands
    assert "wisdom" not in manager._plugin_commands
    assert runtime._closed is True
    assert not runtime._children

    manager.discover_and_load(force=True)
    runtime = registration_generation(manager)
    assert "Collective Wisdom" in run_slash(server_module, "/wisdom help", 8)
    assert not runtime._children
    assert not any(
        name == "hermes_wisdom" or name.startswith("hermes_wisdom.")
        for name in sys.modules
    )
    print("clean-host Wisdom command integration passed", file=sys.__stdout__)
finally:
    if server_module is not None:
        server_module._sessions.pop("wisdom-probe", None)
    if manager is not None:
        manager.unload()
    gateway.shutdown()
    gateway.server_close()
    gateway_thread.join(timeout=2)
    assert not gateway_thread.is_alive()
'''


def test_clean_host_native_cli_and_tui_commands(tmp_path: Path) -> None:
    """Load, dispatch, reload and unload Wisdom through an unmodified host."""

    with immutable_host_checkout(
        EXPECTED_HOST_REVISION, required_path="hermes_cli/plugins.py"
    ) as agent_path:
        profile = tmp_path / ".hermes" / "profiles" / "wisdom-probe"
        plugins = profile / "plugins"
        plugins.mkdir(parents=True)
        (plugins / PLUGIN_ID).symlink_to(PROJECT_ROOT, target_is_directory=True)
        (profile / "config.yaml").write_text(
            f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n"
            f"  entries:\n    {PLUGIN_ID}:\n      settings:\n"
            "        qualification:\n          allow_model_review: true\n"
            "model:\n  provider: custom:recording\n  default: configured-review-model\n"
            "auxiliary:\n"
            "  hermes_collective_wisdom_refinement:\n"
            "    provider: custom:recording\n"
            "    model: configured-review-model\n"
            "custom_providers:\n"
            "  - name: recording\n"
            "    base_url: https://recording.invalid/v1\n"
            "    api_key: recording-key\n"
            "    model: configured-review-model\n",
            encoding="utf-8",
        )
        refresh_guard = write_no_auth_refresh_sitecustomize(tmp_path)
        environment = isolated_host_environment(
            home=tmp_path,
            hermes_home=profile,
            host_path=agent_path,
            python_paths=(refresh_guard, PROJECT_ROOT, agent_path),
        )
        environment["WISDOM_PLUGIN_ROOT"] = str(PROJECT_ROOT)
        result = run_host_python(
            ["-c", PROBE], cwd=tmp_path, environment=environment, timeout=90
        )
        assert result.stdout.strip() == "clean-host Wisdom command integration passed"
