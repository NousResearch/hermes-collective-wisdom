"""Reference-shaped native consent, presentation, and durable setup handoff state.

Contributor-facing copy, compact sharing-check presentation, contextual
organization labels, and post-deferral dismissal are adapted from `hbizi`'s
follow-up in shannonsands/hermes-agent#5 (d68368b7, ea40400b, 872759a9), under
this repository's MIT license. The plugin retains its own review records,
callbacks, and host SDK seams.
"""

# Existing controller methods are intentionally narrow state-machine primitives.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-lines

from __future__ import annotations

import json
import secrets
import shlex
import time
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .native_context import NativeInvocation
from .package import PackagePolicyError
from .portal_urls import portal_version_url as _version_url
from .portal_urls import safe_portal_url as _safe_url
from .professionalism import CHECK_KEYS, CHECK_LABELS
from .professionalism import review_text as professionalism_review_text

CONSENT_SECONDS = 24 * 60 * 60
SLACK_SECTION_LIMIT = 2900
TELEGRAM_TEXT_LIMIT = 3500
TERMINAL = {"completed", "failed", "stale", "expired", "needs_review", "superseded"}
_PUBLICATION_TERMINAL = {
    "published", "rejected", "changes_requested", "declined", "invalidated",
}
_CONSENT_INSERT = (
    "INSERT INTO wisdom_consent("
    "id,organization_id,owner_session,actor_id,platform,profile,operation,state,"
    "plan_json,result_json,expires_at,delivery_state,delivery_error,request_message_id,"
    "message_id,checks_expanded,inspection_json,created_at,updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


@dataclass(frozen=True, slots=True)
class NativeAction:
    label: str
    action: str | None = None
    primary: bool = False
    url: str | None = None


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


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=True)


def _safe_handoff_identifier(
    value: object, placeholder: str = "<draft-id>",
) -> str:
    text = str(value or "")
    if not text or any(
        unicodedata.category(character).startswith("C") for character in text
    ):
        return placeholder
    return shlex.quote(text)


def _safe_display_identifier(value: object, placeholder: str = "Draft") -> str:
    text = str(value or "")
    if not text or any(
        unicodedata.category(character).startswith("C") for character in text
    ):
        return placeholder
    return text


def slack_section_text(value: object) -> str:
    """Apply the exact Slack mrkdwn escaping and section bound."""
    escaped = (
        str(value or "").replace("&", "&amp;")
        .replace("<", "&lt;").replace(">", "&gt;")
    ).strip()
    return (
        escaped if len(escaped) <= SLACK_SECTION_LIMIT
        else escaped[: SLACK_SECTION_LIMIT - 1].rstrip() + "…"
    )


def telegram_view_text(view: NativeView) -> str:
    """Apply the exact Telegram plain-text bound."""
    value = view.to_text()
    return value if len(value) <= TELEGRAM_TEXT_LIMIT else value[:3499].rstrip() + "…"


def _decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["plan"] = json.loads(value.pop("plan_json"))
    value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
    value["inspection"] = (
        json.loads(value.pop("inspection_json")) if value.get("inspection_json") else None
    )
    value["checks_expanded"] = bool(value["checks_expanded"])
    value["assessment_expanded"] = bool(value["assessment_expanded"])
    if isinstance(value["result"], dict) and isinstance(
        value["result"].get("setup_continuation"), dict
    ):
        value["setup_continuation"] = value["result"]["setup_continuation"]
    return value


_STATUS_PRESENTATION = {
    "pass": ("✅", "Pass"),
    "advisory": ("⚠️", "Advisory"),
    "blocked": ("❌", "Blocked"),
    "pending": ("⏳", "Pending"),
    "retry": ("⏳", "Pending"),
    "running": ("⏳", "Pending"),
    "unavailable": ("➖", "Unavailable"),
}

def _check_line(label: str, status: object) -> str:
    icon, state = _STATUS_PRESENTATION.get(
        str(status or "unavailable").lower(), _STATUS_PRESENTATION["unavailable"]
    )
    if state == "Pass":
        label = {
            "Security check": "No security issues detected",
            "Security check (local preflight)": "No security issues detected",
            "Private keys": "No private keys detected",
            "Live credentials": "No live credentials detected",
            "Secret-like assignments": "No secret key assignments",
            "Harmful instruction patterns": "No harmful instruction patterns",
        }.get(label, label)
    return f"{icon} {label}" + (f": {state}" if state != "Pass" else "")


def _summary_text(summary: str) -> str:
    if summary.strip() == "No known matches detected.":
        return "No issues detected"
    local = "Local security checks found no known matches."
    if summary.startswith(local):
        summary = "No issues detected by local security checks." + summary[len(local):]
    return summary[:512]


def _checklist_text(
    title: str,
    check: dict[str, Any] | None,
    *,
    labels: dict[str, str],
    note: str | None = None,
    include_checks: bool = True,
) -> str:
    value = check or {}
    lines = [_check_line(title, value.get("status"))]
    summary = value.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(_summary_text(summary))
    rows = value.get("checks") if include_checks else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            label = str(
                row.get("label") or labels.get(key) or key.replace("_", " ").title()
            )
            count = int(row.get("finding_count") or 0)
            suffix = f" ({count} finding{'s' if count != 1 else ''})" if count else ""
            lines.append(_check_line(label, row.get("status")) + suffix)
            lines.extend(f"  {str(detail)[:256]}" for detail in row.get("details") or [])
    if note:
        lines.append(note)
    return "\n".join(lines)


def _professionalism_text(
    check: dict[str, Any] | None, *, include_checks: bool = True
) -> str:
    value = check or {}
    status = str(value.get("status") or "unavailable")
    if status in {"pass", "advisory"}:
        return professionalism_review_text(value, include_checks=include_checks)
    return _checklist_text(
        "Professionalism check (agent-assessed, advisory)",
        value,
        labels=CHECK_LABELS,
        include_checks=include_checks,
    )


def _security_check(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Adapt plugin scan fields to the pinned renderer's public fact shape."""
    check = plan.get("security_check")
    if isinstance(check, dict):
        return check
    local = plan.get("local_scan")
    server = plan.get("server_checks")
    if not isinstance(local, dict) and not isinstance(server, dict):
        return None
    local = local if isinstance(local, dict) else {}
    server = server if isinstance(server, dict) else {}
    local_status = str(local.get("status") or "")
    if not local_status:
        local_status = (
            "pass" if local.get("allowed") is True
            else "blocked" if local.get("allowed") is False
            else "unavailable"
        )
    statuses = [local_status]
    if server:
        statuses.append(str(server.get("status") or "unavailable"))
    if "blocked" in statuses:
        status = "blocked"
    elif "advisory" in statuses:
        status = "advisory"
    elif statuses and all(value == "pass" for value in statuses):
        status = "pass"
    elif any(value in {"pending", "retry", "running"} for value in statuses):
        status = "pending"
    else:
        status = "unavailable"
    return {
        "status": status,
        "summary": local.get("summary"),
        "source": "local_preflight",
        "local_status": local_status,
        "checks": [
            {"label": "Local security check", "status": local_status},
            *(
                [{"label": "Gateway check", "status": server.get("status")}]
                if server else []
            ),
        ],
    }


def _review_text(plan: dict[str, Any], expanded: bool, *, current: bool = False) -> str:
    security = _security_check(plan) or {}
    saved_professionalism = plan.get("professionalism_check")
    professionalism = saved_professionalism if isinstance(saved_professionalism, dict) else {}
    if current and not expanded and _complete_checks_passed(security, professionalism):
        return "✅ All sharing checks passed"
    if expanded:
        return "\n\n".join((
            _checklist_text(
                "Security check",
                security,
                labels={},
                note="These checks are not a security certification.",
            ),
            _professionalism_text(professionalism),
        ))
    local = security.get("source") == "local_preflight"
    lines = [_check_line(
        "Security check (local preflight)" if local else "Security check",
        security.get("local_status") if local else security.get("status"),
    )]
    if security.get("summary"):
        lines.append(_summary_text(str(security["summary"])))
    lines.append(_professionalism_text(professionalism, include_checks=False))
    for check in (security, professionalism):
        for row in check.get("checks") or []:
            if not isinstance(row, dict):
                continue
            if row.get("status") == "pass" and not row.get("finding_count"):
                continue
            label = str(row.get("label") or row.get("key") or "Check")
            lines.append(_check_line(label, row.get("status")))
            lines.extend(str(detail)[:256] for detail in row.get("details") or [])
    return "\n".join(lines)


def _complete_checks_passed(
    security: dict[str, Any], professionalism: dict[str, Any],
) -> bool:
    if security.get("status") != "pass" or professionalism.get("status") != "pass":
        return False
    local = security.get("source") == "local_preflight"
    if (local and security.get("local_status") != "pass") or (
        security.get("upload_allowed") is False
    ):
        return False
    required_security = (
        {"private_keys", "live_credentials", "secret_assignments", "skills_guard"}
        if local else
        {"private_keys", "live_credentials", "organization_policy",
         "personal_information", "secret_like_assignments"}
    )
    for check, required in ((security, required_security), (professionalism, set(CHECK_KEYS))):
        rows = check.get("checks")
        if not isinstance(rows, list) or not rows:
            return False
        if any(not isinstance(row, dict) or row.get("status") != "pass"
               or row.get("finding_count") != 0 or not isinstance(row.get("key"), str)
               for row in rows):
            return False
        keys = [row["key"] for row in rows]
        if len(set(keys)) != len(keys) or not required.issubset(keys):
            return False
    return True


def _assessment_text(value: object, limit: int) -> str:
    """Return bounded native advice text without terminal/bidi controls."""

    text = str(value or "").strip()
    if not text or len(text) > limit or any(
        (ord(character) < 32 and character not in "\n\t")
        or 127 <= ord(character) < 160
        or character in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for character in text
    ):
        raise ValueError("assessment advice is not presentation safe")
    return text


def assessment_view(job: dict[str, Any]) -> NativeView:
    """Project immutable ready advice without creating approval controls."""

    advice = job.get("advice")
    reference = job.get("reference")
    if not isinstance(reference, dict):
        raise ValueError("ready assessment reference is missing")
    kind = reference.get("kind")
    fallback = advice is None and job.get("state") == "fallback"
    if fallback:
        identity = _assessment_text(
            reference.get("local_skill_id" if kind == "candidate" else "skill_id"), 256,
        )
        advice = {
            "relevance": "digest",
            "title": identity,
            "explanation": (
                "Hermes could not finish the local qualification review. Review it "
                "manually; nothing has been shared."
                if kind == "candidate" else
                "Hermes could not assess this skill's relevance. Review its exact "
                "published details manually; nothing has been changed."
            ),
            "provenance": {"kind": (
                "local_qualification" if kind == "candidate" else "agent_assessed"
            )},
        }
    if not isinstance(advice, dict):
        raise ValueError("ready assessment advice is missing")
    title = _assessment_text(advice.get("title"), 120)
    explanation = _assessment_text(advice.get("explanation"), 600)
    if str(advice.get("relevance") or "") not in {"recommend", "digest"}:
        raise ValueError("assessment advice relevance is invalid")
    provenance = advice.get("provenance")
    provenance_kind = str(
        provenance.get("kind") if isinstance(provenance, dict) else ""
    )
    if kind == "candidate":
        if provenance_kind != "local_qualification":
            raise ValueError("candidate advice provenance is invalid")
        _assessment_text(reference.get("local_skill_id"), 256)
        event_id = _assessment_text(reference.get("event_id"), 128)
        summary = "Local qualification review unavailable" if fallback else ""
        preamble = (
            "" if fallback else
            "Hermes thinks the following skill would be useful to the rest of your team:"
        )
        handoff = (
            "Ask Hermes to inspect this exact local candidate with wisdom_inspect "
            f"(kind=candidate, identity={event_id})"
        )
    elif kind == "skill":
        if provenance_kind != "agent_assessed":
            raise ValueError("shared advice provenance is invalid")
        identity = _assessment_text(reference.get("skill_id"), 256)
        version = reference.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("shared advice version is invalid")
        summary = "Agent assessment unavailable" if fallback else "Agent-assessed advice"
        preamble = ""
        handoff = (
            "Ask Hermes to inspect the exact published version with wisdom_inspect "
            f"(kind=skill, identity={identity}, version={version}); /wisdom show "
            f"{shlex.quote(identity)} shows the current catalog version"
        )
    else:
        raise ValueError("assessment reference is unsupported")
    return NativeView(
        title="Hermes Collective Wisdom",
        summary=summary,
        items=[NativeItem(title=title, detail=explanation, preamble=preamble)],
        notice=(
            f"{handoff}. This is advisory only; nothing was installed, approved, "
            "or shared."
        ),
        actions=(
            [NativeAction(label="Maybe Later", action="candidate_defer")]
            if kind == "candidate" and not fallback else []
        ),
    )


class NativeReviewController:  # pylint: disable=too-many-public-methods
    """Consent authority in dedicated SQLite tables; no metadata index."""

    def __init__(self, store: Any, *, clock: Callable[[], float] = time.time,
                 ttl_seconds: float = CONSENT_SECONDS) -> None:
        self.store = store
        self.clock = clock
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _org(owner: dict[str, Any]) -> str:
        org = str(owner.get("organization_id") or "")
        if not org:
            raise PermissionError("Collective Wisdom enrolment is unavailable.")
        return org

    def request(self, service: Any, invocation: NativeInvocation,
                skill_id: str) -> dict[str, Any]:
        owner = service.native_owner()
        org = self._org(owner)
        plan = service.update_plan(skill_id)
        if plan.get("operation") != "update" or plan.get("skill_id") != skill_id:
            raise PackagePolicyError("native update review is invalid")
        if plan.get("owner") != owner:
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        plan = self._prepare_review_deferral(service, plan)
        now = self.clock()
        identity = secrets.token_urlsafe(12)
        with self.store.transaction() as db:
            self._record_session_db(db, org, invocation, now)
            db.execute(
                "UPDATE wisdom_consent SET state='superseded',"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE organization_id=? AND owner_session=? AND actor_id=? AND platform=? "
                "AND operation='update' AND state IN ('pending','deferred','expired')",
                (now, org, invocation.session_key, invocation.actor_id, invocation.platform),
            )
            db.execute(
                _CONSENT_INSERT,
                (identity, org, invocation.session_key, invocation.actor_id,
                 invocation.platform, invocation.profile, "update", "pending", _json(plan),
                 None, now + self.ttl_seconds, "unsent", None, invocation.message_id,
                 None, 0, None, now, now),
            )
        return self.load(identity)

    def request_install(
        self, service: Any, invocation: NativeInvocation, reference: str, *,
        update_mode: str | None = None,
    ) -> dict[str, Any]:
        """Create one exact install review on the admitted private route."""
        owner = service.native_owner()
        plan = service.install_plan(reference, update_mode=update_mode)
        if plan.get("operation") != "install" or plan.get("owner") != owner:
            raise PackagePolicyError("native install review is invalid")
        plan = self._prepare_review_deferral(service, plan)
        return self._insert_review(service, invocation, "install", plan)

    @staticmethod
    def _prepare_review_deferral(  # pylint: disable=broad-exception-caught
        service: Any, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Attach a verified cooldown snapshot, or explicitly omit Not Now.

        Hosts without the complete shared service seam cannot verify policy and
        therefore cannot offer a misleading local-only Not Now action.
        """

        prepare = getattr(service, "prepare_review_deferral", None)
        defer = getattr(service, "defer_review", None)
        if not callable(prepare) or not callable(defer):
            return {**plan, "review_deferral_unavailable": True}
        try:
            deferral = prepare(plan)
        except Exception:  # policy/owner authority unavailable: omit the action
            return {**plan, "review_deferral_unavailable": True}
        if not isinstance(deferral, dict):
            return {**plan, "review_deferral_unavailable": True}
        return {**plan, "review_deferral": deferral}

    def request_candidate(
        self, service: Any, invocation: NativeInvocation, event_id: str,
    ) -> dict[str, Any]:
        """Prepare locally, retaining a typed recoverable failure without publication consent."""
        from .errors import SharePackagingError  # pylint: disable=import-outside-toplevel

        owner = service.native_owner()
        try:
            draft_id = service.native_candidate_prepare(event_id)
        except SharePackagingError:
            return self._insert_review(service, invocation, "packaging_failed", {
                "owner": owner, "event_id": event_id, "allowed": False,
                "error_code": "share_packaging_failed",
            })
        if service.native_owner() != owner:
            raise PermissionError("Collective Wisdom candidate owner changed.")
        return self.request_publication(service, invocation, draft_id)

    def request_recommendation(
        self, service: Any, invocation: NativeInvocation, job: dict[str, Any],
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind advice navigation, never approval, to this private owner route."""
        assessment_view(job)  # Validate the immutable advisory payload before binding controls.
        reference = job["reference"]
        if reference.get("kind") != "skill" or job.get("state") != "ready":
            raise ValueError("Only ready shared advice has recipient controls.")
        owner = service.native_owner()
        version = facts.get("version") or {}
        if (facts.get("skill", {}).get("id") != reference["skill_id"]
                or version.get("version") != reference["version"]):
            raise ValueError("Recommendation version does not match its source.")
        plan = self._prepare_review_deferral(service, {
            "owner": owner, "operation": "install", "skill_id": reference["skill_id"],
            "version": reference["version"], "job": job, "facts": facts,
            "security_check": version.get("security_check"),
            "professionalism_check": version.get("professionalism_check"),
            "allowed": True,
        })
        organization = service.store.organization_display_name(owner["organization_id"])
        name = (organization or {}).get("display_name")
        if ((organization or {}).get("resolved") and isinstance(name, str)
                and name.strip() and not any(
                    unicodedata.category(char).startswith("C") for char in name
                )):
            plan["organization_display_name"] = name.strip()[:160]
        return self._insert_review(service, invocation, "recommendation", plan)

    def request_install_modes(
        self, service: Any, invocation: NativeInvocation, reference: str,
    ) -> dict[str, Any]:
        """Present install policy choices before exact package planning."""
        owner = service.native_owner()
        return self._insert_review(
            service, invocation, "install_modes",
            {"reference": reference, "owner": owner, "allowed": True},
        )

    def request_catalog(
        self, service: Any, invocation: NativeInvocation, command: str, target: str = "",
    ) -> dict[str, Any]:
        """Read a bounded browse/show/versions surface into one durable native card."""
        owner = service.native_owner()
        kind = command.casefold()
        if kind == "browse":
            plan = {
                "kind": kind, "query": target,
                "data": service.search_skills(target or None), "page": 0,
            }
        elif kind in {"show", "versions"}:
            detail = service.resolve_skill(target)
            skill = detail.get("skill") or {}
            skill_id = str(skill.get("id") or target)
            plan = {
                "kind": kind, "skill_id": skill_id,
                "slug": str(skill.get("slug") or target),
                "data": service.show(skill_id) if kind == "show" else service.versions(skill_id),
                "portal_url": _safe_url(detail.get("portal_url")),
                "page": 0,
            }
        else:
            raise ValueError("Collective Wisdom catalog command was not recognized.")
        return self._insert_review(
            service, invocation, "catalog", {**plan, "owner": owner, "allowed": True}
        )

    def request_publication(
        self, service: Any, invocation: NativeInvocation, draft_id: str,
    ) -> dict[str, Any]:
        """Create one exact publication review without uploading any bytes."""
        owner = service.native_owner()
        plan = service.publication_review(draft_id)
        if plan.get("draft_id") != draft_id or not plan.get("receipt"):
            raise PackagePolicyError("native publication review is invalid")
        plan = self._prepare_review_deferral(
            service, {**plan, "owner": owner, "allowed": True}
        )
        return self._insert_review(service, invocation, "publish", plan)

    def request_setup(
        self, service: Any, invocation: NativeInvocation,
        subject: tuple[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Prepare one exact installed setup step; execution remains native-only."""
        skill_id, details = subject
        version = details["version"]
        step = details["step"]
        phase = str(step.get("phase") or "")
        index = int(step.get("index") or 0)
        command = str(step.get("command") or "")
        plan = service.installed_setup_plan(
            skill_id, version=version, phase=phase, index=index, command=command
        )
        return self._insert_review(
            service, invocation, "setup",
            {
                **plan,
                "title": str(details.get("title") or ""),
                "explanation": str(details.get("explanation") or ""),
            },
        )

    def request_settings(
        self, service: Any, invocation: NativeInvocation,
    ) -> dict[str, Any]:
        """Bind one private card to current local mode and shared mute authority."""
        from .preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

        owner = service.native_owner()
        org = self._org(owner)
        mode = service.notification_mode()
        preferences = WisdomPreferences(service)
        local = preferences.mute_status(org)
        try:
            control = preferences.prepare_mute_control(org)
            gateway = "available"
            gateway_mute = control.get("mute")
        except Exception:  # pylint: disable=broad-exception-caught
            control = None
            gateway = "unavailable"
            gateway_mute = None
        if service.native_owner() != owner:
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        return self._insert_review(
            service, invocation, "settings", {
                "owner": owner, "allowed": True, "delivery_mode": mode,
                "mode_writable": service.notification_mode_writable(),
                "gateway_status": gateway, "mute_control": control,
                "gateway_mute": gateway_mute, "mute_sync": local,
            },
        )

    def _insert_review(
        self, service: Any, invocation: NativeInvocation, operation: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        owner = service.native_owner()
        org = self._org(owner)
        if plan.get("owner") != owner:
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        now = self.clock()
        identity = secrets.token_urlsafe(12)
        with self.store.transaction() as db:
            self._record_session_db(db, org, invocation, now)
            if operation != "recommendation":
                db.execute(
                    "UPDATE wisdom_consent SET state='superseded',"
                    "state_revision=state_revision+1,updated_at=? "
                    "WHERE organization_id=? AND owner_session=? AND actor_id=? AND platform=? "
                    "AND operation=? AND state IN ('pending','deferred','expired')",
                    (now, org, invocation.session_key, invocation.actor_id,
                     invocation.platform, operation),
                )
            db.execute(
                _CONSENT_INSERT,
                (identity, org, invocation.session_key, invocation.actor_id,
                 invocation.platform, invocation.profile, operation, "pending", _json(plan),
                 None, now + self.ttl_seconds, "unsent", None, invocation.message_id,
                 None, 0, None, now, now),
            )
        return self.load(identity)

    def load(self, record_id: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            value = _decode(db.execute(
                "SELECT * FROM wisdom_consent WHERE id=?", (record_id,)
            ).fetchone())
        if value is None:
            raise PermissionError("Collective Wisdom interaction was not found.")
        return value

    def pending(
        self, owner: dict[str, Any], *, invocation: NativeInvocation | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Project unresolved native reviews without granting or refreshing authority."""
        org = self._org(owner)
        params: list[Any] = [org, owner["profile_name"]]
        query = (
            "SELECT c.*,s.address_json FROM wisdom_consent c "
            "LEFT JOIN wisdom_agent_session s ON s.organization_id=c.organization_id "
            "AND s.session_key=c.owner_session WHERE c.organization_id=? AND c.profile=? "
            "AND c.state NOT IN ('completed','failed','superseded')"
        )
        for key in (
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        ):
            query += f" AND json_extract(c.plan_json,'$.owner.{key}')=?"
            params.append(owner[key])
        if invocation is not None:
            query += (
                " AND c.owner_session=? AND c.actor_id=? AND c.platform=?"
                " AND s.actor_id=? AND s.platform=? AND s.profile=?"
            )
            params.extend((
                invocation.session_key, invocation.actor_id, invocation.platform,
                invocation.actor_id, invocation.platform, invocation.profile,
            ))
        query += " ORDER BY c.created_at DESC,c.id DESC LIMIT ?"
        params.append(max(0, min(int(limit), 100)))
        with self.store.transaction() as db:
            rows = db.execute(query, params).fetchall()
        expected_address = self._address(invocation) if invocation is not None else None
        result = []
        for row in rows:
            value = dict(row)
            if expected_address is not None and (
                not value.get("address_json")
                or json.loads(value["address_json"]) != expected_address
            ):
                continue
            state = str(value["state"])
            if state in {"pending", "deferred"} and self.clock() > value["expires_at"]:
                state = "expired"
            result.append({
                "id": str(value["id"]), "operation": str(value["operation"]),
                "state": state, "delivery_state": str(value["delivery_state"]),
                "expires_at": value["expires_at"],
            })
        return result

    def delivery_projection(self, record_id: str) -> tuple[dict[str, Any], int, int]:
        """Claim one delivery attempt and return the exact state snapshot it projects."""
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM wisdom_consent WHERE id=?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise PermissionError("Collective Wisdom interaction was not found.")
            revision = int(row["delivery_revision"])
            attempt = int(row["delivery_attempt"]) + 1
            changed = db.execute(
                "UPDATE wisdom_consent SET delivery_attempt=?,updated_at=? "
                "WHERE id=? AND delivery_revision=? AND delivery_attempt=?",
                (attempt, self.clock(), record_id, revision, attempt - 1),
            )
            if changed.rowcount != 1:
                raise PermissionError("Collective Wisdom delivery state changed.")
            projected = _decode(db.execute(
                "SELECT * FROM wisdom_consent WHERE id=?", (record_id,)
            ).fetchone())
        if projected is None:
            raise PermissionError("Collective Wisdom interaction was not found.")
        return projected, revision, attempt

    def delivery_token(self, record_id: str) -> tuple[int, int]:
        _record, revision, attempt = self.delivery_projection(record_id)
        return revision, attempt

    def bind_delivery(
        self, record_id: str, invocation: NativeInvocation, message_id: str, *,
        expected_revision: int | None = None, attempt: int | None = None,
    ) -> dict[str, Any]:
        if not message_id:
            raise ValueError("native Wisdom delivery acknowledgement has no message id")
        record = self.load(record_id)
        self._validate_route(record, invocation)
        if record["delivery_state"] not in {"unsent", "confirmed"}:
            raise PermissionError("Collective Wisdom review delivery is not confirmed.")
        if record.get("message_id") not in {None, message_id}:
            raise PermissionError("Collective Wisdom review is bound to another message.")
        revision = int(record["delivery_revision"])
        expected_revision = revision if expected_revision is None else expected_revision
        attempt = int(record["delivery_attempt"]) if attempt is None else attempt
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET message_id=?,delivery_state='confirmed',"
                "delivery_error=NULL,delivery_revision=delivery_revision+1,updated_at=? "
                "WHERE id=? AND delivery_revision=? AND delivery_attempt=?",
                (message_id, self.clock(), record_id, expected_revision, attempt),
            )
        latest = self.load(record_id)
        if changed.rowcount == 1 or (
            latest["delivery_state"] == "confirmed"
            and latest.get("message_id") == message_id
        ):
            return latest
        raise PermissionError("Collective Wisdom delivery state changed.")

    def delivery_confirmed(self, record_id: str, *, expected_revision: int,
                           attempt: int) -> dict[str, Any]:
        return self._delivery(
            record_id, "confirmed", None,
            expected_revision=expected_revision, attempt=attempt,
        )

    def delivery_uncertain(
        self, record_id: str, reason: str, *, expected_revision: int | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        return self._delivery(
            record_id, "delivery_uncertain", reason,
            expected_revision=expected_revision, attempt=attempt,
        )

    def delivery_failed(
        self, record_id: str, reason: str, *, expected_revision: int | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        return self._delivery(
            record_id, "edit_failed", reason,
            expected_revision=expected_revision, attempt=attempt,
        )

    def _delivery(
        self, record_id: str, state: str, reason: str | None, *,
        expected_revision: int | None, attempt: int | None,
    ) -> dict[str, Any]:
        record = self.load(record_id)
        expected_revision = (
            int(record["delivery_revision"])
            if expected_revision is None else expected_revision
        )
        attempt = int(record["delivery_attempt"]) if attempt is None else attempt
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET delivery_state=?,delivery_error=?,"
                "delivery_revision=delivery_revision+1,updated_at=? "
                "WHERE id=? AND delivery_revision=? AND delivery_attempt=?",
                (state, str(reason)[:300] if reason is not None else None,
                 self.clock(), record_id, expected_revision, attempt),
            )
        latest = self.load(record_id)
        expected_error = str(reason)[:300] if reason is not None else None
        if changed.rowcount == 1 or (
            latest["delivery_state"] == state
            and latest.get("delivery_error") == expected_error
        ):
            return latest
        raise PermissionError("Collective Wisdom delivery state changed.")

    def act(  # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals,too-many-statements
        self, service: Any, record_id: str, invocation: NativeInvocation,
        action: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        effect_current = may_apply or (lambda: True)
        record = self.load(record_id)
        self._validate(
            record, service, invocation,
            recovery=(
                action in {"recheck", "recover", "setup.recover", "setup.clear"}
                or action.startswith(("checks.", "assessment."))
            ),
        )
        if action == "inspect" and record["operation"] == "setup":
            return self._setup_progress(
                service, record, effect_current=effect_current
            )
        if action == "defer":
            if record["plan"].get("review_deferral_unavailable") is True:
                raise PermissionError("Collective Wisdom deferral is unavailable.")
            if isinstance(record["plan"].get("review_deferral"), dict):
                return self._defer_review(
                    service, record, invocation, effect_current=effect_current
                )
            if record["state"] == "pending":
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE wisdom_consent SET state='deferred',result_json=?,"
                        "state_revision=state_revision+1,updated_at=? "
                        "WHERE id=? AND state='pending'",
                        (_json({"deferred": True}), self.clock(), record_id),
                    )
            return self.load(record_id)
        if action in {"checks.show", "checks.hide"}:
            with self.store.transaction() as db:
                db.execute("UPDATE wisdom_consent SET checks_expanded=?,updated_at=? WHERE id=?",
                           (1 if action.endswith("show") else 0, self.clock(), record_id))
            return self.load(record_id)
        if action in {"assessment.show", "assessment.hide"}:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE wisdom_consent SET assessment_expanded=?,updated_at=? WHERE id=?",
                    (1 if action.endswith("show") else 0, self.clock(), record_id),
                )
            return self.load(record_id)
        if action in {"ack.preserve", "ack.sensitive", "ack.partial"}:
            return self._acknowledge_update(service, record, action)
        if action == "recheck" and record["operation"] == "update":
            expected = record["state"]
            if expected not in {"stale", "expired"}:
                raise PermissionError("Collective Wisdom review is still current.")
            return self._replace_update(
                service, record, expected_state=expected,
                prior_state=expected,
            )
        page_action = action.partition(".")[2] if action.startswith(
            ("inspect.", "review.")
        ) else "0" if action == "inspect" else None
        if action == "back" and record["operation"] in {"publish", "install", "update"}:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE wisdom_consent SET inspection_json=NULL,updated_at=? WHERE id=?",
                    (self.clock(), record_id),
                )
            return self.load(record_id)
        if page_action is not None and record["operation"] == "publish":
            return self._inspect_publication(record, page_action)
        if page_action is not None and record["operation"] in {"install", "update"}:
            try:
                page = int(page_action)
            except ValueError as exc:
                raise ValueError("Collective Wisdom control was not recognized.") from exc
            inspect = (
                service.native_install_inspect
                if record["operation"] == "install" else service.native_update_inspect
            )
            files = inspect(record["plan"]["receipt"])
            if page < 0 or page >= len(files):
                raise ValueError("Collective Wisdom review page was not found.")
            inspection = {"page": page, "page_count": len(files), **files[page]}
            with self.store.transaction() as db:
                db.execute("UPDATE wisdom_consent SET inspection_json=?,updated_at=? WHERE id=?",
                           (_json(inspection), self.clock(), record_id))
            return self.load(record_id)
        if action == "recheck" and record["operation"] == "setup":
            return self._recheck_setup(service, record)
        if action == "packaging.retry":
            if record["operation"] != "packaging_failed" or record["state"] != "pending":
                raise PermissionError("Packaging retry is no longer current.")
            if not self._authority_current(service, record, effect_current):
                raise PermissionError("Packaging owner changed.")
            successor = self.request_candidate(service, invocation, record["plan"]["event_id"])
            self._validate_route(record, invocation)
            if service.native_owner() != record["plan"]["owner"] or not effect_current():
                raise PermissionError("Packaging owner changed.")
            self.bind_delivery(successor["id"], invocation, invocation.message_id)
            if self.load(record_id)["state"] == "pending":
                self._set_state(record, "superseded")
            return self.load(successor["id"])
        if action in {"recommendation.settings", "sharing.settings"}:
            expected_operation = (
                "recommendation" if action.startswith("recommendation.") else "publish"
            )
            if record["operation"] != expected_operation or record["state"] != "pending":
                raise PermissionError("Recommendation is no longer current.")
            settings = self.request_settings(service, invocation)
            if not self._authority_current(service, record, effect_current):
                raise PermissionError("Recommendation owner changed.")
            self.bind_delivery(settings["id"], invocation, invocation.message_id)
            self._set_state(record, "superseded")
            return self.load(settings["id"])
        if action in {"recommendation.details", "recommendation.back"}:
            if record["operation"] != "recommendation" or record["state"] != "pending":
                raise PermissionError("Recommendation is no longer current.")
            return self._replace_catalog(record, {
                **record["plan"], "details_expanded": action.endswith(".details"),
            }, operation="recommendation")
        if action == "recommendation.install":
            if (record["operation"] != "recommendation" or record["state"] != "pending"
                    or not _recommendation_installable(record["plan"])):
                raise PermissionError("Recommendation is no longer current.")
            return self._replace_catalog(record, {
                "reference": f"{record['plan']['skill_id']}@v{record['plan']['version']}",
                "owner": record["plan"]["owner"], "allowed": True,
            }, operation="install_modes")
        if action == "confirm":
            return self._confirm(service, record, effect_current=effect_current)
        if action.startswith("install.mode."):
            return self._select_install_mode(service, record, action)
        if action.startswith("catalog."):
            return self._catalog_action(service, record, action)
        if action == "publication.status":
            return self._publication_action(
                service, record, "reopen", effect_current=effect_current
            )
        if action == "publication.decline":
            return self._publication_action(
                service, record, "decline", effect_current=effect_current
            )
        if action == "recover":
            return self._recover(
                service, record, effect_current=effect_current
            )
        if action in {"setup.recover", "setup.clear"}:
            return self._setup_recovery(
                service, record, clear=action == "setup.clear",
                effect_current=effect_current,
            )
        if action == "setup.status":
            return self._setup_status(service, record)
        if action.startswith("settings."):
            return self._settings_action(service, record, invocation, action)
        raise ValueError("Collective Wisdom control was not recognized.")

    def _inspect_publication(self, record: dict[str, Any], page_action: str) -> dict[str, Any]:
        try:
            page = int(page_action)
        except ValueError as exc:
            raise ValueError("Collective Wisdom control was not recognized.") from exc
        page_count = 0
        selected = None
        for item in record["plan"].get("files") or []:
            if not isinstance(item, dict) or not isinstance(item.get("content_utf8"), str):
                raise ValueError("Complete package inspection requires the terminal.")
            path, content = item["path"], item["content_utf8"]
            file_pages = max(1, (len(content) + 999) // 1000)
            if page_count <= page < page_count + file_pages:
                offset = (page - page_count) * 1000
                selected = {"path": path, "content": content[offset:offset + 1000]}
            page_count += file_pages
        if selected is None:
            raise ValueError("Collective Wisdom review page was not found.")
        inspection = {"page": page, "page_count": page_count, **selected}
        with self.store.transaction() as db:
            db.execute(
                "UPDATE wisdom_consent SET inspection_json=?,updated_at=? WHERE id=?",
                (_json(inspection), self.clock(), record["id"]),
            )
        return self.load(record["id"])

    def _defer_review(
        self, service: Any, record: dict[str, Any], invocation: NativeInvocation, *,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        """Persist the shared policy cooldown before retiring the native card."""

        if record["state"] != "pending":
            raise PermissionError("Collective Wisdom review is no longer pending.")
        deferral = record["plan"]["review_deferral"]

        def current() -> bool:
            if effect_current() is not True:
                return False
            latest = self.load(record["id"])
            try:
                self._validate_route(latest, invocation)
            except PermissionError:
                return False
            return bool(
                latest["state"] == "pending"
                and latest["state_revision"] == record["state_revision"]
                and latest["delivery_state"] in {"confirmed", "edit_failed"}
                and latest.get("message_id") == invocation.message_id
                and self.clock() <= latest["expires_at"]
                and service.native_owner() == latest["plan"].get("owner")
            )

        result = service.defer_review(deferral, may_apply=current)
        if not isinstance(result, dict) or result.get("deferred") is not True:
            raise PermissionError("Collective Wisdom deferral was not confirmed.")
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state='deferred',result_json=?,"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state='pending' AND state_revision=?",
                (_json(result), self.clock(), record["id"], record["state_revision"]),
            )
        if changed.rowcount != 1:
            latest = self.load(record["id"])
            if latest["state"] == "deferred" and latest.get("result") == result:
                return latest
            raise PermissionError("Collective Wisdom review state changed after deferral.")
        return self.load(record["id"])

    def _settings_action(  # pylint: disable=too-many-locals,too-many-branches
        self, service: Any, record: dict[str, Any], invocation: NativeInvocation,
        action: str,
    ) -> dict[str, Any]:
        if record["operation"] != "settings":
            raise ValueError("Notification setting control was not recognized.")
        result = record.get("result") or {}
        if record["state"] == "completed" and result.get("action") == action:
            return record
        if record["state"] != "pending":
            raise PermissionError("Notification setting selection is no longer pending.")
        modes = {
            "settings.mode.agent": "agent",
            "settings.mode.fixed": "fixed",
        }
        mutes = {
            "settings.mute.1d": "1_day",
            "settings.mute.1w": "1_week",
            "settings.mute.30d": "30_days",
            "settings.mute.forever": "forever",
            "settings.mute.off": None,
        }
        if action not in modes and action not in mutes:
            raise ValueError("Notification setting control was not recognized.")
        self._set_state(record, "applying")
        applying = self.load(record["id"])
        try:
            owner = service.native_owner()
            if owner != applying["plan"]["owner"]:
                raise PermissionError(
                    "Collective Wisdom review belongs to another enrolment."
                )
            current_mode = service.notification_mode()
            if action in modes:
                if applying["plan"].get("mode_writable") is not True:
                    raise PermissionError(
                        "Collective Wisdom notification settings are read-only."
                    )
                if current_mode != applying["plan"]["delivery_mode"]:
                    raise PermissionError(
                        "Notification mode changed. Open /wisdom settings again."
                    )
                self._settings_effect_current(applying, service, invocation)
                service.set_notification_mode(modes[action])
                current = service.notification_mode()
                if current != modes[action]:
                    raise PermissionError("Notification mode change was not confirmed.")
                outcome = {"action": action, "delivery_mode": current}
            else:
                from .preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

                control = applying["plan"].get("mute_control")
                if not isinstance(control, dict) or not control.get("id"):
                    raise PermissionError(
                        "Shared mute is unavailable. Open notification settings again."
                    )
                preferences = WisdomPreferences(service)
                self._settings_effect_current(applying, service, invocation)
                preferences.choose_mute_control(
                    self._org(owner), str(control["id"]), mutes[action]
                )
                preferences.flush_mute(self._org(owner))
                outcome = {
                    "action": action,
                    "delivery_mode": service.notification_mode(),
                    "mute_sync": preferences.mute_status(self._org(owner)),
                }
            if service.native_owner() != owner:
                raise PermissionError(
                    "Collective Wisdom review belongs to another enrolment."
                )
            with self.store.transaction() as db:
                changed = db.execute(
                    "UPDATE wisdom_consent SET state='completed',result_json=?,"
                    "state_revision=state_revision+1,updated_at=? "
                    "WHERE id=? AND state='applying' AND state_revision=?",
                    (_json(outcome), self.clock(), applying["id"],
                     applying["state_revision"]),
                )
            if changed.rowcount != 1:
                raise PermissionError("Notification setting state changed.")
            return self.load(record["id"])
        except Exception:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE wisdom_consent SET state='needs_review',"
                    "state_revision=state_revision+1,updated_at=? "
                    "WHERE id=? AND state='applying' AND state_revision=?",
                    (self.clock(), applying["id"], applying["state_revision"]),
                )
            raise

    def _settings_effect_current(
        self, record: dict[str, Any], service: Any, invocation: NativeInvocation,
    ) -> None:
        """Recheck durable route, expiry, and owner immediately before mutation."""

        latest = self.load(record["id"])
        if (
            latest["operation"] != "settings"
            or latest["state"] != "applying"
            or latest["state_revision"] != record["state_revision"]
            or self.clock() > latest["expires_at"]
        ):
            raise PermissionError("Notification setting selection is no longer current.")
        self._validate_route(latest, invocation)
        if (
            latest["delivery_state"] not in {"confirmed", "edit_failed"}
            or latest.get("message_id") != invocation.message_id
            or service.native_owner() != latest["plan"]["owner"]
        ):
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")

    def _select_install_mode(
        self, service: Any, record: dict[str, Any], action: str,
    ) -> dict[str, Any]:
        if record["operation"] != "install_modes" or record["state"] != "pending":
            raise PermissionError("Install policy selection is no longer pending.")
        modes = {
            "install.mode.default": None,
            "install.mode.manual": "MANUAL",
            "install.mode.notice": "AUTO_WITH_NOTICE",
            "install.mode.required": "REQUIRED",
        }
        if action not in modes:
            raise ValueError("Install policy control was not recognized.")
        plan = service.install_plan(
            record["plan"]["reference"], update_mode=modes[action]
        )
        if plan.get("operation") != "install" or plan.get("owner") != service.native_owner():
            raise PackagePolicyError("native install review is invalid")
        plan = self._prepare_review_deferral(service, plan)
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET operation='install',plan_json=?,"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND operation='install_modes' AND state='pending' "
                "AND state_revision=?",
                (_json(plan), self.clock(), record["id"], record["state_revision"]),
            )
        if changed.rowcount != 1:
            raise PermissionError("Install policy selection is no longer pending.")
        return self.load(record["id"])

    def _catalog_action(  # pylint: disable=too-many-locals
        self, service: Any, record: dict[str, Any], action: str,
    ) -> dict[str, Any]:
        if record["operation"] != "catalog" or record["state"] != "pending":
            raise PermissionError("Catalog navigation is no longer current.")
        plan = record["plan"]
        parts = action.split(".")
        if len(parts) != 3:
            raise ValueError("Catalog control was not recognized.")
        verb, raw_index = parts[1:]
        try:
            index = int(raw_index)
        except ValueError as exc:
            raise ValueError("Catalog control was not recognized.") from exc
        if verb == "page":
            replacement = {**plan, "page": index}
        else:
            data = plan.get("data") or []
            if plan.get("kind") == "show" and verb in {"versions", "install"}:
                detail = data if isinstance(data, dict) else {}
                skill = detail.get("skill") or {}
                skill_id = str(plan.get("skill_id") or skill.get("id") or "")
                versions = detail.get("versions") or []
                latest = max(
                    (int(row["version"]) for row in versions if isinstance(row, dict)
                     and isinstance(row.get("version"), int)
                     and not isinstance(row.get("version"), bool)),
                    default=None,
                )
                row = {"slug": skill.get("slug"), "version": latest}
            else:
                rows = data if isinstance(data, list) else data.get("skills", [])
                if index < 0 or index >= len(rows) or not isinstance(rows[index], dict):
                    raise ValueError("Catalog item was not found.")
                row = rows[index]
                skill_id = str(row.get("id") or plan.get("skill_id") or "")
            owner = plan["owner"]
            if verb == "show":
                replacement = {
                    "kind": "show", "skill_id": skill_id,
                    "data": service.show(skill_id), "owner": owner, "allowed": True,
                }
            elif verb == "versions":
                shown = service.show(skill_id)
                replacement = {
                    "kind": "versions", "skill_id": skill_id,
                    "slug": row.get("slug") or skill_id,
                    "data": service.versions(skill_id), "page": 0,
                    "portal_url": _safe_url(shown.get("portal_url")),
                    "owner": owner, "allowed": True,
                }
            elif verb == "install":
                version = row.get("version")
                reference = (
                    f"{skill_id}@v{version}"
                    if isinstance(version, int) and not isinstance(version, bool)
                    and version > 0 else skill_id
                )
                return self._replace_catalog(
                    record,
                    {"reference": reference, "owner": owner, "allowed": True},
                    operation="install_modes",
                )
            else:
                raise ValueError("Catalog control was not recognized.")
        return self._replace_catalog(record, replacement)

    def _replace_catalog(
        self, record: dict[str, Any], plan: dict[str, Any], *, operation: str = "catalog",
    ) -> dict[str, Any]:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET operation=?,plan_json=?,"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND operation=? AND state='pending' AND state_revision=?",
                (operation, _json(plan), self.clock(), record["id"], record["operation"],
                 record["state_revision"]),
            )
        if changed.rowcount != 1:
            raise PermissionError("Catalog navigation is no longer current.")
        return self.load(record["id"])

    def _acknowledge_update(
        self, service: Any, record: dict[str, Any], action: str
    ) -> dict[str, Any]:
        if record["operation"] != "update" or record["state"] != "pending":
            raise PermissionError("Collective Wisdom review is no longer pending.")
        policy = record["plan"].get("policy") or {}
        choices = {
            "preserve_modified": bool(
                policy.get("accepted_modified_preservation")
                or policy.get("preserve_modified")
            ),
            "accept_sensitive": bool(
                policy.get("accepted_sensitive") or policy.get("accept_sensitive")
            ),
            "accept_partial": bool(
                policy.get("accepted_partial") or policy.get("accept_partial")
            ),
        }
        choices[{
            "ack.preserve": "preserve_modified",
            "ack.sensitive": "accept_sensitive",
            "ack.partial": "accept_partial",
        }[action]] = True
        return self._replace_update(
            service, record, expected_state="pending", prior_state="stale",
            choices=choices,
        )

    def _replace_update(
        self, service: Any, record: dict[str, Any], *, expected_state: str,
        prior_state: str, choices: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        if choices is None:
            policy = record["plan"].get("policy") or {}
            choices = {
                "preserve_modified": bool(
                    policy.get("accepted_modified_preservation")
                    or policy.get("preserve_modified")
                ),
                "accept_sensitive": bool(
                    policy.get("accepted_sensitive") or policy.get("accept_sensitive")
                ),
                "accept_partial": bool(
                    policy.get("accepted_partial") or policy.get("accept_partial")
                ),
            }
        plan = service.update_plan(record["plan"]["skill_id"], **choices)
        if (
            plan.get("operation") != "update"
            or plan.get("owner") != record["plan"].get("owner")
        ):
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        plan = self._prepare_review_deferral(service, plan)
        now = self.clock()
        identity = secrets.token_urlsafe(12)
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state=?,state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (prior_state, now, record["id"], expected_state, record["state_revision"]),
            )
            if changed.rowcount != 1:
                raise PermissionError("Collective Wisdom review is no longer pending.")
            db.execute(
                _CONSENT_INSERT,
                (identity, record["organization_id"], record["owner_session"],
                 record["actor_id"], record["platform"], record["profile"], "update",
                 "pending", _json(plan), None, now + self.ttl_seconds, "confirmed", None,
                 record.get("request_message_id"), record.get("message_id"), 0, None, now, now),
            )
        return self.load(identity)

    def _recheck_setup(self, service: Any, record: dict[str, Any]) -> dict[str, Any]:
        latest = self.latest_setup(record)
        if latest is not None and latest["id"] != record["id"]:
            return latest
        step = record["plan"]["step"]
        fresh = service.installed_setup_plan(
            record["plan"]["skill_id"], version=record["plan"]["version"],
            phase=step["phase"], index=step["index"], command=step.get("command", ""),
        )
        fresh = {
            **fresh, "operation": "setup",
            "parent_consent_id": record["plan"].get("parent_consent_id", record["id"]),
            "slug": str(record["plan"].get("slug") or "Installed skill"),
            "setup_explanation": record["plan"].get("setup_explanation", ""),
        }
        if fresh.get("owner") != record["plan"].get("owner"):
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        now = self.clock()
        identity = secrets.token_urlsafe(12)
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state='stale',"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (now, record["id"], record["state"], record["state_revision"]),
            )
            if changed.rowcount != 1:
                raise PermissionError("Collective Wisdom review is no longer current.")
            db.execute(
                _CONSENT_INSERT,
                (identity, record["organization_id"], record["owner_session"],
                 record["actor_id"], record["platform"], record["profile"], "setup",
                 "pending", _json(fresh), None, now + self.ttl_seconds, "confirmed", None,
                 record.get("request_message_id"), record.get("message_id"), 0, None, now, now),
            )
        return self.load(identity)

    def _authority_current(
        self, service: Any, record: dict[str, Any],
        effect_current: Callable[[], bool],
    ) -> bool:
        """Recheck the durable review and invocation around owner rereads."""

        if effect_current() is not True:
            return False
        latest = self.load(record["id"])
        if effect_current() is not True:
            return False
        if (
            latest["state"] != record["state"]
            or latest["state_revision"] != record["state_revision"]
        ):
            return False
        owner_current = service.native_owner() == latest["plan"].get("owner")
        if effect_current() is not True or not owner_current:
            return False
        # Owner resolution can re-enter the host; fence durable consent after it too.
        latest = self.load(record["id"])
        return (
            effect_current() is True
            and latest["state"] == record["state"]
            and latest["state_revision"] == record["state_revision"]
        )

    def _confirm(  # pylint: disable=too-many-return-statements,too-many-branches
        self, service: Any, record: dict[str, Any], *,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        if record["operation"] not in {"install", "update", "publish", "setup"}:
            raise PermissionError("Advice and navigation never authorize an operation.")
        if record["state"] == "completed":
            return record
        if record["state"] != "pending":
            raise PermissionError("Collective Wisdom review is no longer pending.")
        if not record["plan"].get("allowed"):
            raise PermissionError("Review the blocked choices before continuing.")
        if (
            record["operation"] == "setup"
            and not _approval_is_lossless(
                record, _setup_view(record, enforce_rendering=False)
            )
        ):
            raise PermissionError(
                "The complete setup step cannot be reviewed on this native surface. "
                "Use the terminal handoff instead."
            )
        if record["operation"] == "publish" and not any(
            action.action == "confirm" for action in _publication_view(record).actions
        ):
            raise PermissionError(
                "The complete publication review cannot be shown on this native surface. "
                "Use the terminal handoff instead."
            )
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state='applying',"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state='pending' AND state_revision=?",
                (self.clock(), record["id"], record["state_revision"]),
            )
        if changed.rowcount != 1:
            latest = self.load(record["id"])
            if latest["state"] == "completed":
                return latest
            raise PermissionError("Collective Wisdom review is already being applied.")
        applying = {
            **record, "state": "applying",
            "state_revision": int(record["state_revision"]) + 1,
        }

        def applying_current() -> bool:
            return self._authority_current(service, applying, effect_current)

        active = applying
        try:
            if effect_current() is not True:
                raise PermissionError(
                    "Collective Wisdom native review is no longer current."
                )
            if record["operation"] == "publish":
                result = service.publication_submit(
                    record["plan"]["receipt"], may_apply=applying_current
                )
                state = "completed" if result.get("state") == "done" else "needs_review"
                return self._store_result(applying, result, state=state)
            if record["operation"] in {"install", "update"}:
                result = (
                    service.install_apply(
                        record["plan"]["receipt"], may_apply=applying_current
                    )
                    if record["operation"] == "install"
                    else service.update_apply(
                        record["plan"]["receipt"], may_apply=applying_current
                    )
                )
                if isinstance(result, dict) and result.get("pending") is True:
                    return self._store_result(applying, result, state="needs_review")
                if record["operation"] == "update":
                    active = self._store_result(applying, result, state="applying")
                info = service.installed_setup_inspect(
                    record["plan"]["skill_id"], version=record["plan"]["version"]
                )
                if not self._authority_current(service, active, effect_current):
                    raise PermissionError(
                        "Collective Wisdom native review is no longer current."
                    )
            else:
                result = service.installed_setup_apply(
                    record["plan"]["receipt"], may_apply=applying_current
                )
                if isinstance(result, dict) and result.get("state") in {"running", "unknown"}:
                    return self._store_setup_result(applying, result, state="applying")
                if isinstance(result, dict) and result.get("state") in {"failed", "blocked"}:
                    return self._store_setup_result(applying, result, state="failed")
                if isinstance(result, dict) and result.get("state") == "abandoned":
                    return self._store_setup_result(applying, result, state="needs_review")
                info = service.installed_setup_inspect(
                    record["plan"]["skill_id"], version=record["plan"]["version"]
                )
        except Exception:
            self._set_state(active, "needs_review")
            raise
        return self._complete(active, result, info)

    def _publication_action(
        self, service: Any, record: dict[str, Any], action: str, *,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        if record["operation"] != "publish" or not isinstance(record.get("result"), dict):
            raise PermissionError("Publication operation is unavailable.")
        publication_state = str(record["result"].get("publication_state") or "")
        # Terminal moderation outcomes are monotonic. A delayed duplicate click on
        # the old card must not poll again and overwrite a newer final projection.
        if publication_state in _PUBLICATION_TERMINAL:
            return record
        if action == "decline" and publication_state != "pending_moderation":
            raise PermissionError("Publication can no longer be declined from this review.")
        operation_id = str(record["result"].get("operation_id") or "")
        if not operation_id:
            raise PermissionError("Publication operation is unavailable.")
        if effect_current() is not True:
            raise PermissionError("Collective Wisdom native review is no longer current.")

        def current() -> bool:
            return self._authority_current(service, record, effect_current)

        result = (
            service.publication_reopen(operation_id, may_apply=current)
            if action == "reopen"
            else service.publication_decline(operation_id, may_apply=current)
        )
        state = "completed" if result.get("state") == "done" else "needs_review"
        return self._store_result(record, result, state=state)

    def _recover(
        self, service: Any, record: dict[str, Any], *,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        if record["operation"] != "update":
            raise PermissionError("Generic operation recovery is only available for updates.")
        if record["state"] == "completed":
            return record
        if record["state"] not in {"applying", "needs_review"}:
            raise PermissionError("Collective Wisdom operation does not need recovery.")
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state='applying',"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (self.clock(), record["id"], record["state"], record["state_revision"]),
            )
        if changed.rowcount != 1:
            latest = self.load(record["id"])
            if latest["state"] == "completed":
                return latest
            raise PermissionError("Collective Wisdom operation is already being recovered.")
        applying = {
            **record, "state": "applying",
            "state_revision": int(record["state_revision"]) + 1,
        }

        def current() -> bool:
            return self._authority_current(service, applying, effect_current)

        try:
            result = record.get("result")
            if not isinstance(result, dict):
                raise PermissionError(
                    "Exact update recovery is unavailable; inspect it in the terminal."
                )
            if result.get("pending") is True:
                operation_id = result.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    raise PermissionError(
                        "Exact update recovery is unavailable; inspect it in the terminal."
                    )
                recovery = service.update_recovery_inspect(
                    record["plan"]["skill_id"], operation_id
                )
                if not isinstance(recovery, dict) or current() is not True:
                    raise PermissionError(
                        "Collective Wisdom update recovery is no longer current."
                    )
                result = service.update_recovery_resume(
                    record["plan"]["skill_id"], operation_id,
                    record["plan"]["receipt"], may_apply=current,
                )
                if isinstance(result, dict) and result.get("pending") is True:
                    return self._store_result(applying, result, state="needs_review")
            info = service.installed_setup_inspect(
                record["plan"]["skill_id"], version=record["plan"]["version"]
            )
            if current() is not True:
                raise PermissionError(
                    "Collective Wisdom native review is no longer current."
                )
        except Exception:
            self._set_state(applying, "needs_review")
            raise
        return self._complete(applying, result, info)

    def _setup_progress(
        self, service: Any, record: dict[str, Any], *,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        if record["state"] != "applying":
            return record
        result: dict[str, Any] = record.get("result") or {}
        nested = result.get("setup")
        setup: dict[str, Any] = nested if isinstance(nested, dict) else result
        operation_id = setup.get("operation_id")
        if not operation_id:
            raise PermissionError("Setup recovery operation is unavailable.")

        def current_authority() -> bool:
            return self._authority_current(service, record, effect_current)

        recovery = service.installed_setup_recovery_inspect(
            record["plan"]["skill_id"], operation_id
        )
        if current_authority() is not True:
            raise PermissionError("Collective Wisdom native review is no longer current.")
        current = service.installed_setup_recover(
            record["plan"]["skill_id"], operation_id, acknowledged=False,
            expected_recovery=recovery, may_apply=current_authority,
        )
        if current.get("recovery_review") is True:
            reviewed = service.installed_setup_recovery_inspect(
                record["plan"]["skill_id"], operation_id
            )
            if current_authority() is not True:
                raise PermissionError(
                    "Collective Wisdom native review is no longer current."
                )
            current = {**current, "recovery_snapshot": reviewed}
        state = self._setup_record_state(current)
        return self._store_setup_result(record, current, state=state)

    def _setup_recovery(
        self, service: Any, record: dict[str, Any], *, clear: bool,
        effect_current: Callable[[], bool],
    ) -> dict[str, Any]:
        if record["operation"] != "setup" or record["state"] != "applying":
            raise PermissionError("Setup operation does not need recovery.")
        result: dict[str, Any] = record.get("result") or {}
        nested = result.get("setup")
        setup: dict[str, Any] = nested if isinstance(nested, dict) else result
        if clear and setup.get("recovery_review") is not True:
            raise PermissionError("Review the interrupted setup effects before clearing.")
        operation_id = setup.get("operation_id")
        if not operation_id:
            raise PermissionError("Setup recovery operation is unavailable.")

        def current_authority() -> bool:
            return self._authority_current(service, record, effect_current)

        if clear:
            recovery = setup.get("recovery_snapshot")
            if not isinstance(recovery, dict):
                raise PermissionError("Review the interrupted setup effects again.")
        else:
            recovery = service.installed_setup_recovery_inspect(
                record["plan"]["skill_id"], operation_id
            )
        if current_authority() is not True:
            raise PermissionError("Collective Wisdom native review is no longer current.")
        current = service.installed_setup_recover(
            record["plan"]["skill_id"], operation_id, acknowledged=clear,
            expected_recovery=recovery, may_apply=current_authority,
        )
        if current.get("recovery_review") is True:
            reviewed = service.installed_setup_recovery_inspect(
                record["plan"]["skill_id"], operation_id
            )
            if current_authority() is not True:
                raise PermissionError(
                    "Collective Wisdom native review is no longer current."
                )
            current = {**current, "recovery_snapshot": reviewed}
        state = self._setup_record_state(current)
        return self._store_setup_result(record, current, state=state)

    @staticmethod
    def _setup_record_state(setup: dict[str, Any]) -> str:
        return {
            "passed": "completed",
            "failed": "failed",
            "blocked": "failed",
            "abandoned": "needs_review",
        }.get(str(setup.get("state") or ""), "applying")

    def _store_setup_result(
        self, record: dict[str, Any], setup: dict[str, Any], *, state: str
    ) -> dict[str, Any]:
        stored = dict(record.get("result") or {})
        stored["setup"] = setup
        return self._store_result(record, stored, state=state)

    def _store_result(
        self, record: dict[str, Any], result: dict[str, Any], *, state: str
    ) -> dict[str, Any]:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state=?,result_json=?,"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (state, _json(result), self.clock(), record["id"], record["state"],
                 record["state_revision"]),
            )
        if changed.rowcount != 1:
            raise PermissionError("Collective Wisdom operation state changed.")
        return self.load(record["id"])

    def _complete(
        self, record: dict[str, Any], result: Any, info: dict[str, Any]
    ) -> dict[str, Any]:
        continuation = self._continuation(info)
        stored_result = {**(result if isinstance(result, dict) else {"value": result}),
                         "setup_continuation": continuation}
        now = self.clock()
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state='completed',result_json=?,"
                "state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (_json(stored_result), now, record["id"], record["state"],
                 record["state_revision"]),
            )
            if changed.rowcount != 1:
                raise PermissionError("Collective Wisdom operation state changed.")
            if info.get("ready_to_use") is not True:
                self._enqueue_handoff_db(db, record, now)
        return self.load(record["id"])

    def _setup_status(self, service: Any, record: dict[str, Any]) -> dict[str, Any]:
        if record["operation"] not in {"update", "setup"} or record["state"] != "completed":
            return record
        info = service.installed_setup_inspect(
            record["plan"]["skill_id"], version=record["plan"]["version"]
        )
        latest = self.latest_setup(record)
        if latest and latest["state"] != "completed":
            if latest.get("message_id") == record.get("message_id"):
                return latest
            deferred = latest["state"] == "deferred"
            continuation = (
                self._continuation(info)
                if deferred else {
                    "state": "needs_review",
                    "message": "Review the separately posted setup step on its own card.",
                    "inspection": info,
                }
            )
            now = self.clock()
            with self.store.transaction() as db:
                changed = db.execute(
                    "UPDATE wisdom_consent SET state_revision=state_revision+1,updated_at=? "
                    "WHERE id=? AND state='completed' AND state_revision=?",
                    (now, record["id"], record["state_revision"]),
                )
                if changed.rowcount != 1:
                    raise PermissionError("Collective Wisdom operation state changed.")
                if deferred:
                    self._enqueue_handoff_db(db, record, now)
                result = dict(record.get("result") or {})
                result["setup_continuation"] = continuation
                db.execute(
                    "UPDATE wisdom_consent SET result_json=? WHERE id=?",
                    (_json(result), record["id"]),
                )
            return self.load(record["id"])
        now = self.clock()
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state='completed' AND state_revision=?",
                (now, record["id"], record["state_revision"]),
            )
            if changed.rowcount != 1:
                raise PermissionError("Collective Wisdom operation state changed.")
            if info.get("ready_to_use") is not True:
                self._enqueue_handoff_db(db, record, now)
            else:
                db.execute(
                    "UPDATE wisdom_setup_handoff SET state='retired',updated_at=? "
                    "WHERE consent_id=? AND state!='retired'",
                    (now, record["id"]),
                )
            handoff = db.execute(
                "SELECT state FROM wisdom_setup_handoff WHERE consent_id=?",
                (record["id"],),
            ).fetchone()
            continuation = self._continuation(
                info, handoff_state=str(handoff["state"]) if handoff else None
            )
            result = dict(record.get("result") or {})
            result["setup_continuation"] = continuation
            db.execute(
                "UPDATE wisdom_consent SET result_json=? WHERE id=?",
                (_json(result), record["id"]),
            )
        return self.load(record["id"])

    @staticmethod
    def _continuation(
        info: dict[str, Any], *, handoff_state: str | None = None
    ) -> dict[str, Any]:
        ready = info.get("ready_to_use") is True
        waiting = not ready and handoff_state == "waiting_for_model"
        return {
            "state": "ready" if ready else "waiting_for_model" if waiting else "queued",
            "message": (
                "Declared prerequisites and the approved verification command passed."
                if ready else
                "Setup is waiting for this conversation's model. Select or reconnect "
                "the model, then send a message here to resume. No setup command is "
                "started while waiting."
                if waiting else
                "Setup is queued in this conversation. Send a message here to resume "
                "with its active model. Each command still requires separate approval."
            ),
            "inspection": info,
        }

    def claim_handoff(self, service: Any, invocation: NativeInvocation,
                      *, lease_seconds: float = 120.0) -> dict[str, Any] | None:
        owner = service.native_owner()
        org = self._org(owner)
        now = self.clock()
        token = secrets.token_urlsafe(18)
        with self.store.transaction() as db:
            self._record_session_db(db, org, invocation, now)
            row = db.execute(
                "SELECT * FROM wisdom_setup_handoff WHERE organization_id=? AND owner_session=? "
                "AND actor_id=? AND platform=? AND (state IN ('pending','waiting_for_model') "
                "OR (state='claimed' AND lease_until<=?)) ORDER BY created_at,id LIMIT 1",
                (org, invocation.session_key, invocation.actor_id, invocation.platform, now),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                "UPDATE wisdom_setup_handoff SET state='claimed',lease_token=?,lease_until=?,"
                "last_error=NULL,updated_at=? WHERE id=? AND "
                "(state IN ('pending','waiting_for_model') "
                "OR (state='claimed' AND lease_until<=?))",
                (token, now + lease_seconds, now, row["id"], now),
            )
            if changed.rowcount != 1:
                return None
            consent = _decode(db.execute(
                "SELECT * FROM wisdom_consent WHERE id=?", (row["consent_id"],)
            ).fetchone())
        if consent is None or consent["state"] != "completed":
            self.finish_handoff(str(row["id"]), token, state="retired")
            return None
        if consent["plan"].get("owner") != owner:
            self.finish_handoff(str(row["id"]), token, state="retired")
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        self._validate_route(consent, invocation)
        return {"id": str(row["id"]), "lease_token": token, "parent": consent}

    def finish_handoff(self, handoff_id: str, token: str, *, state: str,
                       error: str | None = None) -> None:
        if state not in {"retired", "waiting_for_model", "pending"}:
            raise ValueError("invalid setup handoff state")
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_setup_handoff SET state=?,lease_token=NULL,lease_until=NULL,"
                "last_error=?,updated_at=? WHERE id=? AND state='claimed' AND lease_token=?",
                (state, error, self.clock(), handoff_id, token),
            )
        if changed.rowcount != 1:
            raise PermissionError("Setup handoff ownership changed.")

    def create_setup(  # pylint: disable=too-many-arguments,too-many-locals
        self, service: Any, claim: dict[str, Any], invocation: NativeInvocation,
        *, phase: str, index: int, command: str,
        explanation: str,
    ) -> dict[str, Any]:
        parent = claim["parent"]
        current = self.load(parent["id"])
        if current["state"] != "completed" or current["result"] != parent["result"]:
            raise PermissionError("Setup source changed while preparing the next step.")
        self._validate_route(current, invocation)
        info = service.installed_setup_inspect(
            parent["plan"]["skill_id"], version=parent["plan"]["version"]
        )
        if info.get("content_hash") != parent["plan"]["content_hash"]:
            raise PermissionError("The installed package changed. Review its current version.")
        plan = service.installed_setup_plan(
            parent["plan"]["skill_id"], version=parent["plan"]["version"],
            phase=phase, index=index, command=command,
        )
        plan = {
            **plan,
            "operation": "setup",
            "parent_consent_id": str(
                parent["plan"].get("parent_consent_id") or parent["id"]
            ),
            "slug": str(plan.get("slug") or parent["plan"].get("slug") or "Installed skill"),
            "setup_explanation": explanation,
        }
        if (
            service.native_owner() != parent["plan"].get("owner")
            or plan.get("owner") != parent["plan"].get("owner")
        ):
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")
        now = self.clock()
        identity = secrets.token_urlsafe(12)
        with self.store.transaction() as db:
            owned = db.execute(
                "SELECT 1 FROM wisdom_setup_handoff WHERE id=? AND state='claimed' "
                "AND lease_token=?",
                (claim["id"], claim["lease_token"]),
            ).fetchone()
            if owned is None:
                raise PermissionError("Setup handoff ownership changed.")
            db.execute(
                _CONSENT_INSERT,
                (identity, parent["organization_id"], invocation.session_key,
                 invocation.actor_id, invocation.platform, invocation.profile, "setup",
                 "pending", _json(plan), None, now + self.ttl_seconds, "unsent", None,
                 invocation.message_id, None, 0, None, now, now),
            )
            db.execute(
                "UPDATE wisdom_setup_handoff SET state='retired',lease_token=NULL,lease_until=NULL,"
                "updated_at=? WHERE id=? AND lease_token=?",
                (now, claim["id"], claim["lease_token"]),
            )
        return self.load(identity)

    def latest_setup(self, parent: dict[str, Any]) -> dict[str, Any] | None:
        parent_id = str(parent["plan"].get("parent_consent_id") or parent["id"])
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM wisdom_consent WHERE organization_id=? AND owner_session=? "
                "AND operation='setup' AND json_extract(plan_json,'$.skill_id')=? "
                "AND json_extract(plan_json,'$.version')=? "
                "AND json_extract(plan_json,'$.parent_consent_id')=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (parent["organization_id"], parent["owner_session"],
                 parent["plan"]["skill_id"], parent["plan"]["version"], parent_id),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM wisdom_consent WHERE organization_id=? AND owner_session=? "
                    "AND operation='setup' AND json_extract(plan_json,'$.skill_id')=? "
                    "AND json_extract(plan_json,'$.version')=? "
                    "AND json_extract(plan_json,'$.parent_consent_id') IS NULL "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (parent["organization_id"], parent["owner_session"],
                     parent["plan"]["skill_id"], parent["plan"]["version"]),
                ).fetchone()
        return _decode(row)

    def _enqueue_handoff_db(self, db: Any, consent: dict[str, Any], now: float) -> None:
        db.execute(
            "INSERT INTO wisdom_setup_handoff VALUES(?,?,?,?,?,?,'pending',NULL,NULL,NULL,?,?) "
            "ON CONFLICT(consent_id) DO UPDATE SET state=CASE "
            "WHEN wisdom_setup_handoff.state='retired' THEN 'pending' "
            "ELSE wisdom_setup_handoff.state END,"
            "updated_at=excluded.updated_at",
            (secrets.token_urlsafe(12), consent["organization_id"], consent["id"],
             consent["owner_session"], consent["actor_id"], consent["platform"], now, now),
        )

    @staticmethod
    def _address(invocation: NativeInvocation) -> dict[str, str]:
        return {"profile": invocation.profile, "chat_id": invocation.chat_id,
                "thread_id": invocation.thread_id, "scope_id": invocation.scope_id}

    def _record_session_db(self, db: Any, org: str, invocation: NativeInvocation,
                           now: float) -> None:
        db.execute(
            "INSERT INTO wisdom_agent_session VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,session_key) DO UPDATE SET actor_id=excluded.actor_id,"
            "platform=excluded.platform,profile=excluded.profile,"
            "address_json=excluded.address_json,"
            "last_activity_at=excluded.last_activity_at",
            (org, invocation.session_key, invocation.actor_id, invocation.platform,
             invocation.profile, _json(self._address(invocation)), now),
        )

    def _set_state(self, record: dict[str, Any], state: str) -> None:
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE wisdom_consent SET state=?,state_revision=state_revision+1,updated_at=? "
                "WHERE id=? AND state=? AND state_revision=?",
                (state, self.clock(), record["id"], record["state"],
                 record["state_revision"]),
            )
        if changed.rowcount != 1:
            raise PermissionError("Collective Wisdom operation state changed.")

    def _validate(self, record: dict[str, Any], service: Any,
                  invocation: NativeInvocation, *, recovery: bool = False) -> None:
        self._validate_route(record, invocation)
        if record["delivery_state"] not in {"confirmed", "edit_failed"}:
            raise PermissionError("Collective Wisdom review delivery is not confirmed.")
        if record.get("message_id") != invocation.message_id:
            raise PermissionError("Collective Wisdom control belongs to another message.")
        if record["state"] == "superseded" or (
            record["state"] == "deferred" and not recovery
        ):
            raise PermissionError("Collective Wisdom review is no longer pending.")
        expiry_exempt = record["state"] in {"applying", "completed"} or (
            recovery and record["state"] == "needs_review"
        )
        if not expiry_exempt and self.clock() > record["expires_at"]:
            self._set_state(record, "expired")
            record["state"] = "expired"
            record["state_revision"] = int(record["state_revision"]) + 1
            if not recovery:
                raise PermissionError("Collective Wisdom control expired. Reopen the update.")
        current_owner = service.native_owner()
        if (
            self._org(current_owner) != record["organization_id"]
            or record["plan"].get("owner") != current_owner
        ):
            raise PermissionError("Collective Wisdom review belongs to another enrolment.")

    def _validate_route(self, record: dict[str, Any], invocation: NativeInvocation) -> None:
        for key, value in (
            ("platform", invocation.platform), ("profile", invocation.profile),
            ("actor_id", invocation.actor_id), ("owner_session", invocation.session_key),
        ):
            if record.get(key) != value:
                raise PermissionError("Collective Wisdom control belongs to another session.")
        with self.store.transaction() as db:
            session = db.execute(
                "SELECT actor_id,platform,profile,address_json FROM wisdom_agent_session "
                "WHERE organization_id=? AND session_key=?",
                (record["organization_id"], record["owner_session"]),
            ).fetchone()
        expected = self._address(invocation)
        if session is None or (
            str(session["actor_id"]), str(session["platform"]), str(session["profile"]),
            json.loads(session["address_json"]),
        ) != (invocation.actor_id, invocation.platform, invocation.profile, expected):
            raise PermissionError("Collective Wisdom control belongs to another session.")

    def view(
        self, record: dict[str, Any], *, assessment_expanded: bool | None = None
    ) -> NativeView:
        expanded = (
            bool(record.get("assessment_expanded"))
            if assessment_expanded is None else assessment_expanded
        )
        view = project_view(record, assessment_expanded=expanded)
        if record["state"] == "deferred":
            return replace(view, dismissed=True, actions=[], navigation_actions=[],
                           items=[replace(item, actions=[]) for item in view.items])
        return view


def _has_checks(plan: dict[str, Any]) -> bool:
    return any(
        isinstance(plan.get(key), dict)
        for key in (
            "security_check", "professionalism_check", "local_scan", "server_checks"
        )
    )


def _checks_action(expanded: bool) -> NativeAction:
    return NativeAction(
        "Hide checks" if expanded else "Show checks",
        "checks.hide" if expanded else "checks.show",
    )


def _assessment_action(expanded: bool) -> NativeAction:
    return NativeAction(
        "Hide Assessment" if expanded else "View Assessment",
        "assessment.hide" if expanded else "assessment.show",
    )


def _update_policy_line(operation: str, plan: dict[str, Any]) -> str:
    if operation != "install" or "update_mode" not in plan:
        return ""
    mode = {
        None: "Organization default",
        "MANUAL": "Manual",
        "AUTO_WITH_NOTICE": "Automatic with notice",
        "REQUIRED": "Required",
    }.get(plan.get("update_mode"), "Unavailable")
    return f"\nFuture updates: {mode}"


def _record_actions(record: dict[str, Any]) -> list[str]:
    configured = record.get("actions")
    if isinstance(configured, list):
        return [str(value) for value in configured]
    plan = record["plan"]
    state = record["state"]
    if state != "pending":
        return ["inspect"]
    blocked = bool(
        plan.get("modified")
        or plan.get("sensitive_expansion")
        or plan.get("allowed") is False
        or (plan.get("compatibility") or {}).get("outcome")
        not in {None, "compatible"}
        or (
            record["operation"] in {"install", "update"}
            and (_security_check(plan) or {}).get("status")
            not in {"pass", "advisory"}
        )
    )
    deferral_actions = (
        [] if plan.get("review_deferral_unavailable") is True else ["defer"]
    )
    if not blocked:
        return [*deferral_actions, "inspect", "confirm"]
    actions = []
    policy = plan.get("policy") or {}
    if plan.get("modified") and not (
        policy.get("accepted_modified_preservation") or policy.get("preserve_modified")
    ):
        actions.append("ack.preserve")
    if plan.get("sensitive_expansion") and not (
        policy.get("accepted_sensitive") or policy.get("accept_sensitive")
    ):
        actions.append("ack.sensitive")
    if (plan.get("compatibility") or {}).get("outcome") in {
        "partial", "compatible_after_setup"
    } and not (policy.get("accepted_partial") or policy.get("accept_partial")):
        actions.append("ack.partial")
    return [*actions, *deferral_actions, "inspect"]


def project_view(  # pylint: disable=too-many-return-statements
    record: dict[str, Any], *,
    checks_expanded: bool | None = None,
    assessment_expanded: bool = False,
) -> NativeView:
    """Project stored plugin facts through the pinned reference interaction view."""
    continuation = record.get("setup_continuation")
    if isinstance(continuation, dict):
        state = str(continuation.get("state") or "needs_review")
        summary = {
            "queued": "Setup queued",
            "waiting_for_model": "Setup waiting for model",
            "needs_review": "Setup needs attention",
            "ready": "Ready",
        }.get(state, "Setup needs attention")
        plan = record["plan"]
        return NativeView(
            title="Hermes Collective Wisdom",
            summary=summary,
            items=[NativeItem(
                f"{plan['slug']} · v{plan['version']}",
                str(continuation.get("message") or ""),
            )],
            actions=[NativeAction("Check setup", "setup.status")],
        )
    if record["operation"] == "setup":
        return _setup_view(record)
    if record["operation"] == "install_modes":
        return _install_modes_view(record)
    if record["operation"] == "publish":
        return _publication_view(record)
    if record["operation"] == "catalog":
        return _catalog_view(record)
    if record["operation"] == "settings":
        return _settings_view(record)
    if record["operation"] == "recommendation":
        return _recommendation_view(record)
    if record["operation"] == "packaging_failed":
        return NativeView(
            "Hermes Collective Wisdom", "Share Packaging failed",
            [NativeItem("Local preparation could not finish", (
                "Nothing was uploaded or shared. Check the local skill's files and package "
                "requirements, then retry preparation. A successful retry still requires "
                "review of the exact package before sharing."
            ))],
            [NativeAction("Retry Packaging", "packaging.retry")]
            if record["state"] == "pending" else [],
        )
    expanded = (
        bool(record.get("checks_expanded"))
        if checks_expanded is None else checks_expanded
    )
    return _update_view(
        record,
        checks_expanded=expanded,
        assessment_expanded=assessment_expanded,
    )


def _recommendation_installable(plan: dict[str, Any]) -> bool:
    return bool(
        plan.get("allowed") is True
        and (plan.get("security_check") or {}).get("status") in {"pass", "advisory"}
    )


def _recommendation_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    original = assessment_view(plan["job"])
    actions = [NativeAction("View Details", "recommendation.details")]
    if isinstance(plan.get("review_deferral"), dict):
        actions.append(NativeAction("Not Now", "defer"))
    actions.append(NativeAction("Mute Skill Recommendations", "recommendation.settings"))
    if _recommendation_installable(plan):
        actions.append(NativeAction("Install Skill", "recommendation.install", True))
    name = plan.get("organization_display_name")
    organization = f"your organization ({name})" if name else "your organization"
    detail = original.items[0].detail
    if _has_checks(plan):
        detail += "\n\n" + _review_text(plan, False)
    if plan.get("details_expanded"):
        detail += f"\n\nExact version: v{plan['version']}"
        detail += "\nContent hash: " + str(
            plan["facts"]["version"].get("content_hash") or "unavailable"
        )
        detail += "\n\n" + _review_text(plan, True)
        actions = [NativeAction("Looks good.", "recommendation.back")]
    return replace(
        original, items=[replace(original.items[0], detail=detail, preamble=(
            f"Hermes has detected a newly shared skill in {organization}, "
            "that could be useful to you!"
        ))], actions=actions,
        notice=("Reviewing does not install or approve this skill." if plan.get("details_expanded")
                else "Would you like to install this skill?"),
    )


def _mute_gateway_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "unavailable (not assumed unmuted)"
    if value.get("forever") is True:
        return "muted forever"
    if value.get("muted") is True:
        until = str(value.get("muted_until") or "").strip()
        return f"muted until {until}" if until else "muted"
    return "not muted"


def _mute_sync_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "no pending local change"
    duration = value.get("requested_duration")
    choice = "unmute" if duration is None else str(duration).replace("_", " ")
    state = str(value.get("preference_sync") or "pending")
    expiry = value.get("requested_until")
    suffix = f" until {expiry}" if expiry is not None else ""
    return f"{choice}{suffix} ({state})"


def _settings_view(record: dict[str, Any]) -> NativeView:
    plan = record["plan"]
    result = record.get("result") or {}
    mode = str(result.get("delivery_mode") or plan.get("delivery_mode") or "fixed")
    terminal = record["state"] != "pending"
    mode_detail = (
        "Eligible agent-assessed recommendations may be generated and delivered "
        "in this private conversation."
        if mode == "agent" else
        "Proactive generation and delivery are off; manual Wisdom reviews remain available."
    )
    gateway = str(plan.get("gateway_status") or "unavailable")
    sync = result.get("mute_sync", plan.get("mute_sync"))
    sync_state = (
        str(sync.get("preference_sync") or "none")
        if isinstance(sync, dict) else "none"
    )
    actions = []
    if not terminal and plan.get("mode_writable") is True:
        actions.extend((
            NativeAction("Agent decides", "settings.mode.agent", mode == "agent"),
            NativeAction("Fixed notifications", "settings.mode.fixed", mode == "fixed"),
        ))
    if not terminal and isinstance(plan.get("mute_control"), dict):
        actions.extend((
            NativeAction("Mute 1 day", "settings.mute.1d"),
            NativeAction("Mute 1 week", "settings.mute.1w"),
            NativeAction("Mute 30 days", "settings.mute.30d"),
            NativeAction("Mute forever", "settings.mute.forever"),
            NativeAction("Unmute", "settings.mute.off"),
        ))
    summary = {
        "pending": "Notification settings",
        "completed": "Notification settings updated",
        "needs_review": "Notification settings need review",
        "expired": "Notification settings expired",
        "superseded": "Notification settings superseded",
        "applying": "Notification settings change uncertain",
    }.get(record["state"], "Notification settings unavailable")
    return NativeView(
        "Hermes Collective Wisdom",
        summary,
        [
            NativeItem("Delivery mode", f"Current: {mode}\n{mode_detail}"),
            NativeItem(
                "Recommendation mute",
                "This applies to all Collective Wisdom recommendations for your account in this "
                "organization, not just this skill or category. Opening settings changes nothing.\n"
                f"Gateway status: {gateway}\n"
                f"Current Gateway state: {_mute_gateway_text(plan.get('gateway_mute'))}\n"
                f"Local sync: {sync_state}\n"
                f"Local request: {_mute_sync_text(sync)}",
            ),
        ],
        actions,
        notice=(
            "Run /wisdom settings again to make another change."
            if terminal else
            "Each control makes one explicit owner-bound change."
        ),
    )


def _install_modes_view(record: dict[str, Any]) -> NativeView:
    reference = str(record["plan"].get("reference") or "Skill")
    return NativeView(
        "Hermes Collective Wisdom", "Choose update policy",
        [NativeItem(
            reference,
            "Choose how this managed skill may update after installation. "
            "You will review the exact package before any files are installed.",
        )],
        [
            NativeAction("Organization default", "install.mode.default", True),
            NativeAction("Manual", "install.mode.manual"),
            NativeAction("Automatic with notice", "install.mode.notice"),
            NativeAction("Required", "install.mode.required"),
            NativeAction("Not Now", "defer"),
        ],
    )


def _catalog_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get("skills"), list):
        return [row for row in data["skills"] if isinstance(row, dict)]
    return []


def _catalog_review_text(value: dict[str, Any]) -> str:
    return "\n".join((
        _check_line("Security check", (value.get("security_check") or {}).get("status")),
        _professionalism_text(value.get("professionalism_check"), include_checks=False),
    ))


def _requirements_text(specification: dict[str, Any]) -> str:
    values = []
    for key in ("tools", "plugins", "credentials", "connections"):
        rows = specification.get(key) or []
        values.extend(
            str(row.get("name") or row.get("id") or "")
            for row in rows if isinstance(row, dict)
        )
    return ", ".join(value for value in values if value) or "none declared"


def _catalog_view(record: dict[str, Any]) -> NativeView:  # pylint: disable=too-many-locals
    """Project reference browse, detail and immutable version cards."""
    plan = record["plan"]
    kind = str(plan.get("kind") or "browse")
    page = max(0, int(plan.get("page") or 0))
    page_size = 5
    if kind == "browse":
        rows = _catalog_rows(plan.get("data"))
        start = page * page_size
        visible = rows[start:start + page_size]
        query = str(plan.get("query") or "")
        actions = []
        if page:
            actions.append(NativeAction("Previous", f"catalog.page.{page - 1}"))
        if start + page_size < len(rows):
            actions.append(NativeAction("Next", f"catalog.page.{page + 1}"))
        return NativeView(
            "Shared skills",
            f"Results for “{query}” · page {page + 1}"
            if query else f"Skills published by your team · page {page + 1}",
            [NativeItem(
                str(row.get("slug") or row.get("id") or "Shared skill"),
                "\n".join((
                    f"v{row.get('latest_version') or '?'} · "
                    f"{row.get('author_description') or 'No description'}",
                    _catalog_review_text(row),
                )),
                [
                    NativeAction("View", f"catalog.show.{start + offset}"),
                    NativeAction("Install", f"catalog.install.{start + offset}", True),
                ],
            ) for offset, row in enumerate(visible)],
            actions,
            notice="" if visible else "No matching shared skills.",
        )
    if kind == "show":
        detail = plan.get("data") if isinstance(plan.get("data"), dict) else {}
        skill = detail.get("skill") or {}
        versions = detail.get("versions") or []
        latest = max(
            (int(row["version"]) for row in versions
             if isinstance(row, dict) and isinstance(row.get("version"), int)
             and not isinstance(row.get("version"), bool)),
            default=None,
        )
        version_detail = detail.get("latest_version_detail") or {}
        version = version_detail.get("version") or {}
        description = str(
            version.get("author_description") or skill.get("author_description")
            or "No description"
        )
        scan = (
            (version.get("verified_facts") or {}).get("scan_verdict")
            or (version.get("scan") or {}).get("verdict")
            or skill.get("scan_verdict") or "not reported"
        )
        summary = "\n".join((
            description,
            f"Latest: v{latest or '?'} · scan: {scan}",
            "Requirements: " + _requirements_text(version.get("system_spec") or {}),
            "", _catalog_review_text(version),
        ))
        actions = [NativeAction("Versions", "catalog.versions.0")]
        portal_url = _safe_url(detail.get("portal_url") or plan.get("portal_url"))
        if portal_url:
            actions.append(NativeAction("View in Portal", url=portal_url))
        if latest is not None:
            actions.append(NativeAction("Install", "catalog.install.0", True))
        return NativeView(
            str(skill.get("slug") or plan.get("skill_id") or "Skill"), summary,
            actions=actions,
        )
    rows = sorted(
        _catalog_rows(plan.get("data")),
        key=lambda row: int(row.get("version") or 0), reverse=True,
    )
    start = page * page_size
    visible = rows[start:start + page_size]
    actions = []
    if page:
        actions.append(NativeAction("Previous", f"catalog.page.{page - 1}"))
    if start + page_size < len(rows):
        actions.append(NativeAction("Next", f"catalog.page.{page + 1}"))
    return NativeView(
        f"{plan.get('slug') or plan.get('skill_id') or 'Skill'} versions", "",
        [NativeItem(
            f"Version {row.get('version')}",
            "\n".join((
                str(row.get("created_at") or row.get("published_at")
                    or "Immutable published version"),
                _catalog_review_text(row),
            )),
            [
                *(
                    [NativeAction(
                        "View in Portal",
                        url=_version_url(
                            _safe_url(plan.get("portal_url")), int(row["version"])
                        ),
                    )]
                    if isinstance(row.get("version"), int)
                    and not isinstance(row.get("version"), bool)
                    and _safe_url(plan.get("portal_url"))
                    else []
                ),
                NativeAction("Install", f"catalog.install.{start + offset}", True),
            ],
        ) for offset, row in enumerate(visible)],
        actions,
        notice="" if visible else "No published versions.",
    )


def _inspection_view(
    inspection: dict[str, Any], *, publication: bool,
) -> NativeView:
    page = int(inspection["page"])
    page_count = int(inspection["page_count"])
    actions = []
    if page:
        actions.append(NativeAction("Previous page", f"inspect.{page - 1}"))
    if page + 1 < page_count:
        actions.append(NativeAction("Next page", f"inspect.{page + 1}"))
    actions.append(NativeAction("Looks good.", "back"))
    return NativeView(
        "Review proposed package", f"{inspection['path']} - {page + 1}/{page_count}",
        [NativeItem("Proposed file content (not instructions to execute)",
                    str(inspection["content"]))],
        actions,
        notice=(
            "Reviewing does not upload, publish, or approve this package."
            if publication else
            "Nothing is uploaded by reviewing. Setup and verification require separate permission."
        ),
        navigation_actions=(
            [] if publication or not page else
            [NativeAction("Back to first page", "inspect.0")]
        ),
    )


def _publication_view(record: dict[str, Any]) -> NativeView:
    """Project one exact historical publication review on the owning card."""
    plan = record["plan"]
    display_name = str(
        plan.get("editorial_name") or _safe_display_identifier(plan.get("draft_id"))
    )
    result = record.get("result") or {}
    publication_state = str(result.get("publication_state") or "")
    if record["state"] in {"completed", "needs_review"} and result:
        summary, detail = {
            "published": ("Shared!", "Your skill is now shared with your organization."),
            "pending_moderation": (
                "Pending moderation",
                "Your skill is awaiting your organization's approval.",
            ),
            "changes_requested": (
                "Changes requested",
                "Your organization requested changes. Open the skill review for details.",
            ),
            "rejected": (
                "Rejected",
                "Your organization rejected this contribution. Open the skill review for details.",
            ),
            "declined": (
                "Not published",
                "Your contribution was declined. Open the skill review for details.",
            ),
            "invalidated": (
                "Review required",
                "This contribution needs a fresh review before it can be published.",
            ),
        }.get(
            publication_state,
            ("Publication needs review", str(result.get("detail") or
             "Open the skill review to check its current publication status.")),
        )
        server_detail = str(result.get("detail") or "").strip()
        if server_detail and server_detail not in detail:
            detail += "\n\nModeration detail: " + server_detail
        actions = []
        if publication_state not in _PUBLICATION_TERMINAL:
            actions.append(NativeAction("Check status", "publication.status"))
        if publication_state == "pending_moderation":
            actions.append(NativeAction("Decline", "publication.decline"))
        portal_url = _safe_url(result.get("portal_url"))
        if portal_url:
            actions.append(NativeAction("View details", url=portal_url))
        return NativeView(
            "Hermes Collective Wisdom", summary,
            [NativeItem(display_name, detail)],
            actions,
        )
    inspection = record.get("inspection")
    if record["state"] == "pending" and isinstance(inspection, dict):
        return _inspection_view(inspection, publication=True)
    hashes = plan.get("hashes") or {}
    files = plan.get("files") or []
    detail = str(plan.get("description") or "")
    detail += "\nPublication policy: " + str(plan.get("publication_mode") or "unavailable")
    detail += "\nContent hash: " + str(hashes.get("content") or "unavailable")
    detail += "\nDescription hash: " + str(hashes.get("author_description") or "unavailable")
    detail += "\nManifest hash: " + str(hashes.get("package_manifest") or "unavailable")
    if files:
        detail += "\nPackage files:\n" + "\n".join(
            f"- {item.get('path')} · {item.get('hash')}"
            for item in files if isinstance(item, dict)
        )
    draft_id = _safe_handoff_identifier(plan.get("draft_id"))
    detail += (
        "\n\nThis native card cannot safely edit arbitrary package files. To change files or "
        "the manifest before sharing, leave this card without confirming and use the "
        "complete private JSON workflow in a terminal:\n"
        f"hermes wisdom prepared inspect {draft_id} --json\n"
        "hermes wisdom prepared save <payload.json> --json\n"
        f"Then reopen /wisdom publish {draft_id}."
    )
    detail += "\n\nNothing changes until you use the confirmation control."
    if _has_checks(plan):
        detail += "\n\n" + _review_text(
            plan, bool(record.get("checks_expanded")), current=record["state"] == "pending",
        )
    actions = (
        [_checks_action(bool(record.get("checks_expanded")))] if _has_checks(plan) else []
    )
    if record["state"] == "pending":
        if files and all(isinstance(item.get("content_utf8"), str) for item in files):
            actions.append(NativeAction("View More Details", "inspect"))
        if isinstance(plan.get("review_deferral"), dict):
            actions.append(NativeAction("Maybe Later", "defer"))
        actions.append(NativeAction("Snooze Collective Wisdom", "sharing.settings"))
        actions.append(NativeAction("Share My Skill", "confirm", True))
    view = NativeView(
        "Hermes Collective Wisdom",
        "Deferred on this surface" if record["state"] == "deferred" else "Ready For Review",
        [NativeItem(display_name, detail)],
        actions,
        notice=str(plan.get("notice") or ""),
    )
    if (
        record["state"] == "pending"
        and str(record.get("platform") or "") in {"slack", "telegram"}
        and not _approval_is_lossless(record, view)
    ):
        draft_id = _safe_handoff_identifier(plan.get("draft_id"))
        return NativeView(
            "Hermes Collective Wisdom", "Complete review required in terminal",
            [NativeItem(
                display_name,
                "Native approval is disabled because this surface cannot render every reviewed "
                "publication fact without truncation. Nothing was uploaded or published.\n\n"
                "Review the complete immutable publication snapshot in a terminal:\n"
                f"hermes wisdom publication review {draft_id} --json\n\n"
                "Submit only the fresh receipt returned by that review:\n"
                "hermes wisdom publication submit <publication-receipt> --json",
            )],
            (
                [NativeAction("Maybe Later", "defer")]
                if isinstance(plan.get("review_deferral"), dict) else []
            ),
        )
    return view


def _update_view(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    record: dict[str, Any], *,
    checks_expanded: bool,
    assessment_expanded: bool,
) -> NativeView:
    plan = record["plan"]
    title = str(
        plan.get("editorial_name") or plan.get("slug") or plan.get("skill_id")
        or "Skill details"
    )
    if record["state"] == "completed":
        detail = (
            "Setup is queued. Prerequisites and verification must pass before the "
            "skill is ready."
        )
        actions = [NativeAction("Check setup", "setup.status")]
        actions.append(_assessment_action(assessment_expanded))
        if assessment_expanded:
            assessment = record.get("assessment") or {}
            detail = "Assessment before this operation:\n" + str(
                assessment.get("explanation") or "No saved assessment is available."
            )
            if _has_checks(plan):
                detail += "\n\n" + _review_text(plan, checks_expanded)
            actions.append(_checks_action(checks_expanded))
        return NativeView(
            title="Hermes Collective Wisdom",
            summary="Files installed" if record["operation"] == "install" else "Files updated",
            items=[NativeItem(title, detail)],
            actions=actions,
        )
    inspection = record.get("inspection")
    if record["state"] == "pending" and isinstance(inspection, dict):
        return _inspection_view(inspection, publication=False)
    detail = str(plan.get("editorial_description") or "")
    if plan.get("version"):
        detail += f"\nVersion: v{plan['version']}"
    detail += _update_policy_line(record["operation"], plan)
    compatibility = plan.get("compatibility") or {}
    if compatibility:
        detail += "\nCompatibility: " + str(
            compatibility.get("outcome") or "unavailable"
        )
    if plan.get("modified"):
        detail += "\nLocal changes require separate review."
        destination = (plan.get("preservation") or {}).get("destination")
        if destination:
            detail += "\nYour modified copy will be preserved at: " + str(destination)
    if plan.get("sensitive_expansion"):
        detail += "\nAdditional requirements require separate approval."
    detail += (
        "\nThis approval is no longer current. Review a fresh plan before continuing."
        if record["state"] in {"stale", "expired", "needs_review"}
        else "\nNothing changes until you use the confirmation control."
    )
    actions: list[NativeAction] = []
    if _has_checks(plan):
        detail += "\n\n" + _review_text(
            plan, checks_expanded, current=record["state"] == "pending",
        )
        actions.append(_checks_action(checks_expanded))
    if record["state"] in {"stale", "expired"}:
        actions.append(NativeAction("Recheck", "recheck"))
    elif record["state"] in {"applying", "needs_review"}:
        actions.append(NativeAction("Recover operation", "recover"))
    if record["state"] == "pending":
        labels = {
            "ack.preserve": "Preserve my modified copy as an unmanaged fork first.",
            "ack.sensitive": "I explicitly accept the new sensitive requirements.",
            "ack.partial": "I reviewed and accept the compatibility actions.",
            "defer": "Not Now", "inspect": "Review first",
            "confirm": "Install" if record["operation"] == "install" else "Update",
        }
        for action in _record_actions(record):
            actions.append(NativeAction(
                labels[action], action, primary=action == "confirm"
            ))
    summary = (
        "Deferred on this surface"
        if record["state"] == "deferred"
        else record["state"].replace("_", " ").capitalize()
    )
    return NativeView(
        title="Hermes Collective Wisdom",
        summary=summary,
        items=[NativeItem(title, detail)],
        actions=actions,
    )


def _setup_view(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    record: dict[str, Any], *, enforce_rendering: bool = True,
) -> NativeView:
    facts = record["plan"]
    step = facts["step"]
    result = record.get("result") or {}
    nested_setup = result.get("setup")
    setup = nested_setup if isinstance(nested_setup, dict) else result
    state = str(setup.get("state", record["state"]))
    requirement = facts.get("setup_requirement") or {}
    missing = bool(requirement) and facts.get("allowed") is False
    summary = {
        "pending": "Review setup step",
        "running": "Setup step running",
        "applying": "Checking setup progress",
        "unknown": "Setup outcome unknown",
        "passed": (
            "Prerequisite acknowledged"
            if step["phase"] == "prerequisite" else "Command completed"
        ),
        "failed": "Setup step failed",
        "stale": "Setup needs a fresh review",
        "expired": "Setup approval expired",
        "needs_review": "Setup needs attention",
        "blocked": "Terminal permission required",
        "abandoned": "Interrupted step cleared",
    }.get(state, "Setup progress")
    if state == "pending" and missing:
        summary = "Prerequisite missing"
    elif state == "passed" and requirement.get("status") == "present":
        summary = "Prerequisite checked"
    detail = str(facts["setup_instruction"])
    if facts.get("setup_explanation"):
        detail = str(facts["setup_explanation"]) + "\n\n" + detail
    if step.get("command"):
        detail += "\n\nProposed command (local terminal):\n" + str(step["command"])
    if state == "pending" and not missing:
        detail += (
            "\n\nOnly this step is authorized by confirming. "
            "Do not enter credentials in chat."
        )
    elif state == "unknown":
        detail += "\n\nThe command may have run. It will not be repeated automatically."
        if setup.get("recovery_review"):
            detail += (
                "\n\nBefore clearing this record, check that the command and its child "
                "processes have stopped and inspect any changes they made. Clearing "
                "does not undo changes, mark setup complete, or authorize another command."
            )
    elif state == "abandoned":
        detail += (
            "\n\nThe interrupted attempt remains unverified. Review a fresh step "
            "before continuing."
        )
    elif state == "blocked":
        detail += (
            "\n\nThis command did not run. Resolve terminal permissions, then request "
            "a fresh setup review."
        )
    elif state == "passed":
        detail += (
            "\n\nContinue setup inspection to check remaining prerequisites and verification."
        )
    actions: list[NativeAction] = []
    if record["state"] == "pending" and missing:
        actions = [
            NativeAction("Not Now", "defer"),
            NativeAction("Recheck", "recheck", True),
        ]
    elif record["state"] == "pending":
        label = "Confirm prerequisite" if step["phase"] == "prerequisite" else "Run this step"
        actions = [NativeAction("Not Now", "defer"), NativeAction(label, "confirm", True)]
    elif state == "unknown":
        actions = (
            [
                NativeAction("Back", "inspect"),
                NativeAction("Confirmed stopped; clear record", "setup.clear", True),
            ]
            if setup.get("recovery_review")
            else [
                NativeAction("Check progress", "inspect"),
                NativeAction("Review interruption", "setup.recover"),
            ]
        )
    elif record["state"] == "applying":
        actions = [NativeAction("Check progress", "inspect")]
    elif record["state"] in {"stale", "expired"} or state in {
        "abandoned", "blocked", "failed"
    }:
        actions = [NativeAction("Recheck", "recheck")]
    elif record["state"] == "completed":
        actions = [NativeAction("Check setup", "setup.status")]
    view = NativeView(
        title="Hermes Collective Wisdom",
        summary=summary,
        items=[NativeItem(f"{facts['slug']} · v{facts['version']}", detail)],
        actions=actions,
    )
    if (
        enforce_rendering
        and str(record.get("platform") or "") in {"slack", "telegram"}
        and record["state"] == "pending"
        and step["phase"] != "prerequisite"
        and not _approval_is_lossless(record, view)
    ):
        skill_id = shlex.quote(str(facts["skill_id"]))
        version = int(facts["version"])
        phase = shlex.quote(str(step["phase"]))
        index = int(step["index"])
        handoff = (
            "Native approval is disabled because this surface cannot render the complete "
            "setup guidance and exact proposed command without truncation. No command was "
            "authorized.\n\nReview the complete guidance in a terminal:\n"
            f"hermes wisdom installed-setup inspect {skill_id} --version {version} --json\n\n"
            "Then prepare your own exact command and review its complete receipt:\n"
            f"hermes wisdom installed-setup plan {skill_id} --version {version} "
            f"--phase {phase} --index {index} --command '<exact command>' --json\n\n"
            "Apply only after reviewing that complete receipt:\n"
            "hermes wisdom installed-setup apply <setup-receipt> --json"
        )
        return NativeView(
            title=view.title,
            summary="Complete review required in terminal",
            items=[NativeItem(view.items[0].title, handoff)],
            actions=[NativeAction("Not Now", "defer")],
        )
    return view


def _approval_is_lossless(record: dict[str, Any], view: NativeView) -> bool:
    """Use the actual platform renderer to prove the full approval view survives."""
    platform_name = str(record.get("platform") or "")
    if platform_name == "slack":
        expected_sections = [view.summary, *(
            (item.preamble + "\n\n" if item.preamble else "")
            + (f"*{item.title}*\n{item.detail}" if item.detail else f"*{item.title}*")
            for item in view.items
        )]
        escaped_expected = [
            value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for value in expected_sections if value
        ]
        return len(view.items) <= 5 and [
            slack_section_text(value) for value in expected_sections if value
        ] == escaped_expected
    if platform_name == "telegram":
        return telegram_view_text(view) == view.to_text()
    return False
