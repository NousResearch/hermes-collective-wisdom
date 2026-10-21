"""Bounded CLI and shared-chat-safe text commands for Collective Wisdom.

The CLI dispatch shape is extracted from ``hermes_cli/subcommands/wisdom.py``
and the read-only projections from ``gateway/wisdom_command.py``, both at
``0924f7946b937c2ed02d083fc26752d577c54b3b``. Host registration, service
construction, native callbacks, and all mutations remain outside this module.
"""
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any, cast

from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomUnavailable,
    WisdomValidationError,
)
from .package import (
    MAX_METADATA_BYTES,
    MAX_TREE_BYTES,
    PackagePolicyError,
)
from .release_scope import active as _release_scope
from .share_command import run_share
from .share_suggest import run_share_suggest
from .catalog_table import COLUMNS as _CATALOG_COLUMNS
from .catalog_table import installed_cell as _installed_cell
from .catalog_table import one_line as _one_line
from .catalog_table import version_cell as _version_cell
from .top_command import markdown_cell as _markdown_cell
from .top_command import markdown_code_cell as _markdown_code_cell
from .top_command import page_size as _page_size
from .top_command import positive_page as _positive_page
from .top_command import refusal as _private_refusal
from .top_command import run_top
from .portal_urls import portal_version_url as _version_url
from .portal_urls import safe_portal_url as _safe_url
from .direct_services import WISDOM_DISCLOSURE
from .install_review import confirmed_file_install as _confirmed_file_install
from .terminal_ui import (
    edit_prepared as _edit_prepared,
    has_reviewable_warnings as _has_reviewable_warnings,
    render_action_review as _render_action_review,
    render_file_preview as _render_file_preview,
    render_findings as _render_findings,
    render_inventory as _render_inventory,
    render_operation_outcome as _render_operation_outcome,
    render_read_command as _render_read_command,
    review_identity as _review_identity,
    safe_scan_details as _safe_scan_details,
)


PAGE_SIZE = 5
_MAX_PAGE = 10_000
# A complete advanced export may expand each UTF-8 byte to a six-byte JSON
# escape. The remaining terms bound paths and the fixed inspect envelope.
MAX_PREPARED_PAYLOAD_BYTES = (
    MAX_TREE_BYTES * 6
    + MAX_METADATA_BYTES
    + 1024 * 1024
)
PRIVATE_RANKING_COMMANDS = frozenset({"top", "share-suggest"})
CONTRIBUTION_COMMANDS = frozenset({
    "candidate", "suggest", "share", "prepared", "publish", "publication",
})
# Commands that answer correctly on a profile that was never set up: setup
# itself, help, status (it reports "Setup: required"), and the owner-local
# usage rankings, which read only this profile's Hermes skill usage.
SETUP_EXEMPT_COMMANDS = frozenset({"setup", "help", "status"}) | PRIVATE_RANKING_COMMANDS
SETUP_PROMPT = (
    "Collective Wisdom isn't set up for this profile yet.\n\n"
    "Run `hermes wisdom setup` in your terminal. It shows what data leaves this machine "
    "and, after you accept, enrolls this profile with your team. Nothing has been changed."
)
_CHECK_LABELS = (
    ("security_check", "Security check"),
    ("professionalism_check", "Professionalism check"),
)
_CHECK_STATES = {
    "pass": "Pass",
    "advisory": "Advisory",
    "blocked": "Blocked",
    "pending": "Pending",
    "retry": "Pending",
    "running": "Pending",
    "unavailable": "Unavailable",
}
_PUBLIC_STATUS_STATES = {
    "active": "ready",
    "configured": "ready",
    "ready": "ready",
    "not_configured": "not configured",
    "degraded": "attention required",
    "unavailable": "unavailable",
}


class _SlashUsageError(ValueError):
    """A locally generated, safe-to-render slash usage error."""


class _DuplicateJsonKey(ValueError):
    """One JSON object repeated a key and is therefore ambiguous."""


def parse_slash(raw_args: str) -> tuple[str, list[str]] | None:
    """Return normalized shell words, or no command when quoting is malformed."""
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        return None
    command = parts[0].casefold() if parts else "help"
    return ("help", []) if command in {"--help", "-h"} else (command, parts[1:])


def setup_prompt_needed(command: str | None, service: object) -> bool:
    """Return whether ``command`` should answer with the setup step instead.

    Only a profile that has never been set up qualifies, judged from local state
    without credentials or a request. Any doubt falls through to the command's
    own checks, which still refuse every effect on an unenrolled profile.
    """
    if command is None or command in SETUP_EXEMPT_COMMANDS:
        return False
    pending = getattr(service, "setup_pending", None)
    if not callable(pending):
        return False
    try:
        return pending() is True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    """Return the stable fields used to fence a local JSON read."""

    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    return tuple(getattr(value, field) for field in fields)


def build_parser(  # pylint: disable=too-many-statements,too-many-locals
    parser: argparse.ArgumentParser,
) -> None:
    """Populate an already-created ``hermes wisdom`` argument parser."""

    parser.set_defaults(_wisdom_parser=parser)
    commands = parser.add_subparsers(dest="wisdom_command")
    scope = _release_scope()

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )
        return command

    setup = add("setup", "Validate entitlement and initialize this profile")
    setup.add_argument(
        "--accept-disclosure",
        action="store_true",
        help="Accept the local telemetry and owner-private draft disclosure",
    )
    add("status", "Show local and Gateway Wisdom status")
    if scope.allows_cli("settings"):
        settings = add("settings", "Inspect or explicitly change notification assessment mode")
        settings.add_argument(
            "notification_mode", nargs="?", default="status",
            choices=("status", "agent", "fixed"),
        )
    if scope.allows_cli("notifications"):
        notifications = add("notifications", "Show durable local Wisdom notices")
        notifications.add_argument("--mark-seen", action="store_true")
    if scope.allows_cli("mute"):
        mute = add("mute", "Manage organization-wide proactive Wisdom notifications")
        mute.add_argument(
            "mute_duration", nargs="?", default="status",
            choices=["status", "1d", "1w", "30d", "forever", "off"],
        )
    if scope.allows_cli("inbox"):
        add("inbox", "Read local Wisdom activity without polling or delivery")
    if scope.allows_cli("maintenance"):
        maintenance = add("maintenance", "Refresh policy or run bounded automatic updates")
        maintenance.add_argument("--refresh-policy", action="store_true")
        maintenance.add_argument(
            "--apply-automatic", action="store_true",
            help="Apply only updates freshly authorized by automatic server policy",
        )
    add("list", "List published Collective Wisdom skills")
    add("browse", "Search the organization catalog by keyword").add_argument(
        "query", nargs="?"
    )
    add("show", "Show one published skill").add_argument("skill_id")
    add("versions", "List published versions").add_argument("skill_id")
    top = add("top", "Rank your own local skills by recorded Hermes usage")
    top.add_argument("--page", type=_positive_page, default=1)
    top.add_argument("--per-page", type=_page_size, default=10,
                     help="Skills per page, 1 to 50 (default 10)")
    add("share-suggest", "List your top skills and how to ask Hermes which to share")
    suggest = add("suggest", "Prepare one manually selected owner-private local draft")
    share = add("share", "Share one exact local skill with your team")
    share.add_argument("skill")
    share.add_argument("--yes", action="store_true", help="Confirm sharing the checked package")
    share.add_argument("--accept-warnings", action="store_true",
                       help="Acknowledge exact local scanner warnings, separately from sharing")
    suggest.add_argument("skill")
    if scope.model_editorial:
        suggest.add_argument(
            "--allow-model-review",
            action="store_true", default=None,
            help=(
                "Authorize sending the exact private package and owner description to "
                "the current model provider for editorial and professionalism review"
            ),
        )
        suggest.add_argument(
            "--no-model-review", action="store_false", dest="allow_model_review",
            help="Keep preparation local without sending content to the configured model",
        )
    else:
        suggest.set_defaults(allow_model_review=False)

    prepared = commands.add_parser(
        "prepared", help="Inspect, save, or re-review one owner-private local draft"
    )
    prepared_actions = prepared.add_subparsers(dest="prepared_action", required=True)

    def prepared_action(name: str, help_text: str) -> argparse.ArgumentParser:
        action = prepared_actions.add_parser(name, help=help_text)
        action.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )
        return action

    prepared_action("inspect", "Export the complete exact editable payload").add_argument(
        "draft_id"
    )
    prepared_action("save", "Save one complete hash-bound JSON payload").add_argument(
        "payload"
    )
    review = prepared_action("review", "Re-run checks for the exact current draft")
    review.add_argument("draft_id")
    if scope.model_editorial:
        review.add_argument(
            "--allow-model-review", action="store_true",
            help="Authorize sending exact draft content to the configured model provider",
        )
    else:
        review.set_defaults(allow_model_review=False)
    edit = prepared_action("edit", "Edit a disposable UTF-8 copy in VISUAL or EDITOR")
    edit.add_argument("draft_id")
    publication = commands.add_parser(
        "publication", help="Review, submit, and reconcile exact contribution publication"
    )
    publication_actions = publication.add_subparsers(
        dest="publication_action", required=True
    )

    def publication_action(name: str, help_text: str) -> argparse.ArgumentParser:
        action = publication_actions.add_parser(name, help=help_text)
        action.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )
        return action

    publication_review = publication_action(
        "review", "Review exact bytes, checks, and current server policy"
    )
    publication_review.add_argument("draft_id")
    publication_review.add_argument(
        "--accept-warnings", action="store_true",
        help="Acknowledge exact current local scanner warnings for publication",
    )
    publication_action(
        "submit", "Explicitly submit one exact publication review receipt"
    ).add_argument("receipt")
    for action_name, help_text in (
        ("status", "Inspect durable publication state without network retry"),
        ("reopen", "Reconcile an uncertain publication without duplicate submission"),
        ("decline", "Decline or withdraw one known remote draft when safe"),
    ):
        publication_action(action_name, help_text).add_argument("operation_id")
    publication_action(
        "enroll", "Prove and register an exact already-published source for managed updates"
    ).add_argument("skill_name")
    install = add("install", "Review or apply an exact managed skill installation")
    install.add_argument("reference", nargs="?", default="")
    install.add_argument("--plan", action="store_true", help="Create a review without installing")
    install.add_argument("--apply-receipt", help="Apply an explicitly reviewed install receipt")
    install.add_argument("--accept-partial", action="store_true",
                         help="Acknowledge partial or setup-required compatibility")
    install.add_argument(
        "--accept-warnings", action="store_true",
        help="Acknowledge exact current local scanner warnings during review",
    )
    install.add_argument("--update-mode", choices=["MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"])
    add("installed", "Inspect this enrolment's managed installations")
    add("check", "Check one managed skill for an authoritative update").add_argument(
        "skill_id"
    )
    update = add("update", "Review or apply one exact managed skill update")
    update.add_argument("skill_id", nargs="?", default="")
    update.add_argument("--plan", action="store_true", help="Create a review without updating")
    update.add_argument("--apply-receipt", help="Apply an explicitly reviewed update receipt")
    update.add_argument(
        "--preserve-modified", action="store_true",
        help="Review a durable unmanaged copy of exact edited bytes before replacement",
    )
    update.add_argument(
        "--accept-sensitive", action="store_true",
        help="Bind acceptance of newly requested sensitive capabilities into the review",
    )
    update.add_argument(
        "--accept-partial", action="store_true",
        help="Bind acceptance of partial or setup-required compatibility into the review",
    )
    update.add_argument(
        "--accept-warnings", action="store_true",
        help="Acknowledge exact current local scanner warnings during review",
    )
    installed_setup = commands.add_parser(
        "installed-setup", help="Inspect and separately approve installed-skill setup"
    )
    setup_actions = installed_setup.add_subparsers(dest="setup_action", required=True)

    def setup_action(name: str, help_text: str) -> argparse.ArgumentParser:
        action = setup_actions.add_parser(name, help=help_text)
        action.add_argument(
            "--json", action="store_true", help="Emit stable machine-readable JSON"
        )
        return action

    inspect_setup = setup_action("inspect", "Inspect guidance and prerequisites")
    inspect_setup.add_argument("skill_id")
    inspect_setup.add_argument("--version", type=int)
    plan_setup = setup_action("plan", "Review one exact setup action")
    plan_setup.add_argument("skill_id")
    plan_setup.add_argument("--version", type=int, required=True)
    plan_setup.add_argument(
        "--phase", choices=["prerequisite", "setup", "verify"], required=True
    )
    plan_setup.add_argument("--index", type=int, default=0)
    plan_setup.add_argument("--command", default="")
    setup_action("apply", "Apply one setup review receipt").add_argument("receipt")
    status_setup = setup_action("status", "Reconcile and inspect setup progress")
    status_setup.add_argument("skill_id")
    status_setup.add_argument("--version", type=int)
    recover_setup = setup_action("recover", "Review or abandon uncertain setup work")
    recover_setup.add_argument("skill_id")
    recover_setup.add_argument("operation_id")
    recover_setup.add_argument(
        "--acknowledge-effects-checked-and-stopped", action="store_true"
    )
    uninstall = add("uninstall", "Move a managed skill to recoverable trash")
    uninstall.add_argument("skill_id")
    uninstall.add_argument("--yes", action="store_true", help="Confirm recoverable removal")
    add("help", "Show Collective Wisdom command help")


def run_cli(  # pylint: disable=too-many-branches,too-many-return-statements,too-many-statements
    args: argparse.Namespace, service: object
) -> int:
    """Run one parsed terminal command against an injected service."""

    command = getattr(args, "wisdom_command", None)
    as_json = bool(getattr(args, "json", False))
    if setup_prompt_needed(command, service):
        if as_json:
            _emit({"ok": False, "error": SETUP_PROMPT, "code": "setup_required",
                   "category": 6}, as_json=True, error=True)
        else:
            print(SETUP_PROMPT, file=sys.stderr)
        return 6
    try:
        if command in {"share", "suggest"}:
            return _contribution_cli(args, service)
        if command in {"top", "share-suggest"}:
            return (run_top if command == "top" else run_share_suggest)(args, service)
        if command == "setup":
            accepted = bool(args.accept_disclosure)
            if not accepted:
                if as_json or not sys.stdin.isatty():
                    raise PackagePolicyError(
                        "noninteractive setup requires --accept-disclosure"
                    )
                print(WISDOM_DISCLOSURE)
                answer = input("Enable Collective Wisdom for this profile? [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    return 7
                accepted = True
            result = service.setup(accept_disclosure=accepted)
        elif command in {
            "status", "settings", "notifications", "mute", "inbox",
            "maintenance", "list", "browse", "show", "versions",
            "publication", "installed", "check", "installed-setup",
        }:
            result = _run_noninteractive_cli(args, service, command)
        elif command == "prepared":
            result = _prepared_cli(args, service)
            if args.prepared_action == "edit":
                print("Draft saved locally.\n" + _human_render("prepared", result))
                return 0
        elif command in {"install", "update", "uninstall"}:
            action = {"install": _install_cli, "update": _update_cli, "uninstall": _uninstall_cli}
            result = action[command](args, service)
            if result is None:
                return 7
        elif command == "help":
            if as_json:
                _emit({"commands": list(_release_scope().cli_commands)}, as_json=True)
            else:
                getattr(args, "_wisdom_parser").print_help()
            return 0
        else:
            getattr(args, "_wisdom_parser").print_help()
            return 2
        _emit(result, as_json=as_json, command=command)
        if (command == "install" and isinstance(result, dict)
                and isinstance(result.get("setup"), dict)
                and result["setup"].get("state") != "passed"):
            return 8
        if (command, getattr(args, "publication_action", None)) == ("publication", "submit"):
            return _publication_result_exit(cast(dict[str, Any], result))
        return 0
    except WisdomUnavailable as exc:
        if as_json:
            _emit(
                {"ok": False, "error": str(exc), "category": exc.exit_code},
                as_json=True, error=True,
            )
        else:
            retry_safe = (
                command in {"status", "inbox", "list", "browse", "show", "versions",
                            "installed", "check"}
                or command == "settings" and args.notification_mode == "status"
                or command == "mute" and args.mute_duration == "status"
                or command == "notifications" and not args.mark_seen
                or command == "prepared" and args.prepared_action == "inspect"
                or command == "publication" and args.publication_action == "status"
                or command == "installed-setup" and args.setup_action == "inspect"
            )
            instruction = (
                "Retry this read-only command later." if retry_safe else
                "Check the current status before retrying; the action may have completed."
            )
            _emit(f"{exc} {instruction}", as_json=False, error=True)
        return exc.exit_code
    except WisdomError as exc:
        _emit(
            {"ok": False, "error": str(exc), "category": exc.exit_code},
            as_json=as_json, error=True,
        )
        return exc.exit_code
    except PackagePolicyError as exc:
        _emit(
            {"ok": False, "error": str(exc), "category": 6},
            as_json=as_json, error=True,
        )
        return 6
    except (KeyboardInterrupt, EOFError):
        return 7
    except Exception:  # pylint: disable=broad-exception-caught
        _emit(
            {
                "ok": False,
                "error": "Collective Wisdom is temporarily unavailable.",
                "category": 8,
            },
            as_json=as_json, error=True,
        )
        return 8


def _contribution_cli(args, service):
    """Dispatch terminal-owned contribution flows, keeping their consent distinct."""
    if args.wisdom_command == "share":
        return run_share(args, service)
    result = service.suggest(args.skill, allow_model_review=args.allow_model_review)
    if args.json:
        _emit(result, as_json=True, command="suggest")
        return 0
    presentation = {**result, "skill_name": args.skill} if isinstance(result, dict) else result
    return _publication_result_exit(_suggest_human(presentation, service))


def _run_noninteractive_cli(  # pylint: disable=too-many-return-statements,too-many-branches
    args: argparse.Namespace, service: Any, command: str,
) -> Any:
    """Dispatch commands that never own a prompt or cancellation exit."""
    if command == "status":
        return service.status(include_operations=True)
    if command == "settings":
        service.require_setup()
        if args.notification_mode != "status":
            service.set_notification_mode(args.notification_mode)
        return {
            "delivery_mode": service.notification_mode(),
            "writable": service.notification_mode_writable(),
        }
    if command == "notifications":
        return service.notifications(mark_seen=bool(args.mark_seen))
    if command == "mute":
        return service.mute(args.mute_duration)

    if command == "inbox":
        return service.inbox()
    if command == "maintenance":
        return service.maintenance(
            refresh_policy=bool(args.refresh_policy),
            apply_automatic=bool(args.apply_automatic),
        )
    if command in {"list", "browse", "show", "versions"}:
        service.require_setup()
        if command == "list":
            return service.list_skills()
        if command == "browse":
            return {"skills": service.search_skills(args.query)}
        if command == "show":
            return service.show(args.skill_id)
        return {"versions": service.versions(args.skill_id)}

    if command == "publication":
        return _publication_cli(args, service)
    if command == "installed":
        return {"installations": service.list_installations()}
    if command == "check":
        return service.check_update(args.skill_id)
    if command == "installed-setup":
        return _installed_setup_cli(args, service)
    raise AssertionError(f"unhandled noninteractive command: {command}")


def run_slash(  # pylint: disable=too-many-branches,too-many-return-statements
    parsed: tuple[str, list[str]] | None, service: object
) -> str:
    """Synchronously render one mutation-free, shared-chat-safe slash view."""

    if parsed is None:
        return "Could not parse /wisdom arguments."
    try:
        command, args = parsed
        if not _release_scope().allows_slash(command) or command in PRIVATE_RANKING_COMMANDS:
            # Private rankings are routed by CommandRuntime before this shared-safe view.
            return _shared_chat_refusal(command)
        if command == "list":
            command = "browse"
        if command == "help":
            if args:
                raise _SlashUsageError("Usage: /wisdom help")
            return _slash_help()
        if command in CONTRIBUTION_COMMANDS:
            if command == "share":
                if len(args) != 1:
                    return "Usage: /wisdom share <local-skill>"
                return (
                    "Sharing is only available in an admitted private review. "
                    "Open /wisdom share <local-skill> in a private chat; "
                    "no private state was opened here."
                )
            return (
                "Contribution content is private. Use an admitted private review or the terminal "
                "`hermes wisdom suggest`, `prepared`, and `publication` workflows. "
                "Inspect the complete draft before explicit submission; "
                "no private state was opened."
            )
        if command == "setup":
            return (
                "Set up Collective Wisdom\n\n"
                "Run `hermes wisdom setup` in your terminal to review the disclosure "
                "and configure this profile."
            )
        if command in {"settings", "mute"}:
            return (
                "Use an admitted private control or the terminal for notification settings.\n\n"
                f"Run `hermes wisdom {command} status` to inspect first. Nothing has been changed."
            )
        if command in {
            "install", "installed", "check", "update", "uninstall", "installed-setup"
        }:
            return _terminal_handoff(command)
        if command == "status":
            if args:
                raise _SlashUsageError("Usage: /wisdom status")
            return _render_status(service.status())
        if command == "browse":
            query, page = _paged_arguments(
                args, "Usage: /wisdom browse [query] [--page N]"
            )
            service.require_setup()
            return _render_browse(service.search_skills(query or None), query, page)
        if command == "show":
            if len(args) != 1:
                raise _SlashUsageError("Usage: /wisdom show <skill>")
            service.require_setup()
            return _render_show(service.resolve_skill(args[0]))
        if command == "versions":
            reference, page = _version_arguments(args)
            service.require_setup()
            return _render_versions(service.resolve_skill(reference), page)
    except _SlashUsageError as exc:
        return str(exc)
    except WisdomUnavailable as exc:
        return read_unavailable(parsed) or str(exc)
    except (WisdomNotFound, WisdomAuthError, WisdomConflict, WisdomValidationError) as exc:
        return _safe_service_error(exc)
    except Exception:  # pylint: disable=broad-exception-caught
        return "Collective Wisdom is temporarily unavailable. Try again shortly."
    return _unsupported_command()


def read_unavailable(parsed: tuple[str, list[str]]) -> str | None:
    """Offer a manual retry only for a read, not an ambiguous effect."""
    command, args = parsed
    if command not in {"status", "list", "browse", "show", "versions"}:
        return None
    retry = shlex.join((command, *args))
    retry = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in retry
    )
    return (
        "Collective Wisdom is temporarily unavailable.\n"
        f"Run /wisdom {retry} again to retry."
    )


def _uninstall_cli(args, service):
    """Preserve the reference terminal-only recoverable removal confirmation."""
    if not args.yes:
        if args.json or not sys.stdin.isatty():
            raise PackagePolicyError("noninteractive uninstall requires --yes")
        answer = input("Move this managed skill to recoverable Wisdom trash? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            return None
    return service.uninstall(_managed_reference(service, args.skill_id))


def _managed_reference(service, reference):
    """Map an exact active installation name to its ID, never guess among peers."""
    resolver = getattr(service, "resolve_managed_skill", None)
    if callable(resolver):
        return resolver(reference)
    listing = getattr(service, "list_installations", None)
    rows = cast(list[dict[str, Any]], listing()) if callable(listing) else []
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("slug") == reference and row.get("state", "active") == "active"]
    if len(matches) > 1:
        raise WisdomConflict("Managed skill name is ambiguous; use an exact ID with --json")
    if matches:
        skill_id = matches[0].get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise WisdomValidationError("Managed skill identity is unavailable")
        return skill_id
    return reference


def _publication_cli(args, service):
    """Dispatch explicit publication review, confirmation, and recovery."""

    action = args.publication_action
    if action == "review":
        return service.publication_review(
            _local_reference(service, args.draft_id),
            accept_warnings=bool(args.accept_warnings)
        )
    if action == "submit":
        resolver = getattr(service, "resolve_publication_receipt", None)
        receipt = resolver(args.receipt) if callable(resolver) else args.receipt
        return service.publication_submit(receipt)
    if action == "status":
        return service.publication_status(_operation_reference(service, args.operation_id))
    if action == "reopen":
        return service.publication_reopen(_operation_reference(service, args.operation_id))
    if action == "enroll":
        return service.publication_enroll(args.skill_name)
    if action == "decline":
        return service.publication_decline(_operation_reference(service, args.operation_id))
    raise PackagePolicyError("unsupported publication action")


def _local_reference(service, reference):
    resolver = getattr(service, "resolve_local_draft", None)
    return cast(str, resolver(reference)) if callable(resolver) else reference


def _operation_reference(service, reference):
    resolver = getattr(service, "resolve_publication_operation", None)
    return resolver(reference) if callable(resolver) else reference


def _prepared_cli(args, service):
    """Dispatch the bounded local draft adapter; it has no submission action."""

    if args.prepared_action == "inspect":
        return service.prepared_inspect(_local_reference(service, args.draft_id))
    if args.prepared_action == "review":
        return service.prepared_review(
            _local_reference(service, args.draft_id),
            allow_model_review=bool(args.allow_model_review)
        )
    if args.prepared_action == "edit":
        if args.json or not sys.stdin.isatty():
            raise PackagePolicyError("prepared edit requires an interactive terminal")
        return _edit_prepared(service, _local_reference(service, args.draft_id))
    if args.prepared_action == "save":
        payload = _read_prepared_payload(Path(args.payload))
        return service.prepared_save(
            _local_reference(service, payload["draft_id"]),
            author_description=payload["author_description"],
            files=payload["files"],
            expected_hashes=payload["expected_hashes"],
        )
    raise PackagePolicyError("unsupported prepared draft action")


def _read_prepared_payload(  # pylint: disable=too-many-branches
    path: Path,
) -> dict[str, Any]:
    """Read one bounded regular JSON input without following a final symlink."""

    maximum = MAX_PREPARED_PAYLOAD_BYTES
    try:
        before = path.lstat()
    except OSError as exc:
        raise PackagePolicyError("prepared save payload must be a regular JSON file") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PackagePolicyError("prepared save payload must be a regular JSON file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PackagePolicyError("prepared save payload must be a regular JSON file") from exc
    try:
        opened = os.fstat(descriptor)

        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
            raise PackagePolicyError("prepared save payload changed while reading")
        if opened.st_size > maximum:
            raise PackagePolicyError(
                f"prepared save payload exceeds {MAX_PREPARED_PAYLOAD_BYTES} bytes"
            )
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > maximum or _file_identity(path.lstat()) != _file_identity(opened):
            raise PackagePolicyError("prepared save payload changed while reading")
    finally:
        os.close(descriptor)
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except _DuplicateJsonKey as exc:
        raise PackagePolicyError(str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackagePolicyError("prepared save payload is not valid UTF-8 JSON") from exc
    required = {"draft_id", "author_description", "files", "expected_hashes"}
    if not isinstance(value, dict) or set(value) != required:
        raise PackagePolicyError(
            "prepared save payload requires only draft_id, author_description, "
            "files, and expected_hashes"
        )
    if not isinstance(value["draft_id"], str) or not isinstance(
        value["author_description"], str
    ) or not isinstance(value["files"], list) or not isinstance(
        value["expected_hashes"], dict
    ):
        raise PackagePolicyError("prepared save payload has invalid field types")
    return value


def _acknowledged_install_plan(args, service, plan):
    """Review warnings once and refuse an unapproved change on re-review."""
    if not _has_reviewable_warnings(plan) or args.accept_warnings:
        return plan
    scan = plan.get("local_scan") if isinstance(plan, dict) else None
    if not _safe_scan_details(scan):
        raise PackagePolicyError(
            "scanner warning details are unavailable; create a new review before installing"
        )
    if args.json or not sys.stdin.isatty():
        raise PackagePolicyError(
            "install has scanner warnings; inspect --plan and explicitly use --accept-warnings"
        )
    _emit(plan, as_json=False, command="install")
    if input("Acknowledge the scanner warnings? [y/N] ").strip().casefold() not in {
        "y", "yes",
    }:
        return None
    original_identity = _review_identity(plan)
    plan = service.install_plan(
        args.reference, update_mode=args.update_mode, accept_warnings=True
    )
    if not plan.get("setup_commands"):
        _emit(plan, as_json=False, command="install")
    if _review_identity(plan) != original_identity:
        if plan.get("setup_commands"):
            _emit(plan, as_json=False, command="install")
        answer = input(
            "The reviewed package or findings changed. Acknowledge this updated review? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            return None
    return plan


def _install_cli(args, service):  # pylint: disable=too-many-return-statements,too-many-branches
    """Apply an explicit clean install command; keep plan and receipt APIs explicit."""
    if args.apply_receipt:
        if args.reference or args.plan or args.update_mode is not None or args.accept_warnings:
            raise PackagePolicyError(
                "--apply-receipt cannot change the reviewed reference or policy"
            )
        return service.install_apply(args.apply_receipt, accept_partial=args.accept_partial)
    if not args.reference:
        raise PackagePolicyError("install requires a reference or --apply-receipt")
    plan = service.install_plan(
        args.reference,
        update_mode=args.update_mode,
        accept_warnings=bool(args.accept_warnings),
    )
    if args.plan:
        return plan
    plan = _acknowledged_install_plan(args, service, plan)
    if plan is None:
        return None
    if isinstance(plan, dict) and plan.get("allowed") is False:
        raise PackagePolicyError(
            "installation is blocked; inspect --plan and provide the listed "
            "specific acknowledgement"
        )
    if (plan.get("setup_commands") and not args.json and sys.stdin.isatty()
            and (args.accept_warnings or not _has_reviewable_warnings(plan))):
        _emit(plan, as_json=False, command="install")
    result = service.install_apply(plan["receipt"], accept_partial=args.accept_partial)
    if (plan.get("setup_commands") and not args.json and sys.stdin.isatty()
            and _confirmed_file_install(result)):
        print("Files installed; setup is still needed.")
        print("Hermes will run these commands on this computer:")
        for command in plan["setup_commands"]:
            print(command)
        answer = input(
            "Run setup now? Not now keeps files installed but not ready. [y/N] "
        )
        if answer.strip().casefold() in {"y", "yes"}:
            result = service.install_setup_apply(plan["receipt"])
    return _result_with_reviewed_slug(result, plan) if not args.json else result


def _result_with_reviewed_slug(result: Any, plan: dict[str, Any]) -> Any:
    """Keep a verified human name alongside thin effect results."""
    if isinstance(result, dict) and isinstance(plan.get("slug"), str):
        return {**result, "slug": plan["slug"]}
    return result


def _update_cli(args, service):  # pylint: disable=too-many-return-statements,too-many-branches
    """Keep all acknowledgements in the reviewed plan, never apply-time flags."""
    acknowledgements = (
        args.preserve_modified or args.accept_sensitive or args.accept_partial
        or args.accept_warnings
    )
    if args.apply_receipt:
        if args.skill_id or args.plan or acknowledgements:
            raise PackagePolicyError(
                "--apply-receipt cannot change the reviewed skill or acknowledgements"
            )
        return service.update_apply(args.apply_receipt)
    if not args.skill_id:
        raise PackagePolicyError("update requires a skill id or --apply-receipt")
    plan = service.update_plan(
        args.skill_id,
        preserve_modified=args.preserve_modified,
        accept_sensitive=args.accept_sensitive,
        accept_partial=args.accept_partial,
        accept_warnings=bool(args.accept_warnings),
    )
    if args.plan or args.json:
        return plan
    _emit(plan, as_json=False, command="update")
    if not sys.stdin.isatty():
        raise PackagePolicyError(
            "noninteractive update requires --plan then --apply-receipt"
        )
    if _has_reviewable_warnings(plan) and not args.accept_warnings:
        scan = plan.get("local_scan") if isinstance(plan, dict) else None
        if not _safe_scan_details(scan):
            raise PackagePolicyError(
                "scanner warning details are unavailable; create a new review before updating"
            )
        answer = input(
            "Acknowledge the displayed scanner warnings for this exact update? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            return None
        original_identity = _review_identity(plan)
        plan = service.update_plan(
            args.skill_id,
            preserve_modified=args.preserve_modified,
            accept_sensitive=args.accept_sensitive,
            accept_partial=args.accept_partial,
            accept_warnings=True,
        )
        _emit(plan, as_json=False, command="update")
        if _review_identity(plan) != original_identity:
            answer = input(
                "The reviewed package or findings changed. Acknowledge this updated review? [y/N] "
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                return None
    if isinstance(plan, dict) and plan.get("allowed") is False:
        print("Update remains blocked; create a new plan with the listed acknowledgements.")
        return None
    answer = input("Apply this exact reviewed update plan? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        return None
    result = service.update_apply(plan["receipt"])
    return _result_with_reviewed_slug(result, plan)


def _installed_setup_cli(args, service):
    """Dispatch explicit setup controls; no conversational confirmation exists."""
    if args.setup_action == "inspect":
        return service.installed_setup_inspect(_managed_reference(service, args.skill_id),
                                               version=args.version)
    if args.setup_action == "plan":
        return service.installed_setup_plan(
            _managed_reference(service, args.skill_id), version=args.version, phase=args.phase,
            index=args.index, command=args.command,
        )
    if args.setup_action == "apply":
        return service.installed_setup_apply(args.receipt)
    if args.setup_action == "status":
        return service.installed_setup_status(_managed_reference(service, args.skill_id),
                                              version=args.version)
    if args.setup_action == "recover":
        return service.installed_setup_recover(
            _managed_reference(service, args.skill_id), args.operation_id,
            acknowledged=args.acknowledge_effects_checked_and_stopped,
        )
    raise PackagePolicyError("unsupported installed setup action")


def _terminal_handoff(command):
    instructions = {
        "install": "hermes wisdom install <skill-name>",
        "installed": "hermes wisdom installed",
        "check": "hermes wisdom check <skill-name>",
        "update": "hermes wisdom update <skill-name> --plan",
        "installed-setup": "hermes wisdom installed-setup inspect <skill-name>",
        "uninstall": "hermes wisdom uninstall <skill-name>",
    }
    return ("Use the terminal for this private Wisdom action.\n\n"
            f"Run `{instructions[command]}`. Nothing has been changed.")


def _suggest_summary(prepared: object) -> str:
    data = prepared if isinstance(prepared, dict) else {}
    name = _text(data.get("skill_name") or data.get("slug") or "This skill")
    files = data.get("files") if isinstance(data.get("files"), list) else []
    security = data.get("security_check")
    security = security if isinstance(security, dict) else {}
    warning = _text(security.get("summary") or "No local scanner summary available")
    return "\n".join((
        "Draft prepared locally",
        "",
        f"Skill: {name}",
        f"Files: {len(files)}",
        f"Warnings: {warning}",
        "Nothing has been uploaded. The original skill is unchanged.",
    ))


def _print_named_resume(name: str) -> None:
    """Show only a human-resumable local draft when the terminal is not interactive."""
    if name:
        safe_name = shlex.quote(_without_controls(name))
        print("\nResume local review: hermes wisdom prepared review " + safe_name
              + "\nResume sharing review: hermes wisdom publication review " + safe_name)


def _suggest_human(  # pylint: disable=too-many-locals,too-many-branches
    # pylint: disable=too-many-return-statements,too-many-statements
    prepared: object, service: object
) -> dict[str, Any] | None:
    """Run a local review, warning acknowledgement, then distinct final consent."""

    data = prepared if isinstance(prepared, dict) else {}
    draft_id = str(data.get("draft_id") or data.get("local_draft_id") or "")
    if not draft_id:
        raise PackagePolicyError("prepared draft identity is unavailable")
    print(_suggest_summary(data))
    name = str(data.get("skill_name") or data.get("slug") or "")
    if not sys.stdin.isatty():
        _print_named_resume(name)
        return None
    while True:
        print(
            "\n1. Review files and warnings\n"
            "2. Edit draft\n"
            "3. Continue to sharing review\n"
            "4. Keep draft and exit"
        )
        choice = input("Choose 1-4: ").strip()
        if choice == "1":
            _review_draft_files(data)
            continue
        if choice == "2":
            saved = _edit_prepared(service, draft_id)
            print("\nDraft saved locally. No package code was executed.")
            data = service.prepared_review(draft_id, allow_model_review=False)
            print(_human_render("prepared", saved))
            continue
        if choice == "4":
            print("\nDraft kept. Resume with: hermes wisdom prepared review "
                  + shlex.quote(_without_controls(name)) if name else "\nDraft kept locally.")
            return None
        if choice != "3":
            print("Choose 1, 2, 3, or 4.")
            continue

        displayed = service.prepared_review(draft_id, allow_model_review=False)
        if not isinstance(displayed, dict):
            raise PackagePolicyError("prepared draft review is unavailable")
        print(
            "\nExact local package review\n"
            + _render_action_review("publication", displayed)
        )
        warnings = _has_reviewable_warnings(displayed)
        details = _safe_scan_details(displayed.get("local_scan"))
        if warnings and not details:
            print(
                "Draft kept: scanner warning details are unavailable; "
                "acknowledgement is blocked."
            )
            return None
        accepted = False
        if warnings:
            answer = input(
                "Acknowledge the displayed scanner warnings for this exact package? [y/N] "
            )
            if answer.strip().casefold() not in {"y", "yes"}:
                print("Draft kept; warnings were not accepted.")
                return None
            accepted = True
        review = service.publication_review(draft_id, accept_warnings=accepted)
        if not isinstance(review, dict):
            raise PackagePolicyError("publication review is unavailable")
        if _review_identity(review) != _review_identity(displayed):
            print("\nThe package or scanner findings changed during review.")
            print(_render_action_review("publication", review))
            if _has_reviewable_warnings(review):
                if not _safe_scan_details(review.get("local_scan")):
                    print("Draft kept: updated warning details are unavailable.")
                    return None
                answer = input("Acknowledge the updated scanner warnings? [y/N] ")
                if answer.strip().casefold() not in {"y", "yes"}:
                    print("Draft kept; updated warnings were not accepted.")
                    return None
        owner = service.binding() if callable(getattr(service, "binding", None)) else {}
        exact_review = {**review, "owner": owner if isinstance(owner, dict) else {}}
        print("\n" + _render_action_review("publication", exact_review))
        if input("Share this exact reviewed package? [y/N] ").strip().casefold() not in {
            "y", "yes",
        }:
            print("Draft kept; final sharing consent was not given.")
            return None
        receipt = review.get("receipt")
        if not isinstance(receipt, str) or not receipt:
            raise PackagePolicyError("publication review receipt is unavailable")
        result = service.publication_submit(receipt)
        print(_human_render("publication", result))
        return result


def _publication_result_exit(result: dict[str, Any] | None) -> int:
    """Map an optional interactive publication handoff onto CLI exit semantics."""

    if result is None:
        return 0
    if result.get("state") == "done" and result.get("publication_state") in {
        "published", "pending_moderation",
    }:
        return 0
    if result.get("state") == "rejected":
        return WisdomValidationError.exit_code
    return WisdomError.exit_code


def _review_draft_files(data: dict[str, Any]) -> None:
    """Show complete safe findings and an explicit, inert file preview menu."""

    print("\n" + _render_inventory(data))
    print(_render_findings(data))
    files = data.get("files") if isinstance(data.get("files"), list) else []
    while files:
        answer = input("Inspect file number (Enter to return): ").strip()
        if not answer:
            return
        try:
            index = int(answer) - 1
        except ValueError:
            print("Choose a listed file number or press Enter.")
            continue
        if not 0 <= index < len(files):
            print("Choose a listed file number or press Enter.")
            continue
        print("\n" + _render_file_preview(files[index]))


def _render_prepared_human(data: dict[str, Any]) -> str:
    """Keep draft navigation named without changing its exact internal binding."""
    files = cast(list[Any], data["files"]) if isinstance(data.get("files"), list) else []
    raw_name = data.get("skill_name") or data.get("slug")
    lines = ["Prepared draft", f"Skill: {_text(raw_name or 'This skill')}",
             f"Files: {len(files)}"]
    description = data.get("description") or data.get("drafted_description")
    if isinstance(description, str) and description:
        lines.append("Description: " + _text(description, limit=1024))
    lines.extend((_render_inventory(data), _render_findings(data)))
    if isinstance(raw_name, str) and raw_name:
        safe_name = shlex.quote(_without_controls(raw_name))
        lines.extend((f"Edit: hermes wisdom prepared edit {safe_name}",
                      f"Full export: hermes wisdom prepared inspect {safe_name} --json"))
    return "\n".join(lines)


def _human_render(  # pylint: disable=too-many-return-statements
    command: str | None, value: Any
) -> str:
    """Render bounded human summaries without exposing complete package bytes."""

    if command == "status":
        return _render_status(value)
    data = value if isinstance(value, dict) else {}
    if isinstance(data.get("error"), str):
        return "Error: " + _text(data["error"])
    if command == "check":
        return "\n".join((
            "Update check",
            f"Skill: {_text(data.get('slug') or 'This skill')}",
            f"State: {_text(data.get('state') or 'unknown')}",
        ))
    if command == "prepared":
        return _render_prepared_human(data)
    if command == "publication" and isinstance(data.get("operation_id"), str) and not data.get(
        "receipt"
    ):
        return _render_publication_outcome(data)
    if command in {"install", "update"} and (
        data.get("installed") is True or data.get("updated") is True
    ):
        return _render_operation_outcome(command, data)
    if command == "uninstall" and data.get("uninstalled") is True:
        return _render_operation_outcome(command, data)
    if command in {"publication", "install", "update", "installed-setup"}:
        return _render_action_review(command, value)
    return _render_read_command(command, value)


def _render_publication_outcome(data: dict[str, Any]) -> str:
    """Render operation state without inventing a package or upload outcome."""
    from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel

    state = _text(data.get("state") or "unknown")
    detail = data.get("detail")
    heading, next_step = publication_copy(data)
    managed = data.get("managed_enrollment")
    managed_success = (heading == "Published" and isinstance(managed, dict)
                       and managed.get("state") == "managed")
    lines = (["Publication result"] if managed_success else
             [f"Publication result: {heading}", next_step])
    if isinstance(detail, str) and detail:
        lines.append(_text(detail, limit=1024))
    lines.append(f"State: {state}")
    for key, label in (
        ("phase", "Phase"), ("publication_state", "Publication state"),
    ):
        value = data.get(key)
        if isinstance(value, str) and value:
            lines.append(f"{label}: {_text(value, limit=1024)}")
    if heading in {"Uncertain", "Waiting for team review", "Changes requested", "Rejected"}:
        lines.append("Use `hermes wisdom publication status <skill-name>` to inspect it.")
    return "\n".join(lines)


def _emit(
    value: Any, *, as_json: bool, command: str | None = None, error: bool = False
) -> None:
    stream = sys.stderr if error and not as_json else sys.stdout
    if as_json:
        print(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str),
            file=stream,
        )
    elif isinstance(value, (dict, list)):
        from .terminal_presentation import emit_terminal  # pylint: disable=import-outside-toplevel

        summary = _human_render(command, value)
        rows_key = "installations" if command == "installed" else "skills"
        rows = value.get(rows_key) if isinstance(value, dict) else None
        all_named = not isinstance(rows, list) or all(
            isinstance(row, dict) and isinstance(row.get("slug"), str) and row["slug"]
            for row in rows
        )
        if (not error and all_named and command not in {"show", "versions", "check"}
                and emit_terminal(command, value, stream, summary)):
            return
        print(summary, file=stream)
    else:
        print(_without_controls(value), file=stream)


def _slash_help() -> str:
    scope = _release_scope()
    private = [entry for entry, command in (
        ("/wisdom share <skill>", "share"), ("/wisdom top [page]", "top"),
        ("/wisdom share-suggest", "share-suggest"), ("/wisdom settings", "settings"),
        ("/wisdom mute", "mute"), ("/wisdom candidate <id>", "candidate"),
        ("/wisdom suggest <skill>", "suggest"), ("/wisdom publish <draft>", "publish"),
        ("/wisdom prepared <action>", "prepared"),
        ("/wisdom publication <action> <target>", "publication"),
    ) if scope.allows_slash(command)]
    rows: list[str] = []
    for entry in private:  # Keep every line inside an 80x24 host dialog.
        if rows and len(rows[-1]) + len(entry) + 12 <= 72:
            rows[-1] += " | " + entry
        else:
            rows.append(entry)
    return "\n".join(
        (
            "Collective Wisdom commands",
            "",
            "Read: /wisdom browse [query] | /wisdom show <skill>",
            "      /wisdom versions <skill> | /wisdom status | /wisdom setup",
            *(("Private: " if index == 0 else "         ") + row
              for index, row in enumerate(rows)),
            "Terminal: /wisdom install | /wisdom installed | /wisdom check",
            "          /wisdom update | /wisdom installed-setup | /wisdom uninstall",
            "/wisdom help",
            "Private effects open a review when supported; terminal paths are shown.",
        )
    )


def slash_args_hint() -> str:
    """Return the short host completion hint for the registered slash commands."""
    scope = _release_scope()
    return " | ".join(command for command in (
        "help", "status", "browse", "show", "top", "share", "share-suggest", "install",
    ) if scope.allows_slash(command))


def _shared_chat_refusal(command: str) -> str:
    if command in PRIVATE_RANKING_COMMANDS and _release_scope().allows_slash(command):
        return _private_refusal(command)
    return _unsupported_command()


def _unsupported_command() -> str:
    return "Unsupported Collective Wisdom command.\n\n" + _slash_help()


def _paged_arguments(args: list[str], usage: str) -> tuple[str, int]:
    values: list[str] = []
    page: int | None = None
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--page":
            if page is not None or index + 1 >= len(args):
                raise _SlashUsageError(usage)
            page = _page_number(args[index + 1], usage)
            index += 2
            continue
        if value.startswith("--"):
            raise _SlashUsageError(usage)
        values.append(value)
        index += 1
    return " ".join(values).strip(), page or 1


def _version_arguments(args: list[str]) -> tuple[str, int]:
    usage = "Usage: /wisdom versions <skill> [--page N]"
    values, page = _paged_arguments(args, usage)
    references = shlex.split(values)
    if len(references) != 1:
        raise _SlashUsageError(usage)
    return references[0], page


def _page_number(value: str, usage: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise _SlashUsageError(usage) from exc
    if not 1 <= page <= _MAX_PAGE:
        raise _SlashUsageError(usage)
    return page


def _render_status(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    configured = data.get("configured") is True
    state = _PUBLIC_STATUS_STATES.get(
        str(data.get("state") or "").casefold(),
        "ready" if configured else "not configured",
    )
    if not configured and state == "ready":
        state = "not configured"
    return "\n".join(
        (
            "Collective Wisdom status",
            "",
            f"Setup: {'ready' if configured else 'required'}",
            f"State: {state}",
        )
    )


_BROWSE_COLUMNS = ("#", "Skill", *_CATALOG_COLUMNS[1:])


def _browse_cells(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(item.get("slug") or item.get("id") or "Unnamed skill"),
        _version_cell(item.get("latest_version")),
        _installed_cell(item.get("installation_status")),
        _one_line(item.get("author_description") or "No description"),
    )


def _render_browse(rows: object, query: str, page: int) -> str:
    """One GFM table per page, matching ``/wisdom top``; details stay in show."""
    skills = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    start = (page - 1) * PAGE_SIZE
    selected = skills[start : start + PAGE_SIZE]
    heading = f'Results for "{_text(query)}"' if query else "Skills published by your team"
    lines = [f"**Shared skills · {_markdown_cell(heading)} · page {page}**", ""]
    if not selected:
        lines.append("No matching shared skills.")
    else:
        lines.extend((
            "| " + " | ".join(_BROWSE_COLUMNS) + " |",
            "|---:|:---|:---|:---|:---|",
        ))
        for number, item in enumerate(selected, start=start + 1):
            slug, version, installed, description = _browse_cells(item)
            # Catalog text is shown as typed: free text is a literal code span.
            lines.append(
                f"| {number} | {_markdown_code_cell(slug)} | {_markdown_cell(version)}"
                f" | {_markdown_cell(installed)} | {_markdown_code_cell(description)} |"
            )
        lines.extend(("", "Details: `/wisdom show <skill>`"))
    navigation = _browse_navigation(query, page, start + PAGE_SIZE < len(skills))
    if navigation:
        lines.extend(("", " · ".join(navigation)))
    return "\n".join(lines)


def _browse_navigation(query: str, page: int, has_next: bool) -> list[str]:
    hints: list[str] = []
    if page > 1:
        hints.append(f"Back: `{_paged_command('browse', query, page - 1)}`")
    if has_next:
        hints.append(f"Next: `{_paged_command('browse', query, page + 1)}`")
    return hints


def _installation_status_text(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    status = data.get("installation_status")
    if isinstance(status, dict) and isinstance(status.get("text"), str):
        return status["text"]
    return "Installation status unavailable"


def _render_show(detail: object) -> str:
    data = detail if isinstance(detail, dict) else {}
    skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
    latest_detail = data.get("latest_version_detail")
    latest_detail = latest_detail if isinstance(latest_detail, dict) else {}
    latest = latest_detail.get("version")
    latest = latest if isinstance(latest, dict) else {}
    versions = data.get("versions") if isinstance(data.get("versions"), list) else []
    version_numbers = [
        version
        for item in versions
        if isinstance(item, dict)
        if (version := _positive_version(item.get("version"))) is not None
        and item.get("state") in (None, "published")
    ]
    slug = _text(skill.get("slug") or skill.get("id") or "Shared skill")
    description = _text(
        latest.get("author_description")
        or skill.get("author_description")
        or "No description"
    )
    lines = [
        slug,
        "",
        description,
        f"Latest: v{max(version_numbers) if version_numbers else '?'}",
        _installation_status_text(data),
        f"Requirements: {_requirements_summary(latest.get('system_spec'))}",
        _review_states(latest),
    ]
    portal_url = _safe_url(data.get("portal_url"))
    if portal_url:
        lines.append(f"View in Portal: {portal_url}")
    lines.append(f"Versions: {_slash_command('versions', slug)}")
    lines.append(f"Back: {_slash_command_without_argument('browse')}")
    return "\n".join(lines)


def _render_versions(  # pylint: disable=too-many-locals
    detail: object, page: int
) -> str:
    data = detail if isinstance(detail, dict) else {}
    skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
    slug = _text(skill.get("slug") or skill.get("id") or "Shared skill")
    values = data.get("versions") if isinstance(data.get("versions"), list) else []
    versions = sorted(
        (
            item
            for item in values
            if isinstance(item, dict)
            and _positive_version(item.get("version")) is not None
            and item.get("state") in (None, "published")
        ),
        key=lambda item: int(item["version"]),
        reverse=True,
    )
    start = (page - 1) * PAGE_SIZE
    selected = versions[start : start + PAGE_SIZE]
    lines = [
        f"{slug} versions", "", _installation_status_text(data), "",
        f"Published versions - page {page}",
    ]
    base_url = _safe_url(data.get("portal_url"))
    for item in selected:
        number = int(item["version"])
        published = _text(
            item.get("published_at")
            or item.get("created_at")
            or "Immutable published version"
        )
        lines.extend(("", f"Version {number}", published, _review_states(item)))
        if isinstance(item.get("system_spec"), dict):
            lines.append(f"Requirements: {_requirements_summary(item['system_spec'])}")
        version_url = _version_url(base_url, number)
        if version_url:
            lines.append(f"View in Portal: {version_url}")
    if not selected:
        lines.extend(("", "No published versions."))
    has_next = start + PAGE_SIZE < len(versions)
    lines.extend(("", f"Back: {_slash_command('show', slug)}"))
    if page > 1:
        lines.append(f"Previous: {_paged_command('versions', slug, page - 1)}")
    if has_next:
        lines.append(f"Next: {_paged_command('versions', slug, page + 1)}")
    return "\n".join(lines)


def _requirements_summary(value: object) -> str:
    specification = value if isinstance(value, dict) else {}
    requirements = specification.get("requirements")
    requirements = requirements if isinstance(requirements, dict) else specification
    hermes = requirements.get("hermes")
    hermes = hermes if isinstance(hermes, dict) else {}
    runtime = requirements.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    parts: list[str] = []
    minimum = hermes.get("minimum_version")
    if isinstance(minimum, str) and minimum:
        parts.append(f"Hermes {_text(minimum)}+")
    for field, label in (("platforms", "OS"), ("architectures", "CPU")):
        values = requirements.get(field)
        if isinstance(values, list):
            safe_values = [_text(item) for item in values if isinstance(item, str)]
            if safe_values:
                parts.append(f"{label}: {', '.join(safe_values)}")
    enabled_runtime = _enabled_runtime(runtime)
    if enabled_runtime:
        parts.append("runtime: " + ", ".join(enabled_runtime))
    tools = requirements.get("tools")
    plugins = requirements.get("plugins")
    if isinstance(tools, list) and tools:
        parts.append(f"{len(tools)} tool requirement(s)")
    if isinstance(plugins, list) and plugins:
        parts.append(f"{len(plugins)} plugin requirement(s)")
    model_summary = _model_requirement_summary(requirements.get("model"))
    if model_summary:
        parts.append(model_summary)
    parts.extend(_opaque_requirement_summaries(requirements))
    return "; ".join(parts) or "No special requirements declared"


def _enabled_runtime(runtime: dict[str, Any]) -> list[str]:
    return [
        name for name in ("shell", "browser", "code") if runtime.get(name) is True
    ]


def _model_requirement_summary(value: object) -> str | None:
    model = value if isinstance(value, dict) else {}
    capabilities = _safe_list_count(model.get("capabilities"))
    context_declared = (
        isinstance(model.get("minimum_context_window"), int)
        and not isinstance(model.get("minimum_context_window"), bool)
        and model["minimum_context_window"] > 0
    )
    if capabilities or context_declared:
        qualifiers = []
        if capabilities:
            qualifiers.append(f"{capabilities} capability requirement(s)")
        if context_declared:
            qualifiers.append("context minimum declared")
        return "model: " + ", ".join(qualifiers)
    return None


def _opaque_requirement_summaries(requirements: dict[str, Any]) -> list[str]:
    """Count sensitive requirements without rendering their values."""

    summaries: list[str] = []
    for field, label in (
        ("credentials", "credential"),
        ("connections", "connection"),
        ("hardware", "hardware"),
    ):
        count = _safe_list_count(requirements.get(field))
        if count:
            summaries.append(f"{count} {label} requirement(s)")
    limitations = _safe_list_count(requirements.get("known_limitations"))
    if limitations:
        summaries.append(f"{limitations} known limitation(s)")
    filesystem = requirements.get("filesystem")
    filesystem = filesystem if isinstance(filesystem, dict) else {}
    readable = _safe_list_count(filesystem.get("read"))
    writable = _safe_list_count(filesystem.get("write"))
    if readable or writable:
        summaries.append(f"filesystem: {readable} read, {writable} write location(s)")
    network = requirements.get("network")
    network = network if isinstance(network, dict) else {}
    destinations = _safe_list_count(network.get("destinations"))
    if destinations:
        summaries.append(f"{destinations} network destination requirement(s)")
    return summaries


def _safe_list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _review_states(value: object) -> str:
    data = value if isinstance(value, dict) else {}
    lines = []
    for key, label in _CHECK_LABELS:
        check = data.get(key)
        check = check if isinstance(check, dict) else {}
        state = _CHECK_STATES.get(str(check.get("status") or "").casefold(), "Unavailable")
        lines.append(f"{label}: {state}")
    return " - ".join(lines)


def _safe_service_error(exc: Exception) -> str:
    if isinstance(exc, WisdomNotFound):
        return "That Collective Wisdom item was not found."
    if isinstance(exc, WisdomAuthError):
        return "Sign in again or ask your team administrator for Collective Wisdom access."
    if isinstance(exc, WisdomConflict):
        return "The Collective Wisdom item changed. Open it again to see its current state."
    return "The Collective Wisdom request was invalid."


def _positive_version(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _without_controls(value: object) -> str:
    return "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in str(value)
    )


def _text(value: object, *, limit: int = 512) -> str:
    return " ".join(_without_controls(value).split())[:limit]


def _slash_command(command: str, argument: str) -> str:
    return "/wisdom " + shlex.join((command, _without_controls(argument)))


def _slash_command_without_argument(command: str) -> str:
    return "/wisdom " + shlex.join((command,))


def _paged_command(command: str, argument: str, page: int) -> str:
    parts = [command]
    if argument:
        parts.append(_without_controls(argument))
    parts.extend(("--page", str(page)))
    return "/wisdom " + shlex.join(parts)
