"""Best-effort end-turn notices for supported private surfaces."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from .errors import WisdomAuthError, WisdomNotFound
from .setup_lock import managed_operation_guard


MAX_NOTICE_BATCH = 8

def _publication_unavailable(skill, revision, skill_id, version, identity):
    exact = revision.get("version")
    if (skill.get("id") != skill_id or exact != version
            or revision.get("state", "published") != "published"):
        raise WisdomAuthError("Publication version is not approved")
    org = skill.get("organization_id") or skill.get("org_id") or skill.get("orgId")
    owner = (identity or {}).get("owner") or {}
    if org is not None and org != owner.get("organization_id"):
        raise WisdomAuthError("Publication organization changed")
    if skill.get("state") in {"archived", "taken_down"}:
        return True
    if skill.get("state") != "active":
        raise WisdomAuthError("Publication state is not approved")

    return False


def _publication(
    row: dict[str, Any], service: Any, identity: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Retain only approved exact-version metadata, never package bytes."""
    kind, skill_id, version = row.get("kind", "new"), row.get("skill_id"), row.get("version")
    if kind in {"archived", "taken_down"}:
        return {"skill_id": skill_id, "state": "unavailable"}
    if kind not in {"new", "updated", "restored", "installation_updated"}:
        return None
    # These audit actions can legitimately omit a version. They are not
    # publication proofs; later versioned rows remain consumable.
    if version is None and kind in {"restored", "installation_updated"}:
        return None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Publication feed version is invalid")
    try:
        facts = service.notice_version(skill_id, version)
    except WisdomNotFound as exc:
        # Only an authenticated, org-scoped exact revision read returning a real
        # Gateway 404 proves terminal unavailability. Local/opaque misses do not.
        if exc.status != 404:
            raise
        return {"skill_id": skill_id, "version": version, "state": "unavailable"}
    skill, revision = facts["skill"], facts["version"]
    if _publication_unavailable(skill, revision, skill_id, version, identity):
        return {"skill_id": skill_id, "state": "unavailable"}
    exact = revision["version"]

    def text(value: Any, max_chars: int) -> str:
        return value[:max_chars] if isinstance(value, str) else ""

    return {"skill_id": skill_id, "version": exact, "state": "published",
            "slug": text(skill.get("slug"), 200),
            "title": text(revision.get("title") or skill.get("title"), 200),
            "description": text(
                revision.get("author_description") or skill.get("author_description"), 800),
            "publisher": revision.get("published_by_user_id")
            if isinstance(revision.get("published_by_user_id"), str) else "",
            "team": text(skill.get("organization_name"), 200),
            "inspect_reference": f"{skill_id}@v{exact}"}


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


def _resolve_publishers(publications, service):
    unique = list(dict.fromkeys(
        (fact["skill_id"], fact["version"])
        for fact in publications if fact["state"] == "published"
    ))[:MAX_NOTICE_BATCH]
    references = [{"skill_id": skill_id, "version": version}
                  for skill_id, version in unique]
    if references and callable(getattr(service, "notice_publishers", None)):
        try:
            names = service.notice_publishers(references)
        except WisdomAuthError:
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            names = {}
        for fact in publications:
            resolved = names.get((fact["skill_id"], fact.get("version")))
            if (resolved and fact.get("publisher")
                    == resolved.get("published_by_user_id")):
                fact["publisher_display_name"] = resolved.get("publisher_display_name")
                fact["team_display_name"] = resolved.get("team_display_name")


class EndTurnNotices:  # pylint: disable=too-few-public-methods
    """Poll one page, mark attempts, render once, and forget delivery."""

    def __init__(self, state: Any, service_factory: Callable[[], Any], *, clock=time.time):
        self.state = state
        self.service_factory = service_factory
        self.clock = clock

    def run(  # pylint: disable=too-many-locals,too-many-statements
        self,
        *,
        renderer: Callable[[list[dict[str, Any]]], Any] | None,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Check one page and current candidates without turning misses into retries."""
        del conversation_history
        polled = False
        try:
            identity = self.state.identity()
            now = self.clock()
            with managed_operation_guard(self.state.root, "\0wisdom-notice-poll"):
                service = self.service_factory()
                checkpoint = self.state.notice_state()
                polled = True
                feed_cursor = (checkpoint["awareness_cursor"] if renderer is None
                               else checkpoint["cursor"])
                page = service.notice_feed(feed_cursor)
                events, cursor, more = list(page.events), page.next_cursor, page.has_more
                publications = [fact for row in events
                                if (fact := _publication(row, service, identity)) is not None]
                _resolve_publishers(publications, service)
                unavailable = {fact["skill_id"] for fact in publications
                               if fact["state"] == "unavailable"}
                if renderer is None:
                    self.state.finish_notice_poll(identity=identity, cursor=cursor,
                                                  has_more=more, keys=[], now=now,
                                                  publications=publications,
                                                  expected_cursor=feed_cursor, awareness=True)
                    return {"supported": False, "offered": 0, "polled": True, "more": bool(more)}
                candidates = list(service.notice_candidates())
                available_events = [
                    row for row in events
                    if row.get("category") in {"new_skill", "update_available"}
                    and isinstance(row.get("version"), int)
                    and not isinstance(row.get("version"), bool)
                    and row["version"] > 0
                    and row.get("skill_id") not in unavailable
                    and _key(row) not in checkpoint["offered"]
                ]
                available_candidates = [
                    row for row in candidates if _key(row) not in checkpoint["offered"]
                ]
                policy = service.notice_policy()
                # #49 will supply the grounded recipient match for useful_only.
                # Until then neither missing nor unknown policy may promote a feed row.
                promotion = policy.get("recommendation_mode")
                policy_allowed = [
                    row for row in [*available_events, *available_candidates]
                    if policy.get(str(row.get("category"))) is True
                    and (row.get("category") == "skill_ready_to_share"
                         or promotion == "promote_all")
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
                publisher_facts = {(fact["skill_id"], fact.get("version")): fact
                                   for fact in publications if fact["state"] == "published"}
                allowed_events = [
                    {**row, "publisher_fact": publisher_facts.get(
                        (row["skill_id"], row["version"]), {})}
                    for row in allowed_events
                ]
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
                    keys=[_key(row) for row in rows], now=now, publications=publications,
                    expected_cursor=checkpoint["cursor"],
                    awareness_cursor=page.next_cursor,
                ))
                if self.state.identity() != identity:
                    raise WisdomAuthError("Collective Wisdom notice owner changed")
                offers = [row for row in [*allowed_events, *allowed_candidates]
                          if _key(row) in selected]
                if offers and renderer is not None:
                    renderer(offers)
                return {
                    "supported": renderer is not None,
                    "offered": len(offers),
                    "polled": polled,
                    "more": bool(more),
                }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Disposable notices must never fail the owning turn.
            return {
                "supported": renderer is not None,
                "offered": 0,
                "polled": polled,
                "error": type(exc).__name__,
                **({"more": True} if polled else {}),
            }
