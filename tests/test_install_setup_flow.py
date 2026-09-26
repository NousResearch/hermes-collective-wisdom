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


def test_update_setup_uses_completed_update_receipt_and_runs_only_new_command(monkeypatch):
    """Updated files alone do not grant permission to run the new command."""
    command = "printf new-version"
    calls = []
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda receipt, **_kw: {
            "receipt": receipt, "operation": "update", "skill_id": "skill-1",
            "version": 2, "setup_commands": [command], "content_hash": "new-hash",
        },
        outcome=lambda _receipt: {"updated": True, "setup_required": True,
                                  "ready_to_use": None},
    ))
    setup = SimpleNamespace(
        inspect=lambda *_args, **_kwargs: {
            "content_hash": "new-hash", "guidance": {"setup_commands": [command]}},
        plan_for_install=lambda _skill, **kwargs: calls.append(("plan", kwargs)) or
        {"receipt": "wip_setup"},
        apply=lambda receipt, **_kwargs: calls.append(("run", receipt)) or
        {"state": "passed"},
    )
    monkeypatch.setattr(InvocationService, "_update_services",
                        lambda self: (reviewer, SimpleNamespace()))
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    service = object.__new__(InvocationService)
    result = service.update_setup_apply("wip_" + "a" * 32)
    assert result["updated"] is True
    assert result["setup_execution_authorized"] is True
    assert result["setup"] == {"state": "passed", "steps": 1}
    assert calls[0][1]["version"] == 2
    assert calls[0][1]["command"] == command
    assert calls[1] == ("run", "wip_setup")


@pytest.mark.parametrize("contradiction", [
    {"pending": True}, {"rejected": True}, {"state": "uncertain"},
])
def test_update_setup_refuses_unconfirmed_file_receipt(monkeypatch, contradiction):
    """An update in doubt never grants command authority."""
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda *_args, **_kwargs: {
            "skill_id": "skill-1", "version": 2, "setup_commands": ["printf safe"]},
        outcome=lambda _receipt: {"updated": True, **contradiction},
    ))
    monkeypatch.setattr(InvocationService, "_update_services",
                        lambda self: (reviewer, SimpleNamespace()))
    monkeypatch.setattr(InvocationService, "_setup_services",
                        lambda self: pytest.fail("unconfirmed update ran setup"))
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    with pytest.raises(PackagePolicyError, match="not confirmed updated"):
        object.__new__(InvocationService).update_setup_apply("wip_" + "a" * 32)


@pytest.mark.parametrize("installed_hash,installed_command", [
    ("new-hash", "different"), ("old-hash", "printf safe"),
])
def test_update_setup_refuses_changed_installed_version(
    monkeypatch, installed_hash, installed_command,
):
    """A changed installed hash or command cannot spend the update approval."""
    reviewer = SimpleNamespace(receipts=SimpleNamespace(
        load=lambda *_args, **_kwargs: {
            "skill_id": "skill-1", "version": 2, "content_hash": "new-hash",
            "setup_commands": ["printf safe"]},
        outcome=lambda _receipt: {"updated": True, "setup_required": True},
    ))
    setup = SimpleNamespace(
        inspect=lambda *_args, **_kwargs: {
            "content_hash": installed_hash,
            "guidance": {"setup_commands": [installed_command]}},
        plan_for_install=lambda *_args, **_kwargs:
        pytest.fail("changed package planned a setup command"),
    )
    monkeypatch.setattr(InvocationService, "_update_services",
                        lambda self: (reviewer, SimpleNamespace()))
    monkeypatch.setattr(InvocationService, "_setup_services", lambda self: setup)
    monkeypatch.setattr(InvocationService, "check_owner", lambda self: None)
    result = object.__new__(InvocationService).update_setup_apply("wip_" + "a" * 32)
    assert result["updated"] is True
    assert result["setup"]["state"] == "blocked"
    assert "changed" in result["setup"]["detail"]
