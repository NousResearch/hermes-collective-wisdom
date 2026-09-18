"""Bounded direct automatic-update maintenance without notice queues."""

from __future__ import annotations

from typing import Any, Callable

from .errors import WisdomError
from .package import PackagePolicyError


class AutomaticUpdateMaintenance:  # pylint: disable=too-few-public-methods
    """Check and apply only currently authorized automatic updates."""

    def __init__(self, installed: Any, reviewer: Any, executor: Any,
                 owner: Callable[[], dict], *, limit: int = 8) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("automatic update limit must be between 1 and 100")
        self.installed = installed
        self.reviewer = reviewer
        self.executor = executor
        self.owner = owner
        self.limit = limit

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

    def run(self) -> dict[str, Any]:
        """Apply a bounded batch; a failed skill does not block its neighbors."""
        owner = self.owner()
        active = [
            row for row in self.installed.installations(owner=owner)
            if row.get("state") == "active"
        ]
        results: list[dict[str, Any]] = []
        for installation in self._select(owner, active):
            skill_id = str(installation["skill_id"])
            try:
                checked = self.reviewer.check(skill_id)
                if checked.get("state") != "update_available":
                    results.append({"skill_id": skill_id, "state": checked.get("state")})
                    continue
                if checked.get("effective_update_mode") == "MANUAL":
                    results.append({
                        "skill_id": skill_id, "state": "needs_review",
                        "reason": "manual update policy requires explicit review",
                    })
                    continue
                receipt = str(self.reviewer.plan_automatic(skill_id)["receipt"])
                outcome = self.executor.apply_automatic(receipt)
                results.append({
                    "skill_id": skill_id,
                    "state": "updated" if outcome.get("updated") else "pending",
                    "result": outcome,
                })
            except (PackagePolicyError, WisdomError) as exc:
                results.append({"skill_id": skill_id, "state": "needs_review", "reason": str(exc)})
            except Exception as exc:  # pylint: disable=broad-exception-caught
                results.append({
                    "skill_id": skill_id, "state": "needs_review", "reason": type(exc).__name__
                })
        return {"results": results, "processed": len(results), "more": len(active) > self.limit}


__all__ = ["AutomaticUpdateMaintenance"]
