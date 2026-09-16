"""Registration ownership, profile fencing and cancellable slash command budgets."""

import asyncio
import os
import threading
import time
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin import runtime
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomError


def test_service_refuses_profile_drift_before_authentication(tmp_path, monkeypatch):
    """A cached registration cannot follow a new profile's mutable host data root."""
    service = runtime.InvocationService(tmp_path / "state", ("original", tmp_path))
    monkeypatch.setattr(runtime, "current_profile", lambda: ("other", tmp_path / "other"))
    with pytest.raises(WisdomAuthError, match="profile changed"):
        service.check_owner()
    assert not (tmp_path / "state").exists()


def test_status_without_setup_does_not_create_storage(tmp_path, monkeypatch):
    """Status is not an enrolment shortcut."""
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    monkeypatch.setattr(service, "check_owner", lambda: None)
    assert service.status() == {
        "configured": False, "state": "not_configured", "reason": "not_configured"
    }
    assert not service.root.exists()
    with pytest.raises(WisdomError):
        service.require_setup()
    assert not service.root.exists()


def test_worker_deadline_kills_stalled_request_and_reaps_process(tmp_path, monkeypatch):
    """The overall command deadline applies even when transport timeouts cannot help."""
    monkeypatch.setattr(runtime, "SLASH_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(tmp_path, ("default", tmp_path))
    start = time.monotonic()
    result = owner._run_worker("browse", {}, threading.Event())  # pylint: disable=protected-access
    assert "timed out" in result
    assert time.monotonic() - start < 2
    assert not owner._children  # pylint: disable=protected-access


def test_worker_inherits_captured_profile_without_mutating_host_env(tmp_path, monkeypatch):
    """A host ContextVar profile override must survive the worker boundary."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))
    monkeypatch.setattr(runtime, "_BOOTSTRAP", (
        "import json,os,sys;sys.stdin.read();print(json.dumps(os.environ['HERMES_HOME']))"
    ))
    owner = runtime.CommandRuntime(tmp_path / "state", ("custom", tmp_path / "captured"))
    result = owner._run_worker("status", {}, threading.Event())  # pylint: disable=protected-access
    assert result == str(tmp_path / "captured")
    assert os.environ["HERMES_HOME"] == str(tmp_path / "ambient")


def test_unload_terminates_owned_worker_and_prevents_reuse(tmp_path, monkeypatch):
    """Unload has no unbounded network or thread tail."""
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(tmp_path, ("default", tmp_path))
    thread = threading.Thread(target=owner._run_worker, args=(  # pylint: disable=protected-access
        "browse", {}, threading.Event()
    ))
    thread.start()
    limit = time.monotonic() + 2
    while not owner._children and time.monotonic() < limit:  # pylint: disable=protected-access
        time.sleep(0.01)
    assert owner._children  # pylint: disable=protected-access
    owner.close()
    thread.join(2)
    assert not thread.is_alive()
    assert not owner._children  # pylint: disable=protected-access
    with pytest.raises(WisdomError, match="unloaded"):
        owner._service()  # pylint: disable=protected-access


def test_slash_cancellation_reaps_worker_before_return(tmp_path, monkeypatch):
    """Cancelling an async slash invocation cannot leave its worker behind."""
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(tmp_path, ("default", tmp_path))
    monkeypatch.setattr(owner, "_service", lambda: Mock(
        binding=lambda: {}, check_owner=lambda: None, close=lambda: None
    ))

    async def exercise():
        task = asyncio.create_task(owner.slash("browse"))
        for _ in range(100):
            if owner._children:  # pylint: disable=protected-access
                break
            await asyncio.sleep(0.01)
        assert owner._children  # pylint: disable=protected-access
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._children  # pylint: disable=protected-access

    asyncio.run(exercise())


def test_closed_service_cannot_publish_late_results(tmp_path):
    """Post-response ownership check rejects work after plugin unload."""
    service = runtime.InvocationService(tmp_path, ("default", tmp_path))
    service.close()
    with pytest.raises(WisdomError, match="unloaded"):
        service.check_owner()
