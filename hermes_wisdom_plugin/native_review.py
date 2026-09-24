"""Deterministic native views and PluginState-backed explicit reviews."""
# Self-explanatory immutable view records and narrow adapter methods do not gain clarity
# from repeating their signatures in docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,line-too-long,too-many-lines

from __future__ import annotations

import copy
import secrets
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .errors import SharePackagingError, WisdomAuthError, WisdomConflict, WisdomValidationError
from .native_context import NativeInvocation
from .native_inspection import inspection_pages, sharing_checks
from .review_state import ReviewState
from .terminal_ui import (
    has_reviewable_warnings, render_action_review, render_file_preview,
    render_findings, render_operation_outcome, review_identity, safe_scan_details,
    without_controls,
)

SLACK_SECTION_LIMIT = 2900
TELEGRAM_TEXT_LIMIT = 3500
_UNHANDLED = object()
_NATIVE_FILE_CHUNK = 480


@dataclass(frozen=True, slots=True)
class NativeAction:
    label: str
    action: str | None = None
    primary: bool = False
    url: str | None = None
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class NativeItem:
    title: str
    detail: str
    actions: list[NativeAction] = field(default_factory=list)
    preamble: str = ""
    literal: bool = False


@dataclass(frozen=True, slots=True)
class NativeView:
    title: str
    summary: str
    items: list[NativeItem] = field(default_factory=list)
    actions: list[NativeAction] = field(default_factory=list)
    notice: str = ""
    navigation_actions: list[NativeAction] = field(default_factory=list)
    dismissed: bool = False

    def to_text(self) -> str:
        lines = [self.title]
        if self.summary:
            lines.extend(("", self.summary))
        for item in self.items:
            if item.preamble:
                lines.extend(("", item.preamble))
            lines.extend(("", item.title))
            if item.detail:
                lines.append(item.detail)
            lines.extend(f"{action.label}: {action.url}" for action in item.actions if action.url)
        if self.notice:
            lines.extend(("", self.notice))
        lines.extend(f"{action.label}: {action.url}" for action in self.actions if action.url)
        return "\n".join(lines)


def callback_value(record_id: str, action: str) -> str:
    if not record_id or len(record_id) > 32 or not action or len(action) > 32:
        raise ValueError("invalid native Wisdom control")
    return f"wi:agent:{action}:{record_id}"


def parse_callback(value: str) -> tuple[str, str]:
    parts = str(value or "").split(":")
    if len(parts) != 4 or parts[:2] != ["wi", "agent"]:
        raise ValueError("Collective Wisdom control was not recognized.")
    action, record_id = parts[2:]
    if not action or not record_id or len(action) > 32 or len(record_id) > 32:
        raise ValueError("Collective Wisdom control was not recognized.")
    return record_id, action


def slack_section_text(value: object) -> str:
    escaped = str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").strip()
    return escaped if len(escaped) <= SLACK_SECTION_LIMIT else escaped[:SLACK_SECTION_LIMIT - 1].rstrip() + "…"


def telegram_view_text(view: NativeView) -> str:
    value = view.to_text()
    return value if len(value) <= TELEGRAM_TEXT_LIMIT else value[:TELEGRAM_TEXT_LIMIT - 1].rstrip() + "…"


def _safe(value: object, placeholder: str = "Unknown") -> str:
    text = str(value or "")
    if not text or any(unicodedata.category(character).startswith("C") for character in text):
        return placeholder
    return text


def _prepared_review(service: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Read a prepared review only when every returned identity matches the request."""
    requested = str(plan.get("draft_id") or plan.get("local_draft_id") or "")
    review = service.prepared_review(requested, allow_model_review=False)
    if not isinstance(review, dict):
        raise WisdomValidationError("prepared draft review is unavailable")
    identities = [review[key] for key in ("draft_id", "local_draft_id") if key in review]
    if not (requested and identities and all(
        isinstance(identity, str) and identity == requested for identity in identities
    )):
        raise WisdomConflict("The prepared draft changed; open it again")
    return review


def _approval_is_lossless(view: NativeView) -> bool:
    """Approval controls are allowed only when the rendered review remains complete."""
    if not any(action.primary for action in view.actions):
        return True
    def encoded(value: object) -> str:
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").strip()
    details = [
        (item.preamble + "\n\n" if item.preamble else "")
        + f"*{item.title}*" + ("\n" + item.detail if item.detail else "")
        for item in view.items
    ]
    return (len(view.to_text()) <= TELEGRAM_TEXT_LIMIT and len(view.items) <= 5
            and len(encoded(view.summary)) <= SLACK_SECTION_LIMIT
            and all(item.detail for item in view.items)
            and all(len(encoded(detail)) <= SLACK_SECTION_LIMIT for detail in details))


def _owner_matches(record: dict[str, Any], service: Any, invocation: NativeInvocation) -> bool:
    owner = service.native_owner()
    return (
        record.get("owner") == owner
        and invocation.profile == owner.get("profile_name")
        and bool(record.get("message_id"))
        and invocation.message_id == record.get("message_id")
        and record.get("route") == {
            "platform": invocation.platform,
            "profile": invocation.profile,
            "chat_id": invocation.chat_id,
            "actor_id": invocation.actor_id,
            "thread_id": invocation.thread_id,
            "session_key": invocation.session_key,
        }
    )


def _plan_owner_matches(record: dict[str, Any]) -> bool:
    """Reject authority metadata that conflicts with the persisted owner binding."""
    owner = record.get("owner")
    plan = record.get("plan")
    plan_owner = plan.get("owner") if isinstance(plan, dict) else None
    owner_fields = ("profile_name", "profile_home", "account_id", "organization_id",
                    "base_url", "installation_id")
    return not isinstance(plan_owner, dict) or (
        isinstance(owner, dict) and all(
            key not in plan_owner or plan_owner[key] == owner.get(key)
            for key in owner_fields
        )
    )


def _current_effect(
    current: dict[str, Any], expected: dict[str, Any], service: Any,
    invocation: NativeInvocation, authority: Callable[[], bool],
) -> bool:
    """Check the exact claimed review at the operation's real effect boundary."""
    try:
        return (
            authority() is True
            and current.get("state") == "applying"
            and current.get("id") == expected.get("id")
            and current.get("operation") == expected.get("operation")
            and current.get("plan") == expected.get("plan")
            and _plan_owner_matches(current)
            and _owner_matches(current, service, invocation)
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # A broken/revoked authority check must fail closed.
        return False


def _summary(plan: dict[str, Any]) -> str:
    operation = str(plan.get("operation") or "review").replace("_", " ").title()
    subject = _skill_name(plan)
    version = f" v{plan['version']}" if plan.get("version") else ""
    return f"{operation}: {subject}{version}"

def _skill_name(plan: dict[str, Any]) -> str:
    """Only a supplied human skill name may be presented as a name."""
    name = plan.get("slug") or plan.get("skill_name") or plan.get("name") or plan.get("editorial_name")
    if name == plan.get("skill_id") or name == plan.get("draft_id"):
        return "Name unavailable"
    return _safe(name, "Name unavailable")

def _installed_label(plan: dict[str, Any]) -> str:
    status = plan.get("installation_status")
    if isinstance(status, dict) and status.get("text"):
        return _safe(status["text"])
    version = plan.get("from_version")
    if isinstance(version, int) and not isinstance(version, bool):
        return f"Installed v{version}"
    return "Installation status unavailable"

def _local_installation_label(service: Any, plan: dict[str, Any]) -> dict[str, str]:
    """Advisory only; installation still uses the separately checked exact plan."""
    if isinstance(plan.get("installation_status"), dict):
        return plan["installation_status"]
    listed = getattr(service, "list_installations", None)
    if callable(listed):
        try:
            rows = listed()
            if isinstance(rows, list):
                for row in rows:
                    if (isinstance(row, dict) and row.get("skill_id") == plan.get("skill_id")
                            and row.get("state") == "active"):
                        return {"text": f"Installed v{_safe(row.get('version'), '?')}"}
                return {"text": "No managed installation recorded"}
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return {"text": "Installation status unavailable"}

def _public_review(value: Any) -> Any:
    """Hide opaque routing tokens, not the files, checks or decisions being approved."""
    if isinstance(value, dict):
        return {key: _public_review(child) for key, child in value.items()
                if key not in {"receipt", "draft_id", "local_draft_id", "skill_id"}}
    if isinstance(value, list):
        return [_public_review(child) for child in value]
    return value

def _inspection_pages(record: dict[str, Any]) -> list[str]:
    return inspection_pages({**record, "plan": _public_review(record["plan"])})


def _catalog_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("skills"), list):
        return [row for row in data["skills"] if isinstance(row, dict)]
    if isinstance(data.get("versions"), list):
        return [row for row in data["versions"] if isinstance(row, dict)]
    latest = data.get("latest_version_detail")
    skill = data.get("skill")
    if isinstance(skill, dict):
        combined = dict(skill)
        if isinstance(latest, dict):
            version = latest.get("version")
            combined.update(version if isinstance(version, dict) else latest)
        combined["installation_status"] = data.get("installation_status")
        combined["portal_url"] = data.get("portal_url")
        return [combined]
    return [data]


def _catalog_view(plan: dict[str, Any], state: str) -> NativeView:
    command = str(plan.get("command") or "browse").title()
    items = []
    for index, row in enumerate(_catalog_rows(plan.get("data"))[:5]):
        title = _skill_name(row)
        if title == "Name unavailable" and command == "Versions":
            title = _skill_name(plan)
        detail = row.get("author_description") or row.get("description") or ""
        version = row.get("version", row.get("latest_version"))
        facts = []
        if version is not None:
            facts.append(f"Version: v{_safe(version)}")
        if detail:
            facts.append(_safe(detail))
        facts.append(_installed_label(row))
        for label, key in (("Status", "state"), ("Author", "created_by_display_name")):
            if row.get(key):
                facts.append(f"{label}: {_safe(row[key])}")
        actions = []
        skill_id = row.get("id") or row.get("skill_id")
        if skill_id:
            actions.append(NativeAction("Install", f"catalog.install.{index}", primary=True))
        if row.get("portal_url"):
            actions.append(NativeAction("Open in Portal", url=_safe(row["portal_url"])))
        items.append(NativeItem(title, "\n".join(facts) or "Published skill", actions))
    if not items:
        items = [NativeItem("No matching skills", "The catalog returned no results.")]
    return NativeView(
        "Collective Wisdom catalog", f"{command}: " + (
            _skill_name(plan) if command != "Browse" else _safe(plan.get("target"), "All skills")
        ),
        items, notice=(f"State: {state}" if state != "pending" else ""),
        dismissed=state != "pending",
    )


def _fact_lines(value: Any, prefix: str = "") -> list[str]:
    """Render retained review facts deterministically without replacing them by a summary."""
    if isinstance(value, dict):
        lines = []
        for key, child in sorted(value.items()):
            if not prefix and key in {"owner", "operation", "allowed"}:
                continue
            words = str(key).replace("_", " ")
            label = words[:1].upper() + words[1:]
            lines.extend(_fact_lines(child, f"{prefix} {label}".strip()))
        return lines
    if isinstance(value, list):
        lines = []
        for index, child in enumerate(value, 1):
            lines.extend(_fact_lines(child, f"{prefix} {index}".strip()))
        return lines or [f"{prefix}: []"]
    return [f"{prefix}: {_safe(value, 'None')}"]


def _requires_partial_acknowledgement(plan: dict[str, Any]) -> bool:
    policy = plan.get("policy")
    return bool(
        isinstance(policy, dict)
        and policy.get("compatibility_requires_confirmation") is True
        and policy.get("accepted_partial") is not True
    )


def _fresh_package_plan(record: dict[str, Any], action: str, service: Any) -> dict:
    """Refresh the displayed package exactly and retain its owner/version binding."""
    old = record["plan"]
    if action == "install":
        skill_id = str(old.get("skill_id") or "")
        version = old.get("version")
        reference = f"{skill_id}@v{version}" if skill_id and isinstance(version, int) else skill_id
        fresh = service.install_plan(reference)
    else:
        fresh = service.update_plan(str(old.get("skill_id") or ""))
    if not isinstance(fresh, dict) or fresh.get("operation") != action:
        raise WisdomValidationError(f"native {action} review is invalid")
    for key in ("skill_id", "version", "content_hash", "manifest_hash"):
        if old.get(key) is not None and fresh.get(key) != old.get(key):
            raise WisdomConflict("The displayed package changed; open it again")
    if not _plan_owner_matches({**record, "plan": fresh}):
        raise WisdomAuthError("Collective Wisdom review owner changed")
    return fresh


def _package_needs_acknowledgement(plan: dict[str, Any]) -> bool:
    return (
        plan.get("allowed") is not True
        or has_reviewable_warnings(plan)
        or _requires_partial_acknowledgement(plan)
    )


def _validate_review_action(record: dict[str, Any], action: str) -> None:
    if action.startswith("settings.") and record.get("operation") == "settings":
        return
    plan = record.get("plan")
    if action not in {"confirm", "confirm.partial"} or not isinstance(plan, dict):
        raise WisdomValidationError("Collective Wisdom action was not recognized")
    operation = record.get("operation")
    if operation in {"install", "update"} and (
        not record.get("package_files")
        or record.get("inspection_receipt") != plan.get("receipt")
    ):
        raise WisdomValidationError("Inspect the receipt-bound package files before confirming")
    if operation in {"install", "update", "publish"} or record.get("screen"):
        pages = _inspection_pages(record)
        if (
            record.get("screen") != "approval"
            or record.get("page") != len(pages) - 1
            or record.get("inspected_through", -1) != len(pages) - 1
        ):
            raise WisdomValidationError("Inspect the complete exact review before confirming")
    receipt_missing = operation != "uninstall" and not str(plan.get("receipt") or "")
    if (
        operation not in {"install", "update", "publish", "setup", "uninstall"}
        or plan.get("allowed", True) is not True
        or _requires_partial_acknowledgement(plan) != (action == "confirm.partial")
        or receipt_missing
    ):
        raise WisdomValidationError("Collective Wisdom action is not allowed")


def _apply_review_action(  # pylint: disable=too-many-return-statements
    action: str, operation: str, plan: dict[str, Any], service: Any,
    may_apply: Callable[[], bool],
) -> Any:
    if action in {"settings.mode.agent", "settings.mode.fixed"}:
        return service.set_notification_mode(
            action.rsplit(".", 1)[-1],
            expected_mode=str(plan.get("delivery_mode") or ""), may_apply=may_apply,
        )
    if action in {"settings.mute.1w", "settings.mute.off"}:
        control = plan.get("mute_control")
        if not isinstance(control, dict) or control.get("revision") is None:
            raise WisdomValidationError("Notification mute control is unavailable")
        duration = "1_week" if action.endswith(".1w") else None
        return service.choose_notification_mute(
            str(control["revision"]), duration, may_apply=may_apply,
        )
    if operation == "install":
        return service.install_apply(
            plan["receipt"], accept_partial=action == "confirm.partial",
            may_apply=may_apply,
        )
    if operation == "update":
        return service.update_apply(plan["receipt"], may_apply=may_apply)
    if operation == "publish":
        return service.publication_submit(plan["receipt"], may_apply=may_apply)
    if operation == "setup":
        return service.installed_setup_apply(plan["receipt"], may_apply=may_apply)
    if operation == "uninstall":
        return service.uninstall(plan["skill_id"], may_apply=may_apply)
    raise WisdomValidationError("This review has no effect to confirm")


def _settings_result_view(result: dict[str, Any]) -> NativeView:
    if result.get("muted") is True and result.get("duration") == "1_week":
        return NativeView(
            "Notifications muted",
            "Skill recommendations and sharing suggestions are paused for one week. "
            "This only affects you; you can still browse and install skills.", dismissed=True,
        )
    if result.get("muted") is False:
        return NativeView("Notifications unmuted", "Recommendations are on for you.", dismissed=True)
    if result.get("delivery_mode") == "fixed":
        return NativeView("Automatic suggestions off", "You can still browse and install skills.", dismissed=True)
    if result.get("delivery_mode") == "agent":
        return NativeView("Automatic suggestions on", "You can turn them off in settings.", dismissed=True)
    return NativeView("Settings saved", "", dismissed=True)


def _settings_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    state = record["state"]
    if state == "uncertain":
        return NativeView(
            "Couldn't confirm the change",
            "Please open settings again to check your preference. We won't retry it automatically.",
            dismissed=True,
        )
    if state != "pending":
        result = record.get("result")
        return _settings_result_view(result if isinstance(result, dict) else {})
    control = plan.get("mute_control")
    muted = isinstance(control, dict) and control.get("muted") is True
    duration = control.get("duration") if muted else None
    current = (
        "Muted for one week" if duration == "1_week" else "Muted" if muted else
        "On" if isinstance(control, dict) else "Mute status unavailable"
    )
    if plan.get("delivery_mode") == "fixed":
        current += "; automatic suggestions off"
    item = NativeItem(f"Current: {current}", "")
    actions = [NativeAction("Cancel", "cancel", destructive=True)]
    if plan.get("mode_writable") is True:
        mode = str(plan.get("delivery_mode") or "fixed")
        if mode == "agent":
            actions.insert(0, NativeAction("Turn off automatic suggestions", "settings.mode.fixed"))
        else:
            actions.insert(0, NativeAction("Turn on automatic suggestions", "settings.mode.agent"))
    if isinstance(control, dict) and control.get("revision") is not None:
        actions.insert(0, NativeAction(
            "Unmute recommendations" if muted else "Mute one week",
            "settings.mute.off" if muted else "settings.mute.1w",
        ))
    return NativeView(
        "Collective Wisdom settings",
        "Pause sharing suggestions and skill recommendations for you, not your teammates. "
        "You can still browse and install skills.", [item], actions=actions,
    )


def _inspection_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    pages = _inspection_pages(record)
    page = int(record.get("page", 0))
    approving = record.get("screen") == "approval"
    _projected, authority = _authority_projection(record, plan)
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"page.{page - 1}"))
    if page + 1 < len(pages):
        actions.append(NativeAction("Next", f"page.{page + 1}"))
    elif approving and plan.get("allowed", True):
        partial = _requires_partial_acknowledgement(plan)
        actions.append(NativeAction(
            "Acknowledge partial compatibility and confirm" if partial else "Confirm",
            "confirm.partial" if partial else "confirm", primary=True,
        ))
    actions.append(NativeAction("Back" if approving else "Looks good.", "back"))
    return NativeView(
        "Exact package review" if approving else "Private package details",
        f"Page {page + 1} of {len(pages)}. " + (
            "Confirm only after inspecting the complete exact plan."
            if approving else "Inspection does not authorize sharing, installation or setup."
        ) + (("\n" + "\n".join(authority)) if authority else ""),
        [NativeItem("Exact review data (JSON)", pages[page], literal=True)], actions=actions,
    )


def _candidate_view(plan: dict[str, Any]) -> NativeView:
    facts = plan.get("facts") or {}
    safe, _checks = sharing_checks(plan)
    return NativeView(
        "You created a skill that could help your team",
        _safe(facts.get("name") or plan.get("editorial_name"), "Name unavailable"),
        items=[NativeItem(
            "✅ Safe To Share (security and professionalism checks all passed)"
            if safe else "Checks need review",
            "" if safe else "Required checks may be pending or unavailable. Open Review checks.",
        )],
        notice="Nothing has been uploaded or published. Share starts local preparation; "
        "you will still confirm the exact package before submission.",
        actions=[
            NativeAction("✅ Safe To Share" if safe else "Review checks", "checks"),
            NativeAction("View More Details", "details"),
            NativeAction("Snooze Collective Wisdom", "settings"),
            NativeAction("Maybe Later", "cancel"),
            NativeAction("Share My Skill", "share"),
        ],
    )


def _installation_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    updating = record["operation"] == "update"
    verb = "update" if updating else "install"
    description = plan.get("author_description") or plan.get("description")
    files = plan.get("files")
    setup_commands = plan.get("setup_commands")
    return NativeView(
        "A shared skill in your organization is available",
        _summary(plan),
        [NativeItem("About this skill", _safe(description, "Open details to learn more.")),
         NativeItem("On this profile", _installed_label(plan))]
        + ([NativeItem(f"Files: {len(files)}", "")] if isinstance(files, list) else [])
        + ([NativeItem("Setup", "\n".join(setup_commands))]
           if isinstance(setup_commands, list) and setup_commands
           and all(isinstance(command, str) for command in setup_commands) else [])
        + ([NativeItem("Agent explanation (not authorization)", record["explanation"])]
           if record.get("explanation") else []),
        notice=f"Would you like to {verb} this skill? View Details shows the files and checks. "
        "If there's something to review first, we'll show it before you continue. "
        "Any setup included with Install is shown before you choose it. "
        "If setup cannot finish, we'll tell you. Mute changes recommendations for all skills.",
        actions=[
            NativeAction("View Details", "details"), NativeAction("Not Now", "defer"),
            NativeAction("Mute Skill Recommendations", "settings"),
            NativeAction("Update" if updating else "Install", verb, primary=True),
        ],
    )


def _suggest_terminal_view(plan: dict[str, Any], state: str, result: Any = None) -> NativeView:
    name = _skill_name(plan)
    messages = {
        "kept": ("Draft kept", "This skill is still a local draft. Select Resume review here."),
        "cancelled": (
            "Sharing cancelled",
            "Nothing was shared by this review. Prepare the local skill again to start a new review.",
        ),
        "completed": (
            "Sharing request completed", "Check its publication result before assuming it is available to your team.",
        ),
    }
    title, detail = messages.get(state, (
        "Sharing outcome uncertain",
        "The result is uncertain. Check publication status before attempting another submission; do not retry blindly.",
    ))
    if state == "completed" and isinstance(result, dict):
        publication_state = result.get("publication_state")
        if publication_state == "pending_moderation":
            detail = "Submitted for team review; not published yet."
        elif publication_state == "published":
            detail = "Published to your team."
        elif publication_state in {"changes_requested", "declined", "invalidated"}:
            detail = "Not published; check the publication result before trying again."
    return NativeView(
        "Collective Wisdom contribution", name,
        [NativeItem(title, detail)],
        actions=[NativeAction("Resume review", "suggest.resume")] if state == "kept" else [],
        notice=f"State: {state}", dismissed=state != "kept",
    )


def _suggest_prepared_view(plan: dict[str, Any]) -> NativeView:
    files = [row for row in plan.get("files", []) if isinstance(row, dict)]
    security = plan.get("security_check")
    security = security if isinstance(security, dict) else {}
    detail = "\n".join((
        f"Files: {len(files)}",
        f"Warnings: {_safe(security.get('summary'), 'No local scanner summary available')}",
        "Nothing has been uploaded. The original skill is unchanged.",
    ))
    return NativeView(
        "Collective Wisdom contribution", f"{_skill_name(plan)} — prepared locally",
        [NativeItem("About this draft", detail)],
        actions=[
            NativeAction("Review files", "suggest.files"),
            NativeAction("Review checks", "suggest.checks"),
            NativeAction("Re-review edited draft", "suggest.refresh"),
            NativeAction("Continue to sharing review", "suggest.share"),
            NativeAction("Keep draft", "suggest.keep"),
            NativeAction("Cancel", "cancel", destructive=True),
        ],
        notice="To edit files, use the terminal's prepared-draft workflow. "
        "The draft reference is available in machine-readable output.",
    )


def _native_file_chunks(item: dict[str, Any]) -> list[str]:
    content = item.get("content_utf8")
    if not isinstance(content, str):
        return [render_file_preview(item)]
    if not content:
        return ["[Empty UTF-8 file]"]
    safe = without_controls(content, preserve_layout=True)
    if safe != content:
        safe = ("[Sanitized display: control bytes were replaced. "
                "Use prepared inspect --json for exact bytes.]\n" + safe)
    return [safe[offset:offset + _NATIVE_FILE_CHUNK]
            for offset in range(0, len(safe), _NATIVE_FILE_CHUNK)]


def _suggest_files_view(plan: dict[str, Any]) -> NativeView:
    files = [row for row in plan.get("files", []) if isinstance(row, dict)]
    page = max(0, min(int(plan.get("native_page") or 0), max(0, len(files) - 1)))
    if files:
        selected = files[page]
        chunks = _native_file_chunks(selected)
        chunk = max(0, min(int(plan.get("native_chunk") or 0), len(chunks) - 1))
        facts = " · ".join(
            f"{label}: {_safe(selected.get(key))}"
            for key, label in (("mode", "Mode"), ("size", "Size"))
            if selected.get(key) is not None
        )
        detail = (facts + "\n" if facts else "") + "---\n" + chunks[chunk] + "\n---"
        notice = f"File {page + 1} of {len(files)}; part {chunk + 1} of {len(chunks)}."
        item = NativeItem(_safe(selected.get("path"), "File"), detail)
    else:
        chunk = 0
        chunks = [""]
        notice = "No files were returned for this draft."
        item = NativeItem("No files", "Open the same prepared draft in the terminal.")
    navigation = []
    if chunk > 0 or page > 0:
        label = "Previous part" if chunk > 0 else "Previous file"
        navigation.append(NativeAction(label, "suggest.files.prev"))
    navigation.append(NativeAction("Back", "suggest.back"))
    if chunk + 1 < len(chunks) or page + 1 < len(files):
        label = "Next part" if chunk + 1 < len(chunks) else "Next file"
        navigation.append(NativeAction(label, "suggest.files.next"))
    return NativeView(
        "Collective Wisdom draft files", _skill_name(plan), [item],
        notice=notice, navigation_actions=navigation,
    )


def _suggest_checks_view(plan: dict[str, Any], *, warnings: bool = False) -> NativeView:
    details = render_findings(plan)
    complete = len(details) <= 2200
    shown = details if complete else details[:2199].rstrip() + "…"
    if not warnings:
        return NativeView(
            "Collective Wisdom draft checks", _skill_name(plan),
            [NativeItem("Local scanner findings", shown)],
            notice=("" if complete else "Checks are bounded here; inspect the same draft in the terminal."),
            navigation_actions=[NativeAction("Back", "suggest.back")],
        )
    actions = [
        NativeAction("Back", "suggest.back"),
        NativeAction("Cancel", "cancel", destructive=True),
    ]
    actions.insert(0, NativeAction(
        "Acknowledge displayed warnings and continue", "suggest.ack", primary=True,
    ))
    view = NativeView(
        "Collective Wisdom warning review", _skill_name(plan),
        [NativeItem("Scanner warnings for this exact package", shown)], actions=actions,
    )
    if complete and _approval_is_lossless(view):
        return view
    return NativeView(
        view.title, view.summary, view.items,
        actions=[action for action in actions if not action.primary],
        notice="Warnings cannot be acknowledged here; inspect the same draft in the terminal.",
    )


def _suggest_sharing_view(plan: dict[str, Any]) -> NativeView:
    actions = [
        NativeAction("Share this exact reviewed package", "suggest.confirm", primary=True),
        NativeAction("Back", "suggest.back"),
        NativeAction("Cancel", "cancel", destructive=True),
    ]
    view = NativeView(
        "Collective Wisdom sharing review", f"{_skill_name(plan)} — review before sharing",
        [NativeItem(
            _skill_name(plan),
            render_action_review("publication", _public_review(plan)),
        )], actions=actions,
    )
    if _approval_is_lossless(view):
        return view
    return NativeView(
        view.title, view.summary, view.items,
        actions=[action for action in actions if not action.primary],
        notice=(
            "This review is too large for approval here. Open the same prepared draft "
            "in the terminal and inspect the full review before sharing."
        ),
    )


def _suggest_view(plan: dict[str, Any], state: str, result: Any = None) -> NativeView:
    if state != "pending":
        return _suggest_terminal_view(plan, state, result)
    stage = str(plan.get("native_stage") or "prepared")
    views = {
        "prepared": _suggest_prepared_view,
        "files": _suggest_files_view,
        "checks": _suggest_checks_view,
        "warnings": lambda value: _suggest_checks_view(value, warnings=True),
        "sharing": _suggest_sharing_view,
    }
    render = views.get(stage)
    if render is not None:
        return render(plan)
    return NativeView(
        "Collective Wisdom contribution", _skill_name(plan),
        [NativeItem("Review unavailable", "Open the same prepared draft in the terminal.")],
        notice="Open this prepared draft in the terminal to continue.", dismissed=True,
    )


def _authority_projection(
    record: dict[str, Any], plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Attach and render the exact owner, expiry, and permission facts."""
    owner = record.get("owner")
    enriched = {**plan, **({"owner": owner} if isinstance(owner, dict) else {})}
    if record.get("expires_at") is not None:
        enriched["review_expires_at"] = record["expires_at"]
    owner_labels = (("organization_id", "Organization"),
                    ("account_id", "Owner account"), ("profile_name", "Profile"))
    lines = ([f"{label}: {_safe(owner.get(key))}" for key, label in owner_labels
              if owner.get(key)] if isinstance(owner, dict) else [])
    if record.get("expires_at") is not None:
        lines.append(f"Review expires: {_safe(record['expires_at'])}")
    permission = plan.get("allowed")
    if isinstance(permission, bool):
        lines.append("Approval permission: " + ("allowed" if permission else "blocked"))
    return enriched, lines


def _terminal_state_view(record: dict[str, Any]) -> NativeView | None:
    result = record.get("result")
    if (
        record.get("state") == "completed" and isinstance(result, dict)
        and result.get("publication_state") == "published"
        and isinstance(result.get("detail"), str) and result["detail"]
    ):
        plan = record.get("plan")
        name = _skill_name(plan if isinstance(plan, dict) else {})
        return NativeView("Publication result", f"{name}\n{_safe(result['detail'])}", dismissed=True)
    if record.get("failure") == "share_packaging_failed":
        return NativeView(
            "Share Packaging failed",
            "The local skill could not be packaged. Review its files and try again. "
            "Nothing has been uploaded or published.", dismissed=True,
        )
    if record.get("state") == "cancelled":
        original = project_view({**record, "state": "pending"})
        return replace(
            original, actions=[], navigation_actions=[], dismissed=True,
            items=[replace(item, actions=[]) for item in original.items],
        )
    if record.get("failure") == "share_preparation_failed" and record.get("state") == "pending":
        return NativeView(
            "Share preparation failed",
            "The sharing review could not be prepared. Try again or dismiss this card. "
            "Nothing has been uploaded or published.",
            actions=[NativeAction("Try again", "share"), NativeAction("Maybe Later", "cancel")],
        )
    return None


def project_view(record: dict[str, Any]) -> NativeView:  # pylint: disable=too-many-return-statements
    terminal = _terminal_state_view(record)
    if terminal is not None:
        return terminal
    raw_plan = record.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    state = str(record.get("state") or "pending")
    if state == "pending" and record.get("screen") == "checks":
        safe, checks = sharing_checks(plan)
        return NativeView(
            "Sharing checks", "Results apply only to this reviewed package.",
            [NativeItem("✅ Safe To Share" if safe else "Checks need review", checks)],
            actions=[NativeAction("Looks good.", "back")],
        )
    if state == "pending" and record.get("screen") in {"details", "approval"}:
        return _inspection_view(record)
    if record.get("operation") in {"candidate", "publish"} and state == "pending":
        return _candidate_view(plan)
    if record.get("operation") in {"install", "update"} and state == "pending":
        return _installation_view(record)
    if record.get("operation") == "catalog":
        return _catalog_view(plan, state)
    if record.get("operation") == "settings":
        return _settings_view(record)
    authority, details = _authority_projection(record, plan)
    if record.get("operation") == "suggest":
        return _suggest_view(authority, state, record.get("result"))
    return _effect_view(record, authority, state, details)


def _effect_view(
    record: dict[str, Any], plan: dict[str, Any], state: str,
    authority_details: list[str] | None = None,
) -> NativeView:
    title = "Collective Wisdom review"
    summary = _summary(plan)
    details = list(authority_details or [])
    details.extend(_fact_lines(_public_review(plan)))
    item = NativeItem(summary, "\n".join(details) or "Review the exact action in Collective Wisdom.")
    if state != "pending":
        result = record.get("result")
        operation = str(record.get("operation") or "")
        if operation in {"install", "update", "uninstall"} and isinstance(result, dict):
            name = _skill_name(plan)
            terse = render_operation_outcome(operation, result, skill_name=name)
            return NativeView(terse, "", dismissed=True)
        return NativeView(title, summary, [item], notice=f"State: {state}", dismissed=True)
    operation = str(record.get("operation") or "")
    actions = [NativeAction("Cancel", "cancel", destructive=True)]
    if operation in {"install", "update", "publish", "setup", "uninstall"} and plan.get("allowed", True):
        if _requires_partial_acknowledgement(plan):
            actions.insert(0, NativeAction(
                "Acknowledge partial compatibility and confirm",
                "confirm.partial", primary=True,
            ))
        else:
            actions.insert(0, NativeAction("Confirm", "confirm", primary=True))
    view = NativeView(title, summary, [item], actions=actions)
    if not _approval_is_lossless(view):
        return NativeView(
            title, summary, [item],
            actions=[action for action in actions if not action.primary],
            notice="This exact review is too large for native approval; use the terminal workflow.",
        )
    return view


def _validate_offered_action(record: dict[str, Any], action: str) -> None:
    """Reuse the operation/screen projection as the callback allowlist."""
    view = project_view(record)
    offered = view.actions + view.navigation_actions
    for item in view.items:
        offered.extend(item.actions)
    if not any(control.action == action for control in offered):
        raise WisdomValidationError("Collective Wisdom action is not offered on this screen")


def assessment_view(job: dict[str, Any], advice: dict[str, Any] | None = None) -> NativeView:
    del advice
    reference = job.get("reference") if isinstance(job.get("reference"), dict) else job
    subject = _skill_name(reference)
    return NativeView(
        "Collective Wisdom",
        f"{_safe(subject)} is available.",
        [NativeItem("Review available", "Open Collective Wisdom to inspect and choose an action.")],
    )


class NativeReviewController:
    """Persist explicit user reviews; a notice is never a review or consent."""

    def __init__(
        self, state: ReviewState, *, clock=time.time,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        if not isinstance(state, ReviewState):
            raise TypeError("ReviewState is required")
        self.state = state
        self.clock = clock
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _route(invocation: NativeInvocation) -> dict[str, str]:
        return {
            "platform": invocation.platform, "profile": invocation.profile,
            "chat_id": invocation.chat_id, "actor_id": invocation.actor_id,
            "thread_id": invocation.thread_id, "session_key": invocation.session_key,
        }

    def _insert(self, service: Any, invocation: NativeInvocation, operation: str, plan: dict[str, Any], *, explanation: str = "") -> dict[str, Any]:
        owner = service.native_owner()
        if invocation.profile != owner.get("profile_name"):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        identity = secrets.token_urlsafe(12)
        record = {
            "id": identity, "owner": copy.deepcopy(owner), "route": self._route(invocation),
            "owner_session": invocation.session_key, "actor_id": invocation.actor_id,
            "message_id": "",
            "platform": invocation.platform, "profile": invocation.profile,
            "operation": operation, "state": "pending", "plan": copy.deepcopy(plan),
            "display_ready": False,
            "explanation": _safe(explanation, "")[:1200],
            "result": None, "created_at": self.clock(), "updated_at": self.clock(),
            "expires_at": self.clock() + self.ttl_seconds,
        }
        self.state.save(record)
        return copy.deepcopy(record)

    def request_install(self, service: Any, invocation: NativeInvocation, reference: str, *, update_mode: str | None = None, explanation: str = "") -> dict[str, Any]:
        plan = service.install_plan(reference, update_mode=update_mode)
        if plan.get("operation") != "install":
            raise WisdomValidationError("native install review is invalid")
        return self._insert(service, invocation, "install", {
            **plan, "installation_status": _local_installation_label(service, plan),
        }, explanation=explanation)

    def request_install_modes(self, service: Any, invocation: NativeInvocation, reference: str) -> dict[str, Any]:
        return self.request_install(service, invocation, reference)

    def request_update(
        self, service: Any, invocation: NativeInvocation, skill_id: str
    ) -> dict[str, Any]:
        plan = service.update_plan(skill_id)
        if plan.get("operation") != "update":
            raise WisdomValidationError("native update review is invalid")
        return self._insert(service, invocation, "update", {
            **plan, "installation_status": _local_installation_label(service, plan),
        })

    def request_publication(self, service: Any, invocation: NativeInvocation, draft_id: str) -> dict[str, Any]:
        plan = service.publication_review(draft_id)
        return self._insert(service, invocation, "publish", plan)

    def request_setup(self, service: Any, invocation: NativeInvocation, subject: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        skill_id, inspection = subject
        raw_step = inspection.get("step")
        step: dict[str, Any] = raw_step if isinstance(raw_step, dict) else {}
        plan = service.installed_setup_plan(
            skill_id, version=inspection.get("version"),
            phase=str(step.get("phase") or ""), index=int(step.get("index") or 0),
            command=str(step.get("command") or ""),
        )
        if not plan.get("receipt") or plan.get("skill_id") != skill_id:
            raise WisdomValidationError("native setup review is invalid")
        return self._insert(service, invocation, "setup", {
            **plan, "slug": plan.get("slug") or inspection.get("slug"),
        })

    def request_settings(self, service: Any, invocation: NativeInvocation) -> dict[str, Any]:
        plan = service.notification_settings(may_apply=lambda: True, include_mute_control=True)
        plan = {**plan, "operation": "settings", "allowed": False}
        return self._insert(service, invocation, "settings", plan)

    def request_catalog(self, service: Any, invocation: NativeInvocation, command: str, target: str = "") -> dict[str, Any]:
        if command == "browse":
            data = service.search_skills(target or None)
        elif command == "show":
            data = service.show(target)
        elif command == "versions":
            data = service.versions(target)
        else:
            raise WisdomValidationError("unsupported catalog command")
        row = _catalog_rows(data)
        name = next((item.get("slug") or item.get("name") or item.get("skill_name")
                     for item in row if item.get("slug") or item.get("name") or item.get("skill_name")), None)
        if command == "versions" and not name:
            detail = service.show(target)
            skill = detail.get("skill") if isinstance(detail, dict) else None
            name = skill.get("slug") if isinstance(skill, dict) else None
        return self._insert(service, invocation, "catalog", {
            "operation": "catalog", "command": command, "target": target,
            "data": data, "slug": name, "allowed": False,
        })

    def request_candidate(self, service: Any, invocation: NativeInvocation, event_id: str) -> dict[str, Any]:
        plan = service.native_candidate_inspect(event_id)
        return self._insert(service, invocation, "candidate", {**plan, "operation": "candidate", "allowed": False})

    def request_suggest(
        self, service: Any, invocation: NativeInvocation, skill_name: str,
    ) -> dict[str, Any]:
        if not str(skill_name or "").strip():
            raise WisdomValidationError("Usage: /wisdom suggest <local-skill>")
        prepared = service.suggest(str(skill_name).strip(), allow_model_review=False)
        if not isinstance(prepared, dict) or not str(
            prepared.get("draft_id") or prepared.get("local_draft_id") or ""
        ):
            raise WisdomValidationError("prepared draft identity is unavailable")
        return self._insert(service, invocation, "suggest", {
            **prepared, "skill_name": str(skill_name).strip(), "native_stage": "prepared",
        })

    def load(self, record_id: str) -> dict[str, Any]:
        row = self.state.get(record_id)
        if row is None:
            raise WisdomValidationError("Collective Wisdom review was not found")
        return row

    def bind_message(
        self, record_id: str, invocation: NativeInvocation, message_id: str
    ) -> dict[str, Any]:
        """Bind a pending review once to the exact native card returned by its SDK."""
        record = self.load(record_id)
        if (
            record.get("state") != "pending"
            or record.get("route") != self._route(invocation)
            or record.get("message_id")
            or not str(message_id or "")
        ):
            raise WisdomConflict("Collective Wisdom review message changed")
        bound = {
            **record, "message_id": str(message_id), "updated_at": self.clock(),
            "display_ready": True,
        }
        if not self.state.compare_replace(record, bound):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(bound)

    def pending(self, owner: dict[str, Any], *, invocation: NativeInvocation | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.state.pending(owner, limit=limit)
        if invocation is not None:
            route = self._route(invocation)
            rows = [row for row in rows if row.get("route") == route]
        return rows

    def view(self, record: dict[str, Any]) -> NativeView:
        return project_view(record)

    def _select_catalog(
        self, action: str, record: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            index = int(action.rsplit(".", 1)[-1])
            row = _catalog_rows(record["plan"].get("data"))[index]
        except (IndexError, TypeError, ValueError) as exc:
            raise WisdomValidationError("Catalog selection is no longer available") from exc
        skill_id = str(row.get("id") or row.get("skill_id") or "")
        version = row.get("version", row.get("latest_version"))
        if not skill_id:
            raise WisdomValidationError("Catalog selection is no longer available")
        plan = {
            "operation": "install", "skill_id": skill_id, "version": version,
            "slug": row.get("slug") or row.get("name") or row.get("skill_name"),
            "installation_status": row.get("installation_status"),
        }
        result = {
            **record, "operation": "install", "plan": copy.deepcopy(plan),
            "updated_at": self.clock(), "display_ready": False,
        }
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _navigation_action(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any] | None:
        if action == "settings":
            return self._open_settings(record, service, invocation, authority)
        if action in {"details", "review"} and record["operation"] in {"install", "update"}:
            inspect = getattr(service, f"native_{record['operation']}_inspect")
            files = inspect(record["plan"]["receipt"])
            if not authority() or not _owner_matches(record, service, invocation):
                raise WisdomAuthError("Collective Wisdom review owner changed")
            return self._navigate(record, action, package_files=files)
        if action == "share" and record.get("operation") == "candidate":
            return self._prepare_candidate(record, service, invocation, authority)
        if (
            action in {"details", "back", "checks"} or action.startswith("page.")
            or (action in {"share", "review"} and record.get("operation") in {"publish", "install", "update"})
        ):
            return self._navigate(record, action)
        return None

    def _open_settings(
        self, record: dict[str, Any], service: Any, invocation: NativeInvocation,
        authority: Callable[[], bool],
    ) -> dict[str, Any]:
        plan = service.notification_settings(may_apply=authority, include_mute_control=True)
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        result = {
            **record, "operation": "settings", "screen": "offer",
            "display_ready": False,
            "plan": {**plan, "operation": "settings", "allowed": False},
        }
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return result

    @contextmanager
    def displaying(self, record: dict[str, Any]):
        """Fence approval until the provider acknowledges the complete page edit."""
        if record.get("state") not in {"pending", "kept"}:
            yield
            return
        fenced = {**record, "display_ready": False}
        if not self.state.compare_replace(record, fenced):
            raise WisdomConflict("Collective Wisdom presentation changed")
        try:
            yield
        except Exception:
            self.state.compare_replace(fenced, {**fenced, "state": "failed"})
            raise
        self.state.compare_replace(fenced, {**fenced, "display_ready": True})

    def _navigate(
        self, record: dict[str, Any], action: str, *, package_files: Any = None,
    ) -> dict[str, Any]:
        result = {**copy.deepcopy(record), "display_ready": False}
        if package_files is not None:
            result["package_files"] = copy.deepcopy(package_files)
            result["inspection_receipt"] = record["plan"]["receipt"]
        if action in {"details", "share", "review"}:
            result.update(screen="details" if action == "details" else "approval",
                          page=0, inspected_through=0)
        elif action == "checks":
            result.update(screen="checks")
        elif action == "back":
            result.update(screen="offer", page=0, inspected_through=-1)
        else:
            try:
                page = int(action.split(".", 1)[1])
            except (ValueError, IndexError) as exc:
                raise WisdomValidationError("Invalid review page") from exc
            if (
                record.get("screen") not in {"details", "approval"}
                or abs(page - record.get("page", 0)) != 1
                or not 0 <= page < len(_inspection_pages(record))
            ):
                raise WisdomValidationError("Invalid review page")
            result.update(page=page, inspected_through=max(
                page, record.get("inspected_through", -1)
            ))
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return result

    def _prepare_candidate(
        self, record: dict[str, Any], service: Any, invocation: NativeInvocation,
        authority: Callable[[], bool],
    ) -> dict[str, Any]:
        claimed = {**record, "state": "preparing"}
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        try:
            draft_id = service.native_candidate_prepare(record["plan"]["event_id"])
            prepared = _prepared_review(service, {"draft_id": draft_id})
            plan = {
                **prepared,
                "skill_name": (
                    record["plan"].get("facts", {}).get("name")
                    or prepared.get("skill_name")
                ),
                "candidate_flow": True,
            }
            if has_reviewable_warnings(prepared):
                if not safe_scan_details(prepared.get("local_scan")):
                    raise WisdomValidationError(
                        "scanner warning details are unavailable; acknowledgement is blocked"
                    )
                result = {
                    **record, "operation": "suggest",
                    "plan": {**plan, "native_stage": "warnings"},
                    "updated_at": self.clock(), "display_ready": False, "failure": None,
                }
            else:
                result = self._sharing_review(record, plan, prepared, service, False)
                result.update(display_ready=False, failure=None)
        except SharePackagingError:
            result = {**claimed, "state": "failed", "failure": "share_packaging_failed"}
        except Exception:  # pylint: disable=broad-exception-caught
            # Explicit retryable failure, never success or a packaging diagnosis.
            result = {**record, "failure": "share_preparation_failed", "display_ready": False}
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        return result

    def act(  # pylint: disable=too-many-branches
        self, record_id: str, action: str, service: Any,
        invocation: NativeInvocation, *, authority: Callable[[], bool] = lambda: True,
    ) -> dict[str, Any]:
        record = self.load(record_id)
        state = str(record.get("state") or "")
        if state not in {"pending", "kept", "applying", "preparing"} or not _owner_matches(
            record, service, invocation
        ):
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        if action == "cancel":
            if record.get("effect_action") == "defer":
                raise WisdomAuthError("Collective Wisdom review action is already in progress")
            if state == "pending":
                _validate_offered_action(record, action)
            result = {**record, "state": "cancelled", "updated_at": self.clock()}
            if not self.state.compare_replace(record, result):
                raise WisdomConflict("Collective Wisdom review changed; open it again")
            return copy.deepcopy(result)
        if state not in {"pending", "kept"}:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        if record.get("display_ready") is False:
            raise WisdomConflict("Wait for the exact review to finish displaying")
        if self.clock() >= record["expires_at"]:
            raise WisdomAuthError("Collective Wisdom review expired; open it again")
        _validate_offered_action(record, action)
        if state == "kept":
            return self._resume_kept(record, action, service, invocation, authority)
        if action == "defer" or action.startswith(("suggest.", "catalog.install.")):
            return self._act_special(
                record_id, action, record, service, invocation, authority,
            )
        if action in {"install", "update"} and action == record.get("operation"):
            return self._one_click_package(record, action, service, invocation, authority)
        navigated = self._navigation_action(record, action, service, invocation, authority)
        if navigated is not None:
            return navigated
        _validate_review_action(record, action)

        return self._apply(record, action, service, invocation, authority)

    def _defer(
        self, record: dict[str, Any], service: Any, invocation: NativeInvocation,
        authority: Callable[[], bool],
    ) -> dict[str, Any]:
        """Claim one card before persisting its exact-version preference."""
        claimed = {**record, "state": "applying", "effect_action": "defer",
                   "display_ready": False,
                   "updated_at": self.clock()}
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")

        def current() -> bool:
            return _current_effect(
                self.load(record["id"]), claimed, service, invocation, authority,
            )

        try:
            preference = service.prepare_review_deferral(claimed["plan"])
            if not current():
                raise WisdomAuthError("Collective Wisdom review is no longer current")
            outcome = service.defer_review(preference, may_apply=current)
            if outcome.get("deferred") is not True or outcome.get("preference_sync") != "confirmed":
                raise WisdomConflict("Collective Wisdom deferral was not confirmed")
        except Exception:
            self.state.compare_replace(claimed, {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            })
            raise
        result = {**claimed, "state": "cancelled", "result": outcome,
                  "updated_at": self.clock()}
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _resume_kept(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        """Resume only the owner- and card-bound draft with a fresh local review."""
        if action != "suggest.resume" or record.get("operation") != "suggest":
            raise WisdomValidationError("Collective Wisdom action is not offered on this screen")
        refreshed = self._refresh_suggest(record, record["plan"], service)
        result = {**refreshed, "state": "pending", "display_ready": False}
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _one_click_package(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        """Prepare and spend a fresh exact receipt inside the explicit click."""
        claimed = {
            **record, "state": "preparing", "display_ready": False,
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        try:
            fresh = _fresh_package_plan(record, action, service)
            inspect = getattr(service, f"native_{action}_inspect")
            files = inspect(fresh["receipt"])
            ready = {
                **record, "plan": copy.deepcopy({
                    **fresh, "installation_status": record["plan"].get("installation_status"),
                }),
                "package_files": copy.deepcopy(files),
                "inspection_receipt": fresh.get("receipt"),
                "updated_at": self.clock(), "display_ready": False,
            }
            if _package_needs_acknowledgement(fresh):
                ready.update(screen="approval", page=0, inspected_through=0)
                if not self.state.compare_replace(claimed, ready):
                    raise WisdomConflict("Collective Wisdom review changed; open it again")
                return copy.deepcopy(ready)
            if not authority() or not _owner_matches(record, service, invocation):
                raise WisdomAuthError("Collective Wisdom review owner changed")
            if not self.state.compare_replace(claimed, ready):
                raise WisdomConflict("Collective Wisdom review changed; open it again")
            return self._apply(ready, "confirm", service, invocation, authority)
        except Exception:
            self.state.compare_replace(claimed, record)
            raise

    def _apply(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        record_id = record["id"]
        claimed = {
            **record, "state": "applying", "display_ready": False,
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        plan = claimed["plan"]
        operation = claimed["operation"]
        def current() -> bool:
            return _current_effect(
                self.load(record_id), claimed, service, invocation, authority
            )
        try:
            outcome = _apply_review_action(action, operation, plan, service, current)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            uncertain = {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            }
            if (self.state.compare_replace(claimed, uncertain)
                    and operation == "settings" and not isinstance(exc, WisdomAuthError)):
                return copy.deepcopy(uncertain)
            raise
        result = {
            **claimed, "state": "completed", "result": outcome,
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _act_special(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, record_id: str, action: str, record: dict[str, Any], service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        operation = record.get("operation")
        if operation in {"install", "update"} and action == "defer":
            return self._defer(record, service, invocation, authority)
        if operation == "suggest" and action.startswith("suggest."):
            return self._act_suggest(
                record_id, action, record, service, invocation, authority=authority,
            )
        if operation == "catalog" and action.startswith("catalog.install."):
            selected = self._select_catalog(action, record)
            return self._one_click_package(
                selected, "install", service, invocation, authority,
            )
        raise WisdomValidationError("Collective Wisdom action was not recognized")

    def _sharing_review(
        self, record: dict[str, Any], plan: dict[str, Any], displayed: dict[str, Any],
        service: Any, accept_warnings: bool,
    ) -> dict[str, Any]:
        draft_id = str(displayed.get("draft_id") or displayed.get("local_draft_id") or "")
        expected_draft = str(plan.get("draft_id") or plan.get("local_draft_id") or "")
        if not draft_id or draft_id != expected_draft:
            raise WisdomConflict("The prepared draft changed; open it again")
        review = service.publication_review(draft_id, accept_warnings=accept_warnings)
        if not isinstance(review, dict):
            raise WisdomValidationError("publication review is unavailable")
        review_drafts = [review[key] for key in ("draft_id", "local_draft_id") if key in review]
        if not review_drafts or any(value != draft_id for value in review_drafts):
            raise WisdomConflict("The prepared draft changed; open it again")
        if not _plan_owner_matches({**record, "plan": review}):
            raise WisdomConflict("The publication owner changed; open it again")
        if review_identity(review) != review_identity(displayed):
            raise WisdomConflict("The package or scanner findings changed; open it again")
        if plan.get("candidate_flow") is True:
            return {
                **record, "operation": "publish", "plan": copy.deepcopy(review),
                "screen": "approval", "page": 0, "inspected_through": 0,
                "updated_at": self.clock(),
            }
        return {**record, "plan": {
            **review, "skill_name": plan.get("skill_name"), "native_stage": "sharing",
        }, "updated_at": self.clock()}

    def _suggest_navigation(
        self, action: str, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any] | object:
        stage = str(plan.get("native_stage") or "prepared")
        result: dict[str, Any] | object = _UNHANDLED
        if action == "suggest.keep":
            result = {**record, "state": "kept", "updated_at": self.clock()}
        elif action in {"suggest.refresh", "suggest.back"}:
            if action == "suggest.back" or stage == "prepared":
                result = self._refresh_suggest(record, plan, service)
        elif action in {"suggest.files", "suggest.checks"}:
            if stage == "prepared":
                result = {**record, "plan": {
                    **plan, "native_stage": action.rsplit(".", 1)[-1],
                    "native_page": 0, "native_chunk": 0,
                }, "updated_at": self.clock()}
        elif action in {"suggest.files.next", "suggest.files.prev"} and stage == "files":
            result = self._move_suggest_file(action, record, plan)
        return result

    def _refresh_suggest(
        self, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any]:
        refreshed = _prepared_review(service, plan)
        return {**record, "plan": {
            **refreshed, "skill_name": plan.get("skill_name"), "native_stage": "prepared",
        }, "updated_at": self.clock()}

    def _move_suggest_file(
        self, action: str, record: dict[str, Any], plan: dict[str, Any],
    ) -> dict[str, Any]:
        rows = [row for row in plan.get("files", []) if isinstance(row, dict)]
        page = int(plan.get("native_page") or 0)
        chunk = int(plan.get("native_chunk") or 0)
        if not 0 <= page < len(rows):
            raise WisdomValidationError("Draft file page is no longer available")
        chunks = _native_file_chunks(rows[page])
        if action.endswith(".next"):
            page, chunk = (page, chunk + 1) if chunk + 1 < len(chunks) else (page + 1, 0)
        elif chunk > 0:
            chunk -= 1
        else:
            page -= 1
            if page >= 0:
                chunk = len(_native_file_chunks(rows[page])) - 1
        if not 0 <= page < len(rows):
            raise WisdomValidationError("Draft file page is no longer available")
        return {**record, "plan": {
            **plan, "native_page": page, "native_chunk": chunk,
        }, "updated_at": self.clock()}

    def _suggest_review_transition(
        self, action: str, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any] | object:
        stage = str(plan.get("native_stage") or "prepared")
        allowed_stages = {"prepared", "files", "checks"}
        if action not in {"suggest.share", "suggest.ack"}:
            return _UNHANDLED
        if action == "suggest.ack" and stage != "warnings":
            return _UNHANDLED
        if action == "suggest.share" and stage not in allowed_stages:
            return _UNHANDLED
        displayed = _prepared_review(service, plan)
        if action == "suggest.ack":
            if review_identity(displayed) != review_identity(plan):
                raise WisdomConflict("The package or scanner findings changed; open it again")
            return self._sharing_review(record, plan, displayed, service, True)
        if not has_reviewable_warnings(displayed):
            return self._sharing_review(record, plan, displayed, service, False)
        if not safe_scan_details(displayed.get("local_scan")):
            raise WisdomValidationError(
                "scanner warning details are unavailable; acknowledgement is blocked"
            )
        return {**record, "plan": {
            **displayed, "skill_name": plan.get("skill_name"),
            "native_stage": "warnings",
        }, "updated_at": self.clock()}

    def _confirm_suggest(  # pylint: disable=too-many-arguments
        self, record_id: str, action: str, record: dict[str, Any], service: Any,
        invocation: NativeInvocation, *, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        plan = record.get("plan")
        if not isinstance(plan, dict):
            raise WisdomValidationError("Collective Wisdom draft review is unavailable")
        if action != "suggest.confirm" or plan.get("native_stage") != "sharing":
            raise WisdomValidationError("Collective Wisdom action was not recognized")
        receipt = plan.get("receipt")
        if not isinstance(receipt, str) or not receipt:
            raise WisdomValidationError("publication review receipt is unavailable")
        claimed = {
            **record, "state": "applying", "display_ready": False,
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")

        def current() -> bool:
            return _current_effect(
                self.load(record_id), claimed, service, invocation, authority,
            )
        try:
            outcome = service.publication_submit(receipt, may_apply=current)
        except Exception:  # pylint: disable=broad-exception-caught
            uncertain = {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            }
            self.state.compare_replace(claimed, uncertain)
            raise
        result = {**claimed, "state": "completed", "result": outcome,
                  "updated_at": self.clock()}
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _act_suggest(  # pylint: disable=too-many-arguments
        self, record_id: str, action: str, record: dict[str, Any], service: Any,
        invocation: NativeInvocation, *, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        plan = record.get("plan")
        if not isinstance(plan, dict):
            raise WisdomValidationError("Collective Wisdom draft review is unavailable")
        if action == "suggest.confirm":
            return self._confirm_suggest(
                record_id, action, record, service, invocation, authority=authority,
            )
        result = self._suggest_navigation(action, record, plan, service)
        if result is _UNHANDLED:
            result = self._suggest_review_transition(action, record, plan, service)
        if result is _UNHANDLED:
            raise WisdomValidationError("Collective Wisdom action was not recognized")
        assert isinstance(result, dict)
        result["display_ready"] = False
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)


__all__ = [
    "NativeAction", "NativeItem", "NativeReviewController", "NativeView",
    "assessment_view", "callback_value", "parse_callback", "project_view",
    "slack_section_text", "telegram_view_text", "_approval_is_lossless",
]
