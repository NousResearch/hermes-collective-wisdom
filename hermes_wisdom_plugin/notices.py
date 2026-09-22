"""Best-effort end-turn notices for supported private surfaces."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from .errors import WisdomAuthError
from .setup_lock import managed_operation_guard


MAX_NOTICE_BATCH = 8


def _key(item: dict[str, Any]) -> str:
    if item["category"] == "skill_ready_to_share":
        return "candidate:" + str(item["local_skill_id"]) + ":" + str(item["content_hash"])
    return "feed:" + str(item["event_id"])


def suppression_key(reference: dict[str, Any]) -> str:
    """Use the Gateway suppression identity, shared by reads and explicit deferrals."""
    if reference["kind"] == "candidate":
        identity = ["candidate", reference["content_hash"]]
    else:
        identity = ["skill", reference["skill_id"], reference["version"]]
    return "sha256:" + hashlib.sha256(json.dumps(
        ["wisdom-suppression-v1", *identity], separators=(",", ":")
    ).encode()).hexdigest()


class EndTurnNotices:  # pylint: disable=too-few-public-methods
    """Poll one page, mark attempts, render once, and forget delivery."""

    def __init__(self, state: Any, service_factory: Callable[[], Any], *, clock=time.time):
        self.state = state
        self.service_factory = service_factory
        self.clock = clock

    def run(  # pylint: disable=too-many-locals
        self,
        *,
        renderer: Callable[[list[dict[str, Any]]], Any] | None,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Check one page and current candidates without turning misses into retries."""
        del conversation_history
        if renderer is None:
            return {"supported": False, "offered": 0, "polled": False}
        polled = False
        try:
            identity = self.state.identity()
            now = self.clock()
            with managed_operation_guard(self.state.root, "\0wisdom-notice-poll"):
                service = self.service_factory()
                checkpoint = self.state.notice_state()
                polled = True
                page = service.notice_feed(checkpoint["cursor"])
                events, cursor, more = list(page.events), page.next_cursor, page.has_more
                candidates = list(service.notice_candidates())
                available_events = [
                    row for row in events if _key(row) not in checkpoint["offered"]
                ]
                available_candidates = [
                    row for row in candidates if _key(row) not in checkpoint["offered"]
                ]
                policy = service.notice_policy()
                policy_allowed = [
                    row for row in [*available_events, *available_candidates]
                    if policy.get(str(row.get("category"))) is True
                ]
                references = [
                    (
                        {"kind": "skill", "skill_id": row["skill_id"], "version": row["version"],
                         "notice_key": _key(row)}
                        if row["category"] != "skill_ready_to_share" else
                        {"kind": "candidate", "content_hash": row["content_hash"],
                         "notice_key": _key(row)}
                    )
                    for row in policy_allowed
                ]
                preferences = service.notice_preferences(references)
                allowed = [
                    row for row in policy_allowed
                    if not preferences.get("muted")
                    and not (preferences.get("suppressed") or {}).get(_key(row))
                ]
                allowed_events = [row for row in allowed if row in available_events]
                allowed_candidates = [row for row in allowed if row in available_candidates]
                event_limit = MAX_NOTICE_BATCH - bool(allowed_candidates)
                rows = allowed_events[:event_limit]
                rows.extend(allowed_candidates[:MAX_NOTICE_BATCH - len(rows)])
                selected_keys = {_key(row) for row in rows}
                deferred_feed = any(_key(row) not in selected_keys for row in allowed_events)
                if deferred_feed:
                    cursor, more = checkpoint["cursor"], True
                selected = set(self.state.finish_notice_poll(
                    identity=identity, cursor=cursor, has_more=more,
                    keys=[_key(row) for row in rows], now=now,
                ))
                if self.state.identity() != identity:
                    raise WisdomAuthError("Collective Wisdom notice owner changed")
                offers = [row for row in allowed if _key(row) in selected]
                if offers:
                    renderer(offers)
                return {
                    "supported": True,
                    "offered": len(offers),
                    "polled": polled,
                    "more": bool(more),
                }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Disposable notices must never fail the owning turn.
            return {
                "supported": True,
                "offered": 0,
                "polled": polled,
                "error": type(exc).__name__,
            }
