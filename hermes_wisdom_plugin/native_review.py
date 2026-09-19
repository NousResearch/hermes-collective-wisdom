"""Deterministic native views and PluginState-backed explicit reviews."""
# Self-explanatory immutable view records and narrow adapter methods do not gain clarity
# from repeating their signatures in docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,line-too-long

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

SLACK_SECTION_LIMIT = 2900
TELEGRAM_TEXT_LIMIT = 3500


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


def _approval_is_lossless(view: NativeView) -> bool:
    """Approval controls are allowed only when the rendered review remains complete."""
    if not any(action.primary for action in view.actions):
        return True
    return (
        len(view.to_text()) <= TELEGRAM_TEXT_LIMIT
        and all(item.detail for item in view.items)
        and all(
            len(f"{item.preamble}\n\n{item.title}\n{item.detail}") <= SLACK_SECTION_LIMIT
            for item in view.items
        )
    )


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
            and _owner_matches(current, service, invocation)
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # A broken/revoked authority check must fail closed.
        return False


def _summary(plan: dict[str, Any]) -> str:
    operation = str(plan.get("operation") or "review").replace("_", " ").title()
    subject = plan.get("slug") or plan.get("skill_name") or plan.get("skill_id") or plan.get("draft_id")
    version = f" v{plan['version']}" if plan.get("version") else ""
    return f"{operation}: {_safe(subject)}{version}"


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
        combined["portal_url"] = data.get("portal_url")
        return [combined]
    return [data]


def _catalog_view(plan: dict[str, Any], state: str) -> NativeView:
    command = str(plan.get("command") or "browse").title()
    items = []
    for index, row in enumerate(_catalog_rows(plan.get("data"))[:5]):
        title = _safe(
            row.get("slug") or row.get("name") or row.get("skill_name")
            or row.get("id") or row.get("skill_id")
        )
        detail = row.get("author_description") or row.get("description") or ""
        version = row.get("version", row.get("latest_version"))
        facts = []
        if version is not None:
            facts.append(f"Version: v{_safe(version)}")
        if detail:
            facts.append(_safe(detail))
        for label, key in (("Status", "state"), ("Author", "created_by_display_name")):
            if row.get(key):
                facts.append(f"{label}: {_safe(row[key])}")
        actions = []
        skill_id = row.get("id") or row.get("skill_id")
        if skill_id:
            actions.append(NativeAction("Review install", f"catalog.install.{index}"))
        if row.get("portal_url"):
            actions.append(NativeAction("Open in Portal", url=_safe(row["portal_url"])))
        items.append(NativeItem(title, "\n".join(facts) or "Published skill", actions))
    if not items:
        items = [NativeItem("No matching skills", "The catalog returned no results.")]
    return NativeView(
        "Collective Wisdom catalog", f"{command}: {_safe(plan.get('target'), 'All skills')}",
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


def _validate_review_action(record: dict[str, Any], action: str) -> None:
    if action.startswith("settings.") and record.get("operation") == "settings":
        return
    plan = record.get("plan")
    if action not in {"confirm", "confirm.partial"} or not isinstance(plan, dict):
        raise WisdomValidationError("Collective Wisdom action was not recognized")
    operation = record.get("operation")
    if operation in {"install", "update", "publish"} or record.get("screen"):
        pages = inspection_pages(record)
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
        duration = "1w" if action.endswith(".1w") else None
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


def _settings_view(plan: dict[str, Any], state: str) -> NativeView:
    item = NativeItem("Current settings", "\n".join(_fact_lines(plan)))
    if state != "pending":
        return NativeView(
            "Collective Wisdom settings", "Notification settings", [item],
            notice=f"State: {state}", dismissed=True,
        )
    actions = [NativeAction("Cancel", "cancel", destructive=True)]
    if plan.get("mode_writable") is True:
        mode = str(plan.get("delivery_mode") or "fixed")
        if mode == "agent":
            actions.insert(0, NativeAction("Fixed notifications", "settings.mode.fixed"))
        else:
            actions.insert(0, NativeAction("Agent notifications", "settings.mode.agent"))
    control = plan.get("mute_control")
    if isinstance(control, dict) and control.get("revision") is not None:
        actions.insert(0, NativeAction("Unmute recommendations", "settings.mute.off"))
        actions.insert(0, NativeAction("Mute one week", "settings.mute.1w"))
    return NativeView(
        "Collective Wisdom settings",
        "These controls affect Collective Wisdom sharing and installation recommendations, "
        "not just this skill. Opening settings changes nothing.", [item], actions=actions,
    )


def _inspection_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    pages = inspection_pages(record)
    page = int(record.get("page", 0))
    approving = record.get("screen") == "approval"
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
        ),
        [NativeItem("Exact review data (JSON)", pages[page], literal=True)], actions=actions,
    )


def _candidate_view(plan: dict[str, Any]) -> NativeView:
    facts = plan.get("facts") or {}
    safe, _checks = sharing_checks(plan)
    return NativeView(
        "You created a skill that could help your team",
        _safe(facts.get("name") or plan.get("editorial_name") or plan.get("draft_id"), "Local skill"),
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
    return NativeView(
        "A shared skill in your organization is available",
        _summary(plan),
        [NativeItem("About this skill", _safe(description, "Open details for the exact package."))]
        + ([NativeItem("Agent explanation (not authorization)", record["explanation"])]
           if record.get("explanation") else []),
        notice=f"Would you like to {verb} this skill? "
        "Review the exact package, warnings and prerequisites before confirming. "
        "Installing does not authorize setup. Mute opens settings for all recommendations.",
        actions=[
            NativeAction("View Details", "details"), NativeAction("Not Now", "cancel"),
            NativeAction("Mute Skill Recommendations", "settings"),
            NativeAction("Update Skill" if updating else "Install Skill", "review"),
        ],
    )


def project_view(record: dict[str, Any]) -> NativeView:  # pylint: disable=too-many-return-statements
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
        return _settings_view(plan, state)
    return _effect_view(record, plan, state)


def _effect_view(record: dict[str, Any], plan: dict[str, Any], state: str) -> NativeView:
    title = "Collective Wisdom review"
    summary = _summary(plan)
    details = _fact_lines(plan)
    item = NativeItem(summary, "\n".join(details) or "Review the exact action in Collective Wisdom.")
    if state != "pending":
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


def assessment_view(job: dict[str, Any], advice: dict[str, Any] | None = None) -> NativeView:
    del advice
    reference = job.get("reference") if isinstance(job.get("reference"), dict) else job
    subject = reference.get("skill_name") or reference.get("skill_id") or "shared skill"
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
        return self._insert(service, invocation, "install", plan, explanation=explanation)

    def request_install_modes(self, service: Any, invocation: NativeInvocation, reference: str) -> dict[str, Any]:
        return self.request_install(service, invocation, reference)

    def request_update(
        self, service: Any, invocation: NativeInvocation, skill_id: str
    ) -> dict[str, Any]:
        plan = service.update_plan(skill_id)
        if plan.get("operation") != "update":
            raise WisdomValidationError("native update review is invalid")
        return self._insert(service, invocation, "update", plan)

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
        return self._insert(service, invocation, "setup", plan)

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
        return self._insert(service, invocation, "catalog", {
            "operation": "catalog", "command": command, "target": target,
            "data": data, "allowed": False,
        })

    def request_candidate(self, service: Any, invocation: NativeInvocation, event_id: str) -> dict[str, Any]:
        plan = service.native_candidate_inspect(event_id)
        return self._insert(service, invocation, "candidate", {**plan, "operation": "candidate", "allowed": False})

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
        }
        if not self.state.replace(record_id, "pending", bound):
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
        self, record_id: str, action: str, record: dict[str, Any], service: Any,
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
        reference = f"{skill_id}@v{version}" if isinstance(version, int) else skill_id
        plan = service.install_plan(reference)
        if plan.get("operation") != "install":
            raise WisdomValidationError("native install review is invalid")
        result = {
            **record, "operation": "install", "plan": copy.deepcopy(plan),
            "updated_at": self.clock(),
        }
        if not self.state.replace(record_id, "pending", result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)

    def _navigation_action(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any] | None:
        if action == "settings":
            return self._open_settings(record, service, invocation, authority)
        if (action in {"details", "review"} and record["operation"] in {"install", "update"}
                and record["plan"].get("files")):
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
            "plan": {**plan, "operation": "settings", "allowed": False},
        }
        if not self.state.compare_replace(record, result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return result

    @contextmanager
    def displaying(self, record: dict[str, Any]):
        """Fence approval until the provider acknowledges the complete page edit."""
        if record.get("state") != "pending":
            yield
            return
        try:
            yield
        except Exception:
            self.state.compare_replace(record, {**record, "state": "failed"})
            raise
        self.state.compare_replace(record, {**record, "display_ready": True})

    def _navigate(
        self, record: dict[str, Any], action: str, *, package_files: Any = None,
    ) -> dict[str, Any]:
        result = {**copy.deepcopy(record), "display_ready": False}
        if package_files is not None:
            result["package_files"] = copy.deepcopy(package_files)
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
                or not 0 <= page < len(inspection_pages(record))
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
        if not self.state.replace(record["id"], "pending", claimed):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        try:
            draft_id = service.native_candidate_prepare(record["plan"]["event_id"])
        except SharePackagingError:
            result = {**claimed, "state": "failed", "failure": "share_packaging_failed"}
            if not self.state.compare_replace(claimed, result):
                raise WisdomConflict("Collective Wisdom review changed; open it again") from None
            return result
        plan = service.publication_review(draft_id)
        if not authority() or not _owner_matches(record, service, invocation):
            raise WisdomAuthError("Collective Wisdom review owner changed")
        result = {
            **record, "operation": "publish", "plan": copy.deepcopy(plan),
            "updated_at": self.clock(),
        }
        if not self.state.replace(record["id"], "preparing", result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return result

    def act(
        self, record_id: str, action: str, service: Any,
        invocation: NativeInvocation, *, authority: Callable[[], bool] = lambda: True,
    ) -> dict[str, Any]:
        record = self.load(record_id)
        state = str(record.get("state") or "")
        if state not in {"pending", "applying"} or not _owner_matches(
            record, service, invocation
        ):
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        if action == "cancel":
            result = {**record, "state": "cancelled", "updated_at": self.clock()}
            if not self.state.replace(record_id, state, result):
                raise WisdomConflict("Collective Wisdom review changed; open it again")
            return copy.deepcopy(result)
        if record.get("display_ready") is False:
            raise WisdomConflict("Wait for the exact review to finish displaying")
        if state == "pending" and self.clock() >= record["expires_at"]:
            raise WisdomAuthError("Collective Wisdom review expired; open it again")
        if action.startswith("catalog.install.") and record.get("operation") == "catalog":
            return self._select_catalog(record_id, action, record, service)
        if state == "pending":
            navigated = self._navigation_action(record, action, service, invocation, authority)
            if navigated is not None:
                return navigated
        _validate_review_action(record, action)
        if state != "pending":
            raise WisdomAuthError("Collective Wisdom review is no longer current")

        return self._apply(record, action, service, invocation, authority)

    def _apply(
        self, record: dict[str, Any], action: str, service: Any,
        invocation: NativeInvocation, authority: Callable[[], bool],
    ) -> dict[str, Any]:
        record_id = record["id"]
        claimed = {**record, "state": "applying", "updated_at": self.clock()}
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
        except Exception:  # pylint: disable=broad-exception-caught
            uncertain = {
                **claimed, "state": "uncertain", "result": {"outcome": "unknown"},
                "updated_at": self.clock(),
            }
            self.state.replace(record_id, "applying", uncertain)
            raise
        result = {
            **claimed, "state": "completed", "result": outcome,
            "updated_at": self.clock(),
        }
        if not self.state.replace(record_id, "applying", result):
            raise WisdomConflict("Collective Wisdom review changed; open it again")
        return copy.deepcopy(result)


__all__ = [
    "NativeAction", "NativeItem", "NativeReviewController", "NativeView",
    "assessment_view", "callback_value", "parse_callback", "project_view",
    "slack_section_text", "telegram_view_text", "_approval_is_lossless",
]
