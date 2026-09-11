"""Opt-in compatibility check using unmodified Hermes and synthetic credentials."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import base64
import json
import os
import socket
import sys
import time
from pathlib import Path

def forbidden(*args, **kwargs):
    raise AssertionError("availability must not refresh or contact the network")

socket.socket.connect = forbidden
from hermes_cli import auth
assert Path(auth.__file__).resolve().is_relative_to(Path(os.environ["WISDOM_AGENT_DIR"]))
auth.resolve_nous_runtime_credentials = forbidden
from hermes_wisdom_plugin.account import HermesAccountAdapter
from hermes_wisdom_plugin.errors import WisdomAuthError

home = Path(os.environ["HERMES_HOME"])
assert home.is_relative_to(Path(os.environ["HOME"]))

def persist(subject="member", org="team", scopes=None, ttl=600, marker="a"):
    # Unsigned fixture tests local projection only, never server authorization.
    claims = {"sub": subject, "org_id": org, "exp": time.time() + ttl,
              "scope": "inference:invoke", "jti": marker,
              "wisdom_scopes": ["wisdom:read"] if scopes is None else scopes}
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value).encode()).decode().rstrip("=")
    token = encode({"alg": "none"}) + "." + encode(claims) + ".fixture"
    auth._save_auth_store({"providers": {"nous": {
        "access_token": token, "user_id": subject, "org_id": org,
        "portal_base_url": "https://portal.example", "refresh_token": "never-use",
    }}})
    return token

def denied(callback):
    try:
        callback()
    except WisdomAuthError:
        return
    raise AssertionError("protected credentials unexpectedly available")

denied(HermesAccountAdapter.current)
token = persist()
adapter = HermesAccountAdapter.current()
assert adapter.organization_id == "team"
assert adapter.account_id == "member"
assert adapter.entitlement_claims().scopes == ["wisdom:read"]
assert adapter.credentials().access_token == token
assert adapter.credentials().base_url == "https://gateway.example"

rotated = persist(marker="rotated")
assert adapter.credentials().access_token == rotated
persist(subject="different-member")
assert adapter.entitlement_claims() is None
denied(adapter.credentials)
persist(org="different-team")
denied(adapter.credentials)
persist(scopes=[])
denied(adapter.credentials)
persist(ttl=-1)
denied(adapter.credentials)
persist()
os.environ["HERMES_SYNC_BASE_URL"] = "https://different-gateway.example"
denied(adapter.credentials)
os.environ["HERMES_SYNC_BASE_URL"] = "https://gateway.example"
auth._save_auth_store({"providers": {}})
denied(adapter.credentials)
persist()
os.environ["HERMES_HOME"] = str(home.parent / "different-profile")
denied(adapter.credentials)
assert "hermes_cli.plugin_invocation" not in sys.modules
assert "hermes_wisdom" not in sys.modules
print("unmodified-host account compatibility passed")
'''


def test_unmodified_host_account_boundary(tmp_path: Path) -> None:
    """Actual host readers handle rotation, expiry and ownership without patches."""
    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to the unmodified Agent checkout")
    agent_path = Path(agent_dir).resolve()
    assert (agent_path / "hermes_cli" / "auth.py").is_file()
    paths = [str(PROJECT_ROOT), str(agent_path)]
    if dependencies := os.environ.get("WISDOM_HOST_DEPENDENCIES"):
        paths.append(dependencies)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "profile"),
        "HERMES_SYNC_BASE_URL": "https://gateway.example",
        "WISDOM_AGENT_DIR": str(agent_path),
        "PYTHONPATH": os.pathsep.join(paths),
        "PYTHONNOUSERSITE": "1",
    }
    result = subprocess.run(
        [os.environ.get("WISDOM_HOST_PYTHON", sys.executable), "-c", PROBE],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unmodified-host account compatibility passed"
