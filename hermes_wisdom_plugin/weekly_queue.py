"""Deterministic local weekly candidate queue; it never delivers a user message."""
# pylint: disable=too-many-arguments,too-many-boolean-expressions,too-many-branches
# pylint: disable=too-many-locals,too-many-statements,unidiomatic-typecheck

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

from .editorial import load_skill_editorial_metadata
from .qualification import UnsafeSnapshotError, _host_timezone, snapshot_tree
from .store import WisdomStore

MAX_REVIEW_CANDIDATES = 100
MAX_EVIDENCE_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class WeeklyPolicy:
    """Authoritative policy snapshot supplied by an explicit owning workflow."""

    enabled: bool = True
    window_days: int = 7
    min_aggregate_count: int = 3
    max_candidates: int = 3
    consecutive_day_usage_counts: bool = True
    repeated_edits_count: bool = True
    notification_enabled: bool = True

    def __post_init__(self) -> None:
        if self.window_days < 1 or self.min_aggregate_count < 1:
            raise ValueError("invalid weekly evidence policy")
        if not 0 <= self.max_candidates <= MAX_REVIEW_CANDIDATES:
            raise ValueError("invalid weekly candidate cap")


def weekly_policy_from_agent(policy) -> WeeklyPolicy:
    """Project the authoritative Gateway policy onto local evidence controls."""

    return WeeklyPolicy(
        enabled=True,
        window_days=policy.usage_evidence_window_days,
        min_aggregate_count=policy.min_aggregate_invocations,
        max_candidates=policy.max_recommendations_per_user_per_week,
        consecutive_day_usage_counts=policy.consecutive_day_usage_counts,
        repeated_edits_count=policy.repeated_edits_count,
        notification_enabled=policy.notification_defaults.skill_ready_to_share,
    )


def _week_key(now: datetime) -> str:
    day = now.astimezone(timezone.utc).date()
    return f"weekly:{day - timedelta(days=day.weekday())}"


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("weekly timestamps must include a timezone")
    return current.astimezone(timezone.utc)


def _handled(
    db: Any,
    organization_id: str,
    skill_id: str,
    content_hash: str,
    *,
    now: float,
) -> bool:
    dismissed = db.execute(
        "SELECT 1 FROM candidate WHERE skill_id=? AND content_hash=? AND state='dismissed'",
        (skill_id, content_hash),
    ).fetchone()
    if dismissed:
        return True
    drafted = db.execute(
        "SELECT 1 FROM local_draft WHERE skill_id=? AND source_hash=? LIMIT 1",
        (skill_id, content_hash),
    ).fetchone()
    if drafted:
        return True
    rows = db.execute(
        "SELECT a.id,a.state,d.suppress_until FROM wisdom_assessment a "
        "LEFT JOIN wisdom_candidate_defer d ON d.assessment_id=a.id "
        "WHERE a.organization_id=? AND json_extract(a.reference_json,'$.kind')='candidate' "
        "AND json_extract(a.reference_json,'$.local_skill_id')=? "
        "AND json_extract(a.reference_json,'$.content_hash')=? "
        "AND a.state!='retired'",
        (organization_id, skill_id, content_hash),
    ).fetchall()
    for row in rows:
        if (
            row["state"] == "delivered"
            and row["suppress_until"] is not None
            and float(row["suppress_until"]) <= now
        ):
            continue
        return True
    return False


def _signals(store: WisdomStore, policy: WeeklyPolicy) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    organization_id = store.active_org_id()
    with store.transaction() as db:
        rows = db.execute(
            "SELECT skill_id,qualification FROM local_event "
            "WHERE kind='wisdom.candidate' AND organization_id=?",
            (organization_id,),
        ).fetchall()
    for event in rows:
        qualification = str(event["qualification"] or "")
        if qualification == "high_usage" and not policy.consecutive_day_usage_counts:
            continue
        if qualification == "refinement" and not policy.repeated_edits_count:
            continue
        if qualification not in {"high_usage", "refinement"}:
            continue
        result.setdefault(str(event["skill_id"]), set()).add(qualification)
    return result


def _evidence(
    store: WisdomStore,
    *,
    skills_root: Path,
    policy: WeeklyPolicy,
    current: datetime,
    timezone_info: tuple[tzinfo, str],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    profile_timezone, timezone_name = timezone_info
    today = current.astimezone(profile_timezone).date()
    start = today - timedelta(days=policy.window_days - 1)
    root = Path(skills_root).resolve()
    signals = _signals(store, policy)
    excluded: dict[str, list[str]] = {
        "changed": [], "dismissed": [], "previously_handled": [],
        "below_min_count": [], "ineligible": [], "review_batch_limit": [],
    }
    candidates: list[dict[str, Any]] = []
    organization_id = store.active_org_id()
    with store.transaction() as db:
        rows = db.execute(
            "SELECT s.id,s.canonical_path,s.current_hash,u.day_local,u.use_count "
            "FROM usage_day u JOIN local_skill s ON s.id=u.skill_id "
            "WHERE s.deleted_at IS NULL AND s.source_kind='local' "
            "AND u.timezone_name=? AND u.day_local>=? AND u.day_local<=? "
            "ORDER BY s.id,u.day_local",
            (timezone_name, start.isoformat(), today.isoformat()),
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(str(row["id"]), {
                "path": str(row["canonical_path"]),
                "current_hash": str(row["current_hash"] or ""), "days": {},
            })
            item["days"][str(row["day_local"])] = int(row["use_count"])
        for skill_id, item in grouped.items():
            path = Path(item["path"])
            name = path.name
            try:
                relative = path.resolve().relative_to(root)
                eligible = bool(
                    relative.parts and relative.parts[0]
                    not in {"_org", "_wisdom", ".archive", ".hub"}
                    and path.is_dir() and not path.is_symlink()
                )
            except (OSError, RuntimeError, ValueError):
                eligible = False
            if not eligible or skill_id not in signals:
                excluded["ineligible"].append(name)
                continue
            usage = item["days"]
            total = sum(usage.values())
            if total < policy.min_aggregate_count:
                excluded["below_min_count"].append(name)
                continue
            try:
                content_hash, _tree = snapshot_tree(path)
            except UnsafeSnapshotError:
                excluded["ineligible"].append(name)
                continue
            if content_hash != item["current_hash"]:
                excluded["changed"].append(name)
                continue
            dismissed = db.execute(
                "SELECT 1 FROM candidate WHERE skill_id=? AND content_hash=? AND state='dismissed'",
                (skill_id, content_hash),
            ).fetchone()
            if dismissed:
                excluded["dismissed"].append(name)
                continue
            if organization_id and _handled(
                db, organization_id, skill_id, content_hash, now=current.timestamp()
            ):
                excluded["previously_handled"].append(name)
                continue
            editorial = load_skill_editorial_metadata(path)
            candidates.append({
                "local_skill_id": skill_id,
                "content_hash": content_hash,
                "skill_name": name,
                "evidence": {
                    "skill_name": skill_id,
                    "display_name": name,
                    "description": editorial["editorial_description"],
                    "content_hash": content_hash,
                    "window_days": policy.window_days,
                    "invocation_count": total,
                    "days_used": len(usage),
                    "last_used_day": max(usage) if usage else None,
                    "usage_by_day": dict(sorted(usage.items())),
                    "supporting_signals": sorted(signals[skill_id]),
                },
            })
    candidates.sort(key=lambda item: (
        -item["evidence"]["invocation_count"],
        -item["evidence"]["days_used"], item["skill_name"],
    ))
    if len(candidates) > MAX_REVIEW_CANDIDATES:
        for item in candidates[MAX_REVIEW_CANDIDATES:]:
            excluded["review_batch_limit"].append(item["skill_name"])
        candidates = candidates[:MAX_REVIEW_CANDIDATES]
    return candidates, {key: sorted(values) for key, values in excluded.items()}


def enqueue_weekly_review(
    *,
    store: WisdomStore,
    skills_root: Path,
    policy: WeeklyPolicy,
    authorized: Callable[[], bool],
    at: datetime | None = None,
    timezone_info: tuple[tzinfo, str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Queue one UTC-week review; dry runs are evidence-only and write nothing."""
    if not authorized() or not store.active_org_id():
        return {"queued": False, "skipped_reason": "not_entitled"}
    if not policy.enabled or not policy.max_candidates or not policy.notification_enabled:
        return {"queued": False, "skipped_reason": "organization_notifications_disabled"}
    current = _now(at)
    organization_id = store.active_org_id()
    if not organization_id or not authorized():
        return {"queued": False, "skipped_reason": "not_entitled"}
    candidates, excluded = _evidence(
        store, skills_root=skills_root, policy=policy, current=current,
        timezone_info=timezone_info or _host_timezone(),
    )
    reference = {
        "kind": "weekly_review", "candidates": candidates,
        "window": {
            "start": (current - timedelta(days=policy.window_days - 1)).date().isoformat(),
            "end": current.date().isoformat(), "days": policy.window_days,
        },
        "excluded": excluded, "max_candidates": policy.max_candidates,
    }
    if len(json.dumps(reference, sort_keys=True).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise ValueError("weekly evidence exceeds the bounded review limit")
    if dry_run:
        return {"queued": False, "dry_run": True, **reference}
    identity = store.enqueue_weekly_assessment(
        organization_id, _week_key(current), reference
    )
    existing = store.weekly_assessment(identity)
    return {
        "queued": bool(existing and existing["state"] == "pending"),
        "assessment_id": identity, "considered": len(candidates),
    }


def process_weekly_review(
    *,
    store: WisdomStore,
    assessment_id: str,
    session_id: str,
    runtime: dict[str, Any],
    reviewer: Callable[[dict[str, Any]], list[dict[str, Any]]],
    authorized: Callable[[], bool],
    human_context: bool,
) -> dict[str, Any]:
    """Run an explicitly authorized owning-session review; never deliver it."""
    if not human_context:
        return {"processed": False, "skipped_reason": "human_context_required"}
    if not authorized():
        return {"processed": False, "skipped_reason": "not_entitled"}
    job = store.weekly_assessment(assessment_id)
    if not job or job["state"] != "pending" or not session_id:
        return {"processed": False, "skipped_reason": "unavailable"}
    organization_id = store.active_org_id()
    if not organization_id or job["organization_id"] != organization_id:
        return {"processed": False, "skipped_reason": "not_entitled"}
    candidates = job["reference"]["candidates"]
    max_candidates = min(MAX_REVIEW_CANDIDATES, int(job["reference"]["max_candidates"]))
    token = uuid.uuid4().hex
    now = datetime.now(timezone.utc).timestamp()
    with store.transaction() as db:
        claimed = db.execute(
            "UPDATE wisdom_assessment SET state='assessing',owner_session=?,updated_at=? "
            "WHERE id=? AND organization_id=? AND state='pending'",
            (session_id, now, assessment_id, organization_id),
        ).rowcount
    if not claimed:
        return {"processed": False, "skipped_reason": "unavailable"}
    try:
        selected = reviewer({
            "window": job["reference"]["window"],
            "max_candidates": max_candidates,
            "candidates": candidates,
        })
        if not isinstance(selected, list):
            raise ValueError("weekly reviewer returned an invalid selection")
        if len(selected) > max_candidates:
            raise ValueError("weekly model exceeded the recommendation cap")
        available = {
            (item["local_skill_id"], item["content_hash"]): item for item in candidates
        }
        normalized = []
        for choice in selected:
            if not isinstance(choice, dict):
                raise ValueError("weekly model invented or changed a candidate")
            key = (choice.get("local_skill_id", choice.get("skill_id")), choice.get("content_hash"))
            source = available.get(key)
            if source is None:
                raise ValueError("weekly model invented or changed a candidate")
            normalized.append((source, choice))
        provenance = {
            key: str(runtime.get(key) or "")[:128]
            for key in ("provider", "model")
        }
        result = []
        with store.transaction() as db:
            active = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            state = db.execute(
                "SELECT state,owner_session FROM wisdom_assessment WHERE id=?",
                (assessment_id,),
            ).fetchone()
            if not authorized() or not active or active[0] != organization_id or not state \
                    or state["state"] != "assessing" or state["owner_session"] != session_id:
                raise ValueError("weekly review ownership changed")
            for source, choice in normalized:
                local = db.execute(
                    "SELECT canonical_path,current_hash FROM local_skill "
                    "WHERE id=? AND deleted_at IS NULL AND source_kind='local'",
                    (source["local_skill_id"],),
                ).fetchone()
                try:
                    snapshot_matches = bool(
                        local and snapshot_tree(Path(local["canonical_path"]))[0]
                        == source["content_hash"]
                    )
                except UnsafeSnapshotError:
                    snapshot_matches = False
                if not local or local["current_hash"] != source["content_hash"] \
                        or not snapshot_matches or _handled(
                            db, organization_id, source["local_skill_id"],
                            source["content_hash"], now=now
                        ):
                    continue
                event_id = store.emit_local_event(
                    kind="wisdom.candidate", skill_id=source["local_skill_id"],
                    content_hash=source["content_hash"], session_id=session_id,
                    task_id=None, qualification="weekly_usage", payload={
                        "skill_name": source["skill_name"],
                        "editorial_name": str(choice.get("editorial_name") or source["skill_name"]),
                        "editorial_description": str(choice.get("editorial_description") or ""),
                        "agent_led_weekly": True, "qualification": "weekly_usage",
                        "local_reasons": source["evidence"], "consent_required": True,
                        "networked": False,
                    }, _db=db,
                )
                if not event_id:
                    continue
                child_id = token + uuid.uuid4().hex
                reference = {
                    "kind": "candidate", "event_id": event_id,
                    "local_skill_id": source["local_skill_id"],
                    "content_hash": source["content_hash"],
                    "weekly_review_id": assessment_id,
                }
                advice = {"provenance": provenance, "reviewed": True}
                db.execute(
                    "INSERT INTO wisdom_assessment("
                    "id,organization_id,event_key,origin_session,reference_json,state,"
                    "owner_session,advice_json,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'ready',?,?,?,?)",
                    (child_id, organization_id, f"candidate:{event_id}", session_id,
                     json.dumps(reference, sort_keys=True), session_id,
                     json.dumps(advice, sort_keys=True), now, now),
                )
                result.append({
                    "assessment_id": child_id,
                    "local_skill_id": source["local_skill_id"],
                    "content_hash": source["content_hash"], "provenance": provenance,
                })
            db.execute(
                "UPDATE wisdom_assessment SET state='reviewed',advice_json=?,updated_at=? "
                "WHERE id=? AND state='assessing' AND owner_session=?",
                (json.dumps({"selected": result}, sort_keys=True), now,
                 assessment_id, session_id),
            )
        return {"processed": True, "selected": result, "delivered": False}
    except Exception:
        with store.transaction() as db:
            db.execute(
                "UPDATE wisdom_assessment SET state='pending',owner_session=NULL,updated_at=? "
                "WHERE id=? AND state='assessing' AND owner_session=?",
                (datetime.now(timezone.utc).timestamp(), assessment_id, session_id),
            )
        raise


def record_delivery_state(
    store: WisdomStore, assessment_id: str, state: str
) -> None:
    """Record an actual downstream delivery result without inferring one."""
    if state not in {"delivered", "delivery_uncertain"}:
        raise ValueError("invalid delivery state")
    with store.transaction() as db:
        changed = db.execute(
            "UPDATE wisdom_assessment SET state=?,updated_at=? "
            "WHERE id=? AND state='ready'",
            (state, datetime.now(timezone.utc).timestamp(), assessment_id),
        ).rowcount
    if not changed:
        raise ValueError("candidate assessment is not ready for delivery result")


def defer_candidate(
    store: WisdomStore,
    assessment_id: str,
    *,
    days: int,
    at: datetime | None = None,
) -> None:
    """Persist Not Now suppression; it is neither consent nor rejection."""
    if type(days) is not int or days not in {3, 30, 60}:
        raise ValueError("invalid Not Now suppression duration")
    current = _now(at).timestamp()
    with store.transaction() as db:
        row = db.execute(
            "SELECT state FROM wisdom_assessment WHERE id=?", (assessment_id,)
        ).fetchone()
        if not row or row["state"] not in {"delivered", "delivery_uncertain"}:
            raise ValueError("candidate assessment has no delivery result")
        db.execute(
            "INSERT INTO wisdom_candidate_defer VALUES(?,?,?) "
            "ON CONFLICT(assessment_id) DO UPDATE SET "
            "suppress_until=excluded.suppress_until,deferred_at=excluded.deferred_at",
            (assessment_id, current + days * 86400, current),
        )
