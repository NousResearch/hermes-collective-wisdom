"""Deterministic native views and PluginState-backed explicit reviews."""
# Self-explanatory immutable view records and narrow adapter methods do not gain clarity
# from repeating their signatures in docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,line-too-long,too-many-lines

from __future__ import annotations

import copy
import hashlib
import json
import logging
import secrets
import time
import unicodedata
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .catalog_table import COLUMNS as CATALOG_COLUMNS
from .catalog_table import installed_cell, one_line, page_bounds, version_cell
from .catalog_table import PAGE_SIZE as CATALOG_PAGE_SIZE
from .errors import (
    ShareCopyError, SharePackagingError, StaleControlError, WisdomAuthError, WisdomConflict,
    WisdomNotFound, WisdomValidationError, describe_exception, redacted_message,
)
from .native_context import NativeInvocation
from .native_inspection import finding_pages, inspection_pages, sharing_checks, text_pages
from .package import PackagePolicyError, SkillRefusal
from .portal_urls import safe_portal_url
from .release_scope import active as release_scope
from .review_state import ReviewState
from .install_review import confirmed_file_change
from .terminal_ui import (
    gateway_findings, has_reviewable_warnings, render_gateway_findings,
    render_findings, render_operation_outcome, review_identity, safe_scan_details,
)

SLACK_SECTION_LIMIT = 2900
SLACK_FIELD_LIMIT = 1900
SLACK_FIELD_COUNT = 10
TELEGRAM_TEXT_LIMIT = 3500
# Card buttons stay short enough to render whole on Slack and Telegram phones.
MAX_ACTION_LABEL = 20
_UNHANDLED = object()
logger = logging.getLogger(__name__)



@dataclass(frozen=True, slots=True)
class NativeAction:
    label: str
    action: str | None = None
    primary: bool = False
    url: str | None = None
    destructive: bool = False
    # Global controls sharing a row render side by side; rows render in order.
    row: int = 0


@dataclass(frozen=True, slots=True)
class NativeField:
    """One calm label/value fact rendered as a Slack field or a bold Telegram label."""
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class NativeItem:
    title: str
    detail: str
    actions: list[NativeAction] = field(default_factory=list)
    preamble: str = ""
    literal: bool = False
    emphasis_labels: tuple[str, ...] = ()
    # The detail is owner text (file names): Slack shows it as plain text, so
    # mrkdwn operators in it stay literal. Other surfaces already escape it.
    literal_detail: bool = False

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
class NativeTableRow:
    """One listing row: table cells plus the compact one-line form for plain chat."""
    cells: tuple[str, ...]
    name: str
    compact: str
    detail: str


@dataclass(frozen=True, slots=True)
class NativeTable:
    """A listing rendered as a native table where the surface supports one.

    ``actions`` replace the per-item controls while the table is shown; they use
    the same callback ids as the items, which remain the fallback layout.
    """
    columns: tuple[str, ...]
    rows: list[NativeTableRow]
    actions: list[NativeAction] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NativeView:  # pylint: disable=too-many-instance-attributes
    title: str
    summary: str
    items: list[NativeItem] = field(default_factory=list)
    actions: list[NativeAction] = field(default_factory=list)
    notice: str = ""
    navigation_actions: list[NativeAction] = field(default_factory=list)
    dismissed: bool = False
    fields: list[NativeField] = field(default_factory=list)
    table: NativeTable | None = None

    def to_text(self) -> str:
        lines = [self.title]
        if self.summary:
            lines.extend(("", self.summary))
        if self.fields:
            lines.append("")
            lines.extend(f"{fact.label}: {fact.value}" for fact in self.fields)
        if self.table is not None:
            for row in self.table.rows:
                lines.extend(("", f"{row.name} · {row.compact}"))
                if row.detail:
                    lines.append(row.detail)
            if self.notice:
                lines.extend(("", self.notice))
            lines.extend(f"{action.label}: {action.url}"
                         for action in [*self.table.actions, *self.actions] if action.url)
            return "\n".join(lines)
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
            and len(encoded(view.notice)) <= SLACK_SECTION_LIMIT
            and len(view.fields) <= SLACK_FIELD_COUNT
            and all(len(encoded(f"*{fact.label}*\n{fact.value}")) <= SLACK_FIELD_LIMIT
                    for fact in view.fields)
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
    # Without model-written copy the release shares the skill's own name and
    # description; no teammate-benefit line exists to require.
    benefit = ("coworker_benefit",) if release_scope().model_editorial else ()
    return bool(
        plan.get("receipt") and plan.get("editorial_status") == "complete"
        and all(plan.get(key) for key in (
            "editorial_name", "editorial_description", *benefit,
            "publication_mode", "policy_version", "authorization_expires_at", "hashes",
        ))
    )


def _benefit_shown(value: Any) -> bool:
    """Show the benefit line when copy exists or the release writes it."""
    return bool(value) or release_scope().model_editorial


def action_label(label: object) -> str:
    """Bound a control label so no native button ever overflows."""
    text = " ".join(str(label or "").split())
    return text if len(text) <= MAX_ACTION_LABEL else text[:MAX_ACTION_LABEL - 1].rstrip() + "…"


def _catalog_version(service: Any, plan: dict[str, Any]) -> int | None:
    """Best-effort latest published version: 0 for a new skill, None when unknown."""
    name = plan.get("slug") or plan.get("editorial_name")
    show = getattr(service, "show", None)
    if not isinstance(name, str) or not name or not callable(show):
        return None
    try:
        detail = show(name)
    except WisdomNotFound:
        return 0
    except Exception:  # pylint: disable=broad-exception-caught
        # The version line is advisory; the Gateway assigns the real version.
        return None
    latest = detail.get("latest_version_detail") if isinstance(detail, dict) else None
    row = latest.get("version") if isinstance(latest, dict) else None
    version = row.get("version") if isinstance(row, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _shared_portal_url(service: Any, outcome: Any) -> str | None:
    """Link the published skill through the catalog's own Portal URL helper."""
    publication = outcome.get("publication_result") if isinstance(outcome, dict) else None
    skill_id = publication.get("skill_id") if isinstance(publication, dict) else None
    show = getattr(service, "show", None)
    if not isinstance(skill_id, str) or not skill_id or not callable(show):
        return None
    try:
        detail = show(skill_id)
    except Exception:  # pylint: disable=broad-exception-caught
        # Publication is already authoritative; the link is best effort.
        return None
    url = safe_portal_url(detail.get("portal_url")) if isinstance(detail, dict) else None
    return url if url and url.startswith("https://") else None


def _file_summary(files: Any) -> str:
    if not isinstance(files, list):
        return "Unavailable"
    size = 0
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("content_utf8"), str):
            size += len(item["content_utf8"].encode("utf-8"))
        elif isinstance(item, dict) and isinstance(item.get("content_base64"), str):
            size += len(item["content_base64"]) * 3 // 4
    count = f"{len(files)} file" + ("" if len(files) == 1 else "s")
    if not size:
        return count
    return f"{count} · {size / 1024:.0f} KB" if size >= 1024 else f"{count} · {size} B"


def _checks_summary(plan: dict[str, Any]) -> str:
    """One calm verdict over the local checks; the server scans again on Share."""
    security = plan.get("security_check") or {}
    rows = [row for row in security.get("checks") or [] if isinstance(row, dict)]
    statuses = [row.get("status") for row in rows]
    if "blocked" in statuses or security.get("local_status") == "blocked":
        return "❌ Blocked"
    if has_reviewable_warnings(plan) or plan.get("warning_acceptance"):
        return "⚠️ Warnings acknowledged"
    professional = plan.get("professionalism_check") or {}
    if release_scope().model_editorial and professional.get("status") not in {None, "pass"}:
        return "⚠️ Needs review"
    notes = statuses.count("advisory")
    if not rows or any(status not in {"pass", "pending", "advisory"} for status in statuses):
        return "⚠️ Incomplete"
    return "🔒 Passed" + (f" · {notes} note" + ("" if notes == 1 else "s") if notes else "")


def _version_label(record: dict[str, Any]) -> str | None:
    version = record.get("catalog_version")
    if version == 0:
        return "New skill · v1"
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        return f"v{version} → v{version + 1}"
    return None


def _share_fields(record: dict[str, Any], plan: dict[str, Any]) -> list[NativeField]:
    fields = [
        NativeField("Team", _safe(record.get("team_name"), "Team name unavailable")),
        NativeField("Skill", _safe(plan.get("slug") or _skill_name(plan))),
    ]
    version = _version_label(record)
    if version:
        fields.append(NativeField("Version", version))
    fields.extend((NativeField("Files", _file_summary(plan.get("files"))),
                   NativeField("Checks", _checks_summary(plan))))
    return fields


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


def _catalog_table(rows: list[NativeTableRow], items: list[NativeItem]) -> NativeTable:
    """Browse rows as a table: Install for not-installed rows, then Portal links."""
    installs, links = [], []
    for row, item in zip(rows, items):
        for action in item.actions:
            if action.url:
                links.append(replace(action, label=f"Open {row.name}", row=1))
            else:
                installs.append(replace(action, label=f"Install {row.name}"))
    return NativeTable(CATALOG_COLUMNS, rows, [*installs, *links])


def _catalog_facts(row: dict[str, Any], version: Any, detail: Any) -> list[str]:
    facts = []
    if version is not None:
        facts.append(f"Version: v{_safe(version)}")
    if detail:
        facts.append(_safe(" ".join(str(detail).split())))
    facts.append(_installed_label(row))
    for label, key in (("Status", "state"), ("Author", "created_by_display_name")):
        if row.get(key):
            facts.append(f"{label}: {_safe(row[key])}")
    return facts


_CATALOG_PAGE_ACTION = "catalog.page."


def _catalog_entry(
    row: dict[str, Any], index: int, command: str, plan: dict[str, Any],
) -> tuple[NativeItem, NativeTableRow]:
    """One listing row; Install carries the row's index into the whole listing."""
    title = _skill_name(row)
    if title == "Name unavailable" and command == "Versions":
        title = _skill_name(plan)
    detail = row.get("author_description") or row.get("description") or ""
    version = row.get("version", row.get("latest_version"))
    actions = []
    status = row.get("installation_status")
    if ((row.get("id") or row.get("skill_id")) and isinstance(status, dict)
            and status.get("state") == "not_installed"):
        actions.append(NativeAction("Install", f"catalog.install.{index}", primary=True))
    if row.get("portal_url"):
        actions.append(NativeAction("Open in Portal", url=_safe(row["portal_url"])))
    item = NativeItem(title, "\n".join(_catalog_facts(row, version, detail)) or "Published skill",
                      actions)
    return item, NativeTableRow(
        (title, version_cell(version), installed_cell(status),
         one_line(detail) or "No description"), title,
        f"{version_cell(version)} · {_installed_label(row)}",
        one_line(detail) or "No description",
    )


def _catalog_navigation(total: int, page: int, pages: int) -> tuple[str, list[NativeAction]]:
    """The browse count and position, and Previous/Next between pages."""
    text = f" · {total} skill{'s' if total != 1 else ''}, newest first"
    if pages > 1:
        text += f" · page {page + 1} of {pages}"
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"{_CATALOG_PAGE_ACTION}{page - 1}"))
    if page + 1 < pages:
        actions.append(NativeAction("Next", f"{_CATALOG_PAGE_ACTION}{page + 1}"))
    return text, actions


def _catalog_view(plan: dict[str, Any], state: str, page: object = 0) -> NativeView:
    """Browse pages through every skill, newest first; show and versions keep one card."""
    command = str(plan.get("command") or "browse").title()
    catalog = _catalog_rows(plan.get("data"))
    page, pages = page_bounds(len(catalog), page) if command == "Browse" else (0, 1)
    start = page * CATALOG_PAGE_SIZE
    entries = [_catalog_entry(row, index, command, plan) for index, row in
               enumerate(catalog[start:start + CATALOG_PAGE_SIZE], start=start)]
    items = [item for item, _row in entries] or [
        NativeItem("No matching skills", "The catalog returned no results.")]
    rows = [row for _item, row in entries]
    summary = f"{command}: " + (
        _skill_name(plan) if command != "Browse" else _safe(plan.get("target"), "All skills")
    )
    navigation: list[NativeAction] = []
    if command == "Browse" and catalog:
        position, navigation = _catalog_navigation(len(catalog), page, pages)
        summary += position
    return NativeView(
        "Collective Wisdom catalog", summary,
        items, notice=(f"State: {state}" if state != "pending" else ""),
        navigation_actions=navigation, dismissed=state != "pending",
        table=_catalog_table(rows, items) if command == "Browse" and rows else None,
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


def _fresh_package_plan(record: dict[str, Any], action: str, service: Any, *,
                        accept_warnings: bool = False) -> dict:
    """Refresh the displayed package exactly and retain its owner/version binding."""
    old = record["plan"]
    if action == "install":
        skill_id = str(old.get("skill_id") or "")
        version = old.get("version")
        reference = f"{skill_id}@v{version}" if skill_id and isinstance(version, int) else skill_id
        fresh = (service.install_plan(reference, accept_warnings=True) if accept_warnings
                 else service.install_plan(reference))
    else:
        skill_id = str(old.get("skill_id") or "")
        fresh = (service.update_plan(skill_id, accept_warnings=True) if accept_warnings
                 else service.update_plan(skill_id))
    if not isinstance(fresh, dict) or fresh.get("operation") != action:
        raise WisdomValidationError(f"native {action} review is invalid")
    for key in ("skill_id", "version", "content_hash", "manifest_hash"):
        if old.get(key) is not None and fresh.get(key) != old.get(key):
            raise WisdomConflict("The displayed package changed; open it again")
    if not _plan_owner_matches({**record, "plan": fresh}):
        raise WisdomAuthError("Collective Wisdom review owner changed")
    return fresh


def _compatibility_blocks(plan: dict[str, Any]) -> list[str]:
    """Declared requirements this machine does not meet, as plugin-authored reasons."""
    compatibility = plan.get("compatibility")
    blocked = compatibility.get("blocked") if isinstance(compatibility, dict) else None
    if not isinstance(blocked, list):
        return []
    return [_safe(reason, "an unmet requirement") for reason in blocked if reason]


def _install_display_plan(fresh: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """A warning-card plan: findings to show, never a spendable receipt."""
    display = {key: copy.deepcopy(value) for key, value in fresh.items() if key != "receipt"}
    display["installation_status"] = record["plan"].get("installation_status")
    return display


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, PackagePolicyError):
        return "policy_blocked"
    if isinstance(exc, WisdomAuthError):
        return "authorization"
    if isinstance(exc, WisdomConflict):
        return "changed"
    if isinstance(exc, (WisdomValidationError, WisdomNotFound)):
        return "invalid_review"
    return "unavailable"


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
    del action
    return service.publication_submit(receipt, may_apply=may_apply)


def _definite_refusal(exc: BaseException) -> bool:
    """A domain refusal is a known outcome; transport/acknowledgement failures stay unknown.

    A status-less validation error can follow a committed write (for example a
    mismatched acknowledgement), so only HTTP-status or explicitly coded refusals count.
    """
    from .errors import WisdomUnavailable  # pylint: disable=import-outside-toplevel
    if isinstance(exc, WisdomUnavailable):
        return False
    if isinstance(exc, (WisdomConflict, WisdomNotFound, PackagePolicyError)):
        return True
    return isinstance(exc, WisdomValidationError) and (
        exc.status is not None or exc.code == "scan_review_required"
    )

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
            actions.insert(0, NativeAction("Auto-suggest off", "settings.mode.fixed"))
        else:
            actions.insert(0, NativeAction("Auto-suggest on", "settings.mode.agent"))
    if isinstance(control, dict) and control.get("revision") is not None:
        actions.insert(0, NativeAction(
            "Unmute" if muted else "Mute one week",
            "settings.mute.off" if muted else "settings.mute.1w",
        ))
    return NativeView(
        "Collective Wisdom settings",
        "Pause sharing suggestions and skill recommendations for you, not your teammates. "
        "You can still browse and install skills.", [item], actions=actions,
    )


_SHARE_HANDLING = {
    "open": "Shared with your team right away. The server scans it again before publishing.",
    "moderated": "A team reviewer approves it before it is available.",
    "managed": "Your team's policy decides how it is published.",
}


def _share_ready(record: dict[str, Any], plan: dict[str, Any]) -> bool:
    return bool(
        _candidate_review_ready(plan) and isinstance(record.get("team_name"), str)
        and record["team_name"].strip()
        and record["team_name"] != (record.get("owner") or {}).get("organization_id")
        and plan.get("allowed", True)
    )


def _copy_note(plan: dict[str, Any]) -> str:
    """Provenance of Hermes-written share copy; View details shows the original."""
    line = plan.get("copy_provenance_line")
    return _safe(line) + " View details shows your original. " if line else ""


def _missing_share_facts(record: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Name what keeps a share card from offering Share, in the owner's words."""
    team = record.get("team_name")
    benefit = ("coworker_benefit",) if release_scope().model_editorial else ()
    checks = (
        ("your team's name", isinstance(team, str) and bool(team.strip())
         and team != (record.get("owner") or {}).get("organization_id")),
        ("the skill's name and description", plan.get("editorial_status") == "complete" and all(
            plan.get(key) for key in ("editorial_name", "editorial_description", *benefit))),
        ("your team's sharing policy", bool(plan.get("publication_mode")
                                             and plan.get("policy_version"))),
        ("the review authorization", bool(plan.get("receipt") and plan.get("hashes")
                                          and plan.get("authorization_expires_at"))),
        ("permission to share", bool(plan.get("allowed", True))),
    )
    return [name for name, present in checks if not present]


def _whole_share_card(record: dict[str, Any], plan: dict[str, Any]) -> NativeView:
    """The single /wisdom share confirmation: Share, Cancel, View details.

    A card missing a sharing fact names it and offers Refresh in its place."""
    from .native_inspection import _date  # pylint: disable=import-outside-toplevel
    ready = _share_ready(record, plan)
    actions = [NativeAction("Cancel", "cancel"), NativeAction("View details", "details", row=1)]
    actions.insert(0, NativeAction("Share", "confirm", primary=True) if ready
                   else NativeAction("Refresh", "refresh.review"))
    handling = _SHARE_HANDLING.get(str(plan.get("publication_mode") or ""),
                                   "Publication policy unavailable.")
    missing = ", ".join(_missing_share_facts(record, plan)) or "a sharing fact"
    return NativeView(
        "📤 Share skill",
        _safe(plan.get("editorial_description"), "Description unavailable"),
        fields=_share_fields(record, plan), actions=actions,
        notice=_copy_note(plan)
        + f"{handling} Review open until {_date(plan.get('authorization_expires_at'))}."
        if ready else f"Not ready to share. Missing: {missing}. Press Refresh to check again. "
        "Nothing has been uploaded.",
    )


def _share_pages(record: dict[str, Any], plan: dict[str, Any]) -> tuple[list[str], str]:
    text = _whole_share_card(record, plan).to_text()
    return text_pages(text), _digest("share", json.dumps([text, plan.get("receipt")]))


def _share_card(record: dict[str, Any], plan: dict[str, Any]) -> NativeView:
    """The share card whole, or paged when it is longer than one card holds.

    Paged, Share appears on the last page once every page has been shown."""
    view = _whole_share_card(record, plan)
    if _approval_is_lossless(view):
        return view
    pages, identity = _share_pages(record, plan)
    page = _open_page(record, pages, identity) or 0
    share = next((action for action in view.actions if action.primary), None)
    controls = _page_controls(record, pages, identity, share, back=False)
    return _without_unrendered_approval(replace(
        view, summary=f"This review is longer than one card. Page {page + 1} of {len(pages)}.",
        fields=[], items=[NativeItem("Review", pages[page], literal=True)],
        actions=[*controls, *(action for action in view.actions if not action.primary)],
        notice="Nothing has been uploaded." + (
            " Share appears on the last page once you have read every page."
            if share is not None and page + 1 < len(pages) else ""),
    ))


def _inspection_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    if (record.get("screen") == "approval" and record.get("operation") == "publish"
            and plan.get("candidate_flow") is True):
        return _share_card(record, plan)
    pages = _inspection_pages(record)
    page = int(record.get("page", 0))
    approving = record.get("screen") == "approval"
    sharing = plan.get("candidate_flow") is True
    _projected, authority = _authority_projection(record, plan)
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"page.{page - 1}"))
    if page + 1 < len(pages):
        actions.append(NativeAction("Next", f"page.{page + 1}"))
    elif approving and plan.get("allowed", True) and (
        not sharing or _candidate_review_ready(plan)
    ):
        partial = _requires_partial_acknowledgement(plan)
        actions.append(NativeAction(
            "Accept partial" if partial else "Share" if sharing else "Confirm",
            "confirm.partial" if partial else "confirm", primary=True,
        ))
    actions.append(NativeAction("Back" if approving or sharing else "Looks good.", "back"))
    candidate = record.get("operation") == "candidate"
    view = NativeView(
        "Exact package review" if approving else
        "Candidate details" if candidate else
        "Skill details" if sharing else "Private package details",
        f"Page {page + 1} of {len(pages)}. " + (
            "Confirm only after inspecting the complete exact plan."
            if approving else "Candidate details are not the prepared package. Review proposal first."
            if candidate else "Viewing details does not share anything."
            if sharing else "Inspection does not authorize sharing, installation or setup."
        ) + (("\n" + "\n".join(authority)) if authority and not sharing else ""),
        [NativeItem("Files, checks and description" if sharing else "Review details and files",
                    pages[page], literal=not candidate)], actions=actions,
    )
    if approving and sharing and not _approval_is_lossless(view):
        return replace(view, actions=[action for action in actions if not action.primary],
                       notice="Approval facts cannot be displayed completely here. Nothing was "
                       "shared; run /wisdom share again to start a new review.")
    return view


def _candidate_view(plan: dict[str, Any], team_name: str | None = None) -> NativeView:
    facts = plan.get("facts") or {}
    editorial = facts.get("editorial") or {}
    safe, _checks = sharing_checks({**plan, "candidate_flow": True})
    name = editorial.get("editorial_name") or plan.get("editorial_name") or facts.get("name")
    description = editorial.get("editorial_description") or plan.get("editorial_description") or "Editorial description not yet available."
    benefit = editorial.get("coworker_benefit") or plan.get("coworker_benefit")
    benefit_line = (f"Why others might benefit: {_safe(benefit or 'Coworker benefit not yet available.')}\n"
                    if _benefit_shown(benefit) else "")
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
        + benefit_line +
        f"Destination team: {_safe(team, 'Team name unavailable')}\n\n"
        "Would you like to review a sharing proposal?",
        notice=("Nothing has been uploaded or published. Review proposal opens the "
                "local package and current policy without sharing. The final Share "
                "action authorizes that exact proposal." if complete else
                "Nothing has been uploaded or published. " + _copy_problem(facts)
                if not release_scope().model_editorial else
                "Nothing has been uploaded or published. Editorial copy is incomplete (" +
                reason.replace("_", " ") +
                "); retry local preparation or edit the private draft before reviewing sharing."),
        actions=[
            NativeAction("✅ Safe To Share" if safe else "Review Checks", "checks"),
            NativeAction("View details", "details"),
            NativeAction("Snooze", "settings"),
            NativeAction("Maybe Later", "cancel"),
            NativeAction("Review proposal" if complete else "Retry preparation", "share"),
        ],
    )


def _installation_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    updating = record["operation"] == "update"
    verb = "update" if updating else "install"
    facts = plan.get("verified_facts")
    facts = facts if isinstance(facts, dict) else {}
    title = _skill_name(plan)
    description = plan.get("editorial_description") or plan.get("author_description") or plan.get("description")
    files = plan.get("files")
    setup_commands = plan.get("setup_commands")
    view = NativeView(
        "A shared skill in your organization is available",
        title,
        [NativeItem("What it does", _safe(description, "Description unavailable")),
         NativeItem("Why it may help", _safe(plan.get("coworker_benefit") or facts.get("coworker_benefit") or plan.get("version_explanation"), "Explanation unavailable")),
         *([NativeItem("What changed", "Change summary unavailable; review the exact version in Details.")]
           if updating else []),
         NativeItem("Version publisher", _safe(plan.get("publisher_display_name"), "Publisher name unavailable") +
                    ("\nTeam: " + _safe(plan["team_display_name"])
                     if plan.get("team_display_name") else "") + "\nOn this profile: " +
                    _installed_label(plan) +
                    (f"\nFiles: {len(files)}" if isinstance(files, list) else ""))]
        + ([NativeItem("Setup", "\n".join(setup_commands))]
           if isinstance(setup_commands, list) and setup_commands
           and all(isinstance(command, str) for command in setup_commands) else [])
        + ([NativeItem("Agent explanation (not authorization)", record["explanation"])]
           if record.get("explanation") else []),
        notice=f"Would you like to {verb} this skill? View Details shows the files and checks. "
        "If there's something to review first, we'll show it before you continue. "
        + (("New setup is shown above. You can choose whether to run it after updating. "
            if updating else "Install adds the files. You can choose whether to run setup afterward. ")
           if setup_commands else "")
        + "Mute changes recommendations for all skills.",
        actions=[
            NativeAction("View Details", "details"), NativeAction("Not Now", "defer"),
            NativeAction("Mute suggestions", "settings"),
            NativeAction("Update Skill" if updating else "Install Skill", verb, primary=True),
        ],
    )
    if _approval_is_lossless(view) or not any(item.title == "Setup" for item in view.items):
        return view
    # Installing never runs setup; the setup card shows every command before it can run.
    count = f"{len(setup_commands)} command" + ("" if len(setup_commands) == 1 else "s")
    return replace(view, items=[
        replace(item, detail=f"{count}. You see every command before choosing whether to run "
                "them.") if item.title == "Setup" else item
        for item in view.items
    ])


def _setup_commands(plan: dict[str, Any]) -> list[str]:
    commands = plan.get("setup_commands")
    if not isinstance(commands, list) or not commands or not all(
        isinstance(command, str) and command for command in commands
    ):
        raise WisdomValidationError("Reviewed setup commands are unavailable")
    return commands


def _setup_pages(plan: dict[str, Any]) -> tuple[list[str], str]:
    commands = _setup_commands(plan)
    return text_pages("\n".join(commands)), _digest("setup", json.dumps(commands))


def _setup_permission_view(record: dict[str, Any]) -> NativeView:
    """File-write consent cannot authorize execution of setup commands.

    Commands too long for one card are paged; Run setup needs every page shown."""
    plan = record["plan"]
    commands = _setup_commands(plan)
    run = NativeAction("Run setup", "setup.run", primary=True)
    pages, identity = _setup_pages(plan)
    page = _open_page(record, pages, identity)
    if page is not None:
        return _without_unrendered_approval(NativeView(
            "Setup commands", f"{_skill_name(plan)} · page {page + 1} of {len(pages)}",
            [NativeItem("Commands Hermes will run on this computer", pages[page], literal=True)],
            actions=_page_controls(record, pages, identity, run),
            notice="Reading the commands runs nothing.",
        ))
    verb = "updated" if record["operation"] == "update" else "installed"
    view = NativeView(
        "Setup needed", f"{_skill_name(plan)}: files {verb}; setup incomplete.",
        [NativeItem("Commands Hermes will run on this computer", "\n".join(commands))],
        notice="This skill requires setup before it can be used. Run the commands shown above? "
        "Not now leaves the files installed but not ready.",
        actions=[run, NativeAction("Not now", "setup.defer")],
    )
    if _approval_is_lossless(view):
        return view
    unlocked = _all_pages_seen(record, pages, identity)
    count = f"{len(commands)} command" + ("" if len(commands) == 1 else "s")
    return _without_unrendered_approval(replace(
        view,
        items=[NativeItem(f"Commands Hermes will run on this computer, page 1 of {len(pages)}",
                          pages[0], literal=True)],
        actions=[*([run] if unlocked else []), NativeAction("Not now", "setup.defer"),
                 NativeAction("View all commands", f"{_PAGE_ACTION}0", row=1)],
        notice=f"This skill requires setup before it can be used. View all commands lists all "
        f"{count}." + ("" if unlocked else " Run setup appears once you reach the last page.")
        + " Not now leaves the files installed but not ready.",
    ))


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


_LOCAL_COPY_REQUIRED = ("The skill's name or description cannot be shared as written. Edit the "
                        "skill's SKILL.md frontmatter, then retry.")


def _copy_problem(plan: dict[str, Any]) -> str:
    """The measured copy problem when known, else the unspecific fallback."""
    return _safe(plan.get("editorial_problem"), _LOCAL_COPY_REQUIRED)


def _refused_preparation(skill_name: str, exc: BaseException) -> dict[str, Any]:
    """A plan for a definite, owner-fixable refusal: nothing was prepared or uploaded."""
    logger.warning("Collective Wisdom share refused (%s)", describe_exception(exc))
    return {
        "skill_name": skill_name, "native_stage": "prepared", "editorial_status": "incomplete",
        "preparation_problem": redacted_message(exc, limit=1000),
    }


def _suggest_prepared_view(plan: dict[str, Any]) -> NativeView:
    if plan.get("preparation_problem"):
        # A definite refusal names its exact reason; Retry prepares the source again.
        return NativeView(
            "Skill needs attention", _skill_name(plan),
            [NativeItem("Not shared", _safe(plan["preparation_problem"],
                                            "This skill cannot be shared as it is."),
                        literal_detail=True)],
            actions=[NativeAction("Retry", "suggest.refresh"),
                     NativeAction("Maybe later", "suggest.keep")],
            notice="Nothing has been uploaded. Fix this in the skill's folder, then press Retry.",
        )
    detail = (_safe(plan.get("editorial_failure"), "Editorial copy or local checks are incomplete.")
              if release_scope().model_editorial else _copy_problem(plan))
    return NativeView(
        "Skill needs attention", _skill_name(plan),
        [NativeItem("Preparation incomplete", detail)],
        actions=[NativeAction("Retry", "suggest.refresh"),
                 NativeAction("Maybe later", "suggest.keep")],
        notice="Nothing has been uploaded. Edit the local skill in the terminal if needed.",
    )

FINDINGS_CARD_LIMIT = 2200
_PAGE_ACTION = "paged."


def _digest(kind: str, material: str) -> str:
    return hashlib.sha256(f"{kind}:{material}".encode("utf-8")).hexdigest()


def _warning_review(record: dict[str, Any]) -> bool:
    """A pending card asking the owner to acknowledge local scanner findings."""
    raw = record.get("plan")
    plan: dict[str, Any] = raw if isinstance(raw, dict) else {}
    operation = record.get("operation")
    return record.get("state") == "pending" and (
        (operation == "suggest" and plan.get("native_stage") == "warnings")
        or (operation in {"install", "update"} and record.get("screen") == "install_warnings")
    )


def _findings_pages(plan: dict[str, Any]) -> tuple[list[str], str]:
    return finding_pages(render_findings(plan)), _digest("findings", review_identity(plan))


def _paged_review(record: dict[str, Any]) -> tuple[list[str], str] | None:
    """The pages an approval on this card covers, and the identity they are bound to."""
    raw = record.get("plan")
    plan: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if _warning_review(record):
        return _findings_pages(plan)
    if (record.get("state") == "pending" and record.get("screen") == "approval"
            and record.get("operation") == "publish" and plan.get("candidate_flow") is True):
        return _share_pages(record, plan)
    if (record.get("state") == "pending" and record.get("screen") == "setup"
            and record.get("operation") in {"install", "update"}):
        try:
            return _setup_pages(plan)
        except WisdomValidationError:
            return None
    return None


def _paging(record: dict[str, Any], identity: str) -> tuple[int | None, set[int]]:
    """The open page and the pages seen, only while they belong to exactly this content."""
    state = record.get("paged")
    if not isinstance(state, dict) or state.get("identity") != identity:
        return None, set()
    page = state.get("page")
    seen = {item for item in state.get("seen") or []
            if isinstance(item, int) and not isinstance(item, bool)}
    return (page if isinstance(page, int) and not isinstance(page, bool) else None), seen


def _open_page(record: dict[str, Any], pages: list[str], identity: str) -> int | None:
    page, _seen = _paging(record, identity)
    return page if page is not None and 0 <= page < len(pages) else None


def _all_pages_seen(record: dict[str, Any], pages: list[str], identity: str) -> bool:
    return set(range(len(pages))) <= _paging(record, identity)[1]


def _page_controls(
    record: dict[str, Any], pages: list[str], identity: str, finish: NativeAction | None,
    *, back: bool = True,
) -> list[NativeAction]:
    """Previous/Next through every page; the approval appears on the last one once all were seen."""
    page = _open_page(record, pages, identity) or 0
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"{_PAGE_ACTION}{page - 1}"))
    if page + 1 < len(pages):
        actions.append(NativeAction("Next", f"{_PAGE_ACTION}{page + 1}"))
    elif finish is not None and _all_pages_seen(record, pages, identity):
        actions.append(finish)
    if back:
        actions.append(NativeAction("Back", f"{_PAGE_ACTION}back"))
    return actions


def _without_unrendered_approval(view: NativeView) -> NativeView:
    """A provider that cannot show every fact on this card never shows its approval."""
    if _approval_is_lossless(view):
        return view
    return replace(view, actions=[action for action in view.actions if not action.primary])


def _finding_counts(details: list[dict[str, Any]]) -> str:
    severities: dict[str, int] = {}
    categories: dict[str, int] = {}
    for finding in details:
        severity = str(finding.get("severity") or "warning").upper()
        category = str(finding.get("category") or "other")
        severities[severity] = severities.get(severity, 0) + 1
        categories[category] = categories.get(category, 0) + 1
    return ("By severity: " + " · ".join(f"{name} {count}" for name, count in severities.items())
            + "\nBy category: "
            + " · ".join(f"{name} {count}" for name, count in categories.items()))


@dataclass(frozen=True, slots=True)
class _FindingsCopy:
    acknowledge: NativeAction
    summary: str
    nothing_done: str
    viewing: str
    changed: str = ""


def _findings_card(
    record: dict[str, Any], plan: dict[str, Any], words: _FindingsCopy,
) -> NativeView:
    """Every finding on one card when it fits; otherwise a summary, the first page and
    View details through all of them. Acknowledge needs every finding to have been shown."""
    details = render_findings(plan)
    pages, identity = _findings_pages(plan)
    page = _open_page(record, pages, identity)
    if page is not None:
        return _without_unrendered_approval(NativeView(
            "Scanner findings", f"{_skill_name(plan)} · page {page + 1} of {len(pages)}",
            [NativeItem("Findings", pages[page], literal=True)],
            actions=_page_controls(record, pages, identity, words.acknowledge),
            notice=words.viewing,
        ))
    count = len(safe_scan_details(plan.get("local_scan")))
    listed = f" View details lists all {count} finding" + ("" if count == 1 else "s") + "."
    fields = [NativeField("Skill", _skill_name(plan)),
              NativeField("Files", _file_summary(plan.get("files")))]
    view_details = NativeAction("View details", f"{_PAGE_ACTION}0", row=1)
    whole = NativeView(
        "⚠️ Review warnings", words.summary, [NativeItem("Scanner warnings", details)],
        fields=fields, actions=[words.acknowledge, NativeAction("Cancel", "cancel"), view_details],
        notice=words.nothing_done + listed + words.changed,
    )
    if len(details) <= FINDINGS_CARD_LIMIT and _approval_is_lossless(whole):
        return whole
    unlocked = _all_pages_seen(record, pages, identity)
    return _without_unrendered_approval(replace(
        whole,
        items=[NativeItem("Summary", _finding_counts(safe_scan_details(plan.get("local_scan")))),
               NativeItem(f"Findings, page 1 of {len(pages)}", pages[0], literal=True)],
        actions=[action for action in whole.actions if unlocked or not action.primary],
        notice=words.nothing_done + listed
        + ("" if unlocked else " Acknowledge appears once you reach the last page.")
        + words.changed,
    ))


def _suggest_checks_view(record: dict[str, Any], plan: dict[str, Any]) -> NativeView:
    return _findings_card(record, plan, _FindingsCopy(
        NativeAction("Acknowledge", "suggest.ack", primary=True),
        "The scanner flagged this skill. Acknowledge the warnings to continue to the Share card.",
        "Nothing has been uploaded.", "Viewing findings shares nothing.",
    ))

def _suggest_view(plan: dict[str, Any], state: str, result: Any = None) -> NativeView:
    if state != "pending":
        return _suggest_terminal_view(plan, state, result)
    if str(plan.get("native_stage") or "prepared") == "prepared":
        return _suggest_prepared_view(plan)
    name = _skill_name(plan)
    return NativeView(
        "Collective Wisdom contribution", name,
        [NativeItem("Review unavailable", "This review can't continue on this card.")],
        notice=f"Nothing has been uploaded. Run /wisdom share {name} again to start a new review.",
        dismissed=True,
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


def _install_warnings_view(record: dict[str, Any], plan: dict[str, Any]) -> NativeView:
    """Scanner findings on a shared package: Acknowledge binds to exactly these findings."""
    count = len(safe_scan_details(plan.get("local_scan")))
    noun = "finding" + ("" if count == 1 else "s")
    verb = "update" if record.get("operation") == "update" else "install"
    return _findings_card(record, plan, _FindingsCopy(
        NativeAction("Acknowledge", "package.ack", primary=True),
        f"The scanner found {count} {noun} in {_skill_name(plan)}. Acknowledge them to "
        f"{verb} this exact version.",
        "Nothing has been installed.", "Viewing findings installs nothing.",
        " If the findings change before installing, they are shown again.",
    ))


def _install_blocked_view(name: str, result: dict[str, Any]) -> NativeView:
    reasons = result.get("reasons")
    if isinstance(reasons, list) and reasons:
        return NativeView(
            f"⚠️ {name} unchanged",
            "Can't install here: " + "; ".join(_safe(reason, "an unmet requirement")
                                               for reason in reasons),
            notice="The skill declares this requirement. No changes were made.",
            dismissed=True,
        )
    return NativeView(f"⚠️ {name} unchanged", "Install unavailable; no changes made.",
                      dismissed=True)


def _completed_outcome_view(record: dict[str, Any]) -> NativeView | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    plan = record.get("plan")
    name = _skill_name(plan if isinstance(plan, dict) else {})
    if record.get("state") == "failed" and result.get("install_blocked") is True:
        return _install_blocked_view(name, result)
    if record.get("operation") == "publish":
        return None
    from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
    published = publication_copy(result)[0] == "Published"
    if (record.get("state") == "completed" and published
            and isinstance(result.get("detail"), str) and result["detail"]):
        return NativeView("✅ Shared", f"{name}\nAvailable to your team.\n{_safe(result['detail'])}",
                          dismissed=True)
    return None


def _publication_version(result: dict[str, Any]) -> int | None:
    publication = result.get("publication_result")
    version = publication.get("version") if isinstance(publication, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _publication_result_view(record: dict[str, Any]) -> NativeView:
    """Shared / blocked / pending result cards on the same layout as the share card."""
    from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
    raw_result = record.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    raw_plan = record.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    fields = [NativeField("Team", _safe(record.get("team_name"), "Team name unavailable")),
              NativeField("Skill", _safe(plan.get("slug") or _skill_name(plan)))]
    title, detail = publication_copy(result)
    if record.get("state") == "completed" and title == "Published":
        version = _publication_version(result)
        if version is not None:
            fields.append(NativeField("Version", f"v{version}"))
        url = safe_portal_url(record.get("portal_url"))
        recovery = result.get("detail") if isinstance(result.get("detail"), str) else ""
        return NativeView(
            "✅ Shared", "Available to your team.", fields=fields, dismissed=True,
            notice=_safe(recovery, "") if recovery else "",
            actions=[NativeAction("View in Portal", url=url)] if url else [],
        )
    if result.get("state") == "pending" and result.get("phase") == "policy_review_required":
        return NativeView("Not shared yet", "This skill is uploaded but not shared with your team. "
                          "Run /wisdom share again to finish sharing.", fields=fields,
                          dismissed=True)
    if title in {"Failed", "Rejected"}:
        return NativeView("❌ Not shared", detail, fields=fields, dismissed=True)
    return NativeView(title, detail, fields=fields, dismissed=True)


def _share_cancelled(record: dict[str, Any]) -> bool:
    plan = record.get("plan")
    return record.get("state") == "cancelled" and isinstance(plan, dict) and (
        record.get("operation") == "suggest"
        or (record.get("operation") == "publish" and plan.get("candidate_flow") is True)
    )


def _gateway_advisory_view(record: dict[str, Any], plan: dict[str, Any]) -> NativeView:
    """Gateway findings after upload, on the same card: Acknowledge enables Share."""
    count = len(gateway_findings(plan))
    noun = "item" + ("" if count == 1 else "s")
    summary = (render_gateway_findings(plan, limit=3) if count else
               "Gateway reported an advisory verdict without finding details.")
    actions = [NativeAction("Acknowledge", "publish.ack", primary=True),
               NativeAction("Cancel", "cancel"), NativeAction("View details", "details", row=1)]
    view = NativeView(
        "⚠️ Gateway checks",
        f"Gateway checks found {count} {noun} to review.",
        [NativeItem("Findings", summary)],
        fields=[NativeField("Team", _safe(record.get("team_name"), "Team name unavailable")),
                NativeField("Skill", _safe(plan.get("slug") or _skill_name(plan))),
                NativeField("Files", _file_summary(plan.get("files"))),
                NativeField("Checks", f"⚠️ {count} Gateway {noun}")],
        actions=actions,
        notice="Uploaded but not shared yet. View details lists every finding. "
        "Acknowledge to continue to Share, or Cancel to leave it unshared.",
    )
    if _approval_is_lossless(view):
        return view
    # The full list stays one tap away in View details; the card never dead-ends.
    return replace(view, items=[NativeItem(
        "Findings", f"{count} {noun}. Open View details to read every finding.")])


def _terminal_state_view(record: dict[str, Any]) -> NativeView | None:  # pylint: disable=too-many-return-statements
    if (record.get("state") == "pending" and record.get("operation") == "publish"
            and record.get("screen") == "authorization_expired"):
        return NativeView("Review expired", "Nothing was uploaded. Refresh to review again.",
                          actions=[NativeAction("Refresh", "refresh.review", primary=True),
                                   NativeAction("Cancel", "cancel")])
    outcome = _completed_outcome_view(record)
    if outcome is not None:
        return outcome
    result = record.get("result")
    if record.get("operation") == "publish" and record.get("state") in {"completed", "uncertain"}:
        return _publication_result_view(record)
    if _share_cancelled(record):
        if isinstance(record.get("plan"), dict) and record["plan"].get("operation_id"):
            return NativeView(
                "Not shared", "This skill is uploaded but not shared with your team. "
                "Run /wisdom share again to finish sharing.", dismissed=True)
        return NativeView("Share cancelled", "Nothing was uploaded.", dismissed=True)
    if record.get("failure") == "publication_refused":
        return NativeView(
            "❌ Not shared", "This card didn't share anything. "
            "Run /wisdom share again to see the current state and continue.",
            dismissed=True)
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
            table=replace(original.table, actions=[]) if original.table else None,
        )
    if record.get("failure") == "share_preparation_failed" and record.get("state") == "pending":
        return NativeView(
            "Share preparation failed",
            "The sharing review could not be prepared. Try again or dismiss this card. "
            "Nothing has been uploaded or published.",
            actions=[NativeAction("Try again", "share"), NativeAction("Maybe Later", "cancel")],
        )
    return None


_MUTE_NOTICE = "Mute changes recommendations for all skills."


def _released_actions(actions: list[NativeAction]) -> list[NativeAction]:
    return [action for action in actions
            if not (action.action or "").startswith("settings")]


def project_view(record: dict[str, Any]) -> NativeView:
    """Project a review, offering only controls the current release exposes.

    The same projection is the callback allowlist, so a control removed here
    cannot be invoked from an older card either.
    """
    view = _project_view(record)
    if release_scope().notification_settings:
        return view
    return replace(
        view,
        actions=_released_actions(view.actions),
        navigation_actions=_released_actions(view.navigation_actions),
        items=[replace(item, actions=_released_actions(item.actions)) for item in view.items],
        notice=view.notice.replace(" " + _MUTE_NOTICE, "").replace(_MUTE_NOTICE, ""),
    )


def _project_view(record: dict[str, Any]) -> NativeView:  # pylint: disable=too-many-return-statements
    terminal = _terminal_state_view(record)
    if terminal is not None:
        return terminal
    raw_plan = record.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    state = str(record.get("state") or "pending")
    if state == "pending" and record.get("screen") == "gateway_advisory":
        return _gateway_advisory_view(record, plan)
    if _warning_review(record):
        return (_install_warnings_view(record, plan) if record.get("screen") == "install_warnings"
                else _suggest_checks_view(record, plan))
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
        return _catalog_view(plan, state, record.get("catalog_page", 0))
    if record.get("operation") == "settings":
        return _settings_view(record)
    if record.get("operation") == "share_status":
        return _share_status_view(plan, state)
    authority, details = _authority_projection(record, plan)
    if record.get("operation") == "suggest":
        return _suggest_view(authority, state, record.get("result"))
    return _effect_view(record, authority, state, details)


_AWAITING_REVIEW = frozenset({"policy_review_required", "approval_rejected"})


def _share_status_view(plan: dict[str, Any], state: str) -> NativeView:
    """Report an earlier unfinished share; it offers no effect of its own."""
    raw = plan.get("status")
    status: dict[str, Any] = raw if isinstance(raw, dict) else {}
    name = _skill_name(plan)
    if status.get("state") in {"done", "rejected"}:
        from .publication_copy import publication_copy  # pylint: disable=import-outside-toplevel
        title, detail = publication_copy(status)
    elif status.get("phase") in _AWAITING_REVIEW:
        title, detail = ("Earlier share needs review", "This skill's earlier share is waiting "
                         "for review and couldn't be reopened here. Nothing new was uploaded. "
                         f"Run /wisdom share {name} again to retry.")
    else:
        title, detail = ("Earlier share still in progress", "An earlier share of this skill "
                         "hasn't finished. Nothing new was uploaded. Run /wisdom share "
                         f"{name} again to check on it.")
    return NativeView(
        "Hermes Collective Wisdom", name, [NativeItem(title, detail)],
        actions=[NativeAction("OK", "cancel")] if state == "pending" else [],
        dismissed=state != "pending",
    )


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
                "Accept partial",
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
        raise StaleControlError()


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
        existing = self._existing_share(service, invocation, str(skill_name).strip())
        if existing is not None:
            return existing
        try:
            prepared = service.suggest(str(skill_name).strip())
        except ShareCopyError as exc:
            # Nothing could be prepared; the card names the measured copy problem.
            return self._insert(service, invocation, "suggest", {
                "skill_name": str(skill_name).strip(), "native_stage": "prepared",
                "editorial_status": "incomplete", "editorial_problem": str(exc),
            })
        except SkillRefusal as exc:
            # The skill's own files break a rule: the card names the exact
            # reason. Any other failure keeps the generic reply.
            return self._insert(service, invocation, "suggest",
                                _refused_preparation(str(skill_name).strip(), exc))
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

    def _existing_share(
        self, service: Any, invocation: NativeInvocation, skill_name: str,
    ) -> dict[str, Any] | None:
        """Resume an unfinished share of the same source instead of re-preparing it.

        A submitted local draft cannot be reopened; its retained operation is the
        only authority, exactly as the terminal share fences it.
        """
        read_status = getattr(service, "share_status", None)
        pending = read_status(skill_name) if callable(read_status) else None
        if not isinstance(pending, dict):
            return None
        operation_id = str(pending.get("operation_id") or "")
        if operation_id and pending.get("phase") in _AWAITING_REVIEW:
            try:
                review = service.publication_review(operation_id)
            except WisdomConflict:
                review = None
                advisory = self._resume_gateway_advisory(
                    service, invocation, skill_name, operation_id,
                )
                if advisory is not None:
                    return advisory
            if (isinstance(review, dict) and review.get("operation_id") == operation_id
                    and isinstance(review.get("receipt"), str) and review["receipt"]):
                record = self._insert(service, invocation, "publish", {
                    **review, "skill_name": skill_name, "candidate_flow": True,
                })
                if not _plan_owner_matches(record):
                    raise WisdomConflict("The publication owner changed; open it again")
                updated = {**record, "screen": "approval", "page": 0, "inspected_through": 0}
                if not self.state.compare_replace(record, updated):
                    raise WisdomConflict("Collective Wisdom review changed; open it again")
                return copy.deepcopy(updated)
        return self._insert(service, invocation, "share_status", {
            "operation": "share_status", "skill_name": skill_name, "allowed": False,
            "status": {key: pending.get(key) for key in (
                "operation_id", "state", "phase", "publication_state", "code",
            )},
        })

    def _resume_gateway_advisory(
        self, service: Any, invocation: NativeInvocation, skill_name: str, operation_id: str,
    ) -> dict[str, Any] | None:
        """Reopen a pending Gateway advisory on the same Acknowledge card."""
        read = getattr(service, "publication_gateway_review", None)
        review = read(operation_id) if callable(read) else None
        if not (isinstance(review, dict) and review.get("operation_id") == operation_id
                and review.get("review_required") == "gateway_advisory"):
            return None
        record = self._insert(service, invocation, "publish", {
            **{key: value for key, value in review.items() if key != "receipt"},
            "skill_name": skill_name, "candidate_flow": True,
        })
        if not _plan_owner_matches(record):
            raise WisdomConflict("The publication owner changed; open it again")
        updated = {**record, "screen": "gateway_advisory", "gateway_advisory": True,
                   "page": 0, "inspected_through": 0}
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(updated)

    def load(self, record_id: str) -> dict[str, Any]:
        row = self.state.get(record_id)
        if row is None:
            # A card whose record is gone (pruned or from another install) is stale.
            raise StaleControlError("Collective Wisdom review was not found")
        return row

    def stale_control(self, record: dict[str, Any], action: str) -> bool:
        """True when a callback names a control the record's current card does not offer.

        Mirrors the allowlist in ``act`` without side effects so transports can
        answer old/forged controls with a bounded notice before acknowledging.
        In-flight states are excluded: a repeat click there is a conflict, not a
        stale card.
        """
        state = str(record.get("state") or "")
        if state in {"applying", "preparing"}:
            return False
        if action == "cancel" and state in {"pending", "kept"}:
            return False
        try:
            _validate_offered_action(record, action)
        except StaleControlError:
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            return False
        return False

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

    def _turn_page(self, record: dict[str, Any], action: str) -> dict[str, Any]:
        """Open one offered page, recording it as seen for exactly this content."""
        paged = _paged_review(record)
        if paged is None:
            raise StaleControlError()
        pages, identity = paged
        page, seen = _paging(record, identity)
        target: int | None = None
        if action != f"{_PAGE_ACTION}back":
            try:
                target = int(action[len(_PAGE_ACTION):])
            except ValueError as exc:
                raise WisdomValidationError("Invalid review page") from exc
            if not 0 <= target < len(pages):
                raise WisdomValidationError("Invalid review page")
            seen.add(target)
        # The card shows the first page; a control pressed on a page proves it was displayed.
        seen.add(page if page is not None and 0 <= page < len(pages) else 0)
        result = {**record, "paged": {"identity": identity, "page": target, "seen": sorted(seen)},
                  "display_ready": False, "updated_at": self.clock()}
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _turn_catalog_page(self, record: dict[str, Any], action: str) -> dict[str, Any]:
        """Show one offered browse page; Install keeps its index into the whole listing."""
        try:
            target = int(action[len(_CATALOG_PAGE_ACTION):])
        except ValueError as exc:
            raise WisdomValidationError("Invalid catalog page") from exc
        total = len(_catalog_rows(record["plan"].get("data")))
        if page_bounds(total, target)[0] != target:
            raise WisdomValidationError("Invalid catalog page")
        result = {**record, "catalog_page": target, "display_ready": False,
                  "updated_at": self.clock()}
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

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
            if record.get("screen") == "details" and record.get("gateway_advisory") is True:
                result.update(screen="gateway_advisory", page=0, inspected_through=0)
            elif (record.get("screen") == "details" and record["plan"].get("candidate_flow") is True):
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
        if action.startswith(_PAGE_ACTION):
            return self._turn_page(record, action)
        if action.startswith(_CATALOG_PAGE_ACTION) and record.get("operation") == "catalog":
            return self._turn_catalog_page(record, action)
        if action == "defer" or action.startswith(("suggest.", "catalog.install.")):
            return self._act_special(
                action, record, service, invocation, authority,
                progress=progress,
            )
        if action in {"install", "update"} and action == record.get("operation"):
            return self._one_click_package(
                record, action, service, invocation, authority, progress=progress,
            )
        if (action == "package.ack" and record.get("screen") == "install_warnings"
                and record.get("operation") in {"install", "update"}):
            return self._one_click_package(
                record, record["operation"], service, invocation, authority,
                progress=progress, acknowledged=True,
            )
        if action == "publish.ack" and record.get("operation") == "publish":
            return self._acknowledge_gateway(record, service, invocation, authority)
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
        _validate_review_action(record, action)

        return self._apply(record, action, service, invocation, authority, progress=progress)

    def _refresh_publication(self, record, service, invocation, authority):
        """Read a new exact receipt without spending it or uploading anything."""
        plan = record["plan"]
        target = plan.get("operation_id") or plan.get("draft_id") or plan.get("local_draft_id")
        if not isinstance(target, str) or not target:
            raise WisdomConflict("Publication identity is unavailable; open a new review")
        fresh = service.publication_review(target)
        if not isinstance(fresh, dict):
            raise WisdomConflict("Publication review is unavailable")
        identities = [fresh[key] for key in ("draft_id", "local_draft_id") if key in fresh]
        expected = plan.get("draft_id") or plan.get("local_draft_id")
        matches = all((
            bool(fresh.get("receipt")), bool(identities),
            all(identity == expected for identity in identities),
            _plan_owner_matches({**record, "plan": fresh}),
            authority() is True, _owner_matches(record, service, invocation),
        ))
        if not matches:
            raise WisdomConflict("The publication changed; open it again")
        updated = {**record, "plan": {**fresh, **{
            key: plan[key] for key in ("candidate_flow", "editorial_status", "editorial_name",
                                   "editorial_description", "coworker_benefit") if key in plan
        }}, "screen": "approval", "page": 0, "inspected_through": 0,
                   "display_ready": False, "updated_at": self.clock()}
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
            raise StaleControlError()
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
        acknowledged: bool = False,
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
            prepared, apply_now = self._prepare_one_click(
                record, claimed, action, service, invocation, authority,
                acknowledged=acknowledged,
            )
        except Exception as exc:
            logger.warning(
                "Collective Wisdom %s preparation failed category=%s review=%s (%s)",
                action, _failure_category(exc), record.get("id"), describe_exception(exc),
            )
            if progress is None:
                self.state.compare_replace(claimed, record)
            else:
                failed = {
                    **claimed, "state": "failed", "failure": "package_preparation_failed",
                }
                if self.state.compare_replace(claimed, failed):
                    return copy.deepcopy(failed)
            raise
        # The effect logs its own outcome; it is never reported as a preparation failure.
        return self._apply(prepared, "confirm", service, invocation, authority) if apply_now \
            else prepared

    def _finish_blocked(self, record, claimed, action, reasons=None):
        """Record an install the skill's own declarations (or policy) rule out here."""
        logger.warning(
            "Collective Wisdom %s blocked category=%s review=%s", action,
            "declared_requirement" if reasons else "policy_blocked", record.get("id"),
        )
        blocked = {
            **record, "state": "failed", "display_ready": False,
            "result": {"install_blocked": True, **({"reasons": reasons} if reasons else {})},
            "updated_at": self.clock(),
        }
        if not self.state.compare_replace(claimed, blocked):
            raise WisdomConflict("Collective Wisdom review changed; open it again") from None
        return copy.deepcopy(blocked)

    def _show_install_warnings(self, record, claimed, fresh):
        """Show exactly these findings; Acknowledge is bound to their identity."""
        if not safe_scan_details(fresh.get("local_scan")):
            raise PackagePolicyError("scanner warning details are unavailable")
        shown = {
            **record, "state": "pending", "plan": _install_display_plan(fresh, record),
            "screen": "install_warnings", "acknowledged_findings": review_identity(fresh),
            "display_ready": False, "updated_at": self.clock(),
        }
        for key in ("package_files", "inspection_receipt", "page", "inspected_through", "paged"):
            shown.pop(key, None)
        if not self.state.compare_replace(claimed, shown):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(shown)

    def _prepare_one_click(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, record, claimed, action, service, invocation, authority, *, acknowledged=False,
    ) -> tuple[dict[str, Any], bool]:
        """Return the stored record and whether its exact receipt may be spent now."""
        plan = record["plan"]
        try:
            fresh = _fresh_package_plan(record, action, service, accept_warnings=acknowledged)
        except PackagePolicyError:
            if action != "install" or plan.get("receipt") or acknowledged:
                raise
            if not authority() or not _owner_matches(record, service, invocation):
                raise WisdomAuthError("Collective Wisdom review owner changed") from None
            return self._finish_blocked(record, claimed, action), False
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        reasons = _compatibility_blocks(fresh)
        if reasons and fresh.get("allowed") is not True:
            return self._finish_blocked(record, claimed, action, reasons), False
        if has_reviewable_warnings(fresh) and (
                not acknowledged
                or review_identity(fresh) != record.get("acknowledged_findings")):
            # Unacknowledged, or the findings changed since they were shown: show them again.
            return self._show_install_warnings(record, claimed, fresh), False
        inspect = getattr(service, f"native_{action}_inspect")
        files = inspect(fresh["receipt"])
        ready = {
            **record, "plan": copy.deepcopy({
                **fresh, "installation_status": plan.get("installation_status"),
            }),
            "package_files": copy.deepcopy(files),
            "inspection_receipt": fresh.get("receipt"),
            "updated_at": self.clock(), "display_ready": False,
        }
        ready.pop("acknowledged_findings", None)
        ready.pop("paged", None)
        if fresh.get("allowed") is not True or _requires_partial_acknowledgement(fresh):
            ready.update(screen="approval", page=0, inspected_through=0)
            if not self.state.compare_replace(claimed, ready):
                raise WisdomConflict("Collective Wisdom review changed; open it again")
            return copy.deepcopy(ready), False
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        if not self.state.compare_replace(claimed, ready):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return ready, True

    def _publish_progress(self, claimed, progress, label="⏳ Sharing…"):
        """A lost progress acknowledgement cannot authorize publication."""
        try:
            progress(NativeView("Hermes Collective Wisdom", label), claimed)
        except Exception:
            self.state.compare_replace(claimed, {
                **claimed, "state": "failed", "failure": "progress_display_failed",
            })
            raise

    def _publication_before_claim(self, record):
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
        return None

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
        preflight = self._publication_before_claim(record)
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
            self._publish_progress(claimed, progress)
        def current() -> bool:
            return _current_effect(
                self.load(record["id"]), claimed, service, invocation, authority
            )
        try:
            outcome = _apply_review_action(action, operation, plan, service, current)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            refused = operation == "publish" and _definite_refusal(exc)
            logger.warning(
                "Collective Wisdom %s %s (%s) operation=%s review=%s", operation,
                "refused" if refused else "outcome unknown", describe_exception(exc),
                plan.get("operation_id") or "new", record.get("id"),
            )
            failed = ({**claimed, "state": "failed", "failure": "publication_refused",
                       "result": {"outcome": "refused"},
                       "updated_at": self.clock()} if refused else {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            })
            if (self.state.compare_replace(claimed, failed)
                    and operation in {"settings", "publish"}
                    and not isinstance(exc, WisdomAuthError)):
                return copy.deepcopy(failed)
            raise
        if operation == "publish" and isinstance(outcome, dict):
            advisory = self._gateway_advisory_outcome(claimed, plan, outcome)
            if advisory is not None:
                return advisory
            if outcome.get("state") == "uncertain":
                logger.warning(
                    "Collective Wisdom publication outcome unknown operation=%s phase=%s "
                    "review=%s", outcome.get("operation_id"), outcome.get("phase"),
                    record.get("id"),
                )
        result = {
            **claimed, "state": "completed", "result": outcome,
            "updated_at": self.clock(),
        }
        if operation == "publish":
            url = _shared_portal_url(service, outcome)
            if url:
                result["portal_url"] = url
        result = self._setup_continuation(result, operation, action, plan, outcome)
        if not self.state.compare_replace(claimed, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _gateway_advisory_outcome(
        self, claimed: dict[str, Any], plan: dict[str, Any], outcome: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Turn the same card into the Gateway findings card; nothing was approved."""
        if not (outcome.get("state") == "pending"
                and outcome.get("review_required") == "gateway_advisory"
                and isinstance(outcome.get("operation_id"), str)):
            return None
        display = {key: value for key, value in plan.items()
                   if key not in {"receipt", "authorization_expires_at", "warning_acceptance"}}
        display.update(operation_id=outcome["operation_id"],
                       server_checks=copy.deepcopy(outcome.get("server_checks")),
                       gateway_evidence_hash=outcome.get("gateway_evidence_hash"),
                       review_required="gateway_advisory")
        pending = {
            **claimed, "state": "pending", "plan": display, "screen": "gateway_advisory",
            "gateway_advisory": True, "page": 0, "inspected_through": 0,
            "display_ready": False, "effect_action": None, "updated_at": self.clock(),
        }
        if not self.state.compare_replace(claimed, pending):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(pending)

    def _acknowledge_gateway(
        self, record: dict[str, Any], service: Any, invocation: NativeInvocation,
        authority: Callable[[], bool],
    ) -> dict[str, Any]:
        """Bind an acknowledgement to the live Gateway evidence, then offer Share."""
        plan = record["plan"]
        operation_id = plan.get("operation_id")
        if record.get("screen") != "gateway_advisory" or not isinstance(operation_id, str):
            raise StaleControlError()
        review = service.publication_review(operation_id, accept_warnings=True)
        if not (isinstance(review, dict) and review.get("operation_id") == operation_id
                and isinstance(review.get("receipt"), str) and review["receipt"]
                and _plan_owner_matches({**record, "plan": review})):
            raise WisdomConflict("The publication changed; open it again")
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        rendered = plan.get("gateway_evidence_hash")
        if not (isinstance(rendered, str) and rendered
                and review.get("gateway_evidence_hash") == rendered):
            # Not exactly the evidence this card rendered: show it here and ask again.
            updated = {**record, "plan": {
                **{key: value for key, value in review.items()
                   if key not in {"receipt", "authorization_expires_at", "warning_acceptance"}},
                "skill_name": plan.get("skill_name"), "candidate_flow": True,
                "review_required": "gateway_advisory",
            }, "display_ready": False, "updated_at": self.clock()}
        else:
            updated = {**record, "plan": {
                **review, "skill_name": plan.get("skill_name"), "candidate_flow": True,
            }, "screen": "approval", "gateway_advisory": False, "page": 0,
                "inspected_through": 0, "display_ready": False, "updated_at": self.clock()}
        if not self.state.compare_replace(record, updated):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(updated)

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
            "catalog_version": _catalog_version(service, review),
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
        if plan.get("candidate_flow") is not True and (
                not (plan.get("draft_id") or plan.get("local_draft_id"))
                or plan.get("editorial_problem")):
            # The skill's own copy could not be shared. The owner edits SKILL.md,
            # so Retry prepares the current source again instead of the old draft.
            return self._reprepare_suggest(record, plan, service)
        refreshed = _prepared_review(service, plan)
        updated = {**record, "plan": {
            **refreshed, "skill_name": plan.get("skill_name"), "native_stage": "prepared",
            **({"candidate_flow": True} if plan.get("candidate_flow") is True else {}),
        }, "updated_at": self.clock()}
        return self._advance_prepared(record, updated, refreshed, service)

    def _reprepare_suggest(
        self, record: dict[str, Any], plan: dict[str, Any], service: Any,
    ) -> dict[str, Any]:
        name = str(plan.get("skill_name") or "").strip()
        if not name:
            raise WisdomValidationError("Collective Wisdom draft review is unavailable")
        try:
            prepared = service.suggest(name)
        except ShareCopyError as exc:
            return {**record, "plan": {
                "skill_name": name, "native_stage": "prepared",
                "editorial_status": "incomplete", "editorial_problem": str(exc),
            }, "updated_at": self.clock()}
        except SkillRefusal as exc:
            return {**record, "plan": _refused_preparation(name, exc),
                    "updated_at": self.clock()}
        if not isinstance(prepared, dict) or not str(
            prepared.get("draft_id") or prepared.get("local_draft_id") or ""
        ):
            raise WisdomValidationError("prepared draft identity is unavailable")
        updated = {**record, "plan": {
            **prepared, "skill_name": name, "native_stage": "prepared",
        }, "updated_at": self.clock()}
        if prepared.get("editorial_status") != "complete":
            return updated
        displayed = _prepared_review(service, updated["plan"])
        return self._advance_prepared(record, updated, displayed, service)

    def _advance_prepared(
        self, record: dict[str, Any], updated: dict[str, Any], refreshed: dict[str, Any],
        service: Any,
    ) -> dict[str, Any]:
        if refreshed.get("editorial_status") != "complete":
            return updated
        if has_reviewable_warnings(refreshed):
            if safe_scan_details(refreshed.get("local_scan")):
                updated["plan"]["native_stage"] = "warnings"
            return updated
        return self._sharing_review(record, updated["plan"], refreshed, service, False)

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
                # Not the findings this card showed: show the current ones; nothing is acknowledged.
                result = self._advance_prepared(record, {**record, "plan": {
                    **displayed, "skill_name": plan.get("skill_name"), "native_stage": "prepared",
                    **({"candidate_flow": True} if plan.get("candidate_flow") is True else {}),
                }, "updated_at": self.clock()}, displayed, service)
            else:
                result = self._sharing_review(record, plan, displayed, service, True)
        if result is _UNHANDLED:
            raise WisdomValidationError("Collective Wisdom action was not recognized")
        assert isinstance(result, dict)
        result.pop("paged", None)
        result["display_ready"] = False
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)


__all__ = [
    "NativeAction", "NativeItem", "NativeReviewController", "NativeView",
    "assessment_view", "callback_value", "parse_callback", "project_view",
    "slack_section_text", "telegram_view_text", "_approval_is_lossless",
]
