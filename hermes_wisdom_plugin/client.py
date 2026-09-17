"""Bounded, schema-validated Gateway client for Wisdom discovery."""
# pylint: disable=line-too-long,missing-class-docstring,missing-function-docstring,unidiomatic-typecheck,too-many-public-methods,too-many-lines

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .client_delivery import (
    ClientDeliveryClaim, ClientDeliveryResponse, Identifier, receipt_projection,
)
from .client_outcome import ClientOperationOutcome, ClientOperationResponse
from .contract import SystemSpecification, parse_manifest_bytes
from .delivery import DeliveryReceipt
from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)
from .package import (
    MAX_METADATA_BYTES, MAX_TREE_BYTES, PackagePolicyError, verify_content_files,
)
from .wire import wire_address


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CONTENT_RESPONSE_BYTES = (
    4 * ((MAX_TREE_BYTES + 2) // 3)
    + 6 * MAX_METADATA_BYTES
    + 1024 * 1024
)
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
    content_base64: str = Field(max_length=4 * ((MAX_TREE_BYTES + 2) // 3))


class VersionContent(WireModel):
    """Bounded, exact published package returned by the Gateway."""

    commit: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    files: list[VersionContentFile]


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


class AgentLedNotificationDefaults(WireModel):
    """Server-owned notification defaults included in publication policy."""

    skill_ready_to_share: bool
    teammate_published: bool
    update_available: bool


class AgentLedPolicyResponse(WireModel):
    """Current server publication routing policy and stale-review version."""

    org_id: str = Field(min_length=1, max_length=512)
    usage_evidence_window_days: int = Field(ge=1, le=90)
    min_aggregate_invocations: int = Field(ge=1, le=10_000)
    consecutive_day_usage_counts: bool
    repeated_edits_count: bool
    max_recommendations_per_user_per_week: int = Field(ge=0, le=100)
    publication_mode: Literal["open", "moderated", "managed"]
    install_popularity_threshold: int = Field(ge=1, le=100_000)
    notification_defaults: AgentLedNotificationDefaults
    manager_review_email_cadence: Literal["immediate", "daily"]
    not_now_suppression_days: int = Field(ge=1, le=365)
    version: int = Field(ge=1)
    updated_by_user_id: str | None


class WisdomSuppression(WireModel):
    key: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    suppress_until: str


class WisdomSuppressionResponse(WisdomSuppression):
    org_id: str = Field(min_length=1, max_length=512)


class WisdomSuppressionList(WireModel):
    org_id: str = Field(min_length=1, max_length=512)
    suppressions: list[WisdomSuppression] = Field(max_length=100)


class WisdomMuteResponse(WireModel):
    org_id: str = Field(min_length=1, max_length=512)
    muted: bool
    duration: Literal["1_day", "1_week", "30_days", "forever"] | None
    muted_until: str | None
    forever: bool
    revision: int = Field(default=0, ge=0)
    mutation_id: str | None = None
    updated_at: str | None = None


class FeedEvent(WireModel):
    model_config = ConfigDict(extra="allow", strict=False)
    event_id: str = Field(min_length=1, max_length=512)
    kind: Literal["new", "updated", "archived", "taken_down", "restored",
                  "installed", "installation_updated", "uninstalled"]
    skill_id: str = Field(min_length=1, max_length=512)
    version: int | None = Field(default=None, gt=0)
    takedown_generation: int | None = Field(default=None, ge=0)
    installation_id: str | None
    update_mode: Literal["MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"] | None
    occurred_at: datetime


class Feed(WireModel):
    model_config = ConfigDict(extra="allow", strict=False)
    events: list[FeedEvent] = Field(max_length=100)
    next_cursor: str = Field(max_length=_MAX_CURSOR_BYTES)
    has_more: bool


class Draft(WireModel):
    """Owner-private Gateway draft; names intentionally match the wire."""

    id: str = Field(min_length=1, max_length=512)
    supersedesDraftId: str | None = None
    orgId: str = Field(min_length=1, max_length=512)
    ownerUserId: str = Field(min_length=1, max_length=512)
    slug: str = Field(min_length=1, max_length=512)
    draftCommit: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    contentHash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    authorDescription: str | None
    authorDescriptionHash: str | None
    state: Literal[
        "vetting", "ready", "owner_approved", "publishing",
        "pending_moderation", "changes_requested", "published",
        "declined", "invalidated",
    ]
    moderationNote: str | None = None
    moderationDeciderUserId: str | None = None
    moderationDecidedAt: str | None = None
    packageManifestHash: str | None
    packageManifestSchemaVersion: int | None
    systemSpec: SystemSpecification | None
    scan: dict[str, Any] | None
    scanVerdict: str | None
    security_check: dict[str, Any] | None = None
    professionalism_check: dict[str, Any] | None = None
    explanation: str | None
    updatedAt: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def require_return_metadata(self):
        """Returned-for-changes drafts require complete moderation metadata."""

        if self.state == "changes_requested" and not all((
            self.moderationNote,
            self.moderationDeciderUserId,
            self.moderationDecidedAt,
        )):
            raise ValueError("changes_requested drafts require complete moderator metadata")
        return self


class PublicationProjection(WireModel):
    """Additive authoritative publication recovery evidence."""

    state: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=128)
    retryable_with_fresh_base: bool | None = None
    proposal_id: int | None = Field(default=None, ge=1)


class DraftDetail(WireModel):
    """Owner draft plus the Gateway's effective policy projection."""

    draft: Draft
    effective_policy: dict[str, Any]
    publication: PublicationProjection | None = None


class DraftResponse(WireModel):
    """Single draft mutation response."""

    draft: Draft


class DraftList(WireModel):
    """Bounded owner-private draft listing used for reconciliation."""

    drafts: list[Draft] = Field(max_length=10_000)


class OrgRef(WireModel):
    """One exact ref returned by the organization Sync namespace."""

    name: str = Field(min_length=1, max_length=1024)
    hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class OrgRefList(WireModel):
    """Bounded organization ref listing used to resolve publication HEAD."""

    refs: list[OrgRef] = Field(max_length=10_000)


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
        publish_credential_provider: CredentialProvider | None = None,
        owner_user_id: str | None = None,
        route_base_url: str | None = None,
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
        self._credential_providers = {
            "read": credential_provider,
            "install": install_credential_provider,
            "publish": publish_credential_provider,
        }
        self._closed = False
        self._consumption_identity = (owner_user_id, route_base_url)
        self.timeout = normalized_timeout
        self.max_response_bytes = max_response_bytes
        self.session = session or requests.Session()

    @property
    def owner_user_id(self) -> str | None:
        return self._consumption_identity[0]

    @property
    def route_base_url(self) -> str | None:
        return self._consumption_identity[1]

    def _credentials(
        self, *, install: bool = False, publish: bool = False
    ) -> WisdomGatewayCredentials:
        try:
            credentials = self._credential_provider(
                install=install, publish=publish
            )()
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

    def _credential_provider(
        self, *, install: bool, publish: bool
    ) -> CredentialProvider:
        """Select one non-overlapping request scope."""

        if install and publish:
            raise WisdomAuthError("Collective Wisdom credentials are unavailable")
        scope = "install" if install else "publish" if publish else "read"
        provider = self._credential_providers[scope]
        if provider is None:
            operation = "Publication" if publish else "Installation registration"
            raise WisdomAuthError(
                f"{operation} requires the corresponding Wisdom access"
            )
        return provider

    @property
    def display_org_id(self) -> str:
        return self.expected_organization_id

    @property
    def identity(self) -> dict[str, str | None]:
        return {"owner": self.owner_user_id}

    def organization_context(self) -> WisdomOrganizationContext:
        """Return the configured organization binding without reading credentials."""

        return WisdomOrganizationContext(self.expected_organization_id)

    @staticmethod
    def _url(credentials: WisdomGatewayCredentials, path: str) -> str:
        return f"{credentials.base_url}/v1/sync/wisdom/{path.lstrip('/')}"

    def _read_response_body(
        self, response: requests.Response, *, max_bytes: int | None = None
    ) -> Any:
        limit = self.max_response_bytes if max_bytes is None else max_bytes
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
            if declared_length > limit:
                raise WisdomError("Collective Wisdom Gateway response exceeded size limit")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > limit:
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

    @staticmethod
    def _raise_for_status(response: requests.Response, body: Any) -> None:
        """Translate one bounded Gateway response into public client errors."""

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

    def _request(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        path: str,
        *,
        model: type[BaseModel] | None,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        sync_root: bool = False,
        install: bool = False,
        publish: bool = False,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        response_limit: int | None = None,
    ) -> Any:
        if self._closed:
            raise WisdomError("Collective Wisdom client is closed")
        credentials = self._credentials(install=install, publish=publish)
        request_options: dict[str, Any] = {}
        if json_body is not None:
            request_options["json"] = json_body
        if files is not None:
            request_options["files"] = files
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
                body = self._read_response_body(response, max_bytes=response_limit)
            finally:
                response.close()
        except requests.Timeout as exc:
            raise WisdomError("Collective Wisdom Gateway timed out") from exc
        except requests.RequestException as exc:
            raise WisdomError("Collective Wisdom Gateway is unavailable") from exc
        self._raise_for_status(response, body)
        if model is None:
            return body
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

    def agent_led_policy(self) -> AgentLedPolicyResponse:
        """Read and bind the current server publication routing policy."""

        result = self._request("agent-led/policy", model=AgentLedPolicyResponse)
        if result.org_id != self.expected_organization_id:
            raise WisdomError("Publication policy does not match the active organization")
        return result

    def _preference_org(self, response):
        if response.org_id != self.expected_organization_id:
            raise WisdomError("Preference does not match the active organization")
        return response

    def _delivery_identity(self) -> tuple[str, str]:
        owner = self.owner_user_id
        if not isinstance(owner, str) or not owner:
            raise WisdomAuthError("Notification delivery requires an active account")
        return self.expected_organization_id, owner

    def _delivery_response(
        self, response, identity, claim, *, event_id: str | None = None
    ) -> ClientDeliveryResponse:
        expected = (*identity, claim.request_id, claim.reference, event_id)
        actual = (
            response.org_id, response.recipient_user_id, response.request_id,
            response.reference, event_id if event_id is None else response.event_id,
        )
        if self._delivery_identity() != identity or actual != expected:
            raise WisdomError("Notification delivery response does not match the request")
        return response

    def claim_notification_delivery(self, request_id: str, reference: dict) -> ClientDeliveryResponse:
        identity = self._delivery_identity()
        claim = ClientDeliveryClaim(request_id=request_id, reference=reference)
        response = self._request(
            "agent-led/deliveries/claim", model=ClientDeliveryResponse,
            method="POST", json_body=claim.model_dump(mode="json"),
        )
        return self._delivery_response(response, identity, claim)

    def settle_notification_delivery(self, event_id: str, request_id: str, reference: dict, *,
                                     outcome: Literal["acknowledged", "not_sent", "uncertain"],
                                     receipt: DeliveryReceipt | None = None) -> ClientDeliveryResponse:
        identity = self._delivery_identity()
        claim = ClientDeliveryClaim(request_id=request_id, reference=reference)
        TypeAdapter(Identifier).validate_python(event_id, strict=True)
        if (outcome == "acknowledged") != (receipt is not None):
            raise WisdomValidationError("Only acknowledged delivery requires a receipt")
        body: dict[str, Any] = {"request_id": request_id, "outcome": outcome}
        if receipt is not None:
            body["receipt"] = receipt_projection(receipt).model_dump(mode="json")
        result = self._request(f"agent-led/deliveries/{quote(event_id, safe='')}/settle",
                               model=ClientDeliveryResponse, method="POST", json_body=body)
        self._delivery_response(result, identity, claim, event_id=event_id)
        if result.state != outcome:
            raise WisdomError("Notification delivery settlement was not acknowledged")
        return result

    def report_operation_outcome(self, event_id: str, outcome: dict) -> ClientOperationResponse:
        TypeAdapter(Identifier).validate_python(event_id, strict=True)
        report = ClientOperationOutcome.model_validate(outcome)
        return self._request(f"agent-led/deliveries/{quote(event_id, safe='')}/outcomes",
                             model=ClientOperationResponse, method="POST",
                             json_body=report.model_dump(mode="json"))

    def suppress_recommendation(self, key: str) -> WisdomSuppressionResponse:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", key):
            raise WisdomValidationError("Invalid suppression key")
        return self._preference_org(self._request(
            f"agent-led/suppressions/{quote(key, safe='')}", model=WisdomSuppressionResponse,
            method="PUT", json_body={}))

    def recommendation_suppressions(self, keys: list[str]) -> list[WisdomSuppression]:
        if len(keys) > 100 or len(set(keys)) != len(keys) or any(
                not re.fullmatch(r"sha256:[a-f0-9]{64}", key) for key in keys):
            raise WisdomValidationError("Expected up to 100 distinct suppression keys")
        result = self._preference_org(self._request(
            "agent-led/suppressions/check", model=WisdomSuppressionList, method="POST",
            json_body={"keys": keys}))
        if any(row.key not in keys for row in result.suppressions):
            raise WisdomError("Gateway returned unexpected suppression keys")
        return result.suppressions

    def recommendation_mute(self) -> WisdomMuteResponse:
        return self._preference_org(self._request(
            "agent-led/mute", model=WisdomMuteResponse, params={"include_revision": "1"}))

    def set_recommendation_mute(self, duration: str | None, *, expected_revision: int | None = None,
                                mutation_id: str | None = None,
                                requested_at: str | None = None) -> WisdomMuteResponse:
        if duration not in {None, "1_day", "1_week", "30_days", "forever"}:
            raise WisdomValidationError("Unsupported mute duration")
        guarded = any(value is not None for value in (expected_revision, mutation_id, requested_at))
        if guarded and (type(expected_revision) is not int or not mutation_id or not requested_at):
            raise WisdomValidationError("Queued mute requires revision, mutation ID and request time")
        body = {"duration": duration}
        if guarded:
            body.update(expected_revision=expected_revision, mutation_id=mutation_id,
                        requested_at=requested_at)
        return self._preference_org(self._request(
            "agent-led/mute", model=WisdomMuteResponse,
            method="DELETE" if duration is None and not guarded else "PUT",
            json_body=body if duration is not None or guarded else None))

    def feed(self, cursor: str | None = None, *, installation_id: str | None = None) -> Feed:
        params: dict[str, str] = {}
        if cursor:
            params["cursor"] = cursor
        if installation_id:
            params["installation_id"] = installation_id
        return self._request("feed", model=Feed, params=params or None)

    def upload_private_objects(
        self, objects: dict[str, tuple[str, bytes]]
    ) -> dict[str, Any]:
        """Upload exact raw CAS objects to the owner's private sync namespace."""

        multipart = [
            (digest, (kind, body, "application/octet-stream"))
            for digest, (kind, body) in objects.items()
        ]
        return self._request(
            "objects", model=None, method="POST", sync_root=True,
            publish=True, files=multipart,
        )

    def submit_draft(  # pylint: disable=too-many-arguments
        self,
        *,
        slug: str,
        commit: str,
        content_hash: str,
        description: str,
        description_hash: str,
        manifest_hash: str,
        professionalism_review: dict[str, Any] | None = None,
    ) -> Draft:
        """Submit owner-private bytes for authoritative Gateway vetting."""

        del description_hash, manifest_hash  # checked against the response by the controller
        payload: dict[str, Any] = {
            "slug": slug,
            "draft_commit": commit,
            "content_hash": content_hash,
            "author_description": description,
        }
        if professionalism_review is not None:
            payload["professionalism_review"] = professionalism_review
        return self._request(
            "drafts", model=DraftResponse, method="POST",
            json_body=payload, publish=True,
        ).draft

    def list_drafts(self) -> list[Draft]:
        """List current caller's owner-private drafts for exact reconciliation."""

        return self._request("drafts", model=DraftList, publish=True).drafts

    def draft(self, draft_id: str) -> DraftDetail:
        """Read one owner-private draft and its effective policy."""

        return self._request(
            f"drafts/{_quoted_identifier(draft_id, 'draft_id')}",
            model=DraftDetail,
            publish=True,
        )

    def approve(  # pylint: disable=too-many-arguments
        self,
        draft_id: str,
        *,
        content_hash: str,
        description_hash: str,
        manifest_hash: str,
    ) -> Draft:
        """Bind owner approval to all reviewed hashes."""

        return self._request(
            f"drafts/{_quoted_identifier(draft_id, 'draft_id')}/approve",
            model=DraftResponse,
            method="POST",
            json_body={
                "content_hash": content_hash,
                "author_description_hash": description_hash,
                "package_manifest_hash": manifest_hash,
            },
            publish=True,
        ).draft

    def organization_head(self) -> str | None:
        """Read the exact active organization's authoritative publication HEAD."""

        result = self._request(
            "org/refs", model=OrgRefList, sync_root=True, publish=True
        )
        expected = f"refs/org/{self.expected_organization_id}/HEAD"
        expected_prefix = f"refs/org/{self.expected_organization_id}/"
        if any(not ref.name.startswith(expected_prefix) for ref in result.refs):
            raise WisdomError(
                "Organization refs do not match the active organization"
            )
        heads = [ref.hash for ref in result.refs if ref.name == expected]
        if len(heads) > 1:
            raise WisdomError(
                "Organization refs do not match the active organization"
            )
        return heads[0] if heads else None

    def publish(
        self, draft_id: str, *, content_hash: str, base_commit: str | None
    ) -> dict[str, Any]:
        """Ask the Gateway to route publication under current server policy."""

        if base_commit is not None and not re.fullmatch(
            r"sha256:[a-f0-9]{64}", base_commit
        ):
            raise WisdomValidationError("base_commit must be an exact sha256 address or null")

        return self._request(
            f"drafts/{_quoted_identifier(draft_id, 'draft_id')}/publish",
            model=None,
            method="POST",
            json_body={"content_hash": content_hash, "base_commit": base_commit},
            publish=True,
        )

    def decline(self, draft_id: str) -> dict[str, Any]:
        """Decline or withdraw an owner draft according to server semantics."""

        return self._request(
            f"drafts/{_quoted_identifier(draft_id, 'draft_id')}/decline",
            model=None,
            method="POST",
            publish=True,
        )

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
            response_limit=MAX_CONTENT_RESPONSE_BYTES,
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
