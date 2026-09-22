"""Clean-host integration probe for manual Wisdom install and uninstall."""
# The isolated host subprocess deliberately carries its own wire fixtures.
# pylint: disable=duplicate-code

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
PLUGIN_ID = "collective-wisdom"
PROBE = r'''
import base64
import contextlib
import importlib
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PLUGIN_ID = "collective-wisdom"
PROJECT_ROOT = Path(os.environ["WISDOM_PLUGIN_ROOT"]).resolve()
HOME = Path(os.environ["HERMES_HOME"]).resolve()
SKILLS_ROOT = HOME / "skills"
REQUESTS = []
REQUEST_LOCK = threading.Lock()
ACTIVE_TOKEN = None
INSTALLATION_ID = None
REMOTE_ACTIVE = False
SKILL = None
VERSIONS = None
PACKAGE_FILES = None
CONTENT_HASH = None
MANIFEST_HASH = None
SKILL_BYTES = (
    b"---\n"
    b"name: clear-summary\n"
    b"description: Condense supplied prose into a short summary.\n"
    b"---\n"
    b"# Clear Summary\n\n"
    b"Return a concise summary of supplied prose and retain the key points.\n"
)
MANIFEST_VALUE = {
    "schema_version": 1,
    "name": "clear-summary",
    "requirements": {"hermes": {"minimum_version": "0.0.0"}},
}


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
        parsed = urlsplit(self.path)
        authorized = bool(
            ACTIVE_TOKEN
            and self.headers.get("Authorization") == f"Bearer {ACTIVE_TOKEN}"
        )
        entry = {
            "method": method,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "body": body,
            "authorized": authorized,
        }
        with REQUEST_LOCK:
            REQUESTS.append(entry)
        return entry

    def do_GET(self):
        entry = self._record("GET")
        path = entry["path"]
        if path == "/v1/sync/capabilities":
            self._reply(200, {"features": ["wisdom"]})
        elif path == "/v1/sync/wisdom/skills/skill-1":
            self._reply(200, {"skill": SKILL, "versions": VERSIONS})
        elif path == "/v1/sync/wisdom/skills/skill-1/versions/2":
            self._reply(200, {"skill": SKILL, "version": VERSIONS[0]})
        elif path == "/v1/sync/wisdom/skills/skill-1/versions/2/content":
            expected_query = {
                "installation_id": [INSTALLATION_ID],
                "takedown_generation": ["3"],
            }
            if entry["query"] != expected_query:
                self._reply(422, {"error": "invalid_content_authority"})
            else:
                self._reply(
                    200,
                    {
                        "commit": "sha256:" + "a" * 64,
                        "content_hash": CONTENT_HASH,
                        "files": PACKAGE_FILES,
                    },
                )
        elif path == f"/v1/sync/wisdom/installations/{INSTALLATION_ID}":
            installations = []
            if REMOTE_ACTIVE:
                installations.append(
                    {
                        "skill_id": "skill-1",
                        "installed_version": 2,
                        "latest_version": 2,
                        "update_mode": "MANUAL",
                        "skill_state": "published",
                        "takedown_generation": 3,
                    }
                )
            self._reply(200, {"installations": installations})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):
        global INSTALLATION_ID, REMOTE_ACTIVE
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        entry = self._record("POST", body)
        if entry["path"] == "/v1/sync/wisdom/installation-identities":
            INSTALLATION_ID = body.get("installation_id")
            self._reply(
                200,
                {"installation_id": INSTALLATION_ID, "state": "active"},
            )
        elif entry["path"] == "/v1/sync/wisdom/installations":
            REMOTE_ACTIVE = True
            self._reply(
                200,
                {"installed_version": 2, "effective_update_mode": "MANUAL"},
            )
        else:
            self._reply(404, {"error": "not_found"})

    def do_DELETE(self):
        global REMOTE_ACTIVE
        entry = self._record("DELETE")
        expected = (
            f"/v1/sync/wisdom/installations/{INSTALLATION_ID}/skills/skill-1"
        )
        if entry["path"] == expected:
            REMOTE_ACTIVE = False
            self._reply(
                200,
                {
                    "skill_id": "skill-1",
                    "installation_id": INSTALLATION_ID,
                    "state": "inactive",
                },
            )
        else:
            self._reply(404, {"error": "not_found"})

    def log_message(self, _format, *_args):
        return


def request_snapshot():
    with REQUEST_LOCK:
        return list(REQUESTS)


def mutating_install_requests():
    return [
        item
        for item in request_snapshot()
        if item["path"] == "/v1/sync/wisdom/installations"
        or item["method"] == "DELETE"
    ]


def tree_snapshot(root):
    if not root.exists():
        return {}
    result = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        status = path.stat()
        result[path.relative_to(root).as_posix()] = (
            path.read_bytes(),
            stat.S_IMODE(status.st_mode),
            status.st_mtime_ns,
        )
    return result


def encode_segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def persist_login(auth, gateway_url):
    global ACTIVE_TOKEN
    claims = {
        "sub": "installer-1",
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
                    "user_id": "installer-1",
                    "org_id": "team-1",
                    "portal_base_url": "https://portal.example",
                }
            }
        }
    )
    os.environ["HERMES_SYNC_BASE_URL"] = gateway_url


def configure_package(native_root):
    global CONTENT_HASH, MANIFEST_HASH, PACKAGE_FILES, SKILL, VERSIONS
    contract = importlib.import_module(
        f"{native_root}.hermes_wisdom_plugin.contract"
    )
    package = importlib.import_module(
        f"{native_root}.hermes_wisdom_plugin.package"
    )
    manifest_bytes = contract.canonical_json_bytes(MANIFEST_VALUE)
    manifest = contract.parse_manifest_bytes(manifest_bytes)
    raw_files = [
        ("SKILL.md", "file", SKILL_BYTES),
        ("skill.manifest.json", "file", manifest_bytes),
    ]
    records, CONTENT_HASH = package.verify_content_files(raw_files)
    MANIFEST_HASH = contract.sha256_address(manifest_bytes)
    bodies = {path: body for path, _mode, body in raw_files}
    PACKAGE_FILES = [
        {
            **record.model_dump(mode="json"),
            "content_base64": base64.b64encode(bodies[record.path]).decode("ascii"),
        }
        for record in records
    ]
    system_spec = manifest.requirements.model_dump(mode="json")
    SKILL = {
        "id": "skill-1",
        "slug": "clear-summary",
        "state": "active",
        "created_by_user_id": "publisher-1",
        "latest_version": 2,
        "author_description": "Condense supplied prose into a short summary.",
        "verified_facts": None,
        "explanation": None,
        "install_count": 4,
        "takedown_generation": 3,
        "system_spec": system_spec,
        "security_check": {"status": "pass"},
        "professionalism_check": {"status": "pass"},
    }
    VERSIONS = [
        {
            "version": 2,
            "state": "published",
            "commit": "sha256:" + "a" * 64,
            "content_hash": CONTENT_HASH,
            "package_manifest_hash": MANIFEST_HASH,
            "system_spec": system_spec,
            "published_at": "2026-09-12T00:00:00Z",
            "security_check": {"status": "pass"},
            "professionalism_check": {"status": "pass"},
        }
    ]
    assert contract.derive_content_hash(records) == CONTENT_HASH
    return manifest_bytes


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
            "params": {"session_id": "wisdom-install-probe", "command": command},
        }
    )
    assert response["id"] == request_id
    assert "error" not in response
    output = response["result"]["output"]
    assert isinstance(output, str) and output
    assert ACTIVE_TOKEN not in output
    return output


def assert_safe_handoff(output, command, private_values):
    lowered = output.casefold()
    assert "terminal" in lowered
    assert f"hermes wisdom {command}" in output
    for value in private_values:
        assert value not in output


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
    persist_login(auth, gateway_url)

    sys.argv[:] = ["hermes", "wisdom", "install"]
    from hermes_cli import main
    from hermes_cli.plugins import get_plugin_manager

    parser, subparsers = main._build_cli_parser()
    manager = get_plugin_manager()
    loaded = manager._plugins[PLUGIN_ID]
    assert loaded.enabled is True
    assert loaded.module.__name__.startswith("hermes_plugins.")
    assert Path(loaded.module.__file__).resolve() == PROJECT_ROOT / "__init__.py"
    runtime = manager._plugin_commands["wisdom"]["handler"].__self__
    manifest_bytes = configure_package(loaded.module.__name__)

    help_stdout = io.StringIO()
    with contextlib.redirect_stdout(help_stdout):
        try:
            main._parse_cli_args(parser, subparsers, ["wisdom", "--help"])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("argparse help did not exit")
    help_text = help_stdout.getvalue()
    for command in ("install", "installed", "uninstall"):
        assert command in help_text, (command, help_text)

    code, setup_output = run_cli(
        main, parser, subparsers, "setup", "--accept-disclosure", "--json"
    )
    assert code == 0
    assert contains(parse_json(setup_output), "active")
    assert INSTALLATION_ID and INSTALLATION_ID.startswith("hwi_")
    assert [(item["method"], item["path"]) for item in request_snapshot()] == [
        ("GET", "/v1/sync/capabilities"),
        ("POST", "/v1/sync/wisdom/installation-identities"),
    ]

    from agent.skill_utils import iter_skill_index_files

    assert list(iter_skill_index_files(SKILLS_ROOT, "SKILL.md")) == []
    code, plan_output = run_cli(
        main, parser, subparsers, "install", "skill-1@v2", "--plan", "--json"
    )
    assert code == 0
    plan = parse_json(plan_output)
    assert plan["skill_id"] == "skill-1"
    assert plan["version"] == 2
    assert plan["content_hash"] == CONTENT_HASH
    assert plan["manifest_hash"] == MANIFEST_HASH
    receipt = plan["receipt"]
    assert isinstance(receipt, str) and receipt
    assert not mutating_install_requests()

    staging_root = SKILLS_ROOT / ".hub" / "collective-wisdom"
    staging_probe = staging_root / "index-probe"
    staging_probe.mkdir(parents=True)
    (staging_probe / "SKILL.md").write_bytes(SKILL_BYTES)
    assert list(iter_skill_index_files(SKILLS_ROOT, "SKILL.md")) == []
    shutil.rmtree(staging_probe)

    code, cli_status_output = run_cli(
        main, parser, subparsers, "status", "--json"
    )
    assert code == 0
    assert parse_json(cli_status_output)["pending_operations"] == []

    code, planned_installed_output = run_cli(
        main, parser, subparsers, "installed", "--json"
    )
    assert code == 0
    planned_installations = parse_json(planned_installed_output)
    assert planned_installations == {"installations": []}
    assert not mutating_install_requests()

    code, apply_output = run_cli(
        main,
        parser,
        subparsers,
        "install",
        "--apply-receipt",
        receipt,
        "--accept-partial",
        "--json",
    )
    assert code == 0
    applied = parse_json(apply_output)
    assert applied["installed"] is True
    assert applied["skill_id"] == "skill-1"
    assert applied["version"] == 2
    assert applied["content_hash"] == CONTENT_HASH
    assert set(applied["local_scan"]) == {
        "available",
        "allowed",
        "verdict",
        "summary",
        "findings",
    }
    assert applied["local_scan"]["allowed"] is True
    assert applied["local_scan"]["available"] is True
    target = Path(applied["path"]).resolve()
    target.relative_to(SKILLS_ROOT.resolve())
    assert target == SKILLS_ROOT / "_wisdom" / "team-1" / "clear-summary"
    assert {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} == {
        "SKILL.md",
        "skill.manifest.json",
    }
    assert (target / "SKILL.md").read_bytes() == SKILL_BYTES
    assert (target / "skill.manifest.json").read_bytes() == manifest_bytes

    active_index = list(iter_skill_index_files(SKILLS_ROOT, "SKILL.md"))
    assert active_index == [target / "SKILL.md"]
    install_posts = [
        item
        for item in request_snapshot()
        if item["method"] == "POST"
        and item["path"] == "/v1/sync/wisdom/installations"
    ]
    assert len(install_posts) == 1
    assert install_posts[0]["body"]["installation_id"] == INSTALLATION_ID
    assert install_posts[0]["body"]["skill_id"] == "skill-1"
    assert install_posts[0]["body"]["version"] == 2
    assert install_posts[0]["body"]["takedown_generation"] == 3

    target_before_duplicate = tree_snapshot(target)
    mutations_before_duplicate = list(mutating_install_requests())
    code, duplicate_output = run_cli(
        main,
        parser,
        subparsers,
        "install",
        "--apply-receipt",
        receipt,
        "--accept-partial",
        "--json",
    )
    assert code == 0
    duplicate = parse_json(duplicate_output)
    assert duplicate == applied
    assert mutating_install_requests() == mutations_before_duplicate
    assert tree_snapshot(target) == target_before_duplicate

    code, installed_output = run_cli(
        main, parser, subparsers, "installed", "--json"
    )
    assert code == 0
    installed = parse_json(installed_output)
    assert set(installed) == {"installations"}
    assert isinstance(installed["installations"], list)
    assert contains(installed, "skill-1")
    assert contains(installed, 2)
    installation_reads = [
        item
        for item in request_snapshot()
        if item["method"] == "GET"
        and item["path"] == f"/v1/sync/wisdom/installations/{INSTALLATION_ID}"
    ]
    assert len(installation_reads) >= 2

    content_reads = [
        item
        for item in request_snapshot()
        if item["path"].endswith("/versions/2/content")
    ]
    assert content_reads
    assert all(
        item["query"]
        == {
            "installation_id": [INSTALLATION_ID],
            "takedown_generation": ["3"],
        }
        for item in content_reads
    )

    from tui_gateway import server as server_module

    server_module._sessions["wisdom-install-probe"] = {
        "session_key": "wisdom-install-probe",
        "profile_home": str(HOME),
        "agent": None,
    }
    requests_before_slash = request_snapshot()
    skills_before_slash = tree_snapshot(SKILLS_ROOT)
    state_before_slash = tree_snapshot(runtime.root)
    private_values = (ACTIVE_TOKEN, INSTALLATION_ID, receipt, str(target))
    slash_install = run_slash(
        server_module,
        f"/wisdom install skill-1 --apply-receipt {receipt} --accept-partial",
        1,
    )
    assert_safe_handoff(slash_install, "install", private_values)
    slash_installed = run_slash(server_module, "/wisdom installed", 2)
    assert_safe_handoff(slash_installed, "installed", private_values)
    slash_uninstall = run_slash(
        server_module, "/wisdom uninstall skill-1 --yes", 3
    )
    assert_safe_handoff(slash_uninstall, "uninstall", private_values)
    slash_status = run_slash(server_module, "/wisdom status", 4)
    assert "Collective Wisdom status" in slash_status
    assert "skill-1" not in slash_status
    assert all(value not in slash_status for value in private_values)
    assert request_snapshot() == requests_before_slash
    assert tree_snapshot(SKILLS_ROOT) == skills_before_slash
    assert tree_snapshot(runtime.root) == state_before_slash
    assert target.is_dir()
    assert not runtime._children

    code, uninstall_output = run_cli(
        main, parser, subparsers, "uninstall", "skill-1", "--yes", "--json"
    )
    assert code == 0
    uninstalled = parse_json(uninstall_output)
    assert contains(uninstalled, "skill-1")
    assert contains(uninstalled, "inactive") or contains(uninstalled, True)
    assert not target.exists()
    trash_root = SKILLS_ROOT / ".archive" / "collective-wisdom"
    archived_skills = list(trash_root.rglob("SKILL.md"))
    assert archived_skills
    assert all(path.read_bytes() == SKILL_BYTES for path in archived_skills)
    assert any(
        path.parent.joinpath("skill.manifest.json").read_bytes() == manifest_bytes
        for path in archived_skills
    )
    assert list(iter_skill_index_files(SKILLS_ROOT, "SKILL.md")) == []

    delete_calls = [item for item in request_snapshot() if item["method"] == "DELETE"]
    assert len(delete_calls) == 1
    assert delete_calls[0]["path"] == (
        f"/v1/sync/wisdom/installations/{INSTALLATION_ID}/skills/skill-1"
    )
    code, final_installed_output = run_cli(
        main, parser, subparsers, "installed", "--json"
    )
    assert code == 0
    assert parse_json(final_installed_output) == {"installations": []}
    assert all(item["authorized"] for item in request_snapshot())
    assert not any(
        name == "hermes_wisdom" or name.startswith("hermes_wisdom.")
        for name in sys.modules
    )
    print("clean-host Wisdom manual install integration passed", file=sys.__stdout__)
finally:
    if server_module is not None:
        server_module._sessions.pop("wisdom-install-probe", None)
    if manager is not None:
        manager.unload()
    gateway.shutdown()
    gateway.server_close()
    gateway_thread.join(timeout=2)
    assert not gateway_thread.is_alive()
'''


def test_clean_host_manual_install_lifecycle(tmp_path: Path) -> None:
    """Plan, apply, discover and uninstall through an unmodified Hermes host."""

    with immutable_host_checkout(
        EXPECTED_HOST_REVISION, required_path="hermes_cli/plugins.py"
    ) as agent_path:
        profile = tmp_path / ".hermes" / "profiles" / "wisdom-install-probe"
        plugins = profile / "plugins"
        plugins.mkdir(parents=True)
        (plugins / PLUGIN_ID).symlink_to(PROJECT_ROOT, target_is_directory=True)
        (profile / "config.yaml").write_text(
            f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n", encoding="utf-8"
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
            ["-c", PROBE], cwd=tmp_path, environment=environment, timeout=120
        )
        assert result.stdout.strip() == "clean-host Wisdom manual install integration passed"
