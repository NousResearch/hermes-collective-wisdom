"""TUI-only generic review/action consumer over existing receipt kernels."""
# Keep independent TUI fixtures readable; shared wire shapes are not helper APIs.
# pylint: disable=too-many-lines,duplicate-code

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.runtime import CommandRuntime
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.tui_actions import run_tui_action


class Outcome(str, Enum):
    """Minimal stand-in for the host review outcome enum."""

    SELECTED = "selected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Action:
    """Minimal stand-in for one host review action."""

    id: str
    label: str


@dataclass(frozen=True)
class Result:
    """Minimal stand-in for a host review result."""

    outcome: Outcome
    action_id: str | None = None


OWNER = {
    "profile_name": "default",
    "profile_home": "/tmp/disposable-profile",
    "account_id": "member-1",
    "organization_id": "org-1",
    "base_url": "https://gateway.invalid",
    "installation_id": "hwi_fixture",
}


def complete_plan(operation: str) -> dict:
    """Return representative complete receipt data, including every review family."""

    plan = {
        "operation": operation,
        "receipt": f"receipt-{operation}",
        "owner": dict(OWNER),
        "skill_id": "skill-1",
        "slug": "complete-skill",
        "version": 2,
        "content_hash": "sha256:" + "a" * 64,
        "manifest_hash": "sha256:" + "b" * 64,
        "manifest": {"schema_version": 1, "name": "complete-skill", "requirements": {}},
        "files": [
            {"path": "SKILL.md", "mode": "file", "hash": "sha256:" + "c" * 64,
             "size": 60_000},
            {"path": "skill.manifest.json", "mode": "file",
             "hash": "sha256:" + "d" * 64, "size": 80},
        ],
        "compatibility": {
            "outcome": "compatible_after_setup", "satisfied": [],
            "setup_actions": ["Run the separately approved setup"],
            "limitations": ["fixture limitation"], "blocked": [],
        },
        "server_checks": {"security_check": {"state": "passed"}},
        "local_scan": {"allowed": None, "findings": ["review required"]},
        "policy": {
            "requires_explicit_confirmation": True,
            "compatibility_requires_confirmation": True,
            "scan_requires_confirmation": True,
        },
        "acknowledgements": {"fixture": True},
        "hashes": {
            "content": "sha256:" + "a" * 64,
            "manifest": "sha256:" + "b" * 64,
        },
        "permitted_actions": [f"confirm_{operation}", "recheck"],
        "allowed": True,
        "source_body_fixture": "BEGIN-SOURCE\n" + "x" * 12_000 + "\nEND-SOURCE",
    }
    if operation == "update":
        plan.update({
            "from_version": 1,
            "modified": True,
            "sensitive_expansion": ["new shell permission"],
            "preservation": {
                "action": "preserve_unmanaged_copy",
                "destination": "/tmp/disposable-profile/skills/complete-local-copy",
            },
        })
    if operation == "installed_setup":
        plan.update({
            "step": {"phase": "setup", "index": 0, "command": "printf configured"},
            "setup_instruction": "Configure the fixture.",
            "setup_key": "e" * 64,
            "target_path": "/tmp/disposable-profile/skills/complete-skill",
        })
    return plan


class Service:
    """Record calls made through the TUI action service seam."""
    # This shared recording fixture implements the consumption service boundary.
    # pylint: disable=too-many-instance-attributes,too-many-public-methods

    def __init__(self):
        self.calls: list[tuple] = []
        self.mode = "agent"
        self.mode_writable = True
        self.mute_control: dict | None = {
            "id": "mute-control-1", "expires_at": 600,
            "mute": {"org_id": "org-1", "muted": False, "revision": 4},
        }
        self.plans = {
            "install": complete_plan("install"),
            "update": complete_plan("update"),
            "installed_setup": complete_plan("installed_setup"),
        }

    def notification_settings(self, *, may_apply, include_mute_control=False):
        """Return one owner-bound mode/mute snapshot for deliberate controls."""
        assert may_apply() is True
        self.calls.append(("notification_settings",))
        return {
            "owner": dict(OWNER),
            "delivery_mode": self.mode,
            "mode_writable": self.mode_writable,
            "mode_meanings": {
                "agent": "Eligible recommendations may be assessed and delivered.",
                "fixed": "Assessment and proactive delivery are off; manual review remains.",
            },
            "gateway_status": "available" if self.mute_control else "unavailable",
            "gateway_mute": self.mute_control["mute"] if self.mute_control else None,
            "mute_sync": None,
            "mute_control": (
                dict(self.mute_control)
                if include_mute_control and self.mute_control else None
            ),
        }

    def set_notification_mode(self, mode, *, expected_mode, may_apply):
        """Record a mode write only after the current invocation is rechecked."""
        assert may_apply() is True
        self.calls.append(("set_notification_mode", mode, expected_mode))
        assert self.mode == expected_mode
        self.mode = mode
        return {"delivery_mode": mode}

    def choose_notification_mute(self, control_id, duration, *, may_apply):
        """Record one exact captured mute-control choice."""
        assert may_apply() is True
        self.calls.append(("choose_notification_mute", control_id, duration))
        return {
            "requested_duration": duration,
            "preference_sync": "pending",
            "mutation_id": control_id,
        }

    def notification_mute_status(self):
        """Return observational mute state without preparing another control."""
        self.calls.append(("notification_mute_status",))
        return {
            "organization_id": "org-1", "gateway_available": True,
            "mute": self.mute_control["mute"] if self.mute_control else None,
            "sync": None,
        }

    def sync_status(self):
        """Return the complete non-mutating sync projection."""
        self.calls.append(("sync_status",))
        return {
            "preference": {"pending": 1, "syncing": 0, "retryable": 0,
                           "conflict": 0, "expired": 0},
            "mute": {"pending": 0, "syncing": 0, "retryable": 0,
                     "conflict": 1, "expired": 0},
            "delivery": {"pending": 0, "syncing": 0, "retryable": 0,
                         "conflict": 0, "uncertain": 1, "waiting_for_receipt": 0},
            "operation": {"pending": 0, "syncing": 0, "retryable": 1,
                          "conflict": 0, "uncertain": 0, "waiting_for_receipt": 0},
            "can_retry": True,
        }

    def sync_retry(self, *, may_apply):
        """Record one explicit known-safe retry request."""
        assert may_apply() is True
        self.calls.append(("sync_retry",))
        result = self.sync_status()
        result["can_retry"] = False
        return result

    def update_recovery_inspect(self, skill_id, operation_id):
        """Return exact durable update identity without resuming it."""
        self.calls.append(("update_recovery_inspect", skill_id, operation_id))
        return {
            "kind": "update", "skill_id": skill_id, "operation_id": operation_id,
            "receipt": "receipt-update-recovery", "phase": "local_ledger_committed",
            "state": "pending", "from_version": 1, "version": 2,
            "can_resume": True,
        }

    def update_recovery_resume(self, skill_id, operation_id, receipt, *, may_apply):
        """Resume only the exact inspected update after invocation recheck."""
        assert may_apply() is True
        self.calls.append(("update_recovery_resume", skill_id, operation_id, receipt))
        return {"updated": True, "skill_id": skill_id, "version": 2}

    def prepare_review_deferral(self, _plan):
        """Default fixture has no verified install deferral policy."""
        return None

    def defer_review(self, _value, *, may_apply):
        """A deferral cannot run unless a test supplies verified preparation."""
        assert may_apply() is True
        raise AssertionError("no review deferral was prepared")

    def install_plan(self, reference, *, update_mode=None):
        """Return the representative install plan."""
        self.calls.append(("install_plan", reference, update_mode))
        return self.plans["install"]

    def install_apply(self, receipt, *, accept_partial=False):
        """Record one install application."""
        self.calls.append(("install_apply", receipt, accept_partial))
        return {"installed": True, "skill_id": "skill-1", "version": 2}

    def native_install_inspect(self, receipt):
        """Return complete install file content for review."""
        self.calls.append(("native_install_inspect", receipt))
        return [{
            "path": "SKILL.md", "content": "# exact install\n", "content_start": 0,
            "content_end": 16, "hash": "sha256:" + "c" * 64,
        }]

    def update_plan(self, skill_id, **choices):
        """Return the representative update plan."""
        self.calls.append(("update_plan", skill_id, choices))
        return self.plans["update"]

    def update_apply(self, receipt, *, may_apply=None):
        """Record one update application."""
        assert may_apply is None or may_apply() is True
        self.calls.append(("update_apply", receipt))
        return {"updated": True, "skill_id": "skill-1", "version": 2}

    def prepare_update_deferral(self, plan):
        """Bind the exact organization-policy cooldown before offering Not Now."""
        self.calls.append(("prepare_update_deferral", plan["receipt"]))
        return {
            "owner": dict(OWNER),
            "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
            "not_now_suppression_days": 7,
        }

    def defer_update(self, deferral, *, may_apply):
        """Record one durable local Not Now selection."""
        assert may_apply() is True
        self.calls.append(("defer_update", deferral))
        return {
            "deferred": True,
            "skill_id": deferral["reference"]["skill_id"],
            "version": deferral["reference"]["version"],
            "not_now_suppression_days": deferral["not_now_suppression_days"],
            "preference_sync": "pending",
        }

    def native_update_inspect(self, receipt):
        """Return complete update file content for review."""
        self.calls.append(("native_update_inspect", receipt))
        return [{
            "path": "SKILL.md", "content": "# exact update\n", "content_start": 0,
            "content_end": 15, "hash": "sha256:" + "c" * 64,
        }]

    def installed_setup_inspect(self, skill_id, *, version=None):
        """Return setup guidance without applying it."""
        self.calls.append(("installed_setup_inspect", skill_id, version))
        return {
            "kind": "installed_setup", "skill_id": skill_id, "version": version,
            "owner": dict(OWNER), "content_hash": "sha256:" + "a" * 64,
            "guidance": {"setup_instructions": ["Configure the fixture."]},
            "compatibility": {"outcome": "compatible_after_setup"},
            "files": ["SKILL.md", "refs/wisdom-setup.md"],
        }

    def installed_setup_plan(self, skill_id, *, version, phase, index, command):
        """Return the representative separately approved setup plan."""
        self.calls.append(("installed_setup_plan", skill_id, version, phase, index, command))
        return self.plans["installed_setup"]

    def installed_setup_apply(self, receipt, *, may_apply=None):
        """Record one setup application."""
        assert may_apply is None or may_apply() is True
        self.calls.append(("installed_setup_apply", receipt))
        return {"state": "passed", "skill_id": "skill-1", "version": 2}

    def installed_setup_recover(
        self, skill_id, operation_id, *, acknowledged=False, may_apply=None,
    ):
        """Inspect or explicitly abandon one exact setup operation."""
        self.calls.append(("installed_setup_recover", skill_id, operation_id, acknowledged))
        if acknowledged:
            assert may_apply is not None and may_apply() is True
            return {
                "state": "abandoned", "skill_id": skill_id,
                "operation_id": operation_id, "receipt": "receipt-setup-recovery",
            }
        return {
            "state": "unknown", "skill_id": skill_id, "operation_id": operation_id,
            "receipt": "receipt-setup-recovery", "recovery_review": True,
            "step": {"phase": "setup", "index": 0, "command": "printf maybe"},
        }

    def close(self):
        """Close the fixture service."""


def selected_request(captured):
    """Return a review callback that selects and records the offered action."""
    def request_review(*, title, body, actions):
        captured.append((title, body, actions))
        return Result(Outcome.SELECTED, actions[0].id)
    return request_review


def selected_action_request(captured, action_id):
    """Return a review callback that deliberately chooses one named action."""
    def request_review(*, title, body, actions):
        captured.append((title, body, actions))
        return Result(Outcome.SELECTED, action_id)
    return request_review


def test_install_renders_complete_literal_plan_then_applies_named_action():
    """Install review includes exact data before its named action mutates."""
    service = Service()
    captured = []

    output = run_tui_action(
        "install skill-1@v2 --accept-partial",
        service, selected_request(captured), Action, Outcome,
    )

    assert service.calls == [
        ("install_plan", "skill-1@v2", None),
        ("native_install_inspect", "receipt-install"),
        ("install_apply", "receipt-install", True),
    ]
    title, body, actions = captured[0]
    assert title == "Review Collective Wisdom install"
    assert actions == (Action("install", "Install exact reviewed package"),)
    reviewed = json.loads(body)
    assert reviewed["plan"] == service.plans["install"]
    assert reviewed["package_files"][0]["content"] == "# exact install\n"
    assert reviewed["apply_acknowledgements"] == {"accept_partial": True}
    result = json.loads(output.split("\n\n", 1)[0])
    assert result["installed"] is True
    assert "/wisdom installed-setup inspect skill-1 --version 2" in output
    assert "not run automatically" in output


@pytest.mark.parametrize("operation", ["install", "update"])
def test_blocked_review_offers_fresh_tui_recheck_without_approval(operation):
    """A blocked plan never inherits approval; verified update references may defer."""
    service = Service()
    service.plans[operation]["allowed"] = False
    captured = []
    arguments = f"{operation} 'skill with spaces' --accept-partial"
    output = run_tui_action(arguments, service, selected_request(captured), Action, Outcome)
    if operation == "update":
        assert json.loads(output.split("\n\n", 1)[0])["deferred"] is True
        assert captured[0][2] == (Action("defer", "Not Now"),)
    else:
        assert f"Recheck: /wisdom {arguments}" in output
        assert not captured
    assert not any("apply" in call[0] for call in service.calls)
    service.plans[operation]["allowed"] = True
    service.plans[operation]["receipt"] = "fresh-receipt"
    output = run_tui_action(
        arguments, service, lambda **_kwargs: Result(Outcome.CANCELLED), Action, Outcome
    )
    assert "cancelled" in output
    assert not any("apply" in call[0] for call in service.calls)


@pytest.mark.parametrize("operation", ["install", "update"])
def test_expired_preparation_offers_recheck_but_apply_failure_stays_unknown(operation, monkeypatch):
    """Only a failure before entering apply permits a fresh-plan handoff."""
    from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel

    service = Service()
    captured = []

    def expired(_receipt):
        raise WisdomConflict("review expired")

    inspect = getattr(service, f"native_{operation}_inspect")
    monkeypatch.setattr(service, f"native_{operation}_inspect", expired)
    output = run_tui_action(
        f"{operation} skill-1", service, selected_request(captured), Action, Outcome
    )
    assert f"Recheck: /wisdom {operation} skill-1" in output
    assert not captured
    assert not any("apply" in call[0] for call in service.calls)
    monkeypatch.setattr(service, f"native_{operation}_inspect", inspect)

    def uncertain(*_args, **_kwargs):
        raise WisdomConflict("review expired")

    monkeypatch.setattr(service, f"{operation}_apply", uncertain)
    output = run_tui_action(
        f"{operation} skill-1", service, selected_request(captured), Action, Outcome
    )
    assert "outcome is unknown" in output
    assert "/wisdom check skill-1" in output
    assert "Recheck:" not in output
    assert "Nothing was changed" not in output


@pytest.mark.parametrize(
    ("arguments", "method"),
    [("installed", "list_installations"), ("check skill-1", "check_update"),
     ("installed-setup status skill-1 --version 2", "installed_setup_inspect")],
)
def test_tui_recovery_reads_require_live_authority_without_approval(arguments, method, monkeypatch):
    """Inspect existing state without replaying approval or returning stale-owner data."""
    from hermes_wisdom_plugin.tui_actions import handles_tui_action  # pylint: disable=import-outside-toplevel

    service = Service()
    reads = []
    current = [True]

    def read(*args, **kwargs):
        reads.append((args, kwargs))
        return {"state": "pending", "operation_id": "op-1"}

    monkeypatch.setattr(service, method, read, raising=False)
    captured = []
    assert handles_tui_action(arguments)
    result = run_tui_action(
        arguments, service, selected_request(captured), Action, Outcome, lambda: current[0]
    )
    assert "op-1" in result
    assert len(reads) == 1
    assert not captured
    assert not service.calls

    def read_then_revoke(*args, **kwargs):
        value = read(*args, **kwargs)
        current[0] = False
        return value

    monkeypatch.setattr(service, method, read_then_revoke)
    result = run_tui_action(
        arguments, service, selected_request(captured), Action, Outcome, lambda: current[0]
    )
    assert "op-1" not in result
    assert len(reads) == 2
    run_tui_action(
        arguments, service, selected_request(captured), Action, Outcome, lambda: current[0]
    )
    assert len(reads) == 2


@pytest.mark.parametrize("arguments", [
    "installed extra", "installed --json", "check", "check --json",
    "check skill-1 extra", "installed-setup status skill-1 --phase setup",
    "installed-setup status skill-1 --version -1",
])
def test_recovery_reads_reject_mutating_or_malformed_arguments(arguments):
    """The read-only entry points do not quietly ignore unsupported flags."""
    service = Service()
    captured = []
    output = run_tui_action(arguments, service, selected_request(captured), Action, Outcome)
    assert output.startswith("Usage:")
    assert not service.calls
    assert not captured


@pytest.mark.parametrize("control", ["\u001b", "\u0007", "\u2028", "\u202e"])
def test_recheck_handoff_never_echoes_terminal_or_direction_controls(control):
    """Quoting is not terminal escaping; unsafe command text must not be echoed."""
    service = Service()
    service.plans["install"]["allowed"] = False
    output = run_tui_action(
        f"install 'skill{control}name'", service, selected_request([]), Action, Outcome
    )
    assert control not in output
    assert "Recheck" in output
    service.plans["install"]["allowed"] = True
    service.plans["install"]["skill_id"] = f"skill{control}name"

    def uncertain(*_args, **_kwargs):
        raise OSError("outcome unavailable")

    service.install_apply = uncertain
    output = run_tui_action(
        "install skill-1", service, selected_request([]), Action, Outcome
    )
    assert "outcome is unknown" in output
    assert control not in output


def test_install_never_infers_partial_acknowledgement_from_warning_flags():
    """Warnings alone do not opt the user into partial compatibility."""
    service = Service()
    captured = []

    output = run_tui_action(
        "install skill-1@v2", service, selected_request(captured), Action, Outcome
    )

    assert json.loads(output.split("\n\n", 1)[0])["installed"] is True
    assert service.calls[-1] == ("install_apply", "receipt-install", False)
    reviewed = json.loads(captured[0][1])
    assert reviewed["apply_acknowledgements"] == {"accept_partial": False}


@pytest.mark.parametrize(
    ("raw", "method"),
    [
        ("install skill-1@v2 --accept-partial", "install_apply"),
        ("update skill-1 --accept-partial", "update_apply"),
        (
            "installed-setup plan skill-1 --version 2 --phase setup --index 0 "
            "--command 'printf configured'",
            "installed_setup_apply",
        ),
    ],
)
def test_exception_after_apply_starts_reports_unknown_outcome_without_blind_retry(raw, method):
    """A lost apply acknowledgement reports uncertainty rather than safe failure."""
    service = Service()
    original = getattr(service, method)

    def effect_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("acknowledgement lost after effect")

    setattr(service, method, effect_then_fail)
    output = run_tui_action(raw, service, selected_request([]), Action, Outcome)

    assert "outcome is unknown" in output.casefold()
    assert "do not retry" in output.casefold()
    assert "Nothing was changed" not in output


def test_update_binds_acknowledgements_before_review_and_uses_update_kernel():
    """Update review binds every acknowledgement before calling the update kernel."""
    service = Service()
    captured = []

    output = run_tui_action(
        "update skill-1 --preserve-modified --accept-sensitive --accept-partial",
        service, selected_request(captured), Action, Outcome,
    )

    assert service.calls == [
        ("update_plan", "skill-1", {
            "preserve_modified": True,
            "accept_sensitive": True,
            "accept_partial": True,
        }),
        ("native_update_inspect", "receipt-update"),
        ("prepare_update_deferral", "receipt-update"),
        ("update_apply", "receipt-update"),
    ]
    assert captured[0][2] == (
        Action("update", "Update exact reviewed package"),
        Action("defer", "Not Now"),
    )
    reviewed = json.loads(captured[0][1])
    assert reviewed["plan"] == service.plans["update"]
    assert reviewed["package_files"][0]["content"] == "# exact update\n"
    assert json.loads(output)["updated"] is True


def test_update_not_now_is_a_deliberate_durable_action_without_update_consent():
    """Not Now stages exact-version cooldown state without applying the receipt."""
    service = Service()
    captured = []

    output = run_tui_action(
        "update skill-1", service,
        selected_action_request(captured, "defer"), Action, Outcome,
    )

    deferral = {
        "owner": dict(OWNER),
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
        "not_now_suppression_days": 7,
    }
    assert captured[0][2] == (
        Action("update", "Update exact reviewed package"),
        Action("defer", "Not Now"),
    )
    assert json.loads(captured[0][1])["not_now"] == deferral
    assert service.calls == [
        ("update_plan", "skill-1", {
            "preserve_modified": False,
            "accept_sensitive": False,
            "accept_partial": False,
        }),
        ("native_update_inspect", "receipt-update"),
        ("prepare_update_deferral", "receipt-update"),
        ("defer_update", deferral),
    ]
    assert json.loads(output.split("\n\n", 1)[0]) == {
        "deferred": True,
        "skill_id": "skill-1",
        "version": 2,
        "not_now_suppression_days": 7,
        "preference_sync": "pending",
    }
    assert "Manual reopen: /wisdom update skill-1" in output
    assert "fresh exact review" in output
    assert "update_apply" not in {call[0] for call in service.calls}


@pytest.fixture(name="update_deferral_services")
def _update_deferral_services(tmp_path, monkeypatch):
    """Recreate services sharing one disposable durable owner-bound store."""
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    store.activate_installation_identity("hwi_fixture", "org-1")
    store.bind_qualification_owner(OWNER)

    def service():
        value = InvocationService(root, ("default", tmp_path))
        value._onboarding = SimpleNamespace(store=WisdomStore(root))  # pylint: disable=protected-access
        value.client = Mock(
            expected_organization_id="org-1", display_org_id="org-1",
            owner_user_id="member-1", identity={"owner": "member-1"},
        )
        value.client.agent_led_policy.return_value = {
            "org_id": "org-1",
            "usage_evidence_window_days": 7,
            "min_aggregate_invocations": 3,
            "consecutive_day_usage_counts": True,
            "repeated_edits_count": True,
            "max_recommendations_per_user_per_week": 3,
            "publication_mode": "moderated",
            "install_popularity_threshold": 2,
            "notification_defaults": {
                "skill_ready_to_share": True,
                "teammate_published": True,
                "update_available": True,
            },
            "manager_review_email_cadence": "immediate",
            "not_now_suppression_days": 7,
            "version": 1,
            "updated_by_user_id": "manager",
        }
        monkeypatch.setattr(value, "require_setup", lambda: None)
        monkeypatch.setattr(value, "native_owner", lambda: dict(OWNER))
        monkeypatch.setattr(value, "_installation_owner", lambda: dict(OWNER))
        monkeypatch.setattr(value, "check_owner", lambda: None)
        return value

    return service


def test_update_not_now_uses_verified_policy_and_survives_service_restart(
    update_deferral_services,
):
    """The existing preference store owns the exact policy-duration suppression."""
    first = update_deferral_services()
    plan = complete_plan("update")
    deferral = first.prepare_update_deferral(plan)
    result = first.defer_update(deferral, may_apply=lambda: True)

    assert deferral == {
        "owner": dict(OWNER),
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
        "not_now_suppression_days": 7,
    }
    assert result["deferred"] is True
    assert result["preference_sync"] == "pending"
    assert first.client.agent_led_policy.call_count == 2  # preparation and selection

    reopened = update_deferral_services()
    with reopened.store.transaction() as db:
        row = db.execute(
            "SELECT user_id,suppress_until-created_at,state FROM wisdom_preference_outbox"
        ).fetchone()
    assert tuple(row) == ("member-1", 7 * 86400, "pending")
    assert reopened.client.agent_led_policy.call_count == 0


@pytest.mark.parametrize("change", ["policy", "submitted_duration"])
def test_deferral_rejects_duration_drift_before_persisting(update_deferral_services, change):
    """Not Now cannot apply a stale review or a caller-chosen policy duration."""
    from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel

    service = update_deferral_services()
    deferral = service.prepare_update_deferral(complete_plan("update"))
    if change == "policy":
        service.client.agent_led_policy.return_value["not_now_suppression_days"] = 30
    else:
        deferral["not_now_suppression_days"] = 365

    with pytest.raises(WisdomConflict, match="policy changed"):
        service.defer_update(deferral, may_apply=lambda: True)

    with service.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wisdom_preference_outbox").fetchone()[0] == 0


def test_update_deferral_filter_expires_without_suppressing_new_versions(update_deferral_services):
    """Use the producer's real preference filter, including queued offline writes."""
    first = update_deferral_services()
    deferral = first.prepare_update_deferral(complete_plan("update"))
    first.defer_update(deferral, may_apply=lambda: True)
    reopened = update_deferral_services()
    from hermes_wisdom_plugin.preferences import WisdomPreferences, suppression_key  # pylint: disable=import-outside-toplevel

    reopened.client.suppress_recommendation.side_effect = ConnectionError("write unavailable")
    reopened.client.recommendation_mute.return_value = SimpleNamespace(org_id="org-1", muted=False)
    reopened.client.recommendation_suppressions.return_value = []
    with reopened.store.transaction() as db:
        until = db.execute("SELECT suppress_until FROM wisdom_preference_outbox").fetchone()[0]
    clock = [until - 1]
    preferences = WisdomPreferences(reopened, clock=lambda: clock[0])
    reference = deferral["reference"]
    references = [reference, {**reference, "version": 3}]
    before = preferences.check("org-1", references)
    assert before["available"] is True
    assert before["suppressed"] == {suppression_key(reference): until}
    clock[0] = until
    after = preferences.check("org-1", references)
    assert after["available"] is True
    assert not after["suppressed"]


def test_update_not_now_rechecks_invocation_after_kernel_owner_read(tmp_path, monkeypatch):
    """A revoked invocation must not stage suppression after an owner reread."""
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    store.activate_installation_identity("hwi_fixture", "org-1")
    store.bind_qualification_owner(OWNER)
    MediationStore(store, owner=OWNER)
    kernel = InvocationService(root, ("default", tmp_path))
    kernel._onboarding = SimpleNamespace(store=store)  # pylint: disable=protected-access
    kernel.client = Mock(
        expected_organization_id="org-1", display_org_id="org-1",
        owner_user_id="member-1", identity={"owner": "member-1"},
    )
    active = [True]

    def owner_read():
        active[0] = False
        return dict(OWNER)

    monkeypatch.setattr(kernel, "require_setup", lambda: None)
    monkeypatch.setattr(kernel, "native_owner", owner_read)
    service = Service()
    monkeypatch.setattr(service, "defer_update", kernel.defer_update)
    run_tui_action(
        "update skill-1", service, selected_action_request([], "defer"), Action, Outcome,
        may_apply=lambda: active[0],
    )
    assert active[0] is False
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wisdom_preference_outbox").fetchone()[0] == 0


def test_update_not_now_never_guesses_cooldown_when_policy_is_unavailable(
    tmp_path, monkeypatch
):
    """A missing policy withholds Not Now rather than substituting a local duration."""
    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    store.activate_installation_identity("hwi_fixture", "org-1")
    store.bind_qualification_owner(OWNER)
    service = InvocationService(root, ("default", tmp_path))
    service._onboarding = SimpleNamespace(store=store)  # pylint: disable=protected-access
    service.client = Mock(
        expected_organization_id="org-1", display_org_id="org-1",
        owner_user_id="member-1", identity={"owner": "member-1"},
    )
    service.client.agent_led_policy.side_effect = ConnectionError("policy unavailable")
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "native_owner", lambda: dict(OWNER))

    with pytest.raises(ConnectionError, match="policy unavailable"):
        service.prepare_update_deferral(complete_plan("update"))

    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wisdom_preference_outbox").fetchone()[0] == 0


def test_update_not_now_owner_change_cannot_stage_another_accounts_deferral(
    tmp_path, monkeypatch
):
    """The persisted preference write remains bound to the reviewed account owner."""
    from hermes_wisdom_plugin.errors import WisdomValidationError  # pylint: disable=import-outside-toplevel

    root = tmp_path / "wisdom"
    store = WisdomStore(root)
    store.activate_installation_identity("hwi_fixture", "org-1")
    store.bind_qualification_owner(OWNER)
    MediationStore(store, owner=OWNER)
    service = InvocationService(root, ("default", tmp_path))
    service._onboarding = SimpleNamespace(store=store)  # pylint: disable=protected-access
    service.client = Mock(
        expected_organization_id="org-1", display_org_id="org-1",
        owner_user_id="member-1", identity={"owner": "member-1"},
    )
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(
        service, "native_owner", lambda: {**OWNER, "account_id": "member-2"}
    )
    deferral = {
        "owner": dict(OWNER),
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
        "not_now_suppression_days": 7,
    }

    with pytest.raises(WisdomValidationError, match="deferral is invalid"):
        service.defer_update(deferral, may_apply=lambda: True)

    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wisdom_preference_outbox").fetchone()[0] == 0


def test_unavailable_not_now_policy_does_not_block_manual_update_review():
    """Policy failure removes only Not Now; exact update consent remains independent."""
    service = Service()
    captured = []

    def unavailable(_plan):
        raise ConnectionError("policy unavailable")

    service.prepare_update_deferral = unavailable
    output = run_tui_action(
        "update skill-1", service, selected_request(captured), Action, Outcome,
    )

    assert json.loads(output)["updated"] is True
    assert captured[0][2] == (Action("update", "Update exact reviewed package"),)
    assert "not_now" not in json.loads(captured[0][1])
    assert service.calls[-1] == ("update_apply", "receipt-update")


def test_update_not_now_lost_acknowledgement_is_uncertain_and_not_retried():
    """A post-write exception cannot be downgraded to a safe cancellation."""
    service = Service()
    effects = []

    def effect_then_fail(deferral, *, may_apply):
        assert may_apply() is True
        effects.append(deferral)
        raise RuntimeError("local acknowledgement lost")

    service.defer_update = effect_then_fail
    output = run_tui_action(
        "update skill-1", service,
        selected_action_request([], "defer"), Action, Outcome,
    )

    assert len(effects) == 1
    assert "may have been recorded" in output
    assert "Do not repeat" in output
    assert "fresh exact review" in output
    assert "Nothing was changed" not in output
    assert "update_apply" not in {call[0] for call in service.calls}


def test_update_not_now_selection_loses_authority_before_any_deferral_write():
    """A selected action remains inert when its invocation or owner fence expires."""
    service = Service()
    current = [True]

    def select_then_revoke(**_kwargs):
        current[0] = False
        return Result(Outcome.SELECTED, "defer")

    output = run_tui_action(
        "update skill-1", service, select_then_revoke, Action, Outcome,
        lambda: current[0],
    )

    assert "Nothing was changed" in output
    assert "defer_update" not in {call[0] for call in service.calls}
    assert "update_apply" not in {call[0] for call in service.calls}


def test_manual_update_reopen_uses_a_fresh_receipt_during_deferral():
    """Durable suppression never blocks an explicit command from preparing anew."""
    service = Service()
    receipts = iter(("deferred-receipt", "fresh-receipt"))
    reviews = []

    def new_plan(skill_id, **choices):
        service.calls.append(("update_plan", skill_id, choices))
        plan = complete_plan("update")
        plan["receipt"] = next(receipts)
        return plan

    service.update_plan = new_plan
    deferred = run_tui_action(
        "update skill-1", service,
        selected_action_request(reviews, "defer"), Action, Outcome,
    )
    reopened = run_tui_action(
        "update skill-1", service,
        selected_action_request(reviews, "update"), Action, Outcome,
    )

    assert json.loads(deferred.split("\n\n", 1)[0])["deferred"] is True
    assert json.loads(reopened)["updated"] is True
    assert [json.loads(review[1])["plan"]["receipt"] for review in reviews] == [
        "deferred-receipt", "fresh-receipt",
    ]
    assert ("update_apply", "fresh-receipt") in service.calls
    assert ("update_apply", "deferred-receipt") not in service.calls


def test_installed_setup_inspect_is_complete_and_plan_needs_separate_named_action():
    """Setup inspection is read-only and setup execution needs a separate action."""
    service = Service()
    captured = []

    inspected = run_tui_action(
        "installed-setup inspect skill-1 --version 2",
        service, selected_request(captured), Action, Outcome,
    )
    assert json.loads(inspected)["guidance"]["setup_instructions"] == [
        "Configure the fixture."
    ]
    assert "installed_setup_apply" not in {call[0] for call in service.calls}
    assert not captured

    applied = run_tui_action(
        "installed-setup plan skill-1 --version 2 --phase setup --index 0 "
        "--command 'printf configured'",
        service, selected_request(captured), Action, Outcome,
    )
    assert service.calls[-2:] == [
        ("installed_setup_plan", "skill-1", 2, "setup", 0, "printf configured"),
        ("installed_setup_apply", "receipt-installed_setup"),
    ]
    assert captured[0][2] == (Action("run_setup", "Run exact reviewed setup action"),)
    assert json.loads(captured[0][1]) == service.plans["installed_setup"]
    assert json.loads(applied)["state"] == "passed"


@pytest.mark.parametrize("outcome", [Outcome.CANCELLED, Outcome.UNAVAILABLE])
def test_cancel_or_unavailable_review_never_applies(outcome):
    """Cancelled and unavailable reviews cannot reach the apply kernel."""
    service = Service()

    output = run_tui_action(
        "install skill-1@v2", service,
        lambda **_kwargs: Result(outcome), Action, Outcome,
    )

    assert service.calls == [
        ("install_plan", "skill-1@v2", None),
        ("native_install_inspect", "receipt-install"),
    ]
    assert "Nothing was changed" in output


def test_unknown_action_never_applies():
    """A forged selected action identifier cannot authorize mutation."""
    service = Service()
    output = run_tui_action(
        "update skill-1", service,
        lambda **_kwargs: Result(Outcome.SELECTED, "forged"), Action, Outcome,
    )
    assert service.calls == [
        ("update_plan", "skill-1", {
            "preserve_modified": False,
            "accept_sensitive": False,
            "accept_partial": False,
        }),
        ("native_update_inspect", "receipt-update"),
        ("prepare_update_deferral", "receipt-update"),
    ]
    assert "Nothing was changed" in output


@pytest.mark.parametrize("raw", [
    "install --apply-receipt receipt-install",
    "update --apply-receipt receipt-update",
    "installed-setup apply receipt-setup",
])
def test_tui_slash_receipt_apply_bypasses_are_rejected_without_service_calls(raw):
    """TUI slash input cannot bypass review by supplying an existing receipt."""
    service = Service()
    reviews = []
    output = run_tui_action(raw, service, selected_request(reviews), Action, Outcome)
    assert "cannot apply a receipt" in output
    assert "terminal" in output
    assert not service.calls
    assert not reviews


def test_oversized_exact_package_review_fails_without_truncation_or_apply():
    """Oversized reviews fail closed and cite only real terminal alternatives."""
    service = Service()
    reviews = []
    service.native_install_inspect = lambda receipt: [{
        "path": "SKILL.md", "content": "x" * 70_000, "content_start": 0,
        "content_end": 70_000, "hash": "sha256:" + "c" * 64,
    }]

    output = run_tui_action(
        "install skill-1@v2 --accept-partial",
        service, selected_request(reviews), Action, Outcome,
    )

    assert "too large" in output.casefold()
    assert "TUI" in output
    assert "hermes wisdom install <skill[@vN]> --plan --json" in output
    assert "hermes wisdom install --apply-receipt <receipt> --json" in output
    assert "every exact file" not in output
    assert not reviews
    assert "install_apply" not in {call[0] for call in service.calls}


def test_review_runs_in_copied_context_on_worker_thread():
    """The async CommandRuntime integration must use asyncio.to_thread semantics."""
    service = Service()
    admission = contextvars.ContextVar("review-admission", default="missing")
    caller_thread = threading.get_ident()
    observed = []

    def request_review(**values):
        observed.append((admission.get(), threading.get_ident(), values["actions"][0].id))
        return Result(Outcome.SELECTED, values["actions"][0].id)

    runtime = CommandRuntime(
        "/tmp/wisdom-tui-action-state", ("default", __import__("pathlib").Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=request_review, can_request_review=lambda: admission.get() == "admitted",
        is_review_current=lambda: admission.get() == "admitted",
        review_action_type=Action, review_outcome_type=Outcome,
    )
    runtime._service = lambda: service  # type: ignore[method-assign]  # pylint: disable=protected-access
    token = admission.set("admitted")
    try:
        output = asyncio.run(runtime.slash("install skill-1@v2"))
    finally:
        admission.reset(token)
        runtime.close()

    assert json.loads(output.split("\n\n", 1)[0])["installed"] is True
    assert observed == [("admitted", observed[0][1], "install")]
    assert observed[0][1] != caller_thread


@pytest.mark.parametrize("admitted", [False, None, True])
def test_incomplete_or_false_dynamic_review_admission_hands_off_without_service(admitted):
    """Mutation needs both initial admission and the continuing-authority capability."""
    runtime = CommandRuntime(
        Path("/tmp/wisdom-tui-denied-state"), ("default", Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=lambda **_kwargs: pytest.fail("review must not be requested"),
        can_request_review=(lambda: admitted) if admitted is not None else None,
        is_review_current=None if admitted is True else (lambda: True),
        review_action_type=Action, review_outcome_type=Outcome,
    )
    runtime._service = lambda: pytest.fail(  # type: ignore[method-assign]  # pylint: disable=protected-access
        "denied TUI action must not construct or read a protected service"
    )
    try:
        for raw in (
            "install skill-1@v2", "update skill-1",
            "installed-setup inspect skill-1 --version 2",
        ):
            output = asyncio.run(runtime.slash(raw))
            assert "terminal" in output.casefold()
    finally:
        runtime.close()


def test_dynamic_review_admission_is_evaluated_per_captured_invocation():
    """Registration-time truth cannot authorize a later slash invocation."""
    admitted = contextvars.ContextVar("review-capability", default=False)
    service = Service()
    runtime = CommandRuntime(
        Path("/tmp/wisdom-tui-dynamic-state"), ("default", Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=selected_request([]), can_request_review=admitted.get,
        is_review_current=admitted.get,
        review_action_type=Action, review_outcome_type=Outcome,
    )
    runtime._service = lambda: service  # type: ignore[method-assign]  # pylint: disable=protected-access
    try:
        assert "terminal" in asyncio.run(runtime.slash("install skill-1@v2")).casefold()
        assert not service.calls
        token = admitted.set(True)
        try:
            output = asyncio.run(runtime.slash("install skill-1@v2 --accept-partial"))
        finally:
            admitted.reset(token)
        assert json.loads(output.split("\n\n", 1)[0])["installed"] is True
    finally:
        runtime.close()


def test_review_invalidated_after_outer_admission_never_constructs_private_service():
    """Worker admission is reread before protected plan/service preparation."""
    current = True
    service_calls = []

    def consume_outer_admission():
        nonlocal current
        current = False
        return True

    runtime = CommandRuntime(
        Path("/tmp/wisdom-tui-stale-before-worker"), ("default", Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=selected_request([]), can_request_review=consume_outer_admission,
        is_review_current=lambda: current,
        review_action_type=Action, review_outcome_type=Outcome,
    )
    def service_factory():
        service_calls.append(True)
        return Service()

    runtime._service = service_factory  # type: ignore[method-assign]  # pylint: disable=protected-access
    try:
        output = asyncio.run(runtime.slash("install skill-1@v2"))
    finally:
        runtime.close()

    assert "unavailable" in output.casefold()
    assert not service_calls


def test_unload_while_review_is_open_cannot_reach_mutation():
    """Closing the runtime invalidates the service before a selected action can apply."""
    entered = threading.Event()
    release = threading.Event()
    mutations = []

    class ClosingService(Service):
        """Track closure and reject mutation after closure."""

        def __init__(self):
            super().__init__()
            self.closed = False
            self.closed_event = threading.Event()

        def install_apply(self, receipt, *, accept_partial=False):
            if self.closed:
                raise RuntimeError("service closed")
            mutations.append((receipt, accept_partial))
            return {"installed": True}

        def close(self):
            self.closed = True
            self.closed_event.set()

    service = ClosingService()

    def request_review(**values):
        entered.set()
        assert release.wait(2)
        return Result(Outcome.SELECTED, values["actions"][0].id)

    runtime = CommandRuntime(
        tmp_path := Path("/tmp/wisdom-tui-unload-state"), ("default", tmp_path),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=request_review, can_request_review=lambda: True,
        is_review_current=lambda: True,
        review_action_type=Action, review_outcome_type=Outcome,
    )

    def service_factory():
        runtime._services.add(service)  # pylint: disable=protected-access
        return service

    runtime._service = service_factory  # type: ignore[method-assign]  # pylint: disable=protected-access

    async def scenario():
        task = asyncio.create_task(runtime.slash("install skill-1@v2"))
        assert await asyncio.to_thread(entered.wait, 2)
        runtime.close()
        assert service.closed is False
        release.set()
        return await task

    output = asyncio.run(scenario())
    assert "Nothing was changed" in output
    assert not mutations
    assert service.closed is True


def test_cancelled_slash_returns_without_closing_service_under_live_worker():
    """Cancellation revokes late apply while the synchronous worker owns cleanup."""
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    mutations = []

    class WorkerService(Service):
        """Record worker-owned closure and any attempted mutation."""

        def install_apply(self, receipt, *, accept_partial=False):
            mutations.append((receipt, accept_partial))
            return {"installed": True}

        def close(self):
            closed.set()

    service = WorkerService()

    def request_review(**values):
        entered.set()
        assert release.wait(2)
        return Result(Outcome.SELECTED, values["actions"][0].id)

    runtime = CommandRuntime(
        Path("/tmp/wisdom-tui-cancel-state"), ("default", Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=request_review, can_request_review=lambda: True,
        is_review_current=lambda: True,
        review_action_type=Action, review_outcome_type=Outcome,
    )

    def service_factory():
        runtime._services.add(service)  # pylint: disable=protected-access
        return service

    runtime._service = service_factory  # type: ignore[method-assign]  # pylint: disable=protected-access

    async def scenario():
        task = asyncio.create_task(runtime.slash("install skill-1@v2 --accept-partial"))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.25)
        assert not closed.is_set()
        assert not mutations
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()
    assert not mutations


def test_cancelled_slash_retrieves_late_worker_exception():
    """A detached TUI worker must not emit an unretrieved-task loop error."""
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class FailingCloseService(Service):
        """Expose a late worker cleanup failure to the event-loop regression."""

        def close(self):
            closed.set()
            raise RuntimeError("fixture close failed")

    service = FailingCloseService()

    def request_review(**values):
        entered.set()
        assert release.wait(2)
        return Result(Outcome.SELECTED, values["actions"][0].id)

    runtime = CommandRuntime(
        Path("/tmp/wisdom-tui-orphan-state"), ("default", Path("/tmp")),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=request_review, can_request_review=lambda: True,
        is_review_current=lambda: True,
        review_action_type=Action, review_outcome_type=Outcome,
    )

    def service_factory():
        runtime._services.add(service)  # pylint: disable=protected-access
        return service

    runtime._service = service_factory  # type: ignore[method-assign]  # pylint: disable=protected-access

    async def scenario():
        loop_errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        task = asyncio.create_task(runtime.slash("install skill-1@v2"))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.25)
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)
        for _ in range(3):
            await asyncio.sleep(0)
        return loop_errors

    try:
        errors = asyncio.run(scenario())
    finally:
        runtime.close()
    assert not errors


def test_settings_mode_review_shows_meanings_and_applies_one_named_choice():
    """The TUI reads live settings before one deliberate owner-bound mode change."""
    service = Service()
    captured = []

    output = run_tui_action(
        "settings", service, selected_action_request(captured, "mode_fixed"), Action, Outcome,
    )

    assert json.loads(output)["delivery_mode"] == "fixed"
    assert service.calls == [
        ("notification_settings",),
        ("set_notification_mode", "fixed", "agent"),
    ]
    title, body, actions = captured[0]
    assert title == "Review Collective Wisdom notification settings"
    assert actions == (
        Action("mode_agent", "Agent decides"),
        Action("mode_fixed", "Fixed notifications"),
    )
    reviewed = json.loads(body)
    assert reviewed["delivery_mode"] == "agent"
    assert "manual review remains" in reviewed["mode_meanings"]["fixed"]
    assert reviewed["gateway_mute"]["revision"] == 4
    assert reviewed["mute_control"] is None


def test_read_only_or_unavailable_settings_never_offer_dead_controls():
    """Missing setters and Gateway authority produce a useful read, not unusable actions."""
    service = Service()
    service.mode_writable = False
    service.mute_control = None
    captured = []

    output = run_tui_action("settings", service, selected_request(captured), Action, Outcome)

    assert json.loads(output)["delivery_mode"] == "agent"
    assert service.calls == [("notification_settings",)]
    assert not captured


def test_mute_uses_exact_control_and_stale_or_expired_menu_never_rebases():
    """A mute choice binds the captured Gateway revision and stale controls fail before effect."""
    from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel

    service = Service()
    captured = []
    output = run_tui_action(
        "mute 1w", service, selected_request(captured), Action, Outcome,
    )
    assert json.loads(output)["preference_sync"] == "pending"
    assert captured[0][2] == (Action("mute_1w", "Mute 1 week"),)
    assert service.calls[-1] == ("choose_notification_mute", "mute-control-1", "1_week")

    def stale(*_args, **_kwargs):
        raise WisdomConflict("notification preference changed")

    service.choose_notification_mute = stale
    output = run_tui_action(
        "mute off", service, selected_request([]), Action, Outcome,
    )
    assert "stale or expired" in output
    assert "Nothing was changed" in output
    assert "unknown" not in output.casefold()


def test_mute_and_sync_status_are_observational_but_retry_needs_selection():
    """Status reads never flush; known-safe sync work runs only through its named action."""
    service = Service()
    captured = []

    mute = json.loads(run_tui_action(
        "mute status", service, selected_request(captured), Action, Outcome,
    ))
    sync = json.loads(run_tui_action(
        "sync status", service, selected_request(captured), Action, Outcome,
    ))
    assert mute["gateway_available"] is True
    assert sync["delivery"]["uncertain"] == 1
    assert sync["mute"]["conflict"] == 1
    assert not captured
    assert "sync_retry" not in {call[0] for call in service.calls}

    cancelled = run_tui_action(
        "sync retry", service, lambda **_kwargs: Result(Outcome.CANCELLED), Action, Outcome,
    )
    assert "Nothing was changed" in cancelled
    assert "sync_retry" not in {call[0] for call in service.calls}

    retried = run_tui_action(
        "sync retry", service, selected_request(captured), Action, Outcome,
    )
    assert json.loads(retried)["can_retry"] is False
    assert captured[-1][2] == (Action("retry_sync", "Retry known-safe sync work"),)
    assert [call[0] for call in service.calls].count("sync_retry") == 1


def test_sync_retry_lost_acknowledgement_is_unknown_and_never_replayed():
    """A post-effect failure cannot be described as a harmless retry failure."""
    service = Service()
    effects = []

    def effect_then_fail(*, may_apply):
        assert may_apply() is True
        effects.append("retry")
        raise RuntimeError("ack lost")

    service.sync_retry = effect_then_fail
    output = run_tui_action(
        "sync retry", service, selected_request([]), Action, Outcome,
    )
    assert effects == ["retry"]
    assert "outcome is unknown" in output
    assert "Do not retry blindly" in output


def test_install_and_blocked_plan_can_defer_only_with_verified_policy():
    """Exact install references share durable deferral without weakening blocked reviews."""
    service = Service()
    deferrals = []

    def prepare(plan):
        assert plan["skill_id"] == "skill-1" and plan["version"] == 2
        return {
            "owner": dict(OWNER),
            "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
            "not_now_suppression_days": 7,
        }

    def defer(value, *, may_apply):
        assert may_apply() is True
        deferrals.append(value)
        return {"deferred": True, "skill_id": "skill-1", "version": 2,
                "not_now_suppression_days": 7, "preference_sync": "pending"}

    service.prepare_review_deferral = prepare  # type: ignore[method-assign]
    service.defer_review = defer  # type: ignore[method-assign]
    captured = []
    output = run_tui_action(
        "install skill-1@v2", service,
        selected_action_request(captured, "defer"), Action, Outcome,
    )
    assert json.loads(output.split("\n\n", 1)[0])["deferred"] is True
    assert captured[0][2] == (
        Action("install", "Install exact reviewed package"), Action("defer", "Not Now")
    )
    assert "install_apply" not in {call[0] for call in service.calls}

    service.plans["install"]["allowed"] = False
    blocked = []
    output = run_tui_action(
        "install skill-1@v2", service,
        selected_action_request(blocked, "defer"), Action, Outcome,
    )
    assert json.loads(output.split("\n\n", 1)[0])["deferred"] is True
    assert blocked[0][2] == (Action("defer", "Not Now"),)
    assert json.loads(blocked[0][1])["plan"]["allowed"] is False
    assert "Manual reopen: /wisdom install skill-1@v2" in output
    assert len(deferrals) == 2


def test_unverified_install_deferral_retains_fresh_command_recheck():
    """A policy outage never invents cooldown and cannot remove the safe Recheck path."""
    service = Service()
    service.plans["install"]["allowed"] = False
    service.prepare_review_deferral = Mock(side_effect=ConnectionError("policy unavailable"))
    captured = []

    output = run_tui_action(
        "install skill-1@v2", service, selected_request(captured), Action, Outcome,
    )

    assert "Recheck: /wisdom install skill-1@v2" in output
    assert not captured


def test_exact_update_recovery_requires_review_and_preserves_operation_identity():
    """A pending update resumes only its inspected operation/receipt pair."""
    service = Service()
    captured = []

    output = run_tui_action(
        "update recover skill-1 operation-1", service,
        selected_request(captured), Action, Outcome,
    )

    assert json.loads(output)["updated"] is True
    reviewed = json.loads(captured[0][1])
    assert reviewed["operation_id"] == "operation-1"
    assert reviewed["receipt"] == "receipt-update-recovery"
    assert captured[0][2] == (
        Action("resume_update", "Resume exact reviewed update operation"),
    )
    assert service.calls == [
        ("update_recovery_inspect", "skill-1", "operation-1"),
        ("update_recovery_resume", "skill-1", "operation-1", "receipt-update-recovery"),
    ]


def test_setup_recovery_reconciles_then_requires_explicit_abandon_consequence_review():
    """Unknown setup is never rerun; clearing it requires the exact acknowledgement review."""
    service = Service()
    captured = []

    output = run_tui_action(
        "installed-setup recover skill-1 operation-setup-1", service,
        selected_request(captured), Action, Outcome,
    )

    assert json.loads(output)["state"] == "abandoned"
    reviewed = json.loads(captured[0][1])
    assert reviewed["operation_id"] == "operation-setup-1"
    assert reviewed["receipt"] == "receipt-setup-recovery"
    assert reviewed["step"]["command"] == "printf maybe"
    assert captured[0][2] == (
        Action("abandon_setup", "Abandon after effects were checked and process stopped"),
    )
    assert service.calls == [
        ("installed_setup_recover", "skill-1", "operation-setup-1", False),
        ("installed_setup_recover", "skill-1", "operation-setup-1", True),
    ]


def test_recovery_effect_losing_acknowledgement_stays_unknown_without_repeat():
    """Recovery after its mutation boundary is never downgraded to a safe no-op."""
    service = Service()
    effects = []

    def effect_then_fail(skill_id, operation_id, receipt, *, may_apply):
        assert may_apply() is True
        effects.append((skill_id, operation_id, receipt))
        raise RuntimeError("ack lost")

    service.update_recovery_resume = effect_then_fail
    output = run_tui_action(
        "update recover skill-1 operation-1", service,
        selected_request([]), Action, Outcome,
    )
    assert effects == [("skill-1", "operation-1", "receipt-update-recovery")]
    assert "outcome is unknown" in output
    assert "operation-1" in output
    assert "Do not retry blindly" in output


def test_generic_review_deferral_supports_install_without_changing_update_interface(
    update_deferral_services,
):
    """Lane B can share exact-reference suppression while existing update callers stay stable."""
    service = update_deferral_services()
    install_plan = complete_plan("install")
    install = service.prepare_review_deferral(install_plan)
    update = service.prepare_update_deferral(complete_plan("update"))

    assert install == update == {
        "owner": dict(OWNER),
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
        "not_now_suppression_days": 7,
    }
    result = service.defer_review(install, may_apply=lambda: True)
    assert result["deferred"] is True
    assert result["preference_sync"] == "pending"


def test_real_settings_kernel_binds_mode_and_mute_to_current_snapshot(
    update_deferral_services,
):
    """Mode and mute writes use live owner/config/revision rather than a stale menu rebase."""
    from hermes_wisdom_plugin.client import WisdomMuteResponse  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

    service = update_deferral_services()
    config = {"notifications.delivery_mode": "agent"}
    service._get_config = config.get  # pylint: disable=protected-access
    service._set_config = config.__setitem__  # pylint: disable=protected-access
    service.client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=False, duration=None, muted_until=None,
        forever=False, revision=4,
    )
    service.client.set_recommendation_mute.side_effect = ConnectionError("offline")

    settings = service.notification_settings(
        may_apply=lambda: True, include_mute_control=True
    )
    assert settings["delivery_mode"] == "agent"
    assert settings["mode_writable"] is True
    assert settings["mute_control"]["mute"]["revision"] == 4
    assert service.set_notification_mode(
        "fixed", expected_mode="agent", may_apply=lambda: True
    ) == {"delivery_mode": "fixed"}

    preferences = WisdomPreferences(service)
    preferences.stage_mute("org-1", "1_day", expected_revision=4)
    with pytest.raises(WisdomConflict, match="changed|superseded"):
        service.choose_notification_mute(
            settings["mute_control"]["id"], "1_week", may_apply=lambda: True
        )

    fresh = service.notification_settings(
        may_apply=lambda: True, include_mute_control=True
    )
    with service.store.transaction() as db:
        db.execute(
            "UPDATE wisdom_mute_control SET expires_at=0 WHERE id=?",
            (fresh["mute_control"]["id"],),
        )
    with pytest.raises(WisdomConflict, match="expired"):
        service.choose_notification_mute(
            fresh["mute_control"]["id"], None, may_apply=lambda: True
        )


@pytest.mark.parametrize("include_mute_control", [False, True])
def test_settings_preparation_rechecks_invocation_after_gateway_read_before_control_write(
    update_deferral_services, include_mute_control,
):
    """An expired invocation cannot export settings or create a local mute control."""
    from hermes_wisdom_plugin.client import WisdomMuteResponse  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.errors import WisdomAuthError  # pylint: disable=import-outside-toplevel

    service = update_deferral_services()
    service.client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=False, duration=None, muted_until=None,
        forever=False, revision=4,
    )
    active = [True]

    def remote_then_revoke():
        active[0] = False
        return WisdomMuteResponse(
            org_id="org-1", muted=False, duration=None, muted_until=None,
            forever=False, revision=4,
        )

    service.client.recommendation_mute.side_effect = remote_then_revoke
    with pytest.raises(WisdomAuthError, match="no longer current"):
        service.notification_settings(
            may_apply=lambda: active[0], include_mute_control=include_mute_control
        )
    with service.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM wisdom_mute_control").fetchone()[0] == 0


def test_setter_only_config_is_read_only_without_fresh_readback(tmp_path, monkeypatch):
    """Do not perform a write that the host interface cannot verify afterward."""
    setter = Mock()
    service = InvocationService(tmp_path, ("default", tmp_path), set_config=setter)
    monkeypatch.setattr(service, "native_owner", lambda: dict(OWNER))

    assert service.notification_mode_writable() is False
    with pytest.raises(PermissionError, match="read-only"):
        service.set_notification_mode("fixed")
    setter.assert_not_called()


def test_sync_status_and_retry_include_preference_and_mute_without_replaying_conflict(
    update_deferral_services,
):
    """Explicit retry resets idempotent failures but leaves stale-revision conflicts alone."""
    from hermes_wisdom_plugin.preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

    service = update_deferral_services()
    MediationStore(service.store, owner=OWNER)
    preferences = WisdomPreferences(service)
    with service.store.transaction() as db:
        preferences.stage_suppression(
            db, org="org-1", user="member-1",
            reference={"kind": "skill", "skill_id": "skill-1", "version": 2},
            days=7,
        )
    preferences.stage_mute("org-1", "1_week", expected_revision=4)
    with service.store.transaction() as db:
        db.execute("UPDATE wisdom_preference_outbox SET state='failed',attempts=3")
        db.execute("UPDATE wisdom_mute_outbox SET state='conflict',attempts=1")

    before = service.sync_status()
    assert before["preference"]["retryable"] == 1
    assert before["mute"]["conflict"] == 1
    assert before["can_retry"] is True

    service.client.suppress_recommendation.side_effect = ConnectionError("offline")
    after = service.sync_retry(may_apply=lambda: True)
    assert after["preference"]["pending"] == 1
    assert after["preference"]["retryable"] == 0
    assert after["mute"]["conflict"] == 1
    assert after["can_retry"] is False


def test_sync_retry_rechecks_current_invocation_before_resetting_failed_work(
    update_deferral_services,
):
    """The retry review expiring after status leaves persisted attempts untouched."""
    from hermes_wisdom_plugin.preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

    service = update_deferral_services()
    MediationStore(service.store, owner=OWNER)
    preferences = WisdomPreferences(service)
    with service.store.transaction() as db:
        preferences.stage_suppression(
            db, org="org-1", user="member-1",
            reference={"kind": "skill", "skill_id": "skill-1", "version": 2},
            days=7,
        )
        db.execute("UPDATE wisdom_preference_outbox SET state='failed',attempts=3")

    with pytest.raises(Exception, match="no longer current"):
        service.sync_retry(may_apply=lambda: False)
    with service.store.transaction() as db:
        row = db.execute("SELECT state,attempts FROM wisdom_preference_outbox").fetchone()
    assert tuple(row) == ("failed", 3)


def test_update_recovery_service_reads_exact_journal_identity_before_resume(
    tmp_path, monkeypatch,
):
    """Recovery cannot swap an operation ID, skill, owner, or receipt after inspection."""
    from hermes_wisdom_plugin.update_execution import UpdateExecutor  # pylint: disable=import-outside-toplevel

    store = WisdomStore(tmp_path / "update-recovery")
    payload = {
        "owner": dict(OWNER), "receipt": "receipt-exact", "skill_id": "skill-1",
        "from_version": 1, "version": 2, "content_hash": "sha256:" + "a" * 64,
    }
    operation_id = store.journal("update", "skill-1", "local_ledger_committed", payload)
    executor = UpdateExecutor(
        Mock(), store, Mock(load=Mock(return_value=dict(payload))),
        Mock(), Mock(), lambda: dict(OWNER)
    )
    status = executor.recovery_status("skill-1", operation_id)
    assert status == {
        "kind": "update", "skill_id": "skill-1", "operation_id": operation_id,
        "receipt": "receipt-exact", "phase": "local_ledger_committed",
        "state": "pending", "from_version": 1, "version": 2,
        "content_hash": "sha256:" + "a" * 64, "can_resume": True,
    }

    applied = []

    def apply(receipt, *, may_apply=None, expected_recovery=None):
        assert may_apply is not None and may_apply() is True
        assert expected_recovery == status
        applied.append(receipt)
        return {"updated": True}

    executor.apply = apply  # type: ignore[method-assign]
    service = InvocationService(tmp_path / "runtime", ("default", tmp_path))
    monkeypatch.setattr(service, "_update_services", lambda: (Mock(), executor))
    monkeypatch.setattr(service, "check_owner", lambda: None)
    assert service.update_recovery_inspect("skill-1", operation_id) == status
    assert service.update_recovery_resume(
        "skill-1", operation_id, "receipt-exact", may_apply=lambda: True
    ) == {"updated": True}
    assert applied == ["receipt-exact"]

    with pytest.raises(Exception, match="identity|operation|receipt"):
        service.update_recovery_resume(
            "skill-1", "wrong-operation", "receipt-exact", may_apply=lambda: True
        )
    assert applied == ["receipt-exact"]
