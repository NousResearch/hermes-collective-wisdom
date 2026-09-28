"""Deterministic native views and PluginState-backed explicit reviews."""
# Self-explanatory immutable view records and narrow adapter methods do not gain clarity
# from repeating their signatures in docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,line-too-long,too-many-lines

from __future__ import annotations

import copy
import secrets
import time
import unicodedata
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from .errors import SharePackagingError, WisdomAuthError, WisdomConflict, WisdomValidationError
from .native_context import NativeInvocation
from .native_inspection import inspection_pages, sharing_checks
from .package import PackagePolicyError
from .portal_urls import safe_portal_url
from .review_state import ReviewState
from .install_review import confirmed_file_change
from .terminal_ui import (
    has_reviewable_warnings,
    render_findings, render_operation_outcome, review_identity, safe_scan_details,
)

SLACK_SECTION_LIMIT = 2900
TELEGRAM_TEXT_LIMIT = 3500
_UNHANDLED = object()



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
    emphasis_labels: tuple[str, ...] = ()

    def detail_parts(self) -> list[tuple[str, str]]:
        """Split only explicitly owned line prefixes; leave all original text intact."""
        if self.literal or not self.emphasis_labels:
            return [("", self.detail)]
        parts = []
        for line in self.detail.splitlines(keepends=True):
            label = next((label + ":" for label in self.emphasis_labels
                          if line.startswith(label + ": ")), "")
            parts.append((label, line[len(label):]))
        return parts


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
            and len(encoded(f"_{view.notice}_")) <= SLACK_SECTION_LIMIT
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
    for name in (plan.get("editorial_name"), plan.get("slug"),
                 plan.get("skill_name"), plan.get("name")):
        if (isinstance(name, str) and name.strip()
                and name != plan.get("skill_id") and name != plan.get("draft_id")):
            return _safe(name)
    return "Name unavailable"

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
                if key not in {"receipt", "draft_id", "local_draft_id", "skill_id",
                               "published_by_user_id"}}
    if isinstance(value, list):
        return [_public_review(child) for child in value]
    return value

def _inspection_pages(record: dict[str, Any]) -> list[str]:
    return inspection_pages({**record, "plan": _public_review(record["plan"])})


def _candidate_review_ready(plan: dict[str, Any]) -> bool:
    return bool(
        plan.get("receipt") and plan.get("editorial_status") == "complete"
        and all(plan.get(key) for key in (
            "editorial_name", "editorial_description", "coworker_benefit",
            "publication_mode", "policy_version", "authorization_expires_at", "hashes",
        ))
    )


def _confirmed_private_url(record: dict[str, Any]) -> str | None:
    result = record.get("result")
    if not isinstance(result, dict) or not (
        result.get("state") == "pending" and result.get("phase") == "policy_review_required"
    ):
        return None
    remote_id = result.get("remote_draft_id")
    url = result.get("portal_url")
    if (not isinstance(remote_id, str) or not remote_id or not isinstance(url, str)
            or safe_portal_url(url) != url):
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.query or parsed.fragment
            or not parsed.path.endswith("/wisdom/review/" + quote(remote_id, safe=""))):
        return None
    return url


def _authorization_expired(plan: dict[str, Any], now: float) -> bool:
    expiry = plan.get("authorization_expires_at")
    if not isinstance(expiry, str):
        return False
    try:
        deadline = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return deadline.tzinfo is not None and now >= deadline.timestamp()
    except ValueError:
        return False


def _catalog_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("skills"), list):
        return [row for row in data["skills"] if isinstance(row, dict)]
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
    if isinstance(data.get("versions"), list):
        return [row for row in data["versions"] if isinstance(row, dict)]
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
            facts.append(_safe(" ".join(str(detail).split())))
        facts.append(_installed_label(row))
        for label, key in (("Status", "state"), ("Author", "created_by_display_name")):
            if row.get(key):
                facts.append(f"{label}: {_safe(row[key])}")
        actions = []
        skill_id = row.get("id") or row.get("skill_id")
        status = row.get("installation_status")
        if (skill_id and isinstance(status, dict)
                and status.get("state") == "not_installed"):
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
    if action in {"settings.mode.agent", "settings.mode.fixed"} and record.get("operation") == "settings":
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
    if (operation in {"install", "update", "publish"} or record.get("screen")) and not (
        operation == "publish" and plan.get("candidate_flow") is True
    ):
        pages = _inspection_pages(record)
        if (
            record.get("screen") != "approval"
            or record.get("page") != len(pages) - 1
            or record.get("inspected_through", -1) != len(pages) - 1
        ):
            raise WisdomValidationError("Inspect the complete exact review before confirming")
    if operation == "publish" and plan.get("candidate_flow") is True and (
        record.get("screen") != "approval" or not _candidate_review_ready(plan)
        or not record.get("team_name")
    ):
        raise WisdomValidationError("Sharing proposal is incomplete; review it again")
    receipt_missing = operation != "uninstall" and not str(plan.get("receipt") or "")
    if (
        operation not in {"install", "update", "publish", "setup", "uninstall"}
        or plan.get("allowed", True) is not True
        or _requires_partial_acknowledgement(plan) != (action == "confirm.partial")
        or receipt_missing
    ):
        raise WisdomValidationError("Collective Wisdom action is not allowed")


def _publication_effect(action: str, service: Any, receipt: str,
                        may_apply: Callable[[], bool]) -> Any:
    if action == "portal.draft":
        return service.publication_submit_private(receipt, may_apply=may_apply)
    return service.publication_submit(receipt, may_apply=may_apply)

def _apply_review_action(  # pylint: disable=too-many-return-statements
    action: str, operation: str, plan: dict[str, Any], service: Any,
    may_apply: Callable[[], bool],
) -> Any:
    if action in {"settings.mode.agent", "settings.mode.fixed"}:
        return service.set_notification_mode(
            action.rsplit(".", 1)[-1],
            expected_mode=str(plan.get("delivery_mode") or ""), may_apply=may_apply,
        )
    if operation in {"install", "update"} and action == "setup.run":
        apply_setup = (service.install_setup_apply if operation == "install"
                       else service.update_setup_apply)
        return apply_setup(plan["receipt"], may_apply=may_apply)
    if operation == "install":
        return service.install_apply(
            plan["receipt"], accept_partial=action == "confirm.partial",
            may_apply=may_apply,
        )
    if operation == "update":
        return service.update_apply(plan["receipt"], may_apply=may_apply)
    if operation == "publish":
        return _publication_effect(action, service, plan["receipt"], may_apply)
    if operation == "setup":
        return service.installed_setup_apply(plan["receipt"], may_apply=may_apply)
    if operation == "uninstall":
        return service.uninstall(plan["skill_id"], may_apply=may_apply)
    raise WisdomValidationError("This review has no effect to confirm")


def _settings_result_view(result: dict[str, Any]) -> NativeView:
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
    current = "Off" if plan.get("delivery_mode") == "fixed" else "On"
    item = NativeItem(f"Current: {current}", "")
    actions = [NativeAction("Cancel", "cancel", destructive=True)]
    if plan.get("mode_writable") is True:
        mode = str(plan.get("delivery_mode") or "fixed")
        if mode == "agent":
            actions.insert(0, NativeAction("Turn off automatic suggestions", "settings.mode.fixed"))
        else:
            actions.insert(0, NativeAction("Turn on automatic suggestions", "settings.mode.agent"))
    return NativeView(
        "Collective Wisdom settings",
        "Pause sharing suggestions and skill recommendations for you, not your teammates. "
        "You can still browse and install skills.", [item], actions=actions,
    )


def _sharing_actions(record: dict[str, Any], plan: dict[str, Any], private: str | None) -> list[NativeAction]:
    actions = [NativeAction("View More Details", "details"),
               NativeAction("Notification settings", "settings"),
               NativeAction("Maybe Later", "cancel")]
    if private:
        actions.insert(0, NativeAction("Open in Portal", url=private))
    if (_candidate_review_ready(plan) and isinstance(record.get("team_name"), str)
            and record["team_name"].strip()
            and record["team_name"] != (record.get("owner") or {}).get("organization_id")
            and plan.get("allowed", True)):
        if not private or plan.get("operation_id") == (record.get("result") or {}).get("operation_id"):
            actions.insert(0, NativeAction("Share My Skill", "confirm", primary=True))
        if not private:
            actions.insert(1, NativeAction("Review/edit in Portal", "portal.draft"))
    return actions


def _inspection_view(record: dict[str, Any]) -> NativeView:  # pylint: disable=too-many-locals
    plan = record["plan"]
    private = _confirmed_private_url(record)
    if (record.get("screen") == "approval" and record.get("operation") == "publish"
            and plan.get("candidate_flow") is True):
        from .native_inspection import _date  # pylint: disable=import-outside-toplevel
        mode = plan.get("publication_mode")
        handling = {
            "open": "Gateway may publish directly after its scan.",
            "moderated": "On sharing, sent for team moderation; not published until approved.",
            "managed": "Gateway determines the team publication outcome.",
        }.get(mode, "Publication policy unavailable.")
        security = plan.get("security_check") or {}
        professional = plan.get("professionalism_check") or {}
        gateway = (plan.get("server_checks") or {}).get("security_check")
        scan = (gateway or {}).get("status") if isinstance(gateway, dict) else None
        facts = "\n".join((
            f"What it does: {_safe(plan.get('editorial_description'), 'Description incomplete')}",
            f"Why teammates benefit: {_safe(plan.get('coworker_benefit'), 'Benefit incomplete')}",
            f"Destination team: {_safe(record.get('team_name'), 'Team name unavailable')}",
            f"Publication policy: {_safe(mode, 'Unavailable')} (version {_safe(plan.get('policy_version'), 'unavailable')}). {handling}",
            f"Local security: {_safe(security.get('status'), 'unavailable')}; professionalism: {_safe(professional.get('status'), 'unavailable')}.",
            f"Gateway scan: {_safe(scan, 'pending until authorized upload')}. The Gateway checks before publication; local checks do not certify its verdict.",
            f"Package: {len(plan.get('files') or [])} exact files; inspect files and findings in View More Details.",
            f"Authorization expires: {_date(plan.get('authorization_expires_at'))}; review expires: {_date(record.get('expires_at'))}.",
        ))
        actions = _sharing_actions(record, plan, private)
        view = NativeView(
            "Ready for review" if private else "Ready to share", "", [NativeItem(
                _skill_name(plan),
                ("Your private draft is ready to review and edit in Portal. "
                 "Share it with your team whenever you are ready.\n" if private else
                 "You created a skill that could help your team.\n") + facts,
                emphasis_labels=(
                    "What it does", "Why teammates benefit", "Destination team",
                    "Publication policy", "Local security", "Gateway scan", "Package",
                    "Authorization expires",
                ),
            )],
            actions=actions,
            notice=("Uploaded privately; not published or submitted for moderation. "
                    "Share My Skill shares the version shown here. If you edit it in Portal, "
                    "review and share the edited version there."
                    if private else
                    "Share My Skill uploads the exact package for the displayed policy. "
                    "Review/edit in Portal instead uploads a PRIVATE draft for editing; it does not "
                    "publish or submit for team moderation. Viewing details does not upload anything."),
        )
        if not _approval_is_lossless(view):
            return replace(view, actions=[a for a in actions if a.action not in {"confirm", "portal.draft"}],
                           notice="Sharing facts exceed provider limits; use terminal review. " +
                           ("The private draft remains in Portal." if private else "No upload occurred."))
        return view
    pages = _inspection_pages(record)
    page = int(record.get("page", 0))
    pages = [text.replace("Nothing has been uploaded", "A private draft has been uploaded")
             if private else text for text in pages]
    approving = record.get("screen") == "approval"
    _projected, authority = _authority_projection(record, plan)
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"page.{page - 1}"))
    if page + 1 < len(pages):
        actions.append(NativeAction("Next", f"page.{page + 1}"))
    elif approving and plan.get("allowed", True) and (
        plan.get("candidate_flow") is not True or _candidate_review_ready(plan)
    ):
        partial = _requires_partial_acknowledgement(plan)
        actions.append(NativeAction(
            "Acknowledge partial compatibility and confirm" if partial else
            "Share My Skill" if plan.get("candidate_flow") is True else "Confirm",
            "confirm.partial" if partial else "confirm", primary=True,
        ))
    if approving and plan.get("candidate_flow") is True:
        actions.append(NativeAction("View More Details", "details"))
    if private and not approving and not record.get("portal_changed"):
        actions.append(NativeAction("Refresh review", "refresh.review"))
    actions.append(NativeAction("Back" if approving else
                                "Return to proposal" if plan.get("candidate_flow") is True
                                else "Looks good.", "back"))
    candidate = record.get("operation") == "candidate"
    view = NativeView(
        "Sharing proposal" if approving and plan.get("candidate_flow") is True else
        "Exact package review" if approving else
        "Candidate details" if candidate else "Private package details",
        f"Page {page + 1} of {len(pages)}. " + (
            "Review the complete proposal before sharing. Detailed file inventory is optional."
            if approving and plan.get("candidate_flow") is True else
            "Confirm only after inspecting the complete exact plan."
            if approving else "Candidate details are not the prepared package. Review proposal first."
            if candidate else "Inspection does not authorize sharing, installation or setup."
        ) + (("\n" + "\n".join(authority)) if authority and not (
            approving and plan.get("candidate_flow") is True) else ""),
        [NativeItem("Sharing proposal" if approving and plan.get("candidate_flow") is True
                    else "Review details and files", pages[page],
                    literal=record.get("operation") != "candidate")], actions=actions,
    )
    if approving and plan.get("candidate_flow") is True and not _approval_is_lossless(view):
        return replace(view, actions=[action for action in actions if not action.primary],
                       notice="Approval facts cannot be displayed completely here; use terminal review.")
    return view


def _candidate_view(plan: dict[str, Any], team_name: str | None = None) -> NativeView:
    facts = plan.get("facts") or {}
    editorial = facts.get("editorial") or {}
    safe, _checks = sharing_checks({**plan, "candidate_flow": True})
    name = editorial.get("editorial_name") or plan.get("editorial_name") or facts.get("name")
    description = editorial.get("editorial_description") or plan.get("editorial_description") or "Editorial description not yet available."
    benefit = editorial.get("coworker_benefit") or plan.get("coworker_benefit") or "Coworker benefit not yet available."
    complete = facts.get("editorial_status", plan.get("editorial_status")) == "complete"
    team = team_name
    reason = facts.get("editorial_failure")
    if reason not in {"invalid_response", "adapter_validation", "timeout", "model_error",
                      "provenance_missing",
                      "model_unavailable", "disclosure_blocked", "package_preparation_failed"}:
        reason = "not prepared"
    return NativeView(
        "Hermes Collective Wisdom",
        "You created a skill that could help your team\n\n"
        f"Skill name: {_safe(name, 'Name unavailable')}\n"
        f"What it does: {_safe(description)}\n"
        f"Why others might benefit: {_safe(benefit)}\n"
        f"Destination team: {_safe(team, 'Team name unavailable')}\n\n"
        "Would you like to review a sharing proposal?",
        notice=("Nothing has been uploaded or published. Review proposal opens the "
                "local package and current policy without sharing. The final Share My Skill "
                "action authorizes that exact proposal." if complete else
                "Nothing has been uploaded or published. Editorial copy is incomplete (" +
                reason.replace("_", " ") +
                "); retry local preparation or edit the private draft before reviewing sharing."),
        actions=[
            NativeAction("✅ Safe To Share" if safe else "Review Checks", "checks"),
            NativeAction("View More Details", "details"),
            NativeAction("Notification settings", "settings"),
            NativeAction("Maybe Later", "cancel"),
            NativeAction("Review proposal" if complete else "Retry preparation", "share"),
        ],
    )


def _installation_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    updating = record["operation"] == "update"
    facts = plan.get("verified_facts")
    facts = facts if isinstance(facts, dict) else {}
    description = plan.get("editorial_description") or plan.get("author_description") or plan.get("description")
    setup_commands = plan.get("setup_commands")
    return NativeView(
        "An updated skill is available in your organization's collective wisdom." if updating else
        "A new skill has been added to your organization's collective wisdom.",
        _skill_name(plan),
        [NativeItem("What does it do?", _safe(description, "Description unavailable. View Details to inspect the skill.")),
         NativeItem("Why it may help", _safe(plan.get("coworker_benefit") or facts.get("coworker_benefit") or plan.get("version_explanation"), "Explanation unavailable")),
         *([NativeItem("What changed", _installed_label(plan) +
                       "\nChange summary unavailable; review the exact version in Details.")]
           if updating else []),
         NativeItem("Version publisher", _safe(plan.get("publisher_display_name"), "Publisher name unavailable") +
                    ("\nTeam: " + _safe(plan["team_display_name"])
                     if plan.get("team_display_name") else ""))]
        + ([NativeItem("Setup requires separate permission", "\n".join(setup_commands))]
           if isinstance(setup_commands, list) and setup_commands
           and all(isinstance(command, str) for command in setup_commands) else [])
        + ([NativeItem("Agent explanation (not authorization)", record["explanation"])]
           if record.get("explanation") else []),
        actions=[
            NativeAction("View Details", "details"), NativeAction("Not Now", "defer"),
            NativeAction("Update" if updating else "Install",
                         "update" if updating else "install", primary=True),
        ],
    )


def _setup_permission_view(record: dict[str, Any]) -> NativeView:
    """File-write consent cannot authorize execution of setup commands."""
    plan = record["plan"]
    commands = plan.get("setup_commands")
    if not isinstance(commands, list) or not commands or not all(
        isinstance(command, str) and command for command in commands
    ):
        raise WisdomValidationError("Reviewed setup commands are unavailable")
    verb = "updated" if record["operation"] == "update" else "installed"
    view = NativeView(
        "Setup needed", f"{_skill_name(plan)}: files {verb}; setup incomplete.",
        [NativeItem("Commands Hermes will run on this computer", "\n".join(commands))],
        notice="This skill requires setup before it can be used. Run the commands shown above? "
        "Not now leaves the files installed but not ready.",
        actions=[NativeAction("Run setup", "setup.run", primary=True),
                 NativeAction("Not now", "setup.defer")],
    )
    if _approval_is_lossless(view):
        return view
    return replace(
        view, actions=[NativeAction("Not now", "setup.defer")],
        notice="Setup commands cannot be shown completely here. Use the terminal setup review; "
        "the files are installed but not ready.",
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
    if isinstance(result, dict) and state in {"completed", "failed", "rejected", "uncertain"} and result.get("state") is not None:
        from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
        title, detail = publication_copy(result)
    elif state == "completed" and isinstance(result, dict):
        publication_state = result.get("publication_state")
        if publication_state == "pending_moderation":
            title, detail = "Waiting for team review", "Submitted for team review; not published yet."
        elif publication_state == "published":
            title, detail = "Sharing request completed", "Check publication status to confirm team availability."
    return NativeView(
        "Collective Wisdom contribution", name,
        [NativeItem(title, detail)],
        actions=[NativeAction("Resume review", "suggest.resume")] if state == "kept" else [],
        notice=f"State: {state}", dismissed=state != "kept",
    )


def _suggest_prepared_view(plan: dict[str, Any]) -> NativeView:
    return NativeView(
        "Sharing draft needs attention", _skill_name(plan),
        [NativeItem("Preparation incomplete", _safe(plan.get("editorial_failure"),
                    "Editorial copy or local checks are incomplete."))],
        actions=[NativeAction("Retry local preparation", "suggest.refresh"),
                 NativeAction("Maybe Later", "suggest.keep")],
        notice="Nothing has been uploaded. Edit the local draft in the terminal if needed.",
    )

def _suggest_checks_view(plan: dict[str, Any]) -> NativeView:
    details = render_findings(plan)
    complete = len(details) <= 2200
    shown = details if complete else details[:2199].rstrip() + "…"

    actions = [NativeAction("Maybe Later", "suggest.keep")]
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

def _suggest_view(plan: dict[str, Any], state: str, result: Any = None) -> NativeView:
    if state != "pending":
        return _suggest_terminal_view(plan, state, result)
    stage = str(plan.get("native_stage") or "prepared")
    views = {
        "prepared": _suggest_prepared_view,
        "warnings": _suggest_checks_view,
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
    sharing = record.get("operation") in {"candidate", "publish"}
    lines = ([f"Destination team: {_safe(record.get('team_name'), 'Team name unavailable')}"]
             if sharing else [])
    if isinstance(owner, dict) and owner.get("profile_name"):
        lines.append(f"Profile: {_safe(owner['profile_name'])}")
    if record.get("expires_at") is not None:
        from .native_inspection import _date  # pylint: disable=import-outside-toplevel
        lines.append(f"Review expires: {_date(record['expires_at'])}")
    if record.get("operation") != "candidate" and record.get("screen") == "approval":
        action = "Sharing" if sharing else "Approval"
        lines.append(f"{action} is available only after reviewing the complete proposal."
                     if plan.get("allowed") is True else f"{action} is not available for this review.")
    return enriched, lines


def _completed_outcome_view(record: dict[str, Any]) -> NativeView | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    plan = record.get("plan")
    name = _skill_name(plan if isinstance(plan, dict) else {})
    if record.get("state") == "failed" and result.get("install_blocked") is True:
        return NativeView(f"⚠️ {name} unchanged", "Install unavailable; no changes made.",
                          dismissed=True)
    from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
    published = publication_copy(result)[0] == "Published"
    if (record.get("state") == "completed" and published
            and isinstance(result.get("detail"), str) and result["detail"]):
        return NativeView("Published", f"{name}\nAvailable to your team.\n{_safe(result['detail'])}", dismissed=True)
    return None


def _private_draft_view(result: Any) -> NativeView:
    if isinstance(result, dict):
        remote_id = result.get("remote_draft_id")
        url = result.get("portal_url")
        acknowledged = (result.get("state") == "pending"
                        and result.get("phase") == "policy_review_required"
                        and isinstance(remote_id, str) and bool(remote_id))
        if acknowledged and isinstance(url, str) and isinstance(remote_id, str):
            target = "/wisdom/review/" + quote(remote_id, safe="")
            valid_url = safe_portal_url(url) == url and url.startswith("https://")
            if valid_url and urlsplit(url).path.endswith(target):
                return NativeView("Private draft in Portal", "Uploaded for your editing; not published "
                                  "or submitted for team moderation.",
                                  actions=[NativeAction("Review/edit in Portal", url=url)], dismissed=True)
    return NativeView("Private draft outcome uncertain", "Check publication status before "
                      "another action; do not retry this upload blindly. Not confirmed published.",
                      dismissed=True)

def _private_pending_view(record: dict[str, Any]) -> NativeView | None:
    if record.get("screen") in {"details", "checks"}:
        return None
    private = _confirmed_private_url(record)
    if private and (record.get("screen") == "authorization_expired" or
                    not record.get("plan", {}).get("receipt") or
                    record.get("portal_changed")):
        changed = record.get("portal_changed") is True
        actions = [NativeAction("Open in Portal", url=private),
                   NativeAction("View More Details", "details"),
                   NativeAction("Notification settings", "settings"),
                   NativeAction("Maybe Later", "cancel")]
        if not changed:
            actions.insert(1, NativeAction("Refresh review", "refresh.review"))
        return NativeView("Portal draft changed" if changed else "Review expired" if
                          record.get("screen") == "authorization_expired" else "Ready for review",
                          ("The Portal draft changed. Review and share the edited version in Portal; "
                           "native Share only covers the original package and cannot adopt Portal edits."
                           if changed else "Your private draft is in Portal, not published yet. "
                           "Refresh the exact original review before native sharing; "
                           "review and share any Portal edits in Portal."), actions=actions)
    if record.get("screen") == "authorization_expired":
        return NativeView("Review expired", "No upload occurred.",
                          actions=[NativeAction("Refresh review", "refresh.review"),
                                   NativeAction("Maybe Later", "cancel")])
    return None


def _terminal_state_view(record: dict[str, Any]) -> NativeView | None:  # pylint: disable=too-many-return-statements
    pending_view = _private_pending_view(record) if (record.get("state") == "pending"
                                                  and record.get("operation") == "publish") else None
    if pending_view is not None:
        return pending_view
    outcome = _completed_outcome_view(record)
    if outcome is not None and record.get("effect_action") != "portal.draft":
        return outcome
    result = record.get("result")
    if (record.get("state") != "pending" and record.get("operation") == "publish"
            and record.get("effect_action") == "portal.draft"):
        return _private_draft_view(result if record.get("state") == "completed" else None)
    if record.get("operation") == "publish" and record.get("state") in {"completed", "uncertain"}:
        from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
        title, detail = publication_copy(result if isinstance(result, dict) else {})
        return NativeView(title, detail, dismissed=True)
    if record.get("failure") == "share_packaging_failed":
        return NativeView(
            "Share Packaging failed",
            "The local skill could not be packaged. Review its files and try again. "
            "Nothing has been uploaded or published.", dismissed=True,
        )
    if record.get("state") == "failed" and record.get("failure") == "package_preparation_failed":
        return NativeView(
            "Install/update preparation failed",
            "The package could not be prepared; no files were installed or updated. "
            "Open a new review to try again.", dismissed=True,
        )
    if (record.get("state") == "cancelled" and record.get("effect_action") == "defer"
            and isinstance(result, dict) and result.get("preference_sync") == "confirmed"):
        plan = record.get("plan")
        return NativeView(
            f"⏸ {_skill_name(plan if isinstance(plan, dict) else {})} postponed",
            "", dismissed=True,
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
        safe, checks = sharing_checks({**plan, "candidate_flow": True})
        return NativeView(
            "Sharing checks", "Results apply only to this reviewed package.",
            [NativeItem("✅ Safe To Share" if safe else "Checks need review", checks)],
            actions=[NativeAction("Looks good.", "back")],
        )
    if state == "pending" and record.get("screen") in {"details", "approval"}:
        return _inspection_view(record)
    if (state == "pending" and record.get("operation") in {"install", "update"}
            and record.get("screen") == "setup"):
        return _setup_permission_view(record)
    if record.get("operation") in {"candidate", "publish"} and state == "pending":
        return _candidate_view(plan, record.get("team_name"))
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
            title, _, summary = render_operation_outcome(
                operation, result, skill_name=name,
            ).partition("\n")
            return NativeView(title, summary, dismissed=True)
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
        if operation in {"candidate", "publish", "suggest"}:
            resolve = getattr(service, "native_team_name", None)
            record["team_name"] = resolve() if callable(resolve) else None
        self.state.save(record)
        return copy.deepcopy(record)

    @staticmethod
    def _publisher_display(plan: dict[str, Any], fact: Any) -> dict[str, str]:
        """Only attach display copy when the exact freshly reviewed revision agrees."""
        if (not isinstance(fact, dict) or fact.get("skill_id") != plan.get("skill_id")
                or fact.get("version") != plan.get("version")
                or not plan.get("published_by_user_id")
                or fact.get("publisher") != plan.get("published_by_user_id")):
            return {}
        return {key: value for key in ("publisher_display_name", "team_display_name")
                if isinstance((value := fact.get(key)), str) and value.strip()}

    def request_install(  # pylint: disable=too-many-arguments
        self, service: Any, invocation: NativeInvocation, reference: str, *,
        update_mode: str | None = None, explanation: str = "",
        plan: dict[str, Any] | None = None, publisher_fact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review_plan: dict[str, Any] = (service.install_plan(reference, update_mode=update_mode)
                                       if plan is None else plan)
        if review_plan.get("operation") != "install":
            raise WisdomValidationError("native install review is invalid")
        return self._insert(service, invocation, "install", {
            **review_plan, "installation_status": _local_installation_label(service, review_plan),
            **self._publisher_display(review_plan, publisher_fact),
        }, explanation=explanation)

    def request_update(
        self, service: Any, invocation: NativeInvocation, skill_id: str, *,
        publisher_fact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = service.update_plan(skill_id)
        if plan.get("operation") != "update":
            raise WisdomValidationError("native update review is invalid")
        return self._insert(service, invocation, "update", {
            **plan, "installation_status": _local_installation_label(service, plan),
            **self._publisher_display(plan, publisher_fact),
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
        plan = service.notification_settings(may_apply=lambda: True)
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
        plan = service.prepare_candidate_offer(event_id)
        record = self._insert(service, invocation, "candidate", {
            **plan, "operation": "candidate", "allowed": False,
        })
        if plan.get("facts", {}).get("editorial_status") != "complete":
            return record
        try:
            prepared_plan, prepared = self._candidate_local_review(record, service)
            if prepared.get("editorial_status") != "complete":
                candidate = copy.deepcopy(record["plan"])
                candidate.setdefault("facts", {})["editorial_status"] = "incomplete"
                candidate["facts"]["editorial_failure"] = prepared.get("editorial_failure")
                updated = {**record, "plan": candidate}
            elif has_reviewable_warnings(prepared):
                if not safe_scan_details(prepared.get("local_scan")):
                    raise WisdomValidationError("scanner warning details are unavailable")
                updated = {**record, "operation": "suggest",
                           "plan": {**prepared_plan, "native_stage": "warnings"}}
            else:
                updated = self._sharing_review(record, prepared_plan, prepared, service, False)
        except Exception:  # pylint: disable=broad-exception-caught
            # The first card must still bind before a retry can claim preparation authority.
            updated = {**record, "failure": "share_preparation_failed"}
        if service.native_owner() != record["owner"]:
            self.state.compare_replace(record, {**record, "state": "cancelled"})
            raise WisdomAuthError("Collective Wisdom review owner changed")
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(updated)

    @staticmethod
    def _candidate_local_review(record: dict[str, Any], service: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        draft_id = service.native_candidate_prepare(record["plan"]["event_id"])
        prepared = _prepared_review(service, {"draft_id": draft_id})
        plan = {**prepared, "skill_name": (
            record["plan"].get("facts", {}).get("name") or prepared.get("skill_name")
        ), "candidate_flow": True}
        return plan, prepared

    def request_suggest(
        self, service: Any, invocation: NativeInvocation, skill_name: str,
    ) -> dict[str, Any]:
        if not str(skill_name or "").strip():
            raise WisdomValidationError("Usage: /wisdom suggest <local-skill>")
        prepared = service.suggest(str(skill_name).strip())
        if not isinstance(prepared, dict) or not str(
            prepared.get("draft_id") or prepared.get("local_draft_id") or ""
        ):
            raise WisdomValidationError("prepared draft identity is unavailable")
        record = self._insert(service, invocation, "suggest", {
            **prepared, "skill_name": str(skill_name).strip(), "native_stage": "prepared",
        })
        if prepared.get("editorial_status") != "complete":
            return record
        if has_reviewable_warnings(prepared):
            if not safe_scan_details(prepared.get("local_scan")):
                return record
            updated = {**record, "plan": {**record["plan"], "native_stage": "warnings"}}
        else:
            displayed = _prepared_review(service, record["plan"])
            updated = self._sharing_review(record, record["plan"], displayed, service, False)
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(updated)

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

    def display_current(
        self, claimed: dict[str, Any], service: Any, invocation: NativeInvocation,
        authority: Callable[[], bool],
    ) -> bool:
        """Fence a card edit to the exact owner, route, and record."""
        try:
            return (authority() is True and self.load(claimed["id"]) == claimed
                    and _plan_owner_matches(claimed)
                    and _owner_matches(claimed, service, invocation))
        except Exception:  # pylint: disable=broad-exception-caught
            return False

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

    def _navigation_action(  # pylint: disable=too-many-return-statements
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any] | None:
        if action == "refresh.review" and record.get("operation") == "publish":
            return self._refresh_publication(record, service, invocation, authority)
        if action == "settings":
            return self._open_settings(record, service, invocation, authority)
        if action in {"details", "review"} and record["operation"] in {"install", "update"}:
            inspect = getattr(service, f"native_{record['operation']}_inspect")
            files = inspect(record["plan"]["receipt"])
            if not authority() or not _owner_matches(record, service, invocation):
                raise WisdomAuthError("Collective Wisdom review owner changed")
            return self._navigate(record, action, package_files=files)
        if action == "share" and record.get("operation") == "candidate":
            if record["plan"].get("facts", {}).get("editorial_status") != "complete":
                refreshed = service.prepare_candidate_offer(record["plan"]["event_id"])
                if not authority() or not _owner_matches(record, service, invocation):
                    raise WisdomAuthError("Collective Wisdom review owner changed")
                updated = {**record, "plan": {**refreshed, "operation": "candidate", "allowed": False},
                           "display_ready": False, "updated_at": self.clock()}
                if not self.state.compare_replace(record, updated):
                    raise WisdomConflict("Collective Wisdom review changed; open it again")
                return updated
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
        plan = service.notification_settings(may_apply=authority)
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
            if (record.get("screen") == "details" and record["plan"].get("candidate_flow") is True):
                result.update(screen="approval", page=0, inspected_through=0)
            else:
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
            plan, prepared = self._candidate_local_review(record, service)
            if prepared.get("editorial_status") != "complete":
                candidate = copy.deepcopy(record["plan"])
                candidate.setdefault("facts", {})["editorial_status"] = "incomplete"
                candidate["facts"]["editorial_failure"] = prepared.get("editorial_failure")
                result = {**record, "plan": candidate, "display_ready": False,
                          "updated_at": self.clock(), "failure": None}
            elif has_reviewable_warnings(prepared):
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

    def act(  # pylint: disable=too-many-branches,too-many-return-statements,too-many-arguments
        self, record_id: str, action: str, service: Any,
        invocation: NativeInvocation, *, authority: Callable[[], bool] = lambda: True,
        progress: Callable[[NativeView, dict[str, Any]], None] | None = None,
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
        record = self._enrich_team_label(record, service, invocation, authority)
        return self._act_pending(record, action, service, invocation, authority, progress)

    def _enrich_team_label(self, record, service, invocation, authority):
        """Resolve old cards via the bound host identity without changing consent."""
        if record.get("operation") not in {"candidate", "publish", "suggest"} or record.get("team_name"):
            return record
        resolve = getattr(service, "native_team_name", None)
        if not callable(resolve) or authority() is not True:
            return record
        try:
            label = resolve()
        except (WisdomAuthError, WisdomValidationError, ConnectionError, TimeoutError):
            label = None
        if not _owner_matches(record, service, invocation) or authority() is not True:
            raise WisdomAuthError("Collective Wisdom review owner changed")
        if not isinstance(label, str) or not label.strip() or label == record["owner"].get("organization_id"):
            return record
        updated = {**record, "team_name": label}
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return updated

    def _act_pending(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
        self, record, action, service, invocation, authority, progress,
    ):
        state = record["state"]
        if state == "kept":
            return self._resume_kept(record, action, service, invocation, authority)
        if action == "defer" or action.startswith(("suggest.", "catalog.install.")):
            return self._act_special(
                action, record, service, invocation, authority,
                progress=progress,
            )
        if action in {"install", "update"} and action == record.get("operation"):
            return self._one_click_package(
                record, action, service, invocation, authority, progress=progress,
            )
        if (record.get("operation") in {"install", "update"}
                and record.get("screen") == "setup"):
            if action == "setup.defer":
                finished = {**record, "state": "completed", "display_ready": False,
                            "updated_at": self.clock()}
                if not self.state.compare_replace(record, finished):
                    raise WisdomConflict("Collective Wisdom review changed; open it again")
                return copy.deepcopy(finished)
            if action == "setup.run":
                return self._apply(record, action, service, invocation, authority)
        navigated = self._navigation_action(record, action, service, invocation, authority)
        if navigated is not None:
            return navigated
        if action == "portal.draft" and (record.get("operation") != "publish"
                                         or record.get("plan", {}).get("candidate_flow") is not True):
            raise WisdomValidationError("Collective Wisdom action was not recognized")
        _validate_review_action(record, "confirm" if action == "portal.draft" else action)

        return self._apply(record, action, service, invocation, authority, progress=progress)

    def _refresh_publication(self, record, service, invocation, authority):
        """Read a new exact receipt without spending it or uploading anything."""
        plan = record["plan"]
        previous = record.get("result") or {}
        target = previous.get("operation_id") or plan.get("draft_id") or plan.get("local_draft_id")
        if not isinstance(target, str) or not target:
            raise WisdomConflict("Publication identity is unavailable; open a new review")
        try:
            fresh = service.publication_review(target)
        except WisdomValidationError as exc:
            if not previous.get("operation_id") or str(exc) != (
                "Authoritative publication draft no longer matches intent"
            ):
                raise
            changed = {**record, "portal_changed": True,
                       "plan": {key: value for key, value in plan.items() if key != "receipt"},
                       "screen": "approval", "display_ready": False, "updated_at": self.clock()}
            if not self.state.compare_replace(record, changed):
                raise WisdomConflict("Collective Wisdom review changed; open it again") from exc
            return copy.deepcopy(changed)
        if not isinstance(fresh, dict):
            raise WisdomConflict("Publication review is unavailable")
        identities = [fresh[key] for key in ("draft_id", "local_draft_id") if key in fresh]
        expected = plan.get("draft_id") or plan.get("local_draft_id")
        matches = all((
            bool(fresh.get("receipt")), bool(identities),
            all(identity == expected for identity in identities),
            not previous.get("operation_id") or fresh.get("operation_id") == target,
            _plan_owner_matches({**record, "plan": fresh}),
            authority() is True, _owner_matches(record, service, invocation),
        ))
        if not matches:
            raise WisdomConflict("The publication changed; open it again")
        if previous.get("operation_id") and fresh.get("hashes") != plan.get("hashes"):
            raise WisdomConflict("The private draft changed; open it again")
        updated = {**record, "plan": {**fresh, **{
            key: plan[key] for key in ("candidate_flow", "editorial_status", "editorial_name",
                                   "editorial_description", "coworker_benefit") if key in plan
        }}, "screen": "approval", "page": 0, "inspected_through": 0,
                   "display_ready": False, "portal_changed": False, "updated_at": self.clock()}
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(updated)

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

    def _one_click_package(  # pylint: disable=too-many-branches,too-many-arguments
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool], *,
        progress: Callable[[NativeView, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Prepare and spend a fresh exact receipt inside the explicit click."""
        plan = record["plan"]
        claimed = {
            **record, "state": "preparing", "display_ready": False,
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        if progress is not None:
            try:
                progress(NativeView(
                    "Hermes Collective Wisdom",
                    f"{'Installing' if action == 'install' else 'Updating'} {_skill_name(plan)}…",
                ), claimed)
            except Exception:
                # A lost edit acknowledgement may still have removed the controls.
                # No package effect has started; never call this an uncertain install.
                self.state.compare_replace(claimed, {
                    **claimed, "state": "failed", "failure": "progress_display_failed",
                })
                raise
        try:
            return self._prepare_one_click(record, claimed, action, service, invocation, authority)
        except Exception:
            if progress is None:
                self.state.compare_replace(claimed, record)
            else:
                failed = {
                    **claimed, "state": "failed", "failure": "package_preparation_failed",
                }
                if self.state.compare_replace(claimed, failed):
                    return copy.deepcopy(failed)
            raise

    def _prepare_one_click(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, record, claimed, action, service, invocation, authority,
    ):
        plan = record["plan"]
        try:
            fresh = _fresh_package_plan(record, action, service)
        except PackagePolicyError:
            if action != "install" or plan.get("receipt"):
                raise
            if not authority() or not _owner_matches(record, service, invocation):
                raise WisdomAuthError("Collective Wisdom review owner changed") from None
            blocked = {
                **record, "state": "failed", "display_ready": False,
                "result": {"install_blocked": True},
                "updated_at": self.clock(),
            }
            if not self.state.compare_replace(claimed, blocked):
                raise WisdomConflict("Collective Wisdom review changed; open it again") from None
            return blocked
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

    def _publish_progress(self, claimed, progress, label="⏳ Sharing…"):
        """A lost progress acknowledgement cannot authorize publication."""
        try:
            progress(NativeView("Hermes Collective Wisdom", label), claimed)
        except Exception:
            self.state.compare_replace(claimed, {
                **claimed, "state": "failed", "failure": "progress_display_failed",
            })
            raise

    def _publication_before_claim(self, record, action, service, invocation, authority):
        if record.get("operation") != "publish":
            return None
        if _authorization_expired(record["plan"], self.clock()):
            expired = {**record, "screen": "authorization_expired",
                       "plan": {key: value for key, value in record["plan"].items()
                                if key != "receipt"},
                       "display_ready": False, "updated_at": self.clock()}
            if not self.state.compare_replace(record, expired):
                raise WisdomConflict("Collective Wisdom review changed; open it again")
            return copy.deepcopy(expired)
        if _confirmed_private_url(record) and action == "confirm":
            # Check the EXISTING receipt without replacing it or claiming an effect.
            service.publication_preflight(record["plan"]["receipt"], may_apply=lambda: (
                authority() is True and _owner_matches(record, service, invocation)
            ))
        return None

    @staticmethod
    def _private_continuation(result, plan, record, service, current):
        """Keep the acknowledged URL but require a new operation-bound receipt."""
        pending = {**result, "state": "pending", "screen": "approval",
                   "plan": {key: value for key, value in plan.items() if key != "receipt"}}
        outcome = result["result"]
        try:
            operation_id = outcome.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise WisdomConflict("Private draft operation is unavailable")
            fresh = service.publication_review(operation_id)
            matches = isinstance(fresh, dict) and all((
                fresh.get("operation_id") == operation_id,
                fresh.get("draft_id") == plan.get("draft_id"),
                fresh.get("hashes") == plan.get("hashes"),
                bool(fresh.get("receipt")),
                _plan_owner_matches({**record, "plan": fresh}),
                current(),
            ))
            if not matches:
                raise WisdomConflict("Private draft review changed")
            pending["plan"] = {**fresh, **{
                key: plan[key] for key in ("candidate_flow", "editorial_status",
                                       "editorial_name", "editorial_description",
                                       "coworker_benefit") if key in plan
            }}
        except Exception:  # pylint: disable=broad-exception-caught
            # Acknowledged upload, but read failed; never reuse consumed authority.
            pass
        return pending

    @staticmethod
    def _setup_continuation(result, operation, action, plan, outcome):
        if (operation in {"install", "update"} and action != "setup.run"
                and plan.get("setup_commands")
                and confirmed_file_change(outcome, action=(
                    "installed" if operation == "install" else "updated"
                ))):
            result.update(state="pending", screen="setup", display_ready=False)
        return result

    def _apply(  # pylint: disable=too-many-arguments,too-many-locals
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
        *, progress: Callable[[NativeView, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        preflight = self._publication_before_claim(record, action, service, invocation, authority)
        if preflight is not None:
            return preflight
        claimed = {
            **record, "state": "applying", "display_ready": False,
            "effect_action": action, "updated_at": self.clock(),
        }
        if not self.state.compare_replace(record, claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        plan = claimed["plan"]
        operation = claimed["operation"]
        if operation == "publish" and progress is not None:
            self._publish_progress(claimed, progress, label=(
                "⏳ Uploading private draft…" if action == "portal.draft" else "⏳ Sharing…"
            ))
        def current() -> bool:
            return _current_effect(
                self.load(record["id"]), claimed, service, invocation, authority
            )
        try:
            outcome = _apply_review_action(action, operation, plan, service, current)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            uncertain = {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            }
            if (self.state.compare_replace(claimed, uncertain)
                    and operation in {"settings", "publish"}
                    and not isinstance(exc, WisdomAuthError)):
                return copy.deepcopy(uncertain)
            raise
        result = {
            **claimed, "state": "completed", "result": outcome,
            "updated_at": self.clock(),
        }
        if operation == "publish" and action == "portal.draft" and _confirmed_private_url(result):
            result = self._private_continuation(result, plan, record, service, current)
        result = self._setup_continuation(result, operation, action, plan, outcome)
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _act_special(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, action: str, record: dict[str, Any], service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool], *,
        progress: Callable[[NativeView, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        operation = record.get("operation")
        if operation in {"install", "update"} and action == "defer":
            return self._defer(record, service, invocation, authority)
        if operation == "suggest" and action.startswith("suggest."):
            return self._act_suggest(action, record, service)
        if operation == "catalog" and action.startswith("catalog.install."):
            selected = self._select_catalog(action, record)
            return self._one_click_package(
                selected, "install", service, invocation, authority, progress=progress,
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
        if not isinstance(review.get("receipt"), str) or not review["receipt"]:
            raise WisdomValidationError("publication review receipt is unavailable")
        return {
            **record, "operation": "publish", "plan": copy.deepcopy({
                **review, "candidate_flow": True,
            }),
            "screen": "approval", "page": 0, "inspected_through": 0,
            "updated_at": self.clock(),
        }

    def _suggest_navigation(
        self, action: str, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any] | object:
        if action == "suggest.keep":
            return {**record, "state": "kept", "updated_at": self.clock()}
        if action == "suggest.refresh" and plan.get("native_stage") == "prepared":
            return self._refresh_suggest(record, plan, service)
        return _UNHANDLED

    def _refresh_suggest(
        self, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any]:
        refreshed = _prepared_review(service, plan)
        updated = {**record, "plan": {
            **refreshed, "skill_name": plan.get("skill_name"), "native_stage": "prepared",
            **({"candidate_flow": True} if plan.get("candidate_flow") is True else {}),
        }, "updated_at": self.clock()}
        if refreshed.get("editorial_status") != "complete":
            return updated
        if has_reviewable_warnings(refreshed):
            if safe_scan_details(refreshed.get("local_scan")):
                updated["plan"]["native_stage"] = "warnings"
            return updated
        return self._sharing_review(record, plan, refreshed, service, False)

    def _act_suggest(
        self, action: str, record: dict[str, Any], service: Any,
    ) -> dict[str, Any]:
        plan = record.get("plan")
        if not isinstance(plan, dict):
            raise WisdomValidationError("Collective Wisdom draft review is unavailable")
        result = self._suggest_navigation(action, record, plan, service)
        if result is _UNHANDLED and action == "suggest.ack" and plan.get("native_stage") == "warnings":
            displayed = _prepared_review(service, plan)
            if review_identity(displayed) != review_identity(plan):
                raise WisdomConflict("The package or scanner findings changed; open it again")
            result = self._sharing_review(record, plan, displayed, service, True)
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
