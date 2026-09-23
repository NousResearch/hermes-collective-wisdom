"""Install approval executes only the exact reviewed setup commands."""

from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.runtime import InvocationService


@pytest.mark.parametrize("state,expected_calls", [
    ("passed", ["printf first", "printf second"]),
    ("failed", ["printf first"]),
    ("unknown", ["printf first"]),
])
def test_install_runs_reviewed_setup_in_order_without_another_prompt(
    monkeypatch, state, expected_calls,
):
    """A failed or uncertain command stops the sequence without misreporting success."""
    commands = ["printf first", "printf second"]
    issued = []
    setup = SimpleNamespace(
        inspect=lambda skill_id, version: {"guidance": {"setup_commands": commands}},
        plan_for_install=lambda skill_id, **kw: issued.append(kw) or {"receipt": str(kw["index"])},
        apply=lambda receipt, **_kw: {"state": state if receipt == "0" else "passed"},
    )
    reviewer = SimpleNamespace(receipts=SimpleNamespace(load=lambda receipt, **_kw: {
        "receipt": receipt, "skill_id": "skill-1", "version": 1,
        "setup_commands": commands,
    }))
    executor = SimpleNamespace(apply=lambda receipt, **_kw: {"installed": True})
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    result = service.install_apply("wip_" + "a" * 32)
    assert result["installed"] is True
    assert result["setup"]["state"] == state
    assert [row["command"] for row in issued] == expected_calls


def test_install_refuses_to_execute_changed_guidance(monkeypatch):
    """Setup cannot run if the installed bytes differ from the consented commands."""
    plan = {"skill_id": "skill-1", "version": 1, "setup_commands": ["safe"]}
    setup = SimpleNamespace(inspect=lambda *_args, **_kw: {
        "guidance": {"setup_commands": ["changed"]},
    })
    reviewer = SimpleNamespace(receipts=SimpleNamespace(load=lambda *_args, **_kw: plan))
    executor = SimpleNamespace(apply=lambda *_args, **_kw: {"installed": True})
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    result = object.__new__(InvocationService).install_apply("wip_" + "a" * 32)
    assert result["installed"] is True
    assert result["setup"]["state"] == "blocked"
    assert "changed" in result["setup"]["detail"]


@pytest.mark.parametrize("error", [
    PackagePolicyError("setup prerequisites unavailable"),
    WisdomAuthError("setup owner changed"),
])
def test_setup_policy_failure_reports_files_already_installed(monkeypatch, error):
    """A post-install setup block must not look like a failed file installation."""
    plan = {"skill_id": "skill-1", "version": 1, "setup_commands": ["safe"]}

    def unavailable(*_args, **_kwargs):
        raise error

    reviewer = SimpleNamespace(receipts=SimpleNamespace(load=lambda *_args, **_kw: plan))
    executor = SimpleNamespace(apply=lambda *_args, **_kw: {"installed": True})
    setup = SimpleNamespace(inspect=unavailable)
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    result = object.__new__(InvocationService).install_apply("wip_" + "a" * 32)
    assert result["installed"] is True
    assert result["setup"]["state"] == "blocked"
    assert str(error) in result["setup"]["detail"]
