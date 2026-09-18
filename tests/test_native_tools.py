"""Model-facing Wisdom tools retain exact private/read-only boundaries."""

import json

from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.native_tools import NativeModelTools


class Service:
    """Record calls made by model-tool handlers."""

    def __init__(self):
        """Start with an empty call ledger."""
        self.calls = []

    def close(self):
        """Record service lifetime cleanup."""
        self.calls.append(("close",))

    def native_owner(self):
        """Return the Portal owner namespace, independent of native actors."""
        return {"profile_name": "default", "account_id": "portal-account"}

    def inbox(self, *, invocation):
        """Return one owner-private candidate reference."""
        self.calls.append(("inbox", invocation))
        return {"activity": [{"reference": {"kind": "candidate", "event_id": "event-1"}}]}

    def native_candidate_inspect(self, identity):
        """Return a local-only candidate projection."""
        self.calls.append(("candidate", identity))
        return {"kind": "candidate", "event_id": identity, "networked": False}

    def native_skill_inspect(self, identity, *, version):
        """Return an exact shared version projection."""
        self.calls.append(("skill", identity, version))
        return {"kind": "skill", "skill_id": identity, "version": version}

    def list_installations(self):
        """Return the installed-skill fixture."""
        self.calls.append(("installed",))
        return [{"skill_id": "skill-1", "version": 2}]

    def installed_setup_inspect(self, identity, *, version):
        """Return setup status for the exact installed version."""
        self.calls.append(("setup", identity, version))
        return {"skill_id": identity, "version": version, "ready_to_use": False}


def tools():
    """Build model tools around a recording service."""
    service = Service()
    native = SimpleNamespace(
        tool_invocation=lambda **_kwargs: SimpleNamespace(platform="telegram"),
        validate_tool_owner=lambda *_args, **_kwargs: None,
        queue_model_consent=lambda *_args, **_kwargs: {"state": "queued"},
    )
    runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)
    return NativeModelTools(runtime), service


def test_inbox_returns_exact_activity_instead_of_status_summary():
    """Inbox retains activity references rather than substituting status."""
    model_tools, service = tools()
    result = json.loads(model_tools.inbox({}, task_id="task", session_id="session"))
    assert result == {
        "activity": [{"reference": {"kind": "candidate", "event_id": "event-1"}}]
    }
    assert service.calls == [("inbox", SimpleNamespace(platform="telegram")), ("close",)]


def test_candidate_is_private_and_skill_version_is_exact():
    """Inspection keeps candidate and immutable shared identities distinct."""
    model_tools, service = tools()
    candidate = json.loads(model_tools.inspect(
        {"kind": "candidate", "identity": "event-1", "version": None},
        task_id="task", session_id="session",
    ))
    assert candidate["networked"] is False
    assert json.loads(model_tools.inspect(
        {"kind": "skill", "identity": "skill-1", "version": 7},
        task_id="task", session_id="session",
    )) == {"kind": "skill", "skill_id": "skill-1", "version": 7}
    assert ("candidate", "event-1") in service.calls
    assert ("skill", "skill-1", 7) in service.calls


@pytest.mark.parametrize("identity", ["skill-1", "absent"])
def test_installed_inspection_selects_only_the_requested_skill(identity):
    """Installed inspection preserves exact rows and separate setup status."""
    model_tools, service = tools()
    result = json.loads(model_tools.inspect({
        "kind": "installed", "identity": identity, "version": 2,
    }))
    assert result == {
        "installed": [{"skill_id": "skill-1", "version": 2}] if identity == "skill-1" else [],
        "setup": {"skill_id": identity, "version": 2, "ready_to_use": False},
    }
    assert service.calls == [("installed",), ("setup", identity, 2), ("close",)]


@pytest.mark.parametrize("installed", [
    {"installations": [{"skill_id": "skill-1"}]},
    [{"id": "skill-1"}],
    [None],
])
def test_installed_contract_errors_do_not_return_partial_success(monkeypatch, installed):
    """Internal contract drift surfaces as an error and still closes the service."""
    model_tools, service = tools()
    monkeypatch.setattr(service, "list_installations", lambda: installed)
    with pytest.raises((KeyError, TypeError)):
        model_tools.inspect({"kind": "installed", "identity": "skill-1", "version": 2})
    assert service.calls == [("close",)]


def test_present_reports_queued_not_presented():
    """Presentation truthfully reports an asynchronous queued state."""
    model_tools, _service = tools()
    result = json.loads(model_tools.present(
        {
            "kind": "skill", "identity": "skill-1", "version": 2,
            "step": None, "title": "Review", "explanation": "Exact package review",
        },
        task_id="task", session_id="session",
    ))
    assert result["delivery"] == {"state": "queued"}
    assert result["executed"] is False
    assert "presented" not in result


def test_present_denies_a_local_route_without_a_native_renderer():
    """CLI/TUI read grants do not imply that a native card can be displayed."""
    service = Service()
    native = SimpleNamespace(
        tool_invocation=lambda **_kwargs: SimpleNamespace(platform="cli"),
        queue_model_consent=lambda *_args, **_kwargs: pytest.fail("queued without renderer"),
    )
    runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)

    with pytest.raises(PermissionError, match="renderer"):
        NativeModelTools(runtime).present(
            {
                "kind": "skill", "identity": "skill-1", "version": 2,
                "step": None, "title": "Review", "explanation": "Exact package review",
            },
            task_id="task", session_id="session",
        )

    assert not service.calls


@pytest.mark.parametrize("handler_name", ["inbox", "inspect", "present"])
def test_model_tools_explicitly_deny_a_missing_turn_admission(handler_name):
    """A missing lifecycle grant is denied before service or presentation access."""
    service = Service()
    native = SimpleNamespace(tool_invocation=lambda **_kwargs: None)
    runtime = SimpleNamespace(native=native, _closed=False, _service=lambda: service)
    model_tools = NativeModelTools(runtime)
    args = {
        "kind": "skill", "identity": "skill-1", "version": 2,
        "step": None, "title": "Review", "explanation": "Exact package review",
    }

    with pytest.raises(PermissionError, match="admitted private turn"):
        getattr(model_tools, handler_name)(args, task_id="task", session_id="session")

    assert not service.calls
