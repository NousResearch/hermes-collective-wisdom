"""Bounded, schema-validated Gateway client for Wisdom discovery."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)


class WireModel(BaseModel):
    """Gateway response model that tolerates additive server fields."""

    model_config = ConfigDict(extra="allow")


class SkillSummary(WireModel):
    """One organization-visible skill in a discovery page."""

    id: str
    slug: str
    state: str
    created_by_user_id: str
    latest_version: int | None
    author_description: str | None
    verified_facts: dict[str, Any] | None
    explanation: str | None
    install_count: int
    takedown_generation: int
    system_spec: dict[str, Any] | None
    security_check: dict[str, Any] | None = None
    professionalism_check: dict[str, Any] | None = None


class SkillList(WireModel):
    """Cursor page of organization-visible skills."""

    skills: list[SkillSummary]
    next_cursor: str | None


class SkillDetail(WireModel):
    """Skill metadata and its authoritative version index."""

    skill: dict[str, Any]
    versions: list[dict[str, Any]]


class VersionDetail(WireModel):
    """Metadata for one immutable published version."""

    skill: dict[str, Any]
    version: dict[str, Any]


class VersionContent(WireModel):
    """Encoded published content returned by the Gateway."""

    commit: str
    content_hash: str
    files: list[dict[str, Any]]


class WisdomClient:
    """Read-only Collective Wisdom Gateway client."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        organization_id: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.organization_id = organization_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base}/v1/sync/wisdom/{path.lstrip('/')}"

    def _request(
        self,
        path: str,
        *,
        model: type[BaseModel],
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self.session.request(
                "GET", self._url(path), params=params, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise WisdomError("Collective Wisdom Gateway timed out") from exc
        except requests.RequestException as exc:
            raise WisdomError("Collective Wisdom Gateway is unavailable") from exc
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError as exc:
                raise WisdomError(
                    "Collective Wisdom Gateway returned a malformed response"
                ) from exc
        code = body.get("error") if isinstance(body, dict) else None
        if response.status_code in (401, 403):
            raise WisdomAuthError(
                "Collective Wisdom is unavailable for this account",
                status=response.status_code,
                code=code,
            )
        if response.status_code == 404:
            raise WisdomNotFound(
                "Collective Wisdom item not found", status=404, code=code
            )
        if response.status_code == 409:
            raise WisdomConflict(
                str(code or "Collective Wisdom state changed; refresh and retry"),
                status=409,
                code=code,
            )
        if response.status_code == 422:
            raise WisdomValidationError(
                str(code or "Collective Wisdom rejected invalid content"),
                status=422,
                code=code,
            )
        if not 200 <= response.status_code < 300:
            raise WisdomError(
                f"Collective Wisdom Gateway failed ({response.status_code})",
                status=response.status_code,
                code=code,
            )
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise WisdomError(
                "Collective Wisdom Gateway response failed schema validation"
            ) from exc

    def list_skills(self, *, cursor: str | None = None) -> SkillList:
        """Return one stable discovery page."""

        return self._request(
            "skills",
            model=SkillList,
            params={"cursor": cursor} if cursor else None,
        )

    def skill(self, skill_id: str) -> SkillDetail:
        """Return skill metadata and version history."""

        return self._request(
            f"skills/{quote(skill_id, safe='')}", model=SkillDetail
        )

    def version(self, skill_id: str, version: int) -> VersionDetail:
        """Return metadata for one published version."""

        return self._request(
            f"skills/{quote(skill_id, safe='')}/versions/{version}",
            model=VersionDetail,
        )

    def content(
        self,
        skill_id: str,
        version: int,
        *,
        installation_id: str,
        takedown_generation: int,
    ) -> VersionContent:
        """Fetch install-bound encoded content without applying it locally."""

        return self._request(
            f"skills/{quote(skill_id, safe='')}/versions/{version}/content",
            model=VersionContent,
            params={
                "installation_id": installation_id,
                "takedown_generation": takedown_generation,
            },
        )

    def raw_copy(self, skill_id: str, version: int) -> VersionContent:
        """Fetch encoded immutable content without creating installation state."""

        return self._request(
            f"skills/{quote(skill_id, safe='')}/versions/{version}/raw",
            model=VersionContent,
        )
