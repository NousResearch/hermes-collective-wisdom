"""Install consent never bypasses the host's command safety guards."""

import json
from types import SimpleNamespace


from hermes_wisdom_plugin.setup_host import SetupHost


def test_setup_host_never_forces_terminal_past_guards(tmp_path):
    """The install path must not skip Tirith or dangerous-command checks."""
    calls = []
    host = SetupHost()
    host.execution_policy = lambda: {"backend": "local"}
    host._expected_session_key = lambda task_id: "session"  # pylint: disable=protected-access
    terminal = SimpleNamespace(terminal_tool=lambda **kwargs: (
        calls.append(kwargs) or json.dumps({"status": "blocked"})
    ))
    host._module = lambda name: terminal  # pylint: disable=protected-access
    result = host.execute("printf setup", workdir=tmp_path, task_id="task")
    assert result["state"] == "blocked"
    assert calls[0].get("force", False) is False
    assert calls[0]["command"] == "printf setup"
