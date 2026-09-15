"""Bounded maintenance producer for owner-bound mediation candidates."""
# pylint: disable=too-few-public-methods,too-many-arguments,too-many-locals
# pylint: disable=too-many-instance-attributes
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

from .mediation_store import MediationStore
from .preferences import WisdomPreferences, suppression_key
from .weekly_queue import enqueue_weekly_review, weekly_policy_from_agent

_MAX_CANDIDATES = 8


def _feed_reference(event: dict[str, Any]) -> dict[str, Any] | None:
    if (
        event.get("category") not in {"new_skill", "update_available"}
        or type(event.get("version")) is not int
        or event["version"] < 1
    ):
        return None
    return {
        "kind": "skill",
        "event_id": str(event["event_id"]),
        "skill_id": str(event["skill_id"]),
        "version": int(event["version"]),
        "notification": {
            key: event.get(key)
            for key in (
                "category", "kind", "skill_id", "skill_name", "version", "occurred_at"
            )
        },
    }


def _candidate_reference(store: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve weekly evidence to one exact existing qualification event."""

    skill_id = str(item.get("local_skill_id") or "")
    content_hash = str(item.get("content_hash") or "")
    evidence: dict[str, Any] = (
        item["evidence"] if isinstance(item.get("evidence"), dict) else {}
    )
    signals = set(evidence.get("supporting_signals") or [])
    for event in store.local_events(kind="wisdom.candidate"):
        if (
            event.get("skill_id") == skill_id
            and event.get("content_hash") == content_hash
            and event.get("qualification") in signals
        ):
            return {
                "kind": "candidate", "event_id": str(event["id"]),
                "local_skill_id": skill_id, "content_hash": content_hash,
            }
    return None


class MaintenanceMediationProducer:
    """Apply current policy/preferences before persisting assessment work."""

    def __init__(
        self,
        service,
        consumption,
        owner: dict[str, str],
        *,
        skills_root: Path,
        config: dict[str, Any] | None = None,
        at: datetime | None = None,
        timezone_info: tuple[tzinfo, str] | None = None,
        authorized: Callable[[], bool] | None = None,
    ) -> None:
        self.service = service
        self.consumption = consumption
        self.owner = dict(owner)
        self.skills_root = Path(skills_root)
        self.config = config or {}
        self.at = at
        self.timezone_info = timezone_info
        self.authorized = authorized

    def _authorized(self) -> bool:
        return bool(
            self._store().get_metadata("qualification_owner") == self.owner
            and (self.authorized is None or self.authorized())
        )

    def _store(self):
        value = self.service.store
        return value() if callable(value) else value

    def produce(self, policy) -> dict[str, Any]:
        """Queue no more than eight references; never assess, deliver, or create consent."""

        mode = (self.config.get("notifications") or {}).get("delivery_mode", "agent")
        if mode != "agent":
            return {"queued": 0, "feed": 0, "weekly": 0, "more": False}
        org = self.owner["organization_id"]
        if policy.org_id != org or not self._authorized():
            return {"queued": 0, "feed": 0, "weekly": 0, "more": False}

        queue = MediationStore(self._store(), owner=self.owner)
        queued = queue.assessments(org)
        queued_feed_ids = {
            str(row["reference"]["event_id"])
            for row in queued
            if row["reference"].get("kind") == "skill"
        }
        queued_candidates = {
            (
                str(row["reference"]["local_skill_id"]),
                str(row["reference"]["content_hash"]),
            )
            for row in queued
            if row["reference"].get("kind") == "candidate"
            and row.get("state") != "deferred"
        }
        projected = self.consumption.notifications(
            mark_seen=False, exclude_event_ids=queued_feed_ids
        )
        feed = []
        for event in projected["events"]:
            reference = _feed_reference(event)
            if reference is None:
                continue
            installation = self._store().installation(
                reference["skill_id"], owner=self.owner
            )
            event_type = "update_available" if installation else "teammate_published"
            if getattr(policy.notification_defaults, event_type):
                feed.append(reference)

        weekly = enqueue_weekly_review(
            store=self._store(),
            skills_root=self.skills_root,
            policy=weekly_policy_from_agent(policy),
            authorized=self._authorized,
            at=self.at,
            timezone_info=self.timezone_info,
            dry_run=True,
        )
        local = [
            reference
            for item in weekly.get("candidates", [
            ])[: policy.max_recommendations_per_user_per_week]
            if (item["local_skill_id"], item["content_hash"]) not in queued_candidates
            if (reference := _candidate_reference(self._store(), item)) is not None
        ]
        references = [*feed, *local]
        if not references:
            return {
                "queued": 0,
                "feed": 0,
                "weekly": 0,
                "more": bool(projected.get("more")),
            }
        preferences = WisdomPreferences(self.service).check(org, references)
        if not preferences["available"] or preferences["muted"] or not self._authorized():
            return {
                "queued": 0, "feed": 0, "weekly": 0, "more": bool(references)
            }
        eligible = [
            reference
            for reference in references
            if not preferences["suppressed"].get(suppression_key(reference))
        ]
        selected = eligible[:_MAX_CANDIDATES]
        feed_count = weekly_count = 0
        for reference in selected:
            if reference["kind"] == "skill":
                event_key = "feed:" + reference["event_id"]
            else:
                event_key = (
                    f"candidate:{reference['local_skill_id']}:{reference['content_hash']}"
                )
            _identity, inserted = queue.enqueue_owned_if_new(org, event_key, reference)
            if inserted and reference["kind"] == "skill":
                feed_count += 1
            elif inserted:
                weekly_count += 1
        return {
            "queued": feed_count + weekly_count,
            "feed": feed_count,
            "weekly": weekly_count,
            "more": bool(projected.get("more")) or len(eligible) > len(selected),
        }
