"""Owner-bound feed polling and terminal-safe notification projection.

This is the shared consumption kernel. It persists before presentation and
never sends a card, invokes a model, or applies an update on a passive read.
"""
# pylint: disable=too-many-locals,broad-exception-caught,too-many-branches

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError

_ALLOWED_CADENCES = frozenset({"immediate", "daily", "weekly", "off"})
_MAX_PAGES = 100
_MAX_ITEMS = 8


def _client_org(client: object) -> str | None:
    return getattr(client, "display_org_id", None) or getattr(
        client, "expected_organization_id", None
    )


def _client_owner(client: object) -> str | None:
    owner = getattr(client, "owner_user_id", None)
    identity = getattr(client, "identity", None)
    return owner or (identity.get("owner") if isinstance(identity, dict) else None)


def _client_route(client: object) -> str | None:
    route = getattr(client, "route_base_url", None)
    if isinstance(route, str) and route:
        return route
    route = getattr(client, "base_url", None)
    return route if isinstance(route, str) and route else None


class WisdomConsumption:
    """Durable feed and local activity projection over the existing store/client."""

    def __init__(self, store, client, *, config: dict[str, Any] | None = None) -> None:
        self.store = store
        self.client = client
        self.config = config or {}

    def _binding(self) -> dict[str, str]:
        binding = {
            "organization_id": self.store.active_org_id(),
            "installation_id": self.store.existing_installation_identity(),
            "owner_user_id": _client_owner(self.client),
            "base_url": _client_route(self.client),
        }
        if (
            not all(binding.values())
            or _client_org(self.client) != binding["organization_id"]
        ):
            raise WisdomAuthError(
                "Wisdom account is signed out; sign in and re-verify your team"
            )
        return binding  # type: ignore[return-value]

    def _owned_rows(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        """Read rows only when the persisted generation belongs to this exact account."""

        binding = self._binding()
        checkpoint = self.store.feed_checkpoint()
        if checkpoint["organization_id"] is None:
            return [], int(checkpoint["generation"])
        if any(checkpoint.get(key) != value for key, value in binding.items()):
            raise WisdomAuthError(
                "Wisdom feed belongs to a previous account; run maintenance to refresh it"
            )
        generation = int(checkpoint["generation"])
        try:
            rows = self.store.feed_events(
                owner=binding, expected_generation=generation, **filters
            )
        except WisdomConflict as exc:
            raise WisdomAuthError(
                "Wisdom feed changed while it was read; run maintenance and retry"
            ) from exc
        return rows, generation

    def poll_feed(self) -> dict[str, Any]:
        """Explicitly poll bounded pages with a generation-fenced cursor."""

        binding = self._binding()
        checkpoint = self.store.bind_feed_owner(**binding)
        cursor = checkpoint["cursor"]
        generation = int(checkpoint["generation"])
        installed = {
            str(item["skill_id"])
            for item in self.store.installations()
            if item.get("state", "active") == "active"
        }
        settings = self.config.get("notifications") or {}
        inserted = 0
        pages = 0
        while True:
            page = self.client.feed(
                cursor, installation_id=binding["installation_id"]
            )
            if self._binding() != binding:
                raise WisdomAuthError("Wisdom feed owner changed during refresh")
            events = [item.model_dump(mode="json") for item in page.events]
            cadences: dict[str, str] = {}
            for event in events:
                kind = str(event["kind"])
                if kind == "new" or (
                    kind == "updated" and str(event["skill_id"]) not in installed
                ):
                    cadence = str(settings.get("new_skills", "daily"))
                elif kind in {"updated", "installation_updated"}:
                    cadence = str(settings.get("installed_updates", "immediate"))
                else:
                    cadence = str(settings.get("decisions", "immediate"))
                cadences[str(event["event_id"])] = (
                    cadence if cadence in _ALLOWED_CADENCES else "immediate"
                )
            now = datetime.now(timezone.utc).isoformat()
            inserted += self.store.persist_feed_page(
                events,
                next_cursor=page.next_cursor,
                cadences=cadences,
                now=now,
                expected_generation=generation,
            )
            pages += 1
            cursor = page.next_cursor
            if not page.has_more:
                break
            if pages >= _MAX_PAGES:
                raise WisdomValidationError("Wisdom feed exceeded the bounded page limit")
        return {"inserted": inserted, "pages": pages, "cursor": cursor}

    def refresh_metadata(self) -> dict[str, Any]:
        """Explicitly cache at most eight exact-version presentation records."""

        rows, generation = self._owned_rows()
        binding = self._binding()
        pending = [
            row for row in rows
            if isinstance(row.get("version"), int)
            and not (row.get("payload") or {}).get("metadata_verified")
        ]
        attempted = enriched = 0
        for row in pending[:_MAX_ITEMS]:
            attempted += 1
            skill_id = str(row["skill_id"])
            version = int(row["version"])
            try:
                detail = self.client.skill(skill_id)
                if self._binding() != binding:
                    raise WisdomAuthError("Wisdom feed owner changed during metadata refresh")
                skill = getattr(detail, "skill", None)
                versions = getattr(detail, "versions", None)
                version_data = next(
                    (
                        item for item in versions
                        if isinstance(item, dict) and item.get("version") == version
                    ),
                    None,
                ) if isinstance(versions, list) else None
                exact_version = bool(
                    isinstance(skill, dict)
                    and skill.get("id") == skill_id
                    and isinstance(version_data, dict)
                )
                slug = skill.get("slug") if isinstance(skill, dict) else None
                publisher = (
                    version_data.get("published_by_user_id")
                    if isinstance(version_data, dict) else None
                )
                usable_identity = bool(
                    isinstance(slug, str) and slug
                    and isinstance(publisher, str) and publisher
                )
                if not exact_version or not usable_identity:
                    continue
                self.store.enrich_feed_event(
                    str(row["event_id"]),
                    {
                        "skill_name": slug,
                        "published_by_user_id": publisher,
                        "metadata_skill_id": skill_id,
                        "metadata_version": version,
                        "metadata_verified": True,
                    },
                    owner=binding,
                    expected_generation=generation,
                )
                enriched += 1
            except WisdomAuthError:
                raise
            except WisdomConflict as exc:
                raise WisdomAuthError(
                    "Wisdom feed changed during metadata refresh; run maintenance and retry"
                ) from exc
            except Exception:
                continue
        return {
            "attempted": attempted,
            "enriched": enriched,
            "more": len(pending) > enriched,
        }

    def _category(self, row: dict[str, Any]) -> str | None:
        kind = str(row.get("kind") or "")
        installed = self.store.installation(str(row.get("skill_id") or ""))
        if kind == "new" or (kind == "updated" and not installed):
            return "new_skill"
        if kind == "updated" and installed:
            return "update_available"
        if kind == "installation_updated":
            return "updated"
        if kind == "installed":
            return "installed"
        if kind in {"archived", "taken_down"} and installed:
            return "unavailable"
        return None

    def _project(self, row: dict[str, Any], *, advice: bool) -> dict[str, Any] | None:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        skill_id = str(row.get("skill_id") or "")
        version = row.get("version")
        category = self._category(row)
        if category is None:
            return None

        verified = bool(
            payload.get("metadata_verified") is True
            and payload.get("metadata_skill_id") == skill_id
            and payload.get("metadata_version") == version
        )
        publisher = payload.get("published_by_user_id") if verified else None
        if advice and isinstance(version, int) and not verified:
            return None
        if advice and publisher == _client_owner(self.client):
            return None
        slug = str(payload.get("skill_name") or payload.get("slug") or "")
        if not slug:
            return None
        return {
            "event_id": str(row["event_id"]),
            "source_event_ids": [str(row["event_id"])],
            "category": category,
            "kind": str(row.get("kind") or ""),
            "skill_id": skill_id,
            "skill_name": slug,
            "version": version,
            "occurred_at": row.get("created_at"),
        }

    def notifications(
        self, *, mark_seen: bool = False, exclude_event_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Read due advice without polling, model work, delivery, or mutation by default."""

        rows, _generation = self._owned_rows(unseen_only=True)
        excluded = exclude_event_ids or set()
        items = [
            item for row in rows
            if (item := self._project(row, advice=True))
            and item["event_id"] not in excluded
        ]
        selected = items[:_MAX_ITEMS]
        if mark_seen:
            self.store.mark_feed_local_seen([
                event_id for item in selected for event_id in item["source_event_ids"]
            ])
        pending = any(
            self._category(row) is not None
            and isinstance(row.get("version"), int)
            and not (row.get("payload") or {}).get("metadata_verified")
            for row in rows
        )
        return {"events": selected, "more": pending or len(items) > len(selected)}

    def activity(self) -> dict[str, Any]:
        """Private local activity retains self-publisher and non-advice feed history."""

        rows, _generation = self._owned_rows()
        items = [item for row in rows if (item := self._project(row, advice=False))]
        return {"events": items[-100:]}
