"""Bounded, schema-validated Gateway client for Wisdom discovery."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract import SystemSpecification
from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_IDENTIFIER_BYTES = 512
_MAX_CURSOR_BYTES = 4096
_MAX_TOKEN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class WisdomGatewayCredentials:
    """One host-owned credential snapshot used for a single request."""

    base_url: str
    access_token: str = field(repr=False)
    organization_id: str


@dataclass(frozen=True, slots=True)
class WisdomOrganizationContext:
    """Non-secret organization context projected from current host credentials."""

    organization_id: str


CredentialProvider = Callable[[], WisdomGatewayCredentials]


class WireModel(BaseModel):
    """Gateway response model that tolerates additive server fields."""

    model_config = ConfigDict(extra="allow", strict=True)


class SkillSummary(WireModel):
    """One organization-visible skill in a discovery page."""

    id: str = Field(min_length=1, max_length=512)
    slug: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=1, max_length=64)
    created_by_user_id: str = Field(min_length=1, max_length=512)
    latest_version: int | None = Field(default=None, gt=0)
    author_description: str | None
    verified_facts: dict[str, Any] | None
    explanation: str | None
    install_count: int = Field(ge=0)
    takedown_generation: int = Field(ge=0)
    system_spec: SystemSpecification | None
    security_check: dict[str, Any] | None = None
    professionalism_check: dict[str, Any] | None = None


class SkillList(WireModel):
    """Cursor page of organization-visible skills."""

    skills: list[SkillSummary] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=_MAX_CURSOR_BYTES)


class SkillDetail(WireModel):
    """Skill metadata and its authoritative version index."""

    skill: dict[str, Any]
    versions: list[dict[str, Any]] = Field(max_length=10_000)


class VersionDetail(WireModel):
    """Metadata for one immutable published version."""

    skill: dict[str, Any]
    version: dict[str, Any]


class WisdomClient:
    """Read-only Collective Wisdom Gateway client."""

    def __init__(
        self,
        credential_provider: CredentialProvider,
        expected_organization_id: str,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        session: requests.Session | None = None,
    ) -> None:
        if not callable(credential_provider):
            raise TypeError("credential_provider must be callable")
        self.expected_organization_id = _text(
            expected_organization_id, "expected_organization_id", _MAX_IDENTIFIER_BYTES
        )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        normalized_timeout = float(timeout)
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if not _is_plain_int(max_response_bytes) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._credential_provider = credential_provider
        self.timeout = normalized_timeout
        self.max_response_bytes = max_response_bytes
        self.session = session or requests.Session()

    def _credentials(self) -> WisdomGatewayCredentials:
        try:
            credentials = self._credential_provider()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise WisdomAuthError("Collective Wisdom credentials are unavailable") from exc
        if not isinstance(credentials, WisdomGatewayCredentials):
            raise WisdomAuthError("Collective Wisdom credentials are unavailable")
        base_url = credentials.base_url
        token = credentials.access_token
        org_id = credentials.organization_id
        try:
            parsed = urlsplit(base_url)
            token_bytes = token.encode("utf-8")
            org_bytes = org_id.encode("utf-8")
        except (AttributeError, UnicodeError, ValueError) as exc:
            raise WisdomAuthError("Collective Wisdom credentials are unavailable") from exc
        endpoint_valid = bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        credential_values_valid = bool(
            token_bytes
            and len(token_bytes) <= _MAX_TOKEN_BYTES
            and org_id.strip()
            and len(org_bytes) <= _MAX_IDENTIFIER_BYTES
            and org_id == self.expected_organization_id
        )
        if not endpoint_valid or not credential_values_valid:
            raise WisdomAuthError("Collective Wisdom credentials are unavailable")
        return WisdomGatewayCredentials(base_url.rstrip("/"), token, org_id)

    def organization_context(self) -> WisdomOrganizationContext:
        """Return the current non-secret host projection without network activity."""

        return WisdomOrganizationContext(self._credentials().organization_id)

    @staticmethod
    def _url(credentials: WisdomGatewayCredentials, path: str) -> str:
        return f"{credentials.base_url}/v1/sync/wisdom/{path.lstrip('/')}"

    def _read_response_body(self, response: requests.Response) -> Any:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise WisdomError(
                    "Collective Wisdom Gateway returned invalid response headers"
                ) from exc
            if declared_length < 0:
                raise WisdomError(
                    "Collective Wisdom Gateway returned invalid response headers"
                )
            if declared_length > self.max_response_bytes:
                raise WisdomError("Collective Wisdom Gateway response exceeded size limit")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > self.max_response_bytes:
                raise WisdomError("Collective Wisdom Gateway response exceeded size limit")
        if not body:
            return None
        content_type = response.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise WisdomError("Collective Wisdom Gateway returned a malformed response")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WisdomError(
                "Collective Wisdom Gateway returned a malformed response"
            ) from exc

    def _request(
        self,
        path: str,
        *,
        model: type[BaseModel],
        params: dict[str, Any] | None = None,
    ) -> Any:
        credentials = self._credentials()
        try:
            response = self.session.request(
                "GET",
                self._url(credentials, path),
                headers={
                    "Authorization": f"Bearer {credentials.access_token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            try:
                body = self._read_response_body(response)
            finally:
                response.close()
        except requests.Timeout as exc:
            raise WisdomError("Collective Wisdom Gateway timed out") from exc
        except requests.RequestException as exc:
            raise WisdomError("Collective Wisdom Gateway is unavailable") from exc
        error = body.get("error") if isinstance(body, dict) else None
        code = error if isinstance(error, str) else None
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

    def list_skills(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> SkillList:
        """Return one stable discovery page."""

        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = _text(cursor, "cursor", _MAX_CURSOR_BYTES)
        if limit is not None:
            if not _is_plain_int(limit) or not 1 <= limit <= 100:
                raise WisdomValidationError("limit must be an integer from 1 through 100")
            params["limit"] = str(limit)
        return self._request("skills", model=SkillList, params=params or None)

    def skill(self, skill_id: str) -> SkillDetail:
        """Return skill metadata and version history."""

        return self._request(
            f"skills/{_quoted_identifier(skill_id, 'skill_id')}", model=SkillDetail
        )

    def version(self, skill_id: str, version: int) -> VersionDetail:
        """Return metadata for one published version."""

        version = _version(version)
        return self._request(
            f"skills/{_quoted_identifier(skill_id, 'skill_id')}/versions/{version}",
            model=VersionDetail,
        )


def _text(value: object, name: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WisdomValidationError(f"{name} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise WisdomValidationError(f"{name} is not valid UTF-8") from exc
    if len(encoded) > max_bytes or "\x00" in value:
        raise WisdomValidationError(f"{name} is invalid")
    return value


def _quoted_identifier(value: object, name: str) -> str:
    return quote(_text(value, name, _MAX_IDENTIFIER_BYTES), safe="")


def _version(value: object) -> int:
    if not _is_plain_int(value) or not 1 <= value < 2_147_483_647:
        raise WisdomValidationError("version must be a positive integer")
    return value


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
