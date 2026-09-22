"""Load the Git-installed backend through stock host discovery and HTTP gates."""

import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest


def test_stock_host_mount_auth_and_profile_isolation(tmp_path):
    """No repository PYTHONPATH, production credentials, or running host needed."""
    host = os.environ.get("WISDOM_DESKTOP_HOST_DIR")
    python = os.environ.get("WISDOM_HOST_PYTHON")
    if not host or not python:
        pytest.skip("set WISDOM_DESKTOP_HOST_DIR and WISDOM_HOST_PYTHON for stock API mount")
    source = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    plugin_root = home / "plugins" / "collective-wisdom"
    plugin_root.parent.mkdir(parents=True)
    plugin_root.symlink_to(source, target_is_directory=True)
    config = "plugins:\n  enabled: [collective-wisdom]\n"
    (home / "config.yaml").write_text(config, encoding="utf-8")
    for profile in ("alpha", "beta"):
        directory = home / "profiles" / profile
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text(config, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(home),
        "PYTHONPATH": str(Path(host).resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    script = textwrap.dedent('''
        import json
        import sys
        from pathlib import Path
        from fastapi.testclient import TestClient
        import contextlib
        import io
        from types import SimpleNamespace
        from hermes_cli import plugins_cmd, web_server

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            plugins_cmd.cmd_list(SimpleNamespace(json=True))
        listed = json.loads(output.getvalue())
        wisdom = [row for row in listed if row["name"] == "collective-wisdom"]
        assert len(wisdom) == 1, listed
        assert wisdom[0]["status"] == "enabled", wisdom
        assert not any(row["name"] == "hermes-collective-wisdom" for row in listed)

        name = "hermes_dashboard_plugin_collective-wisdom"
        assert name in sys.modules, "stock host failed to import plugin backend"
        backend = sys.modules[name]
        prefix = "/api/plugins/collective-wisdom"
        http = TestClient(web_server.app, base_url="http://127.0.0.1")
        assert http.get(prefix + "/bootstrap").status_code == 401
        headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
        disclosure = http.get(prefix + "/enrollment/disclosure", headers=headers)
        assert disclosure.status_code == 200, disclosure.text

        class RecordingService:
            def __init__(self, root, profile, **kwargs):
                self.root, self.profile = root, profile
            def status(self, **kwargs):
                return {"configured": False, "profile": self.profile[0], "root": str(self.root)}
            def notification_config(self):
                return {"notifications": {"delivery_mode": "agent"}}
            def native_owner(self):
                return {"profile": self.profile[0], "installation_id": str(self.root)}
            def install_plan(self, reference, **kwargs):
                return {"receipt": "receipt-" + self.profile[0]}
            def install_apply(self, receipt, *, may_apply, **kwargs):
                assert may_apply() is True
                assert receipt == "receipt-" + self.profile[0]
                self.root.mkdir(parents=True, exist_ok=True)
                (self.root / "applied.json").write_text(json.dumps({"receipt": receipt}))
                return {"installed": True}
            def close(self):
                pass

        backend.InvocationService = RecordingService
        roots = {}
        for profile in ("alpha", "beta", "alpha"):
            result = http.get(prefix + "/bootstrap", params={"profile": profile}, headers=headers)
            assert result.status_code == 200, result.text
            status = result.json()["status"]
            assert status["profile"] == profile, status
            roots[profile] = status["root"]
        assert roots["alpha"] != roots["beta"]
        view = http.post(prefix + "/views?profile=alpha", headers=headers).json()["view_id"]
        plan = http.post(prefix + "/install/review?profile=alpha", headers=headers,
                         json={"view_id": view, "reference": "skill@v1"}).json()
        confirmation = {"view_id": view, "receipt": plan["receipt"]}
        assert http.post(prefix + "/install/confirm?profile=beta", headers=headers,
                         json=confirmation).status_code == 401
        applied = http.post(prefix + "/install/confirm?profile=alpha", headers=headers,
                            json=confirmation)
        assert applied.status_code == 200, applied.text
        assert (Path(roots["alpha"]) / "applied.json").is_file()
        assert not (Path(roots["beta"]) / "applied.json").exists()
        assert http.delete(prefix + "/views/" + view + "?profile=alpha",
                           headers=headers).status_code == 200
        assert http.post(prefix + "/install/confirm?profile=alpha", headers=headers,
                         json=confirmation).status_code == 401
        missing = http.get(prefix + "/bootstrap?profile=missing", headers=headers)
        assert missing.status_code == 404, missing.text
        root = Path(__import__("os").environ["HERMES_HOME"])
        (root / "profiles" / "beta" / "config.yaml").write_text(
            "plugins:\\n  disabled: [collective-wisdom]\\n")
        disabled = http.get(prefix + "/bootstrap?profile=beta", headers=headers)
        assert disabled.status_code == 404, disabled.text
        assert http.get(prefix + "/bootstrap?profile=beta").status_code == 401
        print(json.dumps({"mounted": True, "auth_gate": True, "profiles": ["alpha", "beta", "alpha"], "disabled_gate": True}))
    ''')
    result = subprocess.run(
        [python, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["mounted"] and evidence["auth_gate"] and evidence["disabled_gate"]
