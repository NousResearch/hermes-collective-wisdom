"""Exact reviewed contribution submission with durable recovery.

The state machine is adapted from Hermes Agent 0924f7946b publication review,
submit and recovery paths. The Gateway remains the sole publication-policy
authority; this client only binds a displayed policy version against staleness.
"""
# pylint: disable=too-many-lines

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .contract import author_description_hash, parse_manifest_bytes, sha256_address
from .errors import (
    WisdomAuthError, WisdomConflict, WisdomError, WisdomNotFound, WisdomValidationError,
)
from .package import (
    read_validated_package_files,
    source_fingerprint,
    verify_content_files,
)
from .wire import ObjectSet, build_commit, build_tree_from_snapshot, wire_address


REVIEW_TTL = timedelta(minutes=30)
_TERMINAL = {"published", "pending_moderation", "changes_requested", "declined", "invalidated"}
_PUBLISHABLE = {"ready", "owner_approved", "publishing"}
_MAX_BASE_CONFLICT_RETRIES = 3
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
OwnerProvider = Callable[[], dict[str, str]]


def _slug(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
    if not _SLUG_RE.fullmatch(candidate):
        raise WisdomValidationError("Skill name cannot form a valid publication slug")
    return candidate


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


class PublicationController:  # pylint: disable=too-many-public-methods
    """One shared terminal/native publication state machine."""

    def __init__(self, store: Any, reviewer: Any, client: Any, owner_provider: OwnerProvider):
        self.store = store
        self.reviewer = reviewer
        self.client = client
        self.owner_provider = owner_provider

    def _owner(self) -> dict[str, str]:
        owner = self.owner_provider()
        required = {
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        }
        if not isinstance(owner, dict) or set(owner) != required or any(
            not isinstance(owner.get(key), str) or not owner[key].strip() for key in required
        ):
            raise WisdomAuthError("Collective Wisdom publication owner is unavailable")
        return dict(owner)

    @staticmethod
    def _require_current(may_apply: Callable[[], bool] | None) -> None:
        if may_apply is None:
            return
        try:
            current = may_apply() is True
        except Exception as exc:  # authority callbacks fail closed
            raise WisdomAuthError(
                "Collective Wisdom publication is no longer current"
            ) from exc
        if not current:
            raise WisdomAuthError("Collective Wisdom publication is no longer current")

    @staticmethod
    def _is_current(may_apply: Callable[[], bool] | None) -> bool:
        try:
            return may_apply is None or may_apply() is True
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    @staticmethod
    def _receipt_revision(draft: dict[str, Any], policy: Any) -> str:
        return json.dumps(
            {
                "local_updated_at": draft["updated_at"],
                "publication_mode": _value(policy, "publication_mode"),
                "policy_version": _value(policy, "version"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _policy(
        self, owner: dict[str, str], may_apply: Callable[[], bool] | None = None,
    ) -> Any:
        self._require_current(may_apply)
        policy = self.client.agent_led_policy()
        self._require_current(may_apply)
        if _value(policy, "org_id") != owner["organization_id"]:
            raise WisdomAuthError("Collective Wisdom publication policy owner changed")
        return policy

    def review(
        self, draft_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Show all exact local facts and create no publication side effect."""

        self._require_current(may_apply)
        owner = self._owner()
        self._require_current(may_apply)
        operation = self.store.operation(draft_id)
        if operation is not None:
            return self._review_stalled_operation(
                operation, owner, may_apply=may_apply
            )
        prepared = self.reviewer.reopen(draft_id, may_apply=may_apply)
        draft = self.store.draft(draft_id)
        if draft is None or draft.get("owner") != owner or draft.get("state") != "prepared":
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")
        security = prepared.get("security_check") or {}
        if security.get("upload_allowed") is not True:
            raise WisdomConflict(
                str(security.get("summary") or "Local security checks must complete before sharing")
            )
        policy = self._policy(owner, may_apply)
        self._require_current(may_apply)
        receipt = self.store.save_receipt(
            draft_id=draft_id,
            server_revision=self._receipt_revision(draft, policy),
            content_hash=prepared["hashes"]["content"],
            description_hash=prepared["hashes"]["author_description"],
            manifest_hash=prepared["hashes"]["package_manifest"],
        )
        return {
            **prepared,
            "receipt": receipt,
            "publication_mode": _value(policy, "publication_mode"),
            "policy_version": _value(policy, "version"),
            "authorization_expires_at": (
                datetime.now(timezone.utc) + REVIEW_TTL
            ).isoformat(),
            "notice": (
                "Nothing has been uploaded. Submitting this exact receipt is the one final "
                "owner confirmation; the Gateway alone chooses direct or moderated handling."
            ),
        }

    def _review_stalled_operation(
        self, operation: dict[str, Any], owner: dict[str, str], *,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Create a fresh confirmation for a policy-stalled exact operation."""

        if operation.get("kind") != "publication" or operation.get("state") != "pending":
            raise WisdomConflict("Publication operation is not awaiting review")
        if operation.get("phase") != "policy_review_required":
            raise WisdomConflict("Publication operation does not require a fresh policy review")
        payload = operation["payload"]
        if payload.get("owner") != owner:
            raise WisdomAuthError("Collective Wisdom publication owner changed")
        remote_id = payload.get("remote_draft_id")
        if not remote_id:
            raise WisdomConflict("Publication draft identity is unavailable")
        self._require_current(may_apply)
        remote = self.client.draft(remote_id).draft
        self._require_current(may_apply)
        if not self._draft_matches(remote, payload):
            raise WisdomValidationError(
                "Authoritative publication draft no longer matches intent"
            )
        if str(_value(remote, "state")) not in _PUBLISHABLE:
            raise WisdomConflict("Authoritative publication state must be reconciled first")
        policy = self._policy(owner, may_apply)
        if self.store.draft(payload["local_draft_id"]) is None:
            raise WisdomNotFound("Prepared publication draft not found")
        self._require_current(may_apply)
        receipt = self.store.save_receipt(
            draft_id=payload["local_draft_id"],
            server_revision=self._operation_receipt_revision(operation, policy, remote),
            content_hash=payload["hashes"]["content"],
            description_hash=payload["hashes"]["author_description"],
            manifest_hash=payload["hashes"]["package_manifest"],
        )
        return {
            **(payload.get("review") or {}),
            "state": "policy_review_required",
            "draft_id": payload["local_draft_id"],
            "operation_id": operation["id"],
            "hashes": payload["hashes"],
            "receipt": receipt,
            "publication_mode": _value(policy, "publication_mode"),
            "policy_version": _value(policy, "version"),
            "network_submission": False,
            "notice": (
                "The prior remote draft is unchanged. This fresh hash-bound confirmation "
                "authorizes continuing it under the current server policy."
            ),
        }

    @staticmethod
    def _operation_receipt_revision(operation: dict[str, Any], policy: Any, remote: Any) -> str:
        return json.dumps(
            {
                "operation_id": operation["id"],
                "operation_updated_at": operation["updated_at"],
                "remote_revision": _value(remote, "updatedAt"),
                "publication_mode": _value(policy, "publication_mode"),
                "policy_version": _value(policy, "version"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _verified_receipt(
        self, receipt_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], Any, dict[str, Any] | None]:
        self._require_current(may_apply)
        owner = self._owner()
        self._require_current(may_apply)
        receipt = self.store.publication_receipt(receipt_id)
        if receipt is None:
            raise WisdomConflict("Publication review receipt is missing or already used")
        try:
            reviewed_at = datetime.fromisoformat(str(receipt["reviewed_at"]))
        except ValueError as exc:
            raise WisdomConflict("Publication review receipt is invalid") from exc
        if reviewed_at.tzinfo is None or datetime.now(timezone.utc) - reviewed_at > REVIEW_TTL:
            raise WisdomConflict("Publication review expired; review the complete package again")
        draft = self.store.draft(str(receipt["draft_id"]))
        if draft is None or draft.get("owner") != owner:
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")
        expected = {
            "content": receipt["content_hash"],
            "author_description": receipt["description_hash"],
            "package_manifest": receipt["manifest_hash"],
        }
        policy = self._policy(owner, may_apply)
        if draft.get("state") != "prepared":
            operation = self._verified_existing_operation(
                receipt, draft, expected, policy, may_apply=may_apply
            )
            return draft, operation["payload"].get("review") or {}, owner, policy, operation

        prepared = self.reviewer.reopen(
            str(draft["id"]), may_apply=may_apply
        )
        security = prepared.get("security_check") or {}
        if security.get("upload_allowed") is not True:
            raise WisdomConflict(
                str(security.get("summary") or "Current local security checks block sharing")
            )
        skill = self.store.local_skill(str(draft["skill_id"]))
        if skill is None:
            raise WisdomNotFound("Local publication source not found")
        if source_fingerprint(Path(str(skill["canonical_path"]))) != draft["source_hash"]:
            raise WisdomConflict(
                "The local source changed. Prepare and review its current version before confirming"
            )
        if prepared["hashes"] != expected:
            raise WisdomConflict(
                "The package changed after review. Review it again before submitting"
            )
        if receipt["server_revision"] != self._receipt_revision(draft, policy):
            raise WisdomConflict("The package or team publication policy changed; review it again")
        return draft, prepared, owner, policy, None

    def _verified_existing_operation(
        self,
        receipt: dict[str, Any],
        draft: dict[str, Any],
        expected: dict[str, Any],
        policy: Any,
        *,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Validate fresh consent for an existing exact remote draft."""

        operation = self.store.publication_for_local_draft(str(draft["id"]))
        if operation is None or operation["payload"].get("hashes") != expected:
            raise WisdomConflict(
                "This contribution already advanced; reopen its publication status"
            )
        remote_id = operation["payload"].get("remote_draft_id")
        if not remote_id:
            raise WisdomConflict("Publication draft identity is unavailable")
        self._require_current(may_apply)
        remote = self.client.draft(remote_id).draft
        self._require_current(may_apply)
        if not self._draft_matches(remote, operation["payload"]):
            raise WisdomValidationError(
                "Authoritative publication draft no longer matches intent"
            )
        revision = self._operation_receipt_revision(operation, policy, remote)
        if receipt["server_revision"] != revision:
            raise WisdomConflict(
                "The publication or team policy changed; review it again"
            )
        return operation

    @staticmethod
    def _objects(
        raw: list[tuple[str, str, bytes]], owner: dict[str, str]
    ) -> tuple[ObjectSet, str]:
        objects = ObjectSet()
        tree = build_tree_from_snapshot(raw, objects, max_object_bytes=262_144)
        commit = build_commit(
            tree,
            [],
            owner=owner["account_id"],
            device=owner["installation_id"],
            message="Collective Wisdom owner-private draft",
            objects=objects,
        )
        return objects, commit

    def _intent(
        self,
        draft: dict[str, Any],
        prepared: dict[str, Any],
        owner: dict[str, str],
        policy: Any,
    ) -> dict[str, Any]:
        overlay = Path(str(draft["overlay_path"]))
        raw = read_validated_package_files(overlay)
        _records, content_hash = verify_content_files(raw)
        manifest_bytes = next(
            (body for relative, _mode, body in raw if relative == "skill.manifest.json"),
            None,
        )
        if manifest_bytes is None:
            raise WisdomValidationError("Prepared package manifest is unavailable")
        manifest = parse_manifest_bytes(manifest_bytes)
        if content_hash != prepared["hashes"]["content"]:
            raise WisdomConflict("Prepared package changed while creating publication intent")
        objects, commit = self._objects(raw, owner)
        encoded_objects = [
            {
                "hash": digest,
                "kind": kind,
                "content_base64": base64.b64encode(body).decode("ascii"),
            }
            for digest, (kind, body) in objects.objects.items()
        ]
        request = {
            "slug": _slug(manifest.name),
            "commit": commit,
            "content_hash": content_hash,
            "description": str(draft["description"]),
            "description_hash": author_description_hash(str(draft["description"])),
            "manifest_hash": sha256_address(manifest_bytes),
            "professionalism_review": prepared.get("professionalism_check"),
        }
        if {
            "content": request["content_hash"],
            "author_description": request["description_hash"],
            "package_manifest": request["manifest_hash"],
        } != prepared["hashes"]:
            raise WisdomConflict("Prepared description or manifest changed during authorization")
        return {
            "schema_version": 1,
            "local_draft_id": draft["id"],
            "owner": owner,
            "policy": {
                "publication_mode": _value(policy, "publication_mode"),
                "version": _value(policy, "version"),
            },
            "hashes": prepared["hashes"],
            "objects": encoded_objects,
            "request": request,
            "review": {
                key: prepared.get(key)
                for key in (
                    "files", "security_check", "professionalism_check", "source_hash",
                    "description", "system_specification",
                )
            },
        }

    @staticmethod
    def _decode_objects(payload: dict[str, Any]) -> dict[str, tuple[str, bytes]]:
        result: dict[str, tuple[str, bytes]] = {}
        for item in payload.get("objects", []):
            body = base64.b64decode(item["content_base64"], validate=True)
            if wire_address(body) != item["hash"] or item["kind"] not in {
                "blob", "tree", "commit",
            }:
                raise WisdomValidationError(
                    "Persisted publication object failed integrity validation"
                )
            result[item["hash"]] = (item["kind"], body)
        if not result:
            raise WisdomValidationError("Persisted publication intent has no objects")
        return result

    def submit(  # pylint: disable=too-many-locals
        self, receipt_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Consume one final hash-bound authorization and drive its durable operation."""

        draft, prepared, owner, policy, existing = self._verified_receipt(
            receipt_id, may_apply=may_apply
        )
        reviewed_hashes = dict(prepared.get("hashes") or {})
        entity_id = str(draft["skill_id"])
        lock_key = "publication:" + entity_id
        self._require_current(may_apply)
        lock = self.store.acquire_operation_lock(lock_key)
        if lock is None:
            raise WisdomConflict("This package is already being submitted. Reload its status")
        try:
            if existing is not None:
                operation_id = str(existing["id"])
                self._require_current(may_apply)
                self.store.reauthorize_publication(
                    receipt_id=receipt_id,
                    operation_id=operation_id,
                    policy={
                        "publication_mode": _value(policy, "publication_mode"),
                        "version": _value(policy, "version"),
                    },
                    entity_id=lock_key,
                    owner=lock,
                )
            else:
                # Close the review-to-effect window under the durable operation lock.
                prepared = self.reviewer.reopen(
                    str(draft["id"]), may_apply=may_apply
                )
                current_owner = self._owner()
                self._require_current(may_apply)
                if current_owner != owner:
                    raise WisdomAuthError("Collective Wisdom publication owner changed")
                security = prepared.get("security_check") or {}
                if security.get("upload_allowed") is not True:
                    raise WisdomConflict(
                        str(
                            security.get("summary")
                            or "Current local security checks block sharing"
                        )
                    )
                skill = self.store.local_skill(str(draft["skill_id"]))
                if skill is None or source_fingerprint(
                    Path(str(skill["canonical_path"]))
                ) != draft["source_hash"]:
                    raise WisdomConflict(
                        "The local source changed. Prepare and review it before confirming"
                    )
                if prepared["hashes"] != reviewed_hashes:
                    raise WisdomConflict("The package changed; review it again before submitting")
                intent = self._intent(draft, prepared, owner, policy)
                self._require_current(may_apply)
                operation_id = self.store.begin_publication(
                    receipt_id=receipt_id,
                    draft_id=str(draft["id"]),
                    entity_id=entity_id,
                    payload=intent,
                )
            return self._drive(
                operation_id, lock_key=lock_key, lock_owner=lock,
                may_apply=may_apply,
            )
        finally:
            self.store.release_operation_lock(lock_key, lock)

    def submit_private_draft(  # pylint: disable=too-many-locals
        self, receipt_id: str, *, may_apply: Callable[[], bool] | None = None,
        candidate_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload one exact owner-private draft, stopping before approval/publication."""

        draft, prepared, owner, policy, existing = self._verified_receipt(
            receipt_id, may_apply=may_apply
        )
        if existing is not None:
            raise WisdomConflict("This private draft already exists; reopen its current review")
        reviewed_hashes = dict(prepared.get("hashes") or {})
        entity_id = str(draft["skill_id"])
        lock_key = "publication:" + entity_id
        self._require_current(may_apply)
        lock = self.store.acquire_operation_lock(lock_key)
        if lock is None:
            raise WisdomConflict("This package is already being submitted. Reload its status")
        try:
            prepared = self.reviewer.reopen(str(draft["id"]), may_apply=may_apply)
            current_owner = self._owner()
            self._require_current(may_apply)
            if current_owner != owner:
                raise WisdomAuthError("Collective Wisdom publication owner changed")
            security = prepared.get("security_check") or {}
            if security.get("upload_allowed") is not True:
                raise WisdomConflict(
                    str(security.get("summary") or "Current local security checks block sharing")
                )
            skill = self.store.local_skill(str(draft["skill_id"]))
            if skill is None or source_fingerprint(
                Path(str(skill["canonical_path"]))
            ) != draft["source_hash"]:
                raise WisdomConflict(
                    "The local source changed. Prepare and review it before submitting"
                )
            if prepared["hashes"] != reviewed_hashes:
                raise WisdomConflict("The package changed; review it again before submitting")
            intent = self._intent(draft, prepared, owner, policy)
            intent["requested_action"] = "private_draft"
            if candidate_assessment_id is not None:
                intent["candidate_assessment_id"] = candidate_assessment_id
            self._require_current(may_apply)
            operation_id = self.store.begin_publication(
                receipt_id=receipt_id,
                draft_id=str(draft["id"]),
                entity_id=entity_id,
                payload=intent,
            )
            return self._drive(
                operation_id, lock_key=lock_key, lock_owner=lock,
                may_apply=may_apply,
            )
        finally:
            self.store.release_operation_lock(lock_key, lock)

    def _operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.store.operation(operation_id)
        if operation is None or operation.get("kind") != "publication":
            raise WisdomNotFound("Publication operation not found")
        owner = self._owner()
        if operation["payload"].get("owner") != owner:
            raise WisdomAuthError("Collective Wisdom publication owner changed")
        return operation

    @staticmethod
    def _draft_matches(remote: Any, payload: dict[str, Any]) -> bool:
        request, owner = payload["request"], payload["owner"]
        return all((
            _value(remote, "orgId") == owner["organization_id"],
            _value(remote, "ownerUserId") == owner["account_id"],
            _value(remote, "slug") == request["slug"],
            _value(remote, "draftCommit") == request["commit"],
            _value(remote, "contentHash") == request["content_hash"],
            _value(remote, "authorDescription") == request["description"],
            _value(remote, "authorDescriptionHash") == request["description_hash"],
            _value(remote, "packageManifestHash") == request["manifest_hash"],
        ))

    def _record_remote(
        self,
        operation_id: str,
        operation: dict[str, Any],
        remote: Any,
        *,
        lock_key: str,
        lock_owner: str,
    ) -> dict[str, Any]:
        payload = dict(operation["payload"])
        if not self._draft_matches(remote, payload):
            raise WisdomValidationError("Gateway acknowledged a different publication draft")
        self.store.assert_operation_lock(lock_key, lock_owner)
        payload["remote_draft_id"] = str(_value(remote, "id"))
        payload["remote_state"] = str(_value(remote, "state"))
        payload["remote_revision"] = str(_value(remote, "updatedAt"))
        self.store.replace_operation_payload(operation_id, payload)
        self.store.assert_operation_lock(lock_key, lock_owner)
        local = self.store.draft(payload["local_draft_id"])
        if local is not None:
            self.store.set_draft_state(payload["local_draft_id"], "submitted")
            self.store.record_draft({
                **local,
                "id": payload["remote_draft_id"],
                "draft_commit": payload["request"]["commit"],
                "server_revision": payload["remote_revision"],
                "state": payload["remote_state"],
            })
            self.store.bind_draft_owner(payload["remote_draft_id"], payload["owner"])
        return payload

    @staticmethod
    def _stopped_result(operation_id: str, phase: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "state": "pending",
            "phase": phase,
            "detail": (
                "The authorizing invocation ended after the durable operation entered "
                "this phase. Inspect status before explicitly reopening it."
            ),
            "retry_safe": phase in {"intent", "objects_uploaded"},
        }

    @staticmethod
    def _uncertain_result(operation_id: str, phase: str, detail: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "state": "uncertain",
            "phase": phase,
            "detail": detail,
            "retry_safe": False,
        }

    def _uncertain(
        self,
        operation_id: str,
        phase: str,
        detail: str,
        *,
        lock_key: str,
        lock_owner: str,
    ) -> dict[str, Any]:
        self.store.advance_claimed(
            operation_id, phase, entity_id=lock_key, owner=lock_owner
        )
        return self._uncertain_result(operation_id, phase, detail)

    def _persist_base_conflict(
        self,
        operation_id: str,
        payload: dict[str, Any],
        *,
        lock_key: str,
        lock_owner: str,
        proposal_id: Any = None,
    ) -> dict[str, Any]:
        """Persist only server-confirmed stale-base evidence as retryable."""

        updated = dict(payload)
        updated["failure_reason"] = "base_mismatch"
        if isinstance(proposal_id, int) and not isinstance(proposal_id, bool):
            updated["conflicted_proposal_id"] = proposal_id
        self.store.assert_operation_lock(lock_key, lock_owner)
        self.store.replace_operation_payload(operation_id, updated)
        self.store.advance_claimed(
            operation_id, "base_conflict", entity_id=lock_key, owner=lock_owner
        )
        return updated

    @staticmethod
    def _publish_conflict(result: Any) -> tuple[bool, Any]:
        """Recognize only structured, server-confirmed stale-base evidence."""

        if not isinstance(result, dict) or result.get("state") != "pending_approve_conflict":
            return False, None
        if not (
            result.get("reason") == "base_mismatch"
            and result.get("retryable_with_fresh_base") is True
        ):
            return False, None
        return True, result.get("proposal_id")

    def _persist_publication_failure(
        self,
        operation_id: str,
        payload: dict[str, Any],
        error: WisdomError,
        *,
        lock_key: str,
        lock_owner: str,
    ) -> None:
        """Record a definite rejection without misclassifying it as a lost ACK."""

        updated = dict(payload)
        updated["failure_reason"] = str(error.code or type(error).__name__)
        updated["failure_detail"] = str(error)
        self.store.assert_operation_lock(lock_key, lock_owner)
        self.store.replace_operation_payload(operation_id, updated)
        self.store.advance_claimed(
            operation_id, "publication_failed", entity_id=lock_key, owner=lock_owner
        )

    @staticmethod
    def _is_definite_rejection(error: WisdomError) -> bool:
        """Separate typed rejections from errors that may follow a remote effect."""

        return bool(
            isinstance(error, WisdomAuthError)
            or isinstance(error, WisdomConflict) and error.status == 409
            or isinstance(error, WisdomValidationError) and error.status == 422
        )

    @staticmethod
    def _projection_conflict(detail: Any) -> tuple[bool, Any]:
        projection = _value(detail, "publication")
        return (
            bool(
                projection is not None
                and _value(projection, "state") == "base_conflict"
                and _value(projection, "reason") == "base_mismatch"
                and _value(projection, "retryable_with_fresh_base") is True
            ),
            _value(projection, "proposal_id"),
        )

    def _fresh_head(
        self,
        operation_id: str,
        phase: str,
        *,
        lock_key: str,
        lock_owner: str,
        may_apply: Callable[[], bool] | None,
    ) -> tuple[bool, str | None | dict[str, Any]]:
        """Read HEAD with invocation and lease fences around the remote effect."""

        if not self._is_current(may_apply):
            return False, self._stopped_result(operation_id, phase)
        self.store.assert_operation_lock(lock_key, lock_owner)
        if not self._is_current(may_apply):
            return False, self._stopped_result(operation_id, phase)
        head = self.client.organization_head()
        self.store.assert_operation_lock(lock_key, lock_owner)
        if not self._is_current(may_apply):
            return False, self._stopped_result(operation_id, phase)
        return True, head

    def _finish(
        self,
        operation_id: str,
        payload: dict[str, Any],
        state: str,
        *,
        lock_key: str | None = None,
        lock_owner: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload)
        payload["publication_state"] = state
        self.store.complete_publication(
            operation_id,
            payload,
            state,
            entity_id=lock_key,
            owner=lock_owner,
        )
        return self._finish_projection(operation_id, payload)

    def _reconcile_unknown_draft(
        self,
        operation_id: str,
        operation: dict[str, Any],
        *,
        lock_key: str,
        lock_owner: str,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self._is_current(may_apply):
            return self._stopped_result(operation_id, "draft_submission_unknown")
        matches = [
            draft for draft in self.client.list_drafts()
            if self._draft_matches(draft, operation["payload"])
        ]
        if not self._is_current(may_apply):
            return self._stopped_result(operation_id, "draft_submission_unknown")
        if len(matches) != 1:
            detail = (
                "No unique exact owner draft is visible. Do not resubmit; check Gateway status "
                "and reopen this operation later."
            )
            return self._uncertain(
                operation_id, "draft_submission_unknown", detail,
                lock_key=lock_key, lock_owner=lock_owner,
            )
        payload = self._record_remote(
            operation_id, operation, matches[0],
            lock_key=lock_key, lock_owner=lock_owner,
        )
        self.store.advance_claimed(
            operation_id, "draft_submitted", entity_id=lock_key, owner=lock_owner
        )
        return self._drive(
            operation_id,
            payload_override=payload,
            lock_key=lock_key,
            lock_owner=lock_owner,
            may_apply=may_apply,
        )

    # Keeping durable phases in one dispatcher makes each remote effect's prior
    # persisted phase and recovery branch reviewable in execution order.
    def _drive(  # pylint: disable=R0911,R0912,R0913,R0914,R0915
        self,
        operation_id: str,
        *,
        lock_key: str,
        lock_owner: str,
        payload_override: dict[str, Any] | None = None,
        may_apply: Callable[[], bool] | None = None,
        conflict_budget: int = 0,
    ) -> dict[str, Any]:
        operation = self._operation(operation_id)
        payload = payload_override or operation["payload"]
        phase = operation["phase"]
        unresolved_publication = False
        if operation["state"] == "done":
            return self._finish_projection(operation_id, payload)
        if not self._is_current(may_apply):
            return self._stopped_result(operation_id, phase)
        if phase == "draft_submission_unknown":
            return self._reconcile_unknown_draft(
                operation_id, operation, lock_key=lock_key, lock_owner=lock_owner,
                may_apply=may_apply,
            )

        remote_id = payload.get("remote_draft_id")
        remote_state = ""
        if remote_id:
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            try:
                remote_detail = self.client.draft(remote_id)
                remote = remote_detail.draft
            except (WisdomAuthError, WisdomConflict, WisdomValidationError) as exc:
                if self._is_definite_rejection(exc):
                    self._persist_publication_failure(
                        operation_id, payload, exc,
                        lock_key=lock_key, lock_owner=lock_owner,
                    )
                    raise
                return self._uncertain_result(
                    operation_id, phase, "Gateway status is unavailable; no effect was retried."
                )
            # Preserve the phase on any ordinary status transport/schema failure.
            except Exception:  # pylint: disable=broad-exception-caught
                return self._uncertain_result(
                    operation_id, phase, "Gateway status is unavailable; no effect was retried."
                )
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            if not self._draft_matches(remote, payload):
                raise WisdomValidationError(
                    "Authoritative publication draft no longer matches intent"
                )
            remote_state = str(_value(remote, "state"))
            if phase == "decline_unknown":
                if remote_state in {"declined", "published", "changes_requested", "invalidated"}:
                    return self._finish(
                        operation_id, payload, remote_state,
                        lock_key=lock_key, lock_owner=lock_owner,
                    )
                return self._uncertain_result(
                    operation_id,
                    "decline_unknown",
                    f"Gateway draft is {remote_state}; decline outcome remains unknown.",
                )
            if remote_state in _TERMINAL:
                return self._finish(
                    operation_id, payload, remote_state,
                    lock_key=lock_key, lock_owner=lock_owner,
                )
            confirmed_conflict, proposal_id = self._projection_conflict(remote_detail)
            if phase == "server_pending" and not confirmed_conflict:
                return self._uncertain_result(
                    operation_id,
                    phase,
                    "Gateway status has no confirmed base-conflict recovery evidence; "
                    "nothing was retried.",
                )
            if phase == "base_conflict" and not confirmed_conflict:
                return self._uncertain_result(
                    operation_id,
                    phase,
                    "The latest Gateway status no longer confirms a retryable base conflict; "
                    "nothing was retried.",
                )
            if confirmed_conflict:
                payload = self._persist_base_conflict(
                    operation_id, payload, lock_key=lock_key, lock_owner=lock_owner,
                    proposal_id=proposal_id,
                )
                phase = "base_conflict"
            elif phase == "publication_unknown":
                unresolved_publication = True
            if remote_state not in _PUBLISHABLE:
                return self._uncertain(
                    operation_id, "server_pending", f"Gateway draft is {remote_state}",
                    lock_key=lock_key, lock_owner=lock_owner,
                )

        private_draft = payload.get("requested_action") == "private_draft"
        if phase == "policy_review_required":
            return {
                "operation_id": operation_id,
                "state": "pending",
                "phase": phase,
                "publication_state": remote_state or None,
                "remote_draft_id": remote_id,
                "retry_safe": False,
                "detail": (
                    "Private draft submitted. Final sharing still requires a fresh "
                    "exact-package approval."
                    if private_draft else
                    "This publication is waiting for a fresh exact-package approval."
                ),
            }

        if not self._is_current(may_apply):
            return self._stopped_result(operation_id, phase)
        policy = self._policy(payload["owner"], may_apply)
        current_policy = {
            "publication_mode": _value(policy, "publication_mode"),
            "version": _value(policy, "version"),
        }
        if private_draft and payload["policy"] != current_policy:
            payload = dict(payload)
            payload["policy"] = current_policy
            self.store.assert_operation_lock(lock_key, lock_owner)
            self.store.replace_operation_payload(operation_id, payload)
        elif payload["policy"] != current_policy:
            payload = dict(payload)
            payload["resume_phase"] = phase
            self.store.assert_operation_lock(lock_key, lock_owner)
            self.store.replace_operation_payload(operation_id, payload)
            self.store.advance_claimed(
                operation_id, "policy_review_required",
                entity_id=lock_key, owner=lock_owner,
            )
            return {
                "operation_id": operation_id,
                "state": "pending",
                "phase": "policy_review_required",
                "remote_draft_id": remote_id,
                "retry_safe": False,
                "detail": (
                    "The team publication policy changed. Review this operation again "
                    "before any new remote effect."
                ),
            }

        if unresolved_publication:
            return self._uncertain_result(
                operation_id,
                phase,
                "Publication outcome remains unknown; authoritative status showed no "
                "terminal result or confirmed base conflict, so nothing was retried.",
            )

        if phase == "intent":
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            self.store.assert_operation_lock(lock_key, lock_owner)
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            self.client.upload_private_objects(self._decode_objects(payload))
            self.store.advance_claimed(
                operation_id, "objects_uploaded", entity_id=lock_key, owner=lock_owner
            )
            phase = "objects_uploaded"
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
        if phase == "objects_uploaded":
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            self.store.advance_claimed(
                operation_id, "draft_submission_unknown",
                entity_id=lock_key, owner=lock_owner,
            )
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, "draft_submission_unknown")
            try:
                self.store.assert_operation_lock(lock_key, lock_owner)
                if not self._is_current(may_apply):
                    return self._stopped_result(
                        operation_id, "draft_submission_unknown"
                    )
                remote = self.client.submit_draft(**payload["request"])
                self.store.assert_operation_lock(lock_key, lock_owner)
                payload = self._record_remote(
                    operation_id, self._operation(operation_id), remote,
                    lock_key=lock_key, lock_owner=lock_owner,
                )
            # Submission may have committed before any ordinary failure surfaced.
            except Exception:  # pylint: disable=broad-exception-caught
                return self._uncertain_result(
                    operation_id,
                    "draft_submission_unknown",
                    "Draft submission outcome is unknown; exact reconciliation is required.",
                )
            self.store.advance_claimed(
                operation_id, "draft_submitted", entity_id=lock_key, owner=lock_owner
            )
            phase = "draft_submitted"
            remote_id = payload["remote_draft_id"]
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            remote = self.client.draft(remote_id).draft
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            if not self._draft_matches(remote, payload):
                raise WisdomValidationError(
                    "Authoritative publication draft no longer matches intent"
                )
            remote_state = str(_value(remote, "state"))

        if private_draft and phase == "draft_submitted":
            payload = dict(payload)
            payload["resume_phase"] = "draft_submitted"
            self.store.assert_operation_lock(lock_key, lock_owner)
            self.store.replace_operation_payload(operation_id, payload)
            self.store.advance_claimed(
                operation_id, "policy_review_required",
                entity_id=lock_key, owner=lock_owner,
            )
            return {
                "operation_id": operation_id,
                "state": "pending",
                "phase": "policy_review_required",
                "publication_state": remote_state,
                "remote_draft_id": remote_id,
                "retry_safe": False,
                "detail": (
                    "Private draft submitted. Final sharing still requires a fresh "
                    "exact-package approval."
                ),
            }

        if not remote_id:
            return self._uncertain(
                operation_id, "draft_submission_unknown", "Remote draft identity is unknown",
                lock_key=lock_key, lock_owner=lock_owner,
            )
        if phase not in {
            "draft_submitted", "approval_unknown", "approved", "publication_unknown",
            "base_conflict",
        }:
            return self._uncertain_result(
                operation_id, phase, "Publication is waiting for explicit recovery."
            )

        if remote_state == "ready":
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
            self.store.advance_claimed(
                operation_id, "approval_unknown", entity_id=lock_key, owner=lock_owner
            )
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, "approval_unknown")
            try:
                self.store.assert_operation_lock(lock_key, lock_owner)
                if not self._is_current(may_apply):
                    return self._stopped_result(operation_id, "approval_unknown")
                approved = self.client.approve(
                    remote_id,
                    content_hash=payload["hashes"]["content"],
                    description_hash=payload["hashes"]["author_description"],
                    manifest_hash=payload["hashes"]["package_manifest"],
                )
                self.store.assert_operation_lock(lock_key, lock_owner)
                if not self._draft_matches(approved, payload):
                    raise WisdomValidationError("Gateway approved a different publication draft")
            except (WisdomAuthError, WisdomConflict, WisdomValidationError) as exc:
                if self._is_definite_rejection(exc):
                    self._persist_publication_failure(
                        operation_id, payload, exc,
                        lock_key=lock_key, lock_owner=lock_owner,
                    )
                    raise
                return self._uncertain_result(
                    operation_id, "approval_unknown", "Owner approval outcome is unknown"
                )
            # Approval may have committed before any ordinary failure surfaced.
            except Exception:  # pylint: disable=broad-exception-caught
                return self._uncertain_result(
                    operation_id, "approval_unknown", "Owner approval outcome is unknown"
                )
            self.store.advance_claimed(
                operation_id, "approved", entity_id=lock_key, owner=lock_owner
            )
            phase = "approved"
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, phase)
        if not self._is_current(may_apply):
            return self._stopped_result(operation_id, phase)
        attempts = conflict_budget or _MAX_BASE_CONFLICT_RETRIES
        for attempt in range(attempts):
            if attempt:
                if not self._is_current(may_apply):
                    return self._stopped_result(operation_id, "base_conflict")
                self.store.assert_operation_lock(lock_key, lock_owner)
                retry_detail = self.client.draft(remote_id)
                self.store.assert_operation_lock(lock_key, lock_owner)
                if not self._is_current(may_apply):
                    return self._stopped_result(operation_id, "base_conflict")
                retry_remote = retry_detail.draft
                if not self._draft_matches(retry_remote, payload):
                    raise WisdomValidationError(
                        "Authoritative publication draft no longer matches intent"
                    )
                retry_state = str(_value(retry_remote, "state"))
                if retry_state in _TERMINAL:
                    return self._finish(
                        operation_id, payload, retry_state,
                        lock_key=lock_key, lock_owner=lock_owner,
                    )
                retry_confirmed, retry_proposal = self._projection_conflict(retry_detail)
                if not retry_confirmed:
                    return self._uncertain_result(
                        operation_id,
                        "base_conflict",
                        "The latest Gateway status does not confirm a retryable base conflict; "
                        "nothing was retried.",
                    )
                payload = self._persist_base_conflict(
                    operation_id, payload, lock_key=lock_key, lock_owner=lock_owner,
                    proposal_id=retry_proposal,
                )
                retry_policy = self._policy(payload["owner"], may_apply)
                retry_policy_value = {
                    "publication_mode": _value(retry_policy, "publication_mode"),
                    "version": _value(retry_policy, "version"),
                }
                if payload["policy"] != retry_policy_value:
                    payload = dict(payload)
                    payload["resume_phase"] = "base_conflict"
                    self.store.assert_operation_lock(lock_key, lock_owner)
                    self.store.replace_operation_payload(operation_id, payload)
                    self.store.advance_claimed(
                        operation_id, "policy_review_required",
                        entity_id=lock_key, owner=lock_owner,
                    )
                    return {
                        "operation_id": operation_id,
                        "state": "pending",
                        "phase": "policy_review_required",
                        "remote_draft_id": remote_id,
                        "retry_safe": False,
                        "detail": (
                            "The team publication policy changed during conflict recovery. "
                            "Review this operation again before another remote effect."
                        ),
                    }
            head_ok, head = self._fresh_head(
                operation_id, phase, lock_key=lock_key, lock_owner=lock_owner,
                may_apply=may_apply,
            )
            if not head_ok:
                return head
            self.store.advance_claimed(
                operation_id, "publication_unknown", entity_id=lock_key, owner=lock_owner
            )
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, "publication_unknown")
            try:
                self.store.assert_operation_lock(lock_key, lock_owner)
                if not self._is_current(may_apply):
                    return self._stopped_result(operation_id, "publication_unknown")
                published = self.client.publish(
                    remote_id,
                    content_hash=payload["hashes"]["content"],
                    base_commit=head,
                )
                self.store.assert_operation_lock(lock_key, lock_owner)
            except WisdomConflict as exc:
                if exc.code is None and "lease was lost" in str(exc):
                    return self._uncertain_result(
                        operation_id,
                        "publication_unknown",
                        "Publication acknowledgement was lost after the operation lease changed; "
                        "status must reconcile it.",
                    )
                if exc.code != "base_mismatch":
                    self._persist_publication_failure(
                        operation_id, payload, exc,
                        lock_key=lock_key, lock_owner=lock_owner,
                    )
                    raise
                payload = self._persist_base_conflict(
                    operation_id, payload, lock_key=lock_key, lock_owner=lock_owner
                )
                if attempt + 1 < attempts:
                    phase = "base_conflict"
                    continue
                return self._base_conflict_result(operation_id, payload, attempts)
            except (WisdomAuthError, WisdomValidationError) as exc:
                self._persist_publication_failure(
                    operation_id, payload, exc,
                    lock_key=lock_key, lock_owner=lock_owner,
                )
                raise
            # Publication may have committed before any ordinary failure surfaced.
            except Exception:  # pylint: disable=broad-exception-caught
                return self._uncertain_result(
                    operation_id,
                    "publication_unknown",
                    "Publication acknowledgement was lost; status will reconcile "
                    "without resubmitting.",
                )
            publication_state = str((published or {}).get("state") or "")
            confirmed, proposal_id = self._publish_conflict(published)
            if confirmed:
                payload = self._persist_base_conflict(
                    operation_id, payload, lock_key=lock_key, lock_owner=lock_owner,
                    proposal_id=proposal_id,
                )
                if attempt + 1 < attempts:
                    phase = "base_conflict"
                    continue
                return self._base_conflict_result(operation_id, payload, attempts)
            if publication_state in {"published", "pending_moderation"}:
                return self._finish(
                    operation_id, payload, publication_state,
                    lock_key=lock_key, lock_owner=lock_owner,
                )
            return self._uncertain(
                operation_id,
                "server_pending",
                f"Gateway publication is {publication_state or 'unknown'}; do not blindly retry.",
                lock_key=lock_key,
                lock_owner=lock_owner,
            )
        return self._base_conflict_result(operation_id, payload, attempts)

    @staticmethod
    def _base_conflict_result(
        operation_id: str, payload: dict[str, Any], attempts: int,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "state": "pending",
            "phase": "base_conflict",
            "reason": "base_mismatch",
            "retryable_with_fresh_base": True,
            "proposal_id": payload.get("conflicted_proposal_id"),
            "retry_attempts": attempts,
            "detail": "Organization HEAD changed; explicitly reopen to retry with a fresh base.",
        }

    def _finish_projection(self, operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        publication_state = payload.get("publication_state") or payload.get("remote_state")
        if publication_state:
            self.store.complete_publication(
                operation_id, payload, str(publication_state)
            )
        return {
            "operation_id": operation_id,
            "state": "done",
            "publication_state": publication_state,
            "remote_draft_id": payload.get("remote_draft_id"),
            "submitted_request": {
                key: value for key, value in payload["request"].items()
                if key != "professionalism_review"
            },
        }

    def status(
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Inspect durable local state without causing a network retry."""

        self._require_current(may_apply)
        operation = self._operation(operation_id)
        self._require_current(may_apply)
        if operation["state"] == "done":
            return self._finish_projection(operation_id, operation["payload"])
        phase = operation["phase"]
        conflict = phase == "base_conflict"
        return {
            "operation_id": operation_id,
            "state": "uncertain" if "unknown" in phase else "pending",
            "phase": phase,
            "remote_draft_id": operation["payload"].get("remote_draft_id"),
            "reason": "base_mismatch" if conflict else None,
            "retryable_with_fresh_base": conflict,
            "retry_safe": phase in {"intent", "objects_uploaded"},
        }

    def reopen(
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Reconcile one exact operation; never create a second remote draft."""

        self._require_current(may_apply)
        operation = self._operation(operation_id)
        self._require_current(may_apply)
        if operation["state"] == "done":
            payload = operation["payload"]
            remote_id = payload.get("remote_draft_id")
            if not remote_id:
                return self._finish_projection(operation_id, payload)
            self._require_current(may_apply)
            remote = self.client.draft(remote_id).draft
            self._require_current(may_apply)
            if not self._draft_matches(remote, payload):
                raise WisdomValidationError(
                    "Authoritative publication draft no longer matches intent"
                )
            state = str(_value(remote, "state"))
            if state != payload.get("publication_state"):
                return self._finish(operation_id, payload, state)
            return self._finish_projection(operation_id, payload)
        lock_key = "publication:" + str(operation["entity_id"])
        lock = self.store.acquire_operation_lock(lock_key)
        if lock is None:
            raise WisdomConflict("This publication is already being reconciled")
        try:
            return self._drive(
                operation_id, lock_key=lock_key, lock_owner=lock,
                may_apply=may_apply,
                conflict_budget=_MAX_BASE_CONFLICT_RETRIES,
            )
        finally:
            self.store.release_operation_lock(lock_key, lock)

    def decline(  # pylint: disable=too-many-return-statements,too-many-branches
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Persist and reconcile one exact decline; never race publication recovery."""

        self._require_current(may_apply)
        operation = self._operation(operation_id)
        self._require_current(may_apply)
        lock_key = "publication:" + str(operation["entity_id"])
        lock = self.store.acquire_operation_lock(lock_key)
        if lock is None:
            raise WisdomConflict("This publication is already being reconciled")
        try:
            operation = self._operation(operation_id)
            self._require_current(may_apply)
            payload = operation["payload"]
            remote_id = payload.get("remote_draft_id")
            if not remote_id:
                raise WisdomConflict("Unknown draft submission cannot be cancelled safely")
            self._require_current(may_apply)
            remote = self.client.draft(remote_id).draft
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, str(operation["phase"]))
            if not self._draft_matches(remote, payload):
                raise WisdomValidationError(
                    "Authoritative publication draft no longer matches intent"
                )
            state = str(_value(remote, "state"))
            if state == "declined":
                return self._finish(
                    operation_id, payload, state,
                    lock_key=lock_key, lock_owner=lock,
                )
            if state == "published":
                raise WisdomConflict("Published content cannot be declined")
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, str(operation["phase"]))
            should_send = self.store.begin_publication_decline(
                operation_id, entity_id=lock_key, owner=lock
            )
            if not should_send:
                return self._drive(
                    operation_id, lock_key=lock_key, lock_owner=lock,
                    may_apply=may_apply,
                )
            if not self._is_current(may_apply):
                return self._stopped_result(operation_id, "decline_unknown")
            try:
                self.store.assert_operation_lock(lock_key, lock)
                if not self._is_current(may_apply):
                    return self._stopped_result(operation_id, "decline_unknown")
                result = self.client.decline(remote_id)
                self.store.assert_operation_lock(lock_key, lock)
            # Decline may have committed before any ordinary failure surfaced.
            except Exception:  # pylint: disable=broad-exception-caught
                return self._uncertain_result(
                    operation_id,
                    "decline_unknown",
                    "Decline acknowledgement was lost; reconcile before any other action.",
                )
            if str((result or {}).get("state")) != "declined":
                raise WisdomValidationError("Gateway returned an invalid decline state")
            return self._finish(
                operation_id, payload, "declined",
                lock_key=lock_key, lock_owner=lock,
            )
        finally:
            self.store.release_operation_lock(lock_key, lock)
