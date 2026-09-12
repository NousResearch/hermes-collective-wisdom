"""Bounded, schema-validated Gateway client for Wisdom discovery."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract import SystemSpecification, parse_manifest_bytes
from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)
from .package import PackagePolicyError, verify_content_files
from .wire import wire_address


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
    """Non-secret organization binding configured for this client."""

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


class VersionContentFile(WireModel):
    """One exact blob in a published version response."""

    path: str = Field(min_length=1, max_length=1024)
    mode: Literal["file", "exec"]
    hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    content_base64: str = Field(max_length=349_528)


class VersionContent(WireModel):
    """Bounded, exact published package returned by the Gateway."""

    commit: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    files: list[VersionContentFile] = Field(max_length=32)


class InstallationRecord(WireModel):
    """Gateway acknowledgement for an installed version."""

    installed_version: int = Field(gt=0)
    effective_update_mode: Literal["MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"]


class InstallationItem(WireModel):
    """One installation visible to the current installation identity."""

    skill_id: str = Field(min_length=1, max_length=512)
    installed_version: int = Field(gt=0)
    latest_version: int | None = Field(default=None, gt=0)
    update_mode: Literal["MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"]
    skill_state: str = Field(min_length=1, max_length=64)
    takedown_generation: int | None = Field(default=None, ge=0)


class InstallationList(WireModel):
    """Bounded installation listing response."""

    installations: list[InstallationItem] = Field(max_length=10_000)


class InstallationDeactivation(WireModel):
    """Gateway acknowledgement for an installation deactivation."""

    skill_id: str = Field(min_length=1, max_length=512)
    installation_id: str = Field(min_length=1, max_length=512)
    state: Literal["inactive"]


class Capability(WireModel):
    """The Gateway's advertised Sync features."""

    features: list[str] = Field(max_length=256)


class InstallationIdentity(WireModel):
    """Acknowledgement of this account's installation registration."""

    installation_id: str = Field(min_length=1, max_length=512)
    state: Literal["active"]


class WisdomClient:
    """Bounded discovery and explicit installation-enrolment client."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        credential_provider: CredentialProvider,
        expected_organization_id: str,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        session: requests.Session | None = None,
        install_credential_provider: CredentialProvider | None = None,
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
        self._install_credential_provider = install_credential_provider
        self._closed = False
        self.timeout = normalized_timeout
        self.max_response_bytes = max_response_bytes
        self.session = session or requests.Session()

    def _credentials(self, *, install: bool = False) -> WisdomGatewayCredentials:
        try:
            provider = self._install_credential_provider if install else self._credential_provider
            if provider is None:
                raise WisdomAuthError("Installation registration requires Wisdom install access")
            credentials = provider()
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
        """Return the configured organization binding without reading credentials."""

        return WisdomOrganizationContext(self.expected_organization_id)

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

    def _request(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        path: str,
        *,
        model: type[BaseModel],
        params: dict[str, Any] | None = None,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        sync_root: bool = False,
        install: bool = False,
    ) -> Any:
        if self._closed:
            raise WisdomError("Collective Wisdom client is closed")
        credentials = self._credentials(install=install)
        request_options = {"json": json_body} if json_body is not None else {}
        url = (
            f"{credentials.base_url}/v1/sync/{path}"
            if sync_root else self._url(credentials, path)
        )
        try:
            response = self.session.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {credentials.access_token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
                **request_options,
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

    def capability(self) -> Capability:
        """Read advertised capabilities through the same bounded transport."""
        result = self._request("capabilities", model=Capability, sync_root=True)
        if "wisdom" not in result.features:
            raise WisdomAuthError("Gateway does not advertise Collective Wisdom")
        return result

    def register_identity(self, installation_id: str) -> InstallationIdentity:
        """Register a reserved identity; authorization is read afresh for this POST."""
        installation_id = _text(installation_id, "installation_id", _MAX_IDENTIFIER_BYTES)
        result = self._request(
            "installation-identities", model=InstallationIdentity,
            method="POST", json_body={"installation_id": installation_id}, install=True,
        )
        if result.installation_id != installation_id:
            raise WisdomError("Gateway acknowledged a different installation identity")
        return result

    def close(self) -> None:
        """Dispose this invocation's owned transport and refuse further requests."""
        self._closed = True
        self.session.close()

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

    def content(
        self,
        skill_id: str,
        version: int,
        *,
        installation_id: str,
        takedown_generation: int,
    ) -> tuple[VersionContent, list[tuple[str, str, bytes]]]:
        """Download and verify an install-scoped immutable package."""

        version = _version(version)
        installation_id = _text(
            installation_id, "installation_id", _MAX_IDENTIFIER_BYTES
        )
        takedown_generation = _generation(takedown_generation)
        response = self._request(
            f"skills/{_quoted_identifier(skill_id, 'skill_id')}/versions/{version}/content",
            model=VersionContent,
            params={
                "installation_id": installation_id,
                "takedown_generation": str(takedown_generation),
            },
            install=True,
        )
        return self._decode_version_content(response)

    @staticmethod
    def _decode_version_content(
        response: VersionContent,
    ) -> tuple[VersionContent, list[tuple[str, str, bytes]]]:
        # Adapted from Hermes Agent hermes_wisdom/client.py at 0924f7946b.
        decoded: list[tuple[str, str, bytes]] = []
        for item in response.files:
            try:
                raw = base64.b64decode(item.content_base64, validate=True)
            except (TypeError, ValueError) as exc:
                raise WisdomValidationError(
                    "version content contains malformed base64"
                ) from exc
            if wire_address(raw) != item.hash:
                raise WisdomValidationError(
                    f"version blob failed integrity validation: {item.path}"
                )
            decoded.append((item.path, item.mode, raw))
        try:
            _, derived = verify_content_files(decoded)
        except PackagePolicyError as exc:
            raise WisdomValidationError("version content failed package validation") from exc
        if derived != response.content_hash:
            raise WisdomValidationError(
                "version content hash failed integrity validation"
            )
        manifest_body = next(
            (body for path, _, body in decoded if path == "skill.manifest.json"), None
        )
        if manifest_body is None:
            raise WisdomValidationError("version content has no package manifest")
        try:
            parse_manifest_bytes(manifest_body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise WisdomValidationError("version package manifest is invalid") from exc
        return response, decoded

    def record_install(
        self,
        *,
        skill_id: str,
        installation_id: str,
        version: int,
        takedown_generation: int,
        update_mode: str | None,
    ) -> InstallationRecord:
        """Record an install using a fresh install-scoped credential snapshot."""

        skill_id = _text(skill_id, "skill_id", _MAX_IDENTIFIER_BYTES)
        installation_id = _text(
            installation_id, "installation_id", _MAX_IDENTIFIER_BYTES
        )
        version = _version(version)
        takedown_generation = _generation(takedown_generation)
        if update_mode not in {None, "MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"}:
            raise WisdomValidationError("update_mode is invalid")
        payload: dict[str, Any] = {
            "skill_id": skill_id,
            "installation_id": installation_id,
            "version": version,
            "takedown_generation": takedown_generation,
        }
        if update_mode is not None:
            payload["update_mode"] = update_mode
        result = self._request(
            "installations",
            model=InstallationRecord,
            method="POST",
            json_body=payload,
            install=True,
        )
        if result.installed_version != version:
            raise WisdomError("Gateway acknowledged a different installed version")
        return result

    def installations(self, installation_id: str) -> list[dict[str, Any]]:
        """List installs using a fresh install-scoped credential snapshot."""

        result = self._request(
            f"installations/{_quoted_identifier(installation_id, 'installation_id')}",
            model=InstallationList,
            install=True,
        )
        return [item.model_dump(mode="json") for item in result.installations]

    def deactivate_install(
        self, installation_id: str, skill_id: str
    ) -> InstallationDeactivation:
        """Deactivate one install and validate the acknowledged identities."""

        installation_id = _text(
            installation_id, "installation_id", _MAX_IDENTIFIER_BYTES
        )
        skill_id = _text(skill_id, "skill_id", _MAX_IDENTIFIER_BYTES)
        result = self._request(
            f"installations/{quote(installation_id, safe='')}/skills/"
            f"{quote(skill_id, safe='')}",
            model=InstallationDeactivation,
            method="DELETE",
            install=True,
        )
        if result.installation_id != installation_id or result.skill_id != skill_id:
            raise WisdomError("Gateway acknowledged a different installation")
        return result


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


def _generation(value: object) -> int:
    if not _is_plain_int(value) or not 0 <= value < 2_147_483_647:
        raise WisdomValidationError("takedown_generation must be a non-negative integer")
    return value


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
