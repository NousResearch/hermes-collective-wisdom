"""Deterministic native views and PluginState-backed explicit reviews."""
# Self-explanatory immutable view records and narrow adapter methods do not gain clarity
# from repeating their signatures in docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,line-too-long

from __future__ import annotations

import copy
import secrets

import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from .native_context import NativeInvocation
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
        for key, child in value.items():
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
        "Collective Wisdom settings", "Notification settings", [item], actions=actions,
    )


def project_view(record: dict[str, Any]) -> NativeView:
    raw_plan = record.get("plan")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    state = str(record.get("state") or "pending")
    if record.get("operation") == "catalog":
        return _catalog_view(plan, state)
    if record.get("operation") == "settings":
        return _settings_view(plan, state)
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

    def _insert(self, service: Any, invocation: NativeInvocation, operation: str, plan: dict[str, Any]) -> dict[str, Any]:
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
            "result": None, "created_at": self.clock(), "updated_at": self.clock(),
            "expires_at": self.clock() + self.ttl_seconds,
        }
        self.state.save(record)
        return copy.deepcopy(record)

    def request_install(self, service: Any, invocation: NativeInvocation, reference: str, *, update_mode: str | None = None) -> dict[str, Any]:
        plan = service.install_plan(reference, update_mode=update_mode)
        if plan.get("operation") != "install":
            raise WisdomValidationError("native install review is invalid")
        return self._insert(service, invocation, "install", plan)

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
        if state == "pending" and self.clock() >= record["expires_at"]:
            raise WisdomAuthError("Collective Wisdom review expired; open it again")
        if action.startswith("catalog.install.") and record.get("operation") == "catalog":
            return self._select_catalog(record_id, action, record, service)
        _validate_review_action(record, action)
        if state != "pending":
            raise WisdomAuthError("Collective Wisdom review is no longer current")

        claimed = {**record, "state": "applying", "updated_at": self.clock()}
        if not self.state.replace(record_id, "pending", claimed):
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
