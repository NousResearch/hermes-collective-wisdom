"""Bounded explicit maintenance for policy-authorized managed updates."""
# pylint: disable=too-few-public-methods,too-many-locals

from __future__ import annotations

import json
from typing import Any, Callable

from .errors import WisdomError
from .package import PackagePolicyError


class AutomaticUpdateMaintenance:
    """Enumerate owned installs and run only freshly authorized automatic work."""

    def __init__(self, store, reviewer, executor, owner: Callable[[], dict], *, limit=8):
        if not 1 <= limit <= 100:
            raise ValueError("automatic update limit must be between 1 and 100")
        self.store = store
        self.reviewer = reviewer
        self.executor = executor
        self.owner = owner
        self.limit = limit

    def _operation_id(self, receipt: str) -> str | None:
        metadata = self.store.get_metadata("update_receipt:" + receipt)
        value = metadata.get("operation_id") if isinstance(metadata, dict) else None
        return str(value) if value else None

    def _pending(self, owner: dict) -> dict[str, dict[str, Any]]:
        result = {}
        for row in self.store.pending_operations():
            if row.get("kind") != "update":
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("owner") == owner:
                result[str(row.get("entity_id"))] = {**row, "payload": payload}
        return result

    def _notice(self, owner: dict, operation_id: str | None):
        if not operation_id:
            return None
        return self.store.persist_automatic_update_notice(operation_id, owner)

    def _repair_notices(self, owner: dict) -> None:
        for operation in self.store.completed_automatic_updates(owner):
            self._notice(owner, str(operation["id"]))

    def _select(self, owner: dict, installations: list[dict]) -> list[dict]:
        """Select a durable rotating bounded window so later installs cannot starve."""

        cursor = self.store.get_metadata("automatic_update_cursor") or {}
        after = cursor.get("after") if cursor.get("owner") == owner else None
        start = 0
        if after is not None:
            for index, installation in enumerate(installations):
                if installation.get("skill_id") == after:
                    start = (index + 1) % len(installations)
                    break
        rotated = installations[start:] + installations[:start]
        selected = rotated[: self.limit]
        if selected:
            self.store.set_metadata(
                "automatic_update_cursor",
                {"owner": owner, "after": selected[-1]["skill_id"]},
            )
        return selected

    def run(self) -> dict[str, Any]:
        """Perform one explicit bounded pass; ambiguous work is never replayed blindly."""

        owner = self.owner()
        installations = [
            row for row in self.store.installations(owner=owner)
            if row.get("state") == "active"
        ]
        try:
            self._repair_notices(owner)
        except Exception:  # pylint: disable=broad-exception-caught
            # Notice repair is independent of update execution. A later explicit
            # maintenance pass retries from the completed operation journal.
            pass
        pending = self._pending(owner)
        results = []
        for installation in self._select(owner, installations):
            skill_id = str(installation["skill_id"])
            row = pending.get(skill_id)
            try:
                if row is not None:
                    payload = row["payload"]
                    if payload.get("authorization", {}).get("kind") != "automatic_policy":
                        results.append({
                            "skill_id": skill_id, "state": "needs_review",
                            "reason": "another managed update requires explicit review",
                            "operation_id": str(row["id"]),
                        })
                        continue
                    receipt = str(payload["receipt"])
                    outcome = self.executor.apply_automatic(receipt)
                else:
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
                    plan = self.reviewer.plan_automatic(skill_id)
                    receipt = str(plan["receipt"])
                    outcome = self.executor.apply_automatic(receipt)
                operation_id = self._operation_id(receipt)
                notice_id = None
                notice_error = None
                try:
                    notice_id = self._notice(owner, operation_id)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    notice_error = type(exc).__name__
                results.append({
                    "skill_id": skill_id,
                    "state": "updated" if outcome.get("updated") else "pending",
                    "operation_id": operation_id,
                    "result": outcome,
                    "notice_id": notice_id,
                    "notice_error": notice_error,
                })
            except (PackagePolicyError, WisdomError) as exc:
                results.append({
                    "skill_id": skill_id, "state": "needs_review", "reason": str(exc)
                })
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # A filesystem/database exception may follow a local effect. Record only
                # the durable journal identity; a later explicit pass re-enters by phase.
                current = self._pending(owner).get(skill_id)
                results.append({
                    "skill_id": skill_id,
                    "state": "pending" if current else "needs_review",
                    "operation_id": str(current["id"]) if current else None,
                    "reason": type(exc).__name__,
                })
        return {
            "results": results,
            "processed": len(results),
            "more": len(installations) > self.limit,
        }


__all__ = ["AutomaticUpdateMaintenance"]
