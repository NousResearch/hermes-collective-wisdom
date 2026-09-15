"""Optional TUI review/action adapter over existing Wisdom receipt kernels."""

from __future__ import annotations

import json
import shlex
import unicodedata
from typing import Any, Callable

from .errors import WisdomConflict


_RECEIPT_BYPASS = (
    "TUI slash commands cannot apply a receipt directly. Use the terminal CLI "
    "for plan/apply receipt workflows. Nothing was changed."
)
_NOT_CHANGED = "Review cancelled or unavailable. Nothing was changed."
_MAX_REVIEW_BODY_CHARS = 65_536
_USAGE = {
    "settings": "Usage: /wisdom settings",
    "mute": "Usage: /wisdom mute [status|1d|1w|30d|forever|off]",
    "sync": "Usage: /wisdom sync [status|retry]",
    "installed": "Usage: /wisdom installed",
    "check": "Usage: /wisdom check <skill>",
    "install": (
        "Usage: /wisdom install <skill[@vN]> [--update-mode MANUAL|AUTO_WITH_NOTICE|REQUIRED] "
        "[--accept-partial]"
    ),
    "update": (
        "Usage: /wisdom update <skill> [--preserve-modified] [--accept-sensitive] "
        "[--accept-partial]\n"
        "   or: /wisdom update recover <skill> <operation-id>"
    ),
    "installed-setup": (
        "Usage: /wisdom installed-setup inspect|status <skill> [--version N]\n"
        "   or: /wisdom installed-setup plan <skill> --version N "
        "--phase prerequisite|setup|verify [--index N] [--command '<exact command>']\n"
        "   or: /wisdom installed-setup recover <skill> <operation-id>"
    ),
}


class _UsageError(ValueError):
    """Malformed TUI action input; rendered without exposing parser internals."""


class _ReviewTooLarge(ValueError):
    """The exact review cannot fit the host contract without truncation."""


def handles_tui_action(raw_args: str) -> bool:
    """Return whether the optional bridge owns this slash-command family."""

    try:
        words = shlex.split(raw_args or "")
    except ValueError:
        return False
    return bool(words and words[0].casefold() in _USAGE)


# Keep command-family dispatch and the host's review authority types explicit at
# this narrow adapter boundary; hiding either in a generic context weakens review.
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-branches
def run_tui_action(
    raw_args: str,
    service: Any,
    request_review: Callable[..., Any],
    review_action_type: type,
    review_outcome_type: type,
    may_apply: Callable[[], bool] | None = None,
) -> str:
    """Review one exact plan and call its existing apply kernel only on selection."""

    try:
        words = shlex.split(raw_args or "")
    except ValueError:
        return "Could not parse /wisdom arguments. Nothing was changed."
    if not words:
        return _USAGE["install"]
    command = words[0].casefold()
    arguments = words[1:]
    if _is_receipt_bypass(command, arguments):
        return _RECEIPT_BYPASS
    try:
        if command in {"installed", "check"}:
            if (command == "installed" and arguments) or (
                command == "check" and (len(arguments) != 1 or arguments[0].startswith("--"))
            ):
                raise _UsageError
            reader = service.list_installations if command == "installed" else (
                lambda: service.check_update(arguments[0])
            )
            return _authorized_read(service, may_apply or (lambda: True), reader)
        if command == "settings":
            if arguments:
                raise _UsageError
            return _settings(
                service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
        if command == "mute":
            return _mute(
                arguments, service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
        if command == "sync":
            return _sync(
                arguments, service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
        if command == "install":
            return _install(
                arguments, service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
        if command == "update":
            return _update(
                arguments, service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
        if command == "installed-setup":
            return _installed_setup(
                arguments, service, request_review, review_action_type, review_outcome_type,
                may_apply or (lambda: True),
            )
    except _UsageError:
        return _USAGE.get(command, "Unsupported Collective Wisdom command.")
    except _ReviewTooLarge:
        return _review_too_large_handoff(command)
    except Exception:  # pylint: disable=broad-exception-caught
        # Each apply boundary catches its own failures as uncertain outcomes.
        # Only failures before those boundaries may suggest a fresh review.
        handoff = _recheck_handoff(command, arguments) if command in {"install", "update"} else ""
        return "Collective Wisdom review is unavailable. Nothing was changed." + handoff
    return "Unsupported Collective Wisdom command. Nothing was changed."


# The explicit return points distinguish cancellation, revocation, deferral, and
# unknown apply outcomes; collapsing them would obscure those security states.
def _install(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    values, flags = _parse_options(
        arguments,
        value_options={"--update-mode"},
        boolean_options={"--accept-partial"},
    )
    if len(values) != 1:
        raise _UsageError
    update_mode = flags.get("--update-mode")
    if update_mode is not None and update_mode not in {
        "MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"
    }:
        raise _UsageError
    plan = service.install_plan(values[0], update_mode=update_mode)
    deferral = _prepare_deferral(service, plan, update=False)
    if not _reviewable(plan, "confirm_install"):
        if deferral is not None:
            return _blocked_deferral(
                "install", arguments, plan, deferral, service, request_review,
                action_type, outcome_type, may_apply,
            )
        return (
            _complete_json(plan)
            + "\n\nThis plan cannot currently be applied. Nothing was changed."
            + _recheck_handoff("install", arguments)
        )
    package_files = service.native_install_inspect(plan["receipt"])
    review = {
        "plan": plan,
        "package_files": package_files,
        "apply_acknowledgements": {
            "accept_partial": bool(flags.get("--accept-partial")),
        },
    }
    if deferral is not None:
        review["not_now"] = deferral
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Review Collective Wisdom install",
        plan=review,
        action_id="install",
        action_label="Install exact reviewed package",
        additional_actions=(("defer", "Not Now"),) if deferral is not None else (),
    )
    if not selected:
        return _NOT_CHANGED
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    if selected == "defer":
        assert deferral is not None
        return _apply_deferral(
            "install", arguments, plan, deferral, service, may_apply
        )
    try:
        result = service.install_apply(
            plan["receipt"], accept_partial=bool(flags.get("--accept-partial"))
        )
        skill_id = result.get("skill_id", plan.get("skill_id"))
        version = result.get("version", plan.get("version"))
        next_command = shlex.join((
            "/wisdom", "installed-setup", "inspect", str(skill_id),
            "--version", str(version),
        ))
        return (
            _complete_json(result)
            + "\n\nSetup is separately approved and was not run automatically.\n"
            + f"Next: {next_command}"
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_apply_outcome("install", plan)


def _update(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    if arguments and arguments[0].casefold() == "recover":
        return _update_recovery(
            arguments[1:], service, request_review, action_type, outcome_type, may_apply
        )
    values, flags = _parse_options(
        arguments,
        value_options=set(),
        boolean_options={"--preserve-modified", "--accept-sensitive", "--accept-partial"},
    )
    if len(values) != 1:
        raise _UsageError
    choices = {
        "preserve_modified": bool(flags.get("--preserve-modified")),
        "accept_sensitive": bool(flags.get("--accept-sensitive")),
        "accept_partial": bool(flags.get("--accept-partial")),
    }
    plan = service.update_plan(values[0], **choices)
    if not _reviewable(plan, "confirm_update"):
        deferral = _prepare_deferral(service, plan, update=True)
        if deferral is not None:
            return _blocked_deferral(
                "update", arguments, plan, deferral, service, request_review,
                action_type, outcome_type, may_apply,
            )
        return (
            _complete_json(plan)
            + "\n\nThis plan cannot currently be applied. Nothing was changed."
            + _recheck_handoff("update", arguments)
        )
    package_files = service.native_update_inspect(plan["receipt"])
    deferral = _prepare_deferral(service, plan, update=True)
    review = {
        "plan": plan,
        "package_files": package_files,
        "apply_acknowledgements": choices,
    }
    if deferral is not None:
        review["not_now"] = deferral
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Review Collective Wisdom update",
        plan=review,
        action_id="update",
        action_label="Update exact reviewed package",
        additional_actions=(("defer", "Not Now"),) if deferral is not None else (),
    )
    if not selected:
        return _NOT_CHANGED
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    if selected == "defer":
        assert deferral is not None
        return _apply_deferral(
            "update", arguments, plan, deferral, service, may_apply
        )
    try:
        return _complete_json(service.update_apply(plan["receipt"], may_apply=may_apply))
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_apply_outcome("update", plan)


# Setup keeps each pre-effect authority check and outcome visible at the call site.
def _installed_setup(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    if not arguments:
        raise _UsageError
    action = arguments[0].casefold()
    if action == "recover":
        return _setup_recovery(
            arguments[1:], service, request_review, action_type, outcome_type, may_apply
        )
    values, flags = _parse_options(
        arguments[1:],
        value_options={"--version", "--phase", "--index", "--command"},
        boolean_options=set(),
    )
    if len(values) != 1:
        raise _UsageError
    version = _positive_int(flags.get("--version"), required=action == "plan")
    if action in {"inspect", "status"}:
        if set(flags) - {"--version"}:
            raise _UsageError
        # TUI status is an observational snapshot. The terminal status kernel
        # polls handles and finalizes receipts, so it is not a read capability.
        return _authorized_read(
            service, may_apply,
            lambda: service.installed_setup_inspect(values[0], version=version),
        )
    if action != "plan":
        raise _UsageError
    phase = flags.get("--phase")
    if phase not in {"prerequisite", "setup", "verify"}:
        raise _UsageError
    index = _positive_int(flags.get("--index", "0"), required=True, allow_zero=True)
    command = flags.get("--command", "")
    plan = service.installed_setup_plan(
        values[0], version=version, phase=phase, index=index, command=command
    )
    if not isinstance(plan, dict) or not plan.get("allowed"):
        return (
            _complete_json(plan)
            + "\n\nThis setup action cannot be applied. Nothing was changed."
        )
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Review installed skill setup action",
        plan=plan,
        action_id="run_setup",
        action_label="Run exact reviewed setup action",
    )
    if not selected:
        return _NOT_CHANGED
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    try:
        return _complete_json(
            service.installed_setup_apply(plan["receipt"], may_apply=may_apply)
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_apply_outcome("installed-setup", plan)


# These parameters are the host-owned review authority boundary, not application state.
def _settings(  # pylint: disable=too-many-arguments
    service, request_review, action_type, outcome_type, may_apply, *, only_action=None
) -> str:
    """Present current mode/mute facts and apply at most one named choice."""

    mode_actions = {
        "mode_agent": ("Agent decides", "agent"),
        "mode_fixed": ("Fixed notifications", "fixed"),
    }
    mute_actions = {
        "mute_1d": ("Mute 1 day", "1_day"),
        "mute_1w": ("Mute 1 week", "1_week"),
        "mute_30d": ("Mute 30 days", "30_days"),
        "mute_forever": ("Mute forever", "forever"),
        "mute_off": ("Unmute", None),
    }
    plan = service.notification_settings(
        may_apply=may_apply, include_mute_control=only_action in mute_actions
    )
    offered = []
    if only_action is None and plan.get("mode_writable") is True:
        offered.extend(mode_actions)
    elif only_action in mute_actions and isinstance(plan.get("mute_control"), dict):
        offered = [only_action]
    if not offered:
        return _complete_json(plan)
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Review Collective Wisdom notification settings",
        plan=plan,
        action_id=offered[0],
        action_label=(mode_actions | mute_actions)[offered[0]][0],
        additional_actions=tuple(
            (identity, (mode_actions | mute_actions)[identity][0]) for identity in offered[1:]
        ),
    )
    if not selected or not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    try:
        if selected in mode_actions:
            return _complete_json(service.set_notification_mode(
                mode_actions[selected][1],
                expected_mode=plan["delivery_mode"],
                may_apply=may_apply,
            ))
        control = plan.get("mute_control")
        if not isinstance(control, dict) or not isinstance(control.get("id"), str):
            raise WisdomConflict("notification mute control is unavailable")
        return _complete_json(service.choose_notification_mute(
            control["id"], mute_actions[selected][1], may_apply=may_apply
        ))
    except WisdomConflict:
        return (
            "This notification settings review is stale or expired. Nothing was changed. "
            "Open `/wisdom settings` again for current controls."
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return (
            "The notification settings action may have completed, but its outcome is unknown. "
            "Do not repeat it blindly. Inspect `/wisdom settings` and `/wisdom sync status`."
        )


def _mute(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    """Keep mute status observational and each change behind a fresh review."""

    if not arguments or arguments == ["status"]:
        return _authorized_read(service, may_apply, service.notification_mute_status)
    choices = {
        "1d": "mute_1d", "1w": "mute_1w", "30d": "mute_30d",
        "forever": "mute_forever", "off": "mute_off",
    }
    if len(arguments) != 1 or arguments[0] not in choices:
        raise _UsageError
    return _settings(
        service, request_review, action_type, outcome_type, may_apply,
        only_action=choices[arguments[0]],
    )


# Status, cancellation, revocation, non-retryable work, and uncertain retry are
# intentionally separate results at this authority boundary.
def _sync(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    """Separate observational status from explicit known-safe outbox retry."""

    action = arguments[0].casefold() if len(arguments) == 1 else "status" if not arguments else ""
    if action not in {"status", "retry"}:
        raise _UsageError
    if action == "status":
        return _authorized_read(service, may_apply, service.sync_status)
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    status = service.sync_status()
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    if status.get("can_retry") is not True:
        return _complete_json(status) + "\n\nNo known-safe failed sync work is retryable."
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Review Collective Wisdom synchronization retry",
        plan={
            "status": status,
            "effect": (
                "Retry only failed idempotent preference, delivery settlement, and operation "
                "report work. Uncertain message sends are not resent."
            ),
        },
        action_id="retry_sync",
        action_label="Retry known-safe sync work",
    )
    if not selected or not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    try:
        return _complete_json(service.sync_retry(may_apply=may_apply))
    except Exception:  # pylint: disable=broad-exception-caught
        return (
            "Collective Wisdom sync retry may have reconciled some saved work, but its outcome "
            "is unknown. Do not retry blindly. Inspect `/wisdom sync status`; uncertain message "
            "sends remain non-replayable."
        )


def _prepare_deferral(service, plan, *, update: bool):
    """Offer Not Now only when the exact reference and current policy were verified."""

    try:
        prepare = service.prepare_update_deferral if update else service.prepare_review_deferral
        return prepare(plan)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


# Keep the blocked plan, exact deferral, review types, and current-authority gate
# explicit: none of these values can safely be reconstructed from another.
def _blocked_deferral(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    command, arguments, plan, deferral, service, request_review, action_type, outcome_type,
    may_apply,
) -> str:
    """Allow exact-reference deferral without turning a blocked plan into approval."""

    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title=f"Review blocked Collective Wisdom {command}",
        plan={"plan": plan, "not_now": deferral},
        action_id="defer",
        action_label="Not Now",
    )
    if not selected or not _still_authorized(service, may_apply):
        return (
            _complete_json(plan)
            + "\n\nThis plan cannot currently be applied. Nothing was changed."
            + _recheck_handoff(command, arguments)
        )
    return _apply_deferral(command, arguments, plan, deferral, service, may_apply)


def _apply_deferral(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    command, arguments, plan, deferral, service, may_apply
) -> str:
    try:
        defer = service.defer_update if command == "update" else service.defer_review
        result = defer(deferral, may_apply=may_apply)
        return _complete_json(result) + _manual_reopen_handoff(command, arguments)
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_deferral_outcome(plan)


# Recovery keeps inspected operation identity and fresh host authority separate.
def _update_recovery(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    """Inspect, then deliberately resume one exact durable update operation."""

    if len(arguments) != 2 or any(value.startswith("--") for value in arguments):
        raise _UsageError
    skill_id, operation_id = arguments
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    recovery = service.update_recovery_inspect(skill_id, operation_id)
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    if recovery.get("can_resume") is not True:
        return _complete_json(recovery)
    if (
        recovery.get("kind") != "update"
        or recovery.get("skill_id") != skill_id
        or recovery.get("operation_id") != operation_id
        or not isinstance(recovery.get("receipt"), str)
        or not recovery["receipt"]
    ):
        raise WisdomConflict("update recovery identity changed")
    review = {
        **recovery,
        "recovery_consequence": (
            "Resume only this exact receipt-bound operation. Existing committed phases may be "
            "reconciled; no new package review or operation is created."
        ),
    }
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Recover Collective Wisdom update",
        plan=review,
        action_id="resume_update",
        action_label="Resume exact reviewed update operation",
    )
    if not selected or not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    try:
        return _complete_json(service.update_recovery_resume(
            skill_id, operation_id, recovery["receipt"], may_apply=may_apply
        ))
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_recovery_outcome("update", skill_id, operation_id)


def _setup_recovery(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    arguments, service, request_review, action_type, outcome_type, may_apply
) -> str:
    """Reconcile recorded setup state, then explicitly abandon UNKNOWN only."""

    if len(arguments) != 2 or any(value.startswith("--") for value in arguments):
        raise _UsageError
    skill_id, operation_id = arguments
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    recovery = service.installed_setup_recover(
        skill_id, operation_id, acknowledged=False, may_apply=may_apply
    )
    if not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    if recovery.get("recovery_review") is not True:
        return _complete_json(recovery)
    if (
        recovery.get("operation_id") != operation_id
        or not isinstance(recovery.get("receipt"), str)
        or not recovery["receipt"]
    ):
        raise WisdomConflict("setup recovery identity changed")
    review = {
        **recovery,
        "recovery_consequence": (
            "Abandoning records that effects were checked and every remaining process was "
            "stopped. It does not undo effects, rerun the command, or make the skill ready."
        ),
    }
    selected = _request_selected(
        request_review,
        action_type,
        outcome_type,
        title="Recover installed skill setup",
        plan=review,
        action_id="abandon_setup",
        action_label="Abandon after effects were checked and process stopped",
    )
    if not selected or not _still_authorized(service, may_apply):
        return _NOT_CHANGED
    try:
        return _complete_json(service.installed_setup_recover(
            skill_id, operation_id, acknowledged=True, may_apply=may_apply
        ))
    except Exception:  # pylint: disable=broad-exception-caught
        return _unknown_recovery_outcome("setup", skill_id, operation_id)


def _authorized_read(service, may_read, reader) -> str:
    if not _still_authorized(service, may_read):
        return _NOT_CHANGED
    result = reader()
    if not _still_authorized(service, may_read):
        return _NOT_CHANGED
    return _complete_json(result)


def _has_display_controls(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _recheck_handoff(command: str, arguments: list[str]) -> str:
    if any(_has_display_controls(argument) for argument in arguments):
        return (
            f"\n\nRecheck: re-enter /wisdom {command} with a target and options "
            "without control characters. A fresh review requires explicit confirmation."
        )
    return (
        "\n\nRecheck: " + shlex.join(("/wisdom", command, *arguments))
        + "\nThis prepares a fresh exact review and requires a new explicit confirmation. "
        "It does not retry an operation. Resolve the reported blocker first."
    )


def _manual_reopen_handoff(command: str, arguments: list[str]) -> str:
    """Expose a fresh manual path without treating it as automatic re-presentation."""

    if any(_has_display_controls(argument) for argument in arguments):
        target = f"/wisdom {command} with the original target and options"
    else:
        target = shlex.join(("/wisdom", command, *arguments))
    return (
        f"\n\nManual reopen: {target}\n"
        "This prepares a fresh exact review and requires a new deliberate selection."
    )


def _review_too_large_handoff(command: str) -> str:
    workflows = {
        "install": (
            "`hermes wisdom install <skill[@vN]> --plan --json`, then "
            "`hermes wisdom install --apply-receipt <receipt> --json`"
        ),
        "update": (
            "`hermes wisdom update <skill-id> --plan --json`, then "
            "`hermes wisdom update --apply-receipt <receipt> --json`"
        ),
        "installed-setup": (
            "`hermes wisdom installed-setup plan <skill-id> --version <N> "
            "--phase <phase> --json`, then "
            "`hermes wisdom installed-setup apply <setup-receipt> --json`"
        ),
    }
    workflow = workflows.get(command, "the existing terminal plan and receipt commands")
    return (
        f"The complete review is too large for the TUI's {_MAX_REVIEW_BODY_CHARS:,}-character "
        f"review bound. Nothing was changed. Use the existing terminal plan/receipt workflow: "
        f"{workflow}."
    )


def _unknown_apply_outcome(operation: str, plan: dict) -> str:
    skill = str(plan.get("skill_id") or "<skill-id>")
    if _has_display_controls(skill):
        skill = "<skill-id>"
    if operation == "installed-setup":
        recovery = (
            f"Inspect with `/wisdom installed-setup status {shlex.quote(skill)}`, "
            "then use the reported operation ID with `hermes wisdom installed-setup recover` "
            "only after checking effects and stopping any remaining process."
        )
    else:
        recovery = (
            "Inspect with `/wisdom installed` and "
            f"`/wisdom check {shlex.quote(skill)}` before deciding whether "
            "the exact receipt needs recovery."
        )
    return (
        f"Collective Wisdom {operation} may have started or completed, but its outcome is unknown. "
        f"Do not retry or create a new plan blindly. {recovery}"
    )


def _unknown_deferral_outcome(plan: dict) -> str:
    """A lost local acknowledgement cannot be presented as a safe non-effect."""

    skill = str(plan.get("skill_id") or "<skill-id>")
    if _has_display_controls(skill):
        skill = "<skill-id>"
    return (
        "Collective Wisdom Not Now may have been recorded, but its outcome is unknown. "
        "Do not repeat the selection blindly. Manual review remains available with "
        f"`/wisdom update {shlex.quote(skill)}` and creates a fresh exact review."
    )


def _unknown_recovery_outcome(operation: str, skill_id: str, operation_id: str) -> str:
    """A recovery call may cross an idempotent effect boundary before acknowledgement."""

    safe_skill = "<skill-id>" if _has_display_controls(skill_id) else shlex.quote(skill_id)
    safe_operation = (
        "<operation-id>" if _has_display_controls(operation_id) else shlex.quote(operation_id)
    )
    command = (
        f"/wisdom update recover {safe_skill} {safe_operation}"
        if operation == "update"
        else f"/wisdom installed-setup recover {safe_skill} {safe_operation}"
    )
    return (
        f"Collective Wisdom {operation} recovery for {safe_operation} may have completed, "
        "but its outcome is unknown. Do not retry blindly. Inspect current state with "
        f"`{command}`; a new invocation and deliberate selection are required for any effect."
    )


def _still_authorized(service: Any, may_apply: Callable[[], bool]) -> bool:
    if may_apply() is not True:
        return False
    check_owner = getattr(service, "check_owner", None)
    if callable(check_owner):
        check_owner()
    return may_apply() is True


def _request_selected(  # pylint: disable=too-many-arguments
    request_review,
    action_type,
    outcome_type,
    *,
    title: str,
    plan: dict,
    action_id: str,
    action_label: str,
    additional_actions: tuple[tuple[str, str], ...] = (),
) -> str | None:
    body = _complete_json(plan)
    if len(body) > _MAX_REVIEW_BODY_CHARS:
        raise _ReviewTooLarge
    actions = (
        action_type(id=action_id, label=action_label),
        *(action_type(id=identity, label=label) for identity, label in additional_actions),
    )
    result = request_review(
        title=title,
        body=body,
        actions=actions,
    )
    if getattr(result, "outcome", None) is not outcome_type.SELECTED:
        return None
    selected = getattr(result, "action_id", None)
    allowed = {action.id for action in actions}
    return selected if selected in allowed else None


def _reviewable(plan: object, permitted: str) -> bool:
    return bool(
        isinstance(plan, dict)
        and plan.get("allowed") is True
        and permitted in plan.get("permitted_actions", ())
        and isinstance(plan.get("receipt"), str)
        and plan["receipt"]
    )


def _complete_json(value: object) -> str:
    """Render complete literal JSON; never summarize or truncate review content."""

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _parse_options(
    arguments: list[str], *, value_options: set[str], boolean_options: set[str]
) -> tuple[list[str], dict[str, Any]]:
    values: list[str] = []
    flags: dict[str, Any] = {}
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in boolean_options:
            if value in flags:
                raise _UsageError
            flags[value] = True
            index += 1
            continue
        if value in value_options:
            if value in flags or index + 1 >= len(arguments):
                raise _UsageError
            flags[value] = arguments[index + 1]
            index += 2
            continue
        if value.startswith("--"):
            raise _UsageError
        values.append(value)
        index += 1
    return values, flags


def _positive_int(value: Any, *, required: bool, allow_zero: bool = False) -> int | None:
    if value is None:
        if required:
            raise _UsageError
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _UsageError from exc
    if parsed < (0 if allow_zero else 1) or isinstance(value, bool):
        raise _UsageError
    return parsed


def _is_receipt_bypass(command: str, arguments: list[str]) -> bool:
    return "--apply-receipt" in arguments or (
        command == "installed-setup"
        and bool(arguments)
        and arguments[0].casefold() == "apply"
    )
