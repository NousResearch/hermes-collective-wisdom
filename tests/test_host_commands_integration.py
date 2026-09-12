"""Clean-host integration probe for native Wisdom CLI and TUI commands."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"
PLUGIN_ID = "hermes-collective-wisdom"
PROBE = r'''
import base64
import contextlib
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = "hermes-collective-wisdom"
PROJECT_ROOT = Path(os.environ["WISDOM_PLUGIN_ROOT"]).resolve()
HOME = Path(os.environ["HERMES_HOME"]).resolve()
REQUESTS = []
REQUEST_LOCK = threading.Lock()
ACTIVE_TOKEN = None

SKILL = {
    "id": "skill-1",
    "slug": "release-notes",
    "state": "published",
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
                        "system_spec": None,
                        "security_check": {"state": "pass"},
                        "professionalism_check": {"state": "pass"},
                    },
                },
            )
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        path = self._record("POST", body)
        if path == "/v1/sync/wisdom/installation-identities":
            self._reply(
                200,
                {"installation_id": body.get("installation_id"), "state": "active"},
            )
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
        "wisdom_scopes": ["wisdom:read", "wisdom:install"],
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
    assert set(manager._cli_commands).intersection({"wisdom"}) == {"wisdom"}
    assert set(manager._plugin_commands).intersection({"wisdom"}) == {"wisdom"}
    registrations = [
        item
        for item in manager._ownership_ledger.get(PLUGIN_ID, ())
        if item.active
    ]
    keys = [(item.kind, item.key) for item in registrations]
    assert keys.count(("cli_command", "wisdom")) == 1
    assert keys.count(("command", "wisdom")) == 1
    assert sum(kind == "on_unload" for kind, _key in keys) == 1
    assert len(keys) == len(set(keys)) == 3
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
    assert installation_id.startswith("hwi_")
    assert contains(setup_payload, installation_id)
    assert (runtime.root / "wisdom.db").is_file()

    code, status_output = run_cli(main, parser, subparsers, "status", "--json")
    assert code == 0
    status_payload = parse_json(status_output)
    assert status_payload["configured"] is True
    assert contains(status_payload, installation_id)
    assert request_snapshot() == setup_calls

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

    post_count = sum(item["method"] == "POST" for item in request_snapshot())
    state_before_slash_setup = state_snapshot(runtime.root)
    slash_setup = run_slash(
        server_module, "/wisdom setup --accept-disclosure", 6
    )
    assert "hermes wisdom setup" in slash_setup
    assert sum(item["method"] == "POST" for item in request_snapshot()) == post_count
    assert post_count == 1
    assert state_snapshot(runtime.root) == state_before_slash_setup

    first_runtime = runtime
    manager.discover_and_load(force=True)
    runtime = registration_generation(manager)
    assert runtime is not first_runtime
    assert first_runtime._closed is True
    assert not first_runtime._children

    assert manager.unload(PLUGIN_ID) is True
    assert "wisdom" not in manager._cli_commands
    assert "wisdom" not in manager._plugin_commands
    assert runtime._closed is True
    assert not runtime._children

    manager.discover_and_load(force=True)
    runtime = registration_generation(manager)
    assert "Collective Wisdom" in run_slash(server_module, "/wisdom help", 7)
    assert not runtime._children
    assert not any(
        name == "hermes_wisdom" or name.startswith("hermes_wisdom.")
        for name in sys.modules
    )
    print("clean-host Wisdom command integration passed")
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


def _git(agent_path: Path, *arguments: str) -> str:
    """Read one fact from the pinned, unmodified host checkout."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=agent_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"{result.stderr}\nstdout:\n{result.stdout}"
    return result.stdout.strip()


def test_clean_host_native_cli_and_tui_commands(tmp_path: Path) -> None:
    """Load, dispatch, reload and unload Wisdom through an unmodified host."""

    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to the unmodified Agent checkout")
    agent_path = Path(agent_dir).resolve()
    assert (agent_path / "hermes_cli" / "plugins.py").is_file()
    assert _git(agent_path, "rev-parse", "HEAD") == EXPECTED_HOST_REVISION
    assert _git(agent_path, "status", "--porcelain") == ""

    profile = tmp_path / "profile"
    plugins = profile / "plugins"
    plugins.mkdir(parents=True)
    (plugins / PLUGIN_ID).symlink_to(PROJECT_ROOT, target_is_directory=True)
    (profile / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n", encoding="utf-8"
    )

    refresh_guard = tmp_path / "refresh-guard"
    refresh_guard.mkdir()
    (refresh_guard / "sitecustomize.py").write_text(
        "from hermes_cli import auth\n"
        "def forbidden_refresh(*_args, **_kwargs):\n"
        "    raise AssertionError('Wisdom commands must not refresh credentials')\n"
        "auth.resolve_nous_runtime_credentials = forbidden_refresh\n",
        encoding="utf-8",
    )

    paths = [str(refresh_guard), str(PROJECT_ROOT), str(agent_path)]
    if dependencies := os.environ.get("WISDOM_HOST_DEPENDENCIES"):
        paths.append(dependencies)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(profile),
        "WISDOM_AGENT_DIR": str(agent_path),
        "WISDOM_PLUGIN_ROOT": str(PROJECT_ROOT),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONNOUSERSITE": "1",
    }
    result = subprocess.run(
        [os.environ.get("WISDOM_HOST_PYTHON", sys.executable), "-c", PROBE],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean-host Wisdom command integration passed"
    assert _git(agent_path, "status", "--porcelain") == ""
