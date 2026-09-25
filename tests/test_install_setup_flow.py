"""Separate setup consent executes only the installed, reviewed commands."""

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
def test_setup_permission_runs_reviewed_commands_in_order(
    monkeypatch, state, expected_calls,
):
    """A failed or uncertain command stops the sequence without misreporting success."""
    commands = ["printf first", "printf second"]
    issued = []
    setup = SimpleNamespace(
        inspect=lambda *_args, **_kw: {
            "content_hash": "reviewed-hash", "guidance": {"setup_commands": commands}},
        plan_for_install=lambda skill_id, **kw: issued.append(kw) or {"receipt": str(kw["index"])},
        apply=lambda receipt, **_kw: {"state": state if receipt == "0" else "passed"},
    )
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda receipt, **_kw: {
            "receipt": receipt, "skill_id": "skill-1", "version": 1,
            "setup_commands": commands, "content_hash": "reviewed-hash",
        },
        outcome=lambda _receipt: {"installed": True},
    ))
    executor = SimpleNamespace(apply=lambda receipt, **_kw: {"installed": True})
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    receipt = "wip_" + "a" * 32
    installed = service.install_apply(receipt)
    assert installed["installed"] is True
    assert "setup" not in installed and not issued
    result = service.install_setup_apply(receipt)
    assert result["installed"] is True
    assert result["setup"]["state"] == state
    assert [row["command"] for row in issued] == expected_calls


@pytest.mark.parametrize("installed_hash,installed_command", [
    ("reviewed-hash", "changed"), ("new-hash", "safe"),
])
def test_install_refuses_to_execute_changed_guidance(
    monkeypatch, installed_hash, installed_command,
):
    """Setup cannot run if installed bytes or commands differ from the review."""
    plan = {"skill_id": "skill-1", "version": 1, "content_hash": "reviewed-hash",
            "setup_commands": ["safe"]}
    setup = SimpleNamespace(inspect=lambda *_args, **_kw: {
        "content_hash": installed_hash,
        "guidance": {"setup_commands": [installed_command]},
    })
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda *_args, **_kw: plan, outcome=lambda _receipt: {"installed": True},
    ))
    executor = SimpleNamespace(apply=lambda *_args, **_kw: {"installed": True})
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    assert service.install_apply("wip_" + "a" * 32)["installed"] is True
    result = service.install_setup_apply("wip_" + "a" * 32)
    assert result["installed"] is True
    assert result["setup"]["state"] == "blocked"
    assert "changed" in result["setup"]["detail"]


@pytest.mark.parametrize("contradiction", [
    {"pending": True}, {"rejected": True}, {"state": "uncertain"},
])
def test_setup_refuses_unconfirmed_file_receipt(monkeypatch, contradiction):
    """A contradictory install receipt never grants command authority."""
    plan = {"skill_id": "skill-1", "version": 1,
            "content_hash": "reviewed-hash", "setup_commands": ["printf safe"]}
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda *_args, **_kw: plan,
        outcome=lambda _receipt: {"installed": True, **contradiction},
    ))
    monkeypatch.setattr(
        InvocationService, "_installation_services",
        lambda self: (reviewer, SimpleNamespace()),
    )
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    with pytest.raises(PackagePolicyError, match="not confirmed installed"):
        object.__new__(InvocationService).install_setup_apply("wip_" + "a" * 32)


@pytest.mark.parametrize("error", [
    PackagePolicyError("setup prerequisites unavailable"),
    WisdomAuthError("setup owner changed"),
])
def test_setup_policy_failure_reports_files_already_installed(monkeypatch, error):
    """A post-install setup block must not look like a failed file installation."""
    plan = {"skill_id": "skill-1", "version": 1, "setup_commands": ["safe"]}

    def unavailable(*_args, **_kwargs):
        raise error

    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda *_args, **_kw: plan, outcome=lambda _receipt: {"installed": True},
    ))
    executor = SimpleNamespace(apply=lambda *_args, **_kw: {"installed": True})
    setup = SimpleNamespace(inspect=unavailable)
    monkeypatch.setattr(
        InvocationService, "_installation_services", lambda self: (reviewer, executor)
    )
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    assert service.install_apply("wip_" + "a" * 32)["installed"] is True
    result = service.install_setup_apply("wip_" + "a" * 32)
    assert result["installed"] is True
    assert result["setup"]["state"] == "blocked"
    assert str(error) in result["setup"]["detail"]
