"""Owner-bound read-only Collective Wisdom discovery operations.

Extracted from ``hermes_wisdom/service.py`` at
``0924f7946b937c2ed02d083fc26752d577c54b3b`` (methods beginning at lines
2292, 2375, 2413, 2468, and 2500). Local installation and compatibility reads
from the original service are deliberately outside this kernel.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote, urlencode

from .errors import WisdomNotFound, WisdomValidationError


MAX_DISCOVERY_PAGES = 100


class Discovery:
    """Read the organization catalog while continuously checking ownership."""

    def __init__(
        self,
        client: object,
        check_owner: Callable[[], object],
        portal_base_url: str,
        organization_id: str,
    ) -> None:
        required_client_methods = ("list_skills", "skill", "version")
        if any(not callable(getattr(client, name, None)) for name in required_client_methods):
            raise TypeError("client must provide Wisdom discovery read methods")
        if not callable(check_owner):
            raise TypeError("check_owner must be callable")
        if not isinstance(portal_base_url, str) or not portal_base_url.strip():
            raise ValueError("portal_base_url must be a non-empty string")
        if not isinstance(organization_id, str) or not organization_id.strip():
            raise ValueError("organization_id must be a non-empty string")
        self.client = client
        self._check_owner = check_owner
        self.portal_base_url = portal_base_url.rstrip("/")
        self.organization_id = organization_id

    def _response(self, operation: Callable[[], Any]) -> Any:
        """Check the bound owner on both sides of every client operation."""

        self._check_owner()
        try:
            return operation()
        finally:
            self._check_owner()

    def list_skills(self) -> dict[str, Any]:
        """Return the first authoritative discovery page."""

        response = self._response(self.client.list_skills)
        return response.model_dump(mode="json")

    def search_skills(self, query: str | None = None) -> list[dict[str, Any]]:
        """Search authoritative discovery pages without inventing authority."""

        skills: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_DISCOVERY_PAGES):
            response = self._response(
                lambda current_cursor=cursor: self.client.list_skills(
                    cursor=current_cursor
                )
            )
            skills.extend(item.model_dump(mode="json") for item in response.skills)
            cursor = response.next_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                raise WisdomValidationError(
                    "Gateway returned a repeated Wisdom discovery cursor"
                )
            seen_cursors.add(cursor)
        else:
            raise WisdomValidationError("Wisdom discovery exceeded the page limit")

        needle = str(query or "").strip().casefold()
        if not needle:
            return skills
        return [
            item
            for item in skills
            if any(
                needle in str(item.get(field) or "").casefold()
                for field in (
                    "slug",
                    "author_description",
                    "created_by_user_id",
                    "created_by_display_name",
                    "author_name",
                    "author",
                )
            )
        ]

    def resolve_skill(self, reference: str) -> dict[str, Any]:
        """Resolve an opaque ID or exact slug while keeping misses opaque."""

        raw = _reference(reference)
        try:
            return self.show(raw)
        except WisdomNotFound as exc:
            matches = [
                item for item in self.search_skills(raw) if item.get("slug") == raw
            ]
            if len(matches) != 1:
                raise WisdomNotFound("Wisdom skill not found") from exc
            return self.show(str(matches[0]["id"]))

    def show(self, skill_id: str) -> dict[str, Any]:
        """Return public metadata plus the newest positive published version."""

        skill_id = _reference(skill_id)
        detail = self._response(lambda: self.client.skill(skill_id))
        result = detail.model_dump(mode="json")
        versions = [
            item["version"]
            for item in detail.versions
            if _is_published_positive_version(item)
        ]
        if versions:
            latest = self._response(
                lambda: self.client.version(skill_id, max(versions))
            )
            result["latest_version_detail"] = latest.model_dump(mode="json")
        result["portal_url"] = self._portal_skill_url(skill_id)
        return result

    def versions(self, skill_id: str) -> list[dict[str, Any]]:
        """Return the authoritative published version index."""

        skill_id = _reference(skill_id)
        detail = self._response(lambda: self.client.skill(skill_id))
        return list(detail.versions)

    def version(self, skill_id: str, version: int) -> dict[str, Any]:
        """Return one exact positive published version with identity checks."""
        skill_id = _reference(skill_id)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise WisdomValidationError("Wisdom version is invalid")
        client = cast(Any, self.client)
        detail = self._response(lambda: client.version(skill_id, version))
        result = detail.model_dump(mode="json")
        skill = result.get("skill") or {}
        version_row = result.get("version") or {}
        if skill.get("id") != skill_id or version_row.get("version") != version:
            raise WisdomValidationError("Wisdom version identity changed")
        return result

    def _portal_skill_url(self, skill_id: str) -> str:
        return (
            f"{self.portal_base_url}/wisdom/skills/{quote(skill_id, safe='')}?"
            f"{urlencode({'org_id': self.organization_id})}"
        )


def _reference(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise WisdomValidationError("Wisdom skill reference is invalid")
    return value


def _is_published_positive_version(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    version = item.get("version")
    state = item.get("state")
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > 0
        and state in (None, "published")
    )
