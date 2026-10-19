"""Bounded direct automatic-update maintenance without notice queues.

cw/v1 auto-apply rule, per installed team skill with auto-update on:
- local edits → never applied silently; prepare a Combine and mark
  ``needs_look/edited`` (or ``needs_look/cant_combine`` when no aux model / error);
- Gateway publish-time verdict ``scan_status=pass`` → apply, even when access
  expanded, and record the new capabilities for the "Updated" notice;
- verdict ``flagged`` or missing/unreadable → do not apply; ``needs_look/flagged``.
Compatibility and local-scanner refusals collapse into the same one-click Update.
"""

from __future__ import annotations

from typing import Any, Callable

from .errors import WisdomError
from .package import PackagePolicyError


class AutomaticUpdateMaintenance:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Check and apply only currently authorized automatic updates."""

    def __init__(  # pylint: disable=too-many-arguments
        self, installed: Any, reviewer: Any, executor: Any,
        owner: Callable[[], dict], *, limit: int = 8,
        team_state: Any | None = None,
        prepare_combine: Callable[[str], Any] | None = None,
        tick: str | None = None,
    ) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("automatic update limit must be between 1 and 100")
        self.installed = installed
        self.reviewer = reviewer
        self.executor = executor
        self.owner = owner
        self.limit = limit
        self.team_state = team_state
        self.prepare_combine = prepare_combine
        self.tick = tick

    def _select(self, owner: dict, installations: list[dict]) -> list[dict]:
        rows = sorted(installations, key=lambda row: str(row.get("skill_id") or ""))
        after = self.installed.automatic_cursor(owner)
        if after is not None:
            rows = [row for row in rows if str(row.get("skill_id")) > str(after)] + [
                row for row in rows if str(row.get("skill_id")) <= str(after)
            ]
        selected = rows[:self.limit]
        if selected:
            self.installed.set_automatic_cursor(owner, str(selected[-1]["skill_id"]))
        return selected

    def _record(self, method: str, owner: dict, skill_id: str, **values: Any) -> None:
        if self.team_state is not None:
            getattr(self.team_state, method)(owner, skill_id, **values)

    def _team_record(self, owner: dict, skill_id: str) -> dict[str, Any]:
        return self.team_state.record(owner, skill_id) if self.team_state is not None else {}

    def _edited(self, owner: dict, skill_id: str, slug: Any, latest: int) -> dict[str, Any]:
        """Prepare a Combine at most once per update per tick; never in a hot loop."""
        record = self._team_record(owner, skill_id)
        prepared = record.get("prepared") if isinstance(record.get("prepared"), dict) else None
        if prepared and prepared.get("theirs_version") == latest:
            return {"skill_id": skill_id, "state": "needs_look", "reason": "edited"}
        already_tried = (
            record.get("version") == latest
            and record.get("reason") == "cant_combine"
            and record.get("combine_attempt_tick") is not None
            and record.get("combine_attempt_tick") == self.tick
        )
        if self.prepare_combine is None or already_tried:
            self._record("set_needs_look", owner, skill_id, slug=slug,
                         reason="cant_combine", version=latest)
            return {"skill_id": skill_id, "state": "needs_look", "reason": "cant_combine"}
        result = self.prepare_combine(skill_id)
        reason = ("edited" if result is not None and result.other_files != "conflict"
                  else "cant_combine")
        return {"skill_id": skill_id, "state": "needs_look", "reason": reason}

    def _one(self, owner: dict, skill_id: str) -> dict[str, Any]:  # pylint: disable=too-many-return-statements
        checked = self.reviewer.check(skill_id)
        state = checked.get("state")
        slug = checked.get("slug")
        if state in {"archived", "taken_down"}:
            self._record("set_no_longer_shared", owner, skill_id, slug=slug)
            return {"skill_id": skill_id, "state": "no_longer_shared"}
        if state != "update_available":
            return {"skill_id": skill_id, "state": state}
        raw_latest = checked.get("latest_version")
        latest = raw_latest if isinstance(raw_latest, int) and not isinstance(raw_latest, bool) else None
        if checked.get("effective_update_mode") == "MANUAL":
            # Auto-update off: the status model still shows the newer version.
            return {
                "skill_id": skill_id, "state": "needs_review",
                "reason": "manual update policy requires explicit review",
            }
        if latest is not None and self._team_record(owner, skill_id).get("skipped_version") == latest:
            return {"skill_id": skill_id, "state": "skipped", "version": latest}
        if checked.get("modified") is True:
            return self._edited(owner, skill_id, slug, latest)
        review = checked.get("latest_access_review")
        if not isinstance(review, dict) or review.get("scan_status") != "pass":
            # Fail-safe: never auto-apply without a readable pass verdict.
            capabilities = (review or {}).get("new_capabilities") or []
            self._record("set_needs_look", owner, skill_id, slug=slug, reason="flagged",
                         version=latest, new_capabilities=capabilities)
            return {"skill_id": skill_id, "state": "needs_look", "reason": "flagged"}
        try:
            receipt = str(self.reviewer.plan_automatic(
                skill_id, gateway_access_review=review,
            )["receipt"])
            outcome = self.executor.apply_automatic(receipt)
        except PackagePolicyError as exc:
            self._record("set_needs_look", owner, skill_id, slug=slug, reason="flagged",
                         version=latest, new_capabilities=review.get("new_capabilities") or [])
            return {"skill_id": skill_id, "state": "needs_look", "reason": "flagged",
                    "detail": str(exc)}
        if outcome.get("updated"):
            capabilities = review.get("new_capabilities") if review.get("access_expanded") else []
            self._record("set_updated", owner, skill_id, slug=slug, version=latest,
                         new_capabilities=capabilities or [])
        return {
            "skill_id": skill_id,
            "state": "updated" if outcome.get("updated") else "pending",
            "result": outcome,
        }

    def run(self) -> dict[str, Any]:
        """Apply a bounded batch; a failed skill does not block its neighbors."""
        owner = self.owner()
        active = [
            row for row in self.installed.installations(owner=owner)
            if row.get("state") == "active"
            and (row.get("target_provenance") or {}).get("kind") != "publisher_source"
        ]
        results: list[dict[str, Any]] = []
        for installation in self._select(owner, active):
            skill_id = str(installation["skill_id"])
            try:
                results.append(self._one(owner, skill_id))
            except (PackagePolicyError, WisdomError) as exc:
                results.append({"skill_id": skill_id, "state": "needs_review", "reason": str(exc)})
            except Exception as exc:  # pylint: disable=broad-exception-caught
                results.append({
                    "skill_id": skill_id, "state": "needs_review", "reason": type(exc).__name__
                })
        return {"results": results, "processed": len(results), "more": len(active) > self.limit}


__all__ = ["AutomaticUpdateMaintenance"]
