"""Proof-backed enrollment of an author's published source in managed updates."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from .contract import sha256_address
from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from .managed_target import publisher_provenance, validate_managed_target
from .package import PackagePolicyError, source_fingerprint, verify_content_files
from .package_application import tree_hashes
from .setup_lock import managed_operation_guard

_UPDATE_MODES = frozenset({"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"})


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class PublisherEnrollment:  # pylint: disable=too-few-public-methods
    """Link an unchanged reviewed author source after exact remote publication proof."""

    def __init__(self, direct, installed, client, host, owner: Callable[[], dict[str, str]]):
        self.direct = direct
        self.installed = installed
        self.client = client
        self.host = host
        self.owner_provider = owner

    def link(self, operation, result, *, may_apply=None) -> dict[str, Any]:
        """Register one proven publication and atomically retain its source path."""

        owner = self._owner()
        self._current(may_apply)
        if operation.get("owner") != owner or operation.get("publication_state") != "published":
            raise WisdomAuthError("published source owner or state is unavailable")
        draft = self.direct.draft(str(operation.get("local_draft_id") or ""))
        if not isinstance(draft, dict) or draft.get("owner") != owner:
            raise WisdomAuthError("published source draft owner changed")
        source = Path(str(draft.get("source_path") or ""))
        source_hash = str(draft.get("source_hash") or "")
        baseline = self._source_snapshot(source, source_hash)
        publication = self._publication_identity(operation, result)
        proof = self._published_proof(publication, owner)
        if proof["content_hash"] != operation.get("hashes", {}).get("content"):
            raise WisdomValidationError("published package does not match the reviewed publication")
        if proof["manifest_hash"] != operation.get("hashes", {}).get("package_manifest"):
            raise WisdomValidationError("published manifest does not match reviewed publication")
        if proof["commit"] != operation.get("request", {}).get("commit"):
            raise WisdomValidationError("published commit does not match the reviewed publication")
        publication_revision = {
            "operation_id": operation["id"],
            "remote_draft_id": operation["remote_draft_id"],
            "proposal_id": publication["proposal_id"],
            "skill_id": proof["skill_id"],
            "version": proof["version"],
            "content_hash": proof["content_hash"],
            "commit": proof["commit"],
            "manifest_hash": proof["manifest_hash"],
        }
        record = {
            "skill_id": proof["skill_id"],
            "org_id": owner["organization_id"],
            "slug": proof["slug"],
            "version": proof["version"],
            "content_hash": proof["content_hash"],
            "manifest_hash": proof["manifest_hash"],
            "baseline": baseline,
            "target_path": str(source),
            "update_mode": "MANUAL",
            "state": "active",
            "owner": owner,
            "publisher_revision": publication_revision,
            "target_provenance": publisher_provenance(
                owner=owner, source_path=source, source_hash=source_hash,
                publication=publication_revision,
            ),
        }
        record, previous = self._available_record(record, owner)
        validate_managed_target(self.host, record, owner)
        mode = self._remote_registration(proof, owner, previous, may_apply)
        self._current(may_apply)
        self._assert_owner(owner)
        if self._source_snapshot(source, source_hash) != baseline:
            raise WisdomConflict("published source changed before managed enrollment completed")
        record["update_mode"] = mode
        with managed_operation_guard(Path(self.installed.root), proof["skill_id"]):
            self._assert_owner(owner)
            self._current(may_apply)
            if self._source_snapshot(source, source_hash) != baseline:
                raise WisdomConflict("published source changed before managed enrollment completed")
            validate_managed_target(self.host, record, owner)
            self.installed.record_publisher_install(record, previous=previous)
        return {
            "state": "managed", "skill_id": proof["skill_id"],
            "version": proof["version"], "path": str(source),
            "effective_update_mode": mode,
        }

    def link_existing(self, operation, *, may_apply=None) -> dict[str, Any]:
        """Use bounded read-only catalog proof before explicit install registration."""

        result = self._discover_publication(operation, may_apply)
        return self.link(operation, result, may_apply=may_apply)

    def _discover_publication(self, operation, may_apply):
        owner = self._owner()
        if operation.get("owner") != owner or operation.get("publication_state") != "published":
            raise WisdomAuthError("published source owner or state is unavailable")
        request = operation.get("request")
        if not isinstance(request, dict):
            raise WisdomValidationError("reviewed publication identity is unavailable")
        matches = []
        for skill_id in self._candidate_skills(request.get("slug"), owner, may_apply):
            self._current(may_apply)
            self._assert_owner(owner)
            detail = self.client.skill(skill_id)
            self._assert_owner(owner)
            matches.extend(
                (skill_id, version)
                for version in self._matching_versions(detail, request, owner)
            )
        if len(matches) != 1:
            raise WisdomConflict("No unique exact published version is visible for this source")
        publication_result = operation.get("publication_result")
        proposal = publication_result.get("proposal_id") if isinstance(
            publication_result, dict
        ) else None
        return {
            "state": "published", "skill_id": matches[0][0], "version": matches[0][1],
            "proposal_id": proposal,
        }

    def _candidate_skills(self, slug, owner, may_apply):
        cursor, seen, candidates = None, set(), set()
        for _page in range(100):
            self._current(may_apply)
            self._assert_owner(owner)
            page = self.client.list_skills(cursor=cursor)
            self._assert_owner(owner)
            for item in _value(page, "skills", []):
                row = item.model_dump(mode="json") if callable(
                    getattr(item, "model_dump", None)
                ) else item
                if isinstance(row, dict) and row.get("state") == "active":
                    if row.get("slug") == slug and isinstance(row.get("id"), str):
                        candidates.add(row["id"])
            cursor = _value(page, "next_cursor")
            if not cursor:
                return sorted(candidates)
            if not isinstance(cursor, str) or cursor in seen:
                raise WisdomValidationError("Gateway publication catalog pagination is invalid")
            seen.add(cursor)
        raise WisdomValidationError("Gateway publication catalog exceeded its bounded search")

    @staticmethod
    def _matching_versions(detail, request, owner):
        versions = _value(detail, "versions")
        if not isinstance(versions, list):
            raise WisdomValidationError("Gateway publication history is invalid")
        expected = {
            "content_hash": request.get("content_hash"),
            "commit": request.get("commit"),
            "published_by_user_id": owner["account_id"],
        }
        for metadata in versions:
            if not isinstance(metadata, dict) or not _positive_integer(metadata.get("version")):
                continue
            if metadata.get("state", "published") == "published" and all(
                metadata.get(key) == value for key, value in expected.items()
            ):
                yield metadata["version"]

    def _published_metadata(self, publication, owner):
        skill_id, version = publication["skill_id"], publication["version"]
        self._assert_owner(owner)
        detail = self.client.skill(skill_id)
        self._assert_owner(owner)
        skill = _value(detail, "skill")
        versions = _value(detail, "versions")
        if (
            not isinstance(skill, dict)
            or skill.get("id") != skill_id
            or skill.get("state") != "active"
            or not isinstance(versions, list)
            or len([row for row in versions if isinstance(row, dict)
                    and row.get("version") == version
                    and row.get("state", "published") == "published"]) != 1
        ):
            raise WisdomValidationError("Gateway cannot prove the exact published skill owner")
        generation = skill.get("takedown_generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise WisdomValidationError("Gateway omitted publication install authority")
        version_detail = self.client.version(skill_id, version)
        self._assert_owner(owner)
        version_skill = _value(version_detail, "skill")
        metadata = _value(version_detail, "version")
        if not isinstance(version_skill, dict) or not isinstance(metadata, dict):
            raise WisdomValidationError("Gateway returned invalid publication metadata")
        if (
            version_skill.get("id") != skill_id
            or metadata.get("published_by_user_id") != owner["account_id"]
            or version_skill.get("takedown_generation") != generation
            or metadata.get("version") != version
            or metadata.get("state", "published") != "published"
        ):
            raise WisdomValidationError("Gateway publication identity changed")
        return skill, metadata, generation

    def _published_proof(self, publication, owner):
        skill_id, version = publication["skill_id"], publication["version"]
        skill, metadata, generation = self._published_metadata(publication, owner)
        content, files = self.client.content(
            skill_id, version, installation_id=owner["installation_id"],
            takedown_generation=generation,
        )
        self._assert_owner(owner)
        _records, content_hash = verify_content_files(files)
        manifest = next(
            (body for path, _mode, body in files if path == "skill.manifest.json"), None,
        )
        if manifest is None:
            raise WisdomValidationError("published package manifest is unavailable")

        manifest_hash = sha256_address(manifest)
        commit = _value(content, "commit")
        if (
            _value(content, "content_hash") != content_hash
            or metadata.get("content_hash") != content_hash
            or metadata.get("commit") != commit
            or metadata.get("package_manifest_hash") != manifest_hash
            or publication["content_hash"] != content_hash
        ):
            raise WisdomValidationError("Gateway publication proof differs from package bytes")
        return {
            "skill_id": skill_id, "slug": skill.get("slug"), "version": version,
            "content_hash": content_hash, "manifest_hash": manifest_hash,
            "commit": commit, "takedown_generation": generation,
        }

    def _remote_registration(self, proof, owner, previous, may_apply):
        self._assert_owner(owner)
        rows = self.client.installations(owner["installation_id"])
        self._assert_owner(owner)
        if not isinstance(rows, list):
            raise WisdomConflict("Gateway managed publication state is unavailable")
        matches = [row for row in rows if isinstance(row, dict)
                   and row.get("skill_id") == proof["skill_id"]]
        if len(matches) > 1:
            raise WisdomConflict("Gateway installation state is ambiguous")
        if matches:
            row = matches[0]
            mode = row.get("update_mode")
            if (
                mode not in _UPDATE_MODES
                or row.get("skill_state") != "active"
                or row.get("takedown_generation") != proof["takedown_generation"]
                or not isinstance(row.get("takedown_generation"), int)
                or isinstance(row.get("takedown_generation"), bool)
            ):
                raise WisdomConflict("Gateway already records a different managed publication")
            remote_version = row.get("installed_version")
            if remote_version == proof["version"]:
                return mode
            if (
                previous is None
                or remote_version != previous.get("version")
                or mode != previous.get("update_mode")
            ):
                raise WisdomConflict("Gateway already records a different managed publication")
            requested_mode = mode
        else:
            if previous is not None:
                raise WisdomConflict("Gateway no longer records the managed publication")
            requested_mode = "MANUAL"
        self._current(may_apply)
        self._assert_owner(owner)
        acknowledged = self.client.record_install(
            skill_id=proof["skill_id"], installation_id=owner["installation_id"],
            version=proof["version"], takedown_generation=proof["takedown_generation"],
            update_mode=requested_mode,
        )
        self._assert_owner(owner)
        mode = _value(acknowledged, "effective_update_mode")
        if mode not in _UPDATE_MODES:
            raise WisdomValidationError("Gateway returned an invalid managed update mode")
        return mode

    def _available_record(self, record, owner):
        target = Path(record["target_path"])
        for existing in self.installed.installations():
            if not isinstance(existing, dict) or existing.get("state") != "active":
                continue
            if existing.get("skill_id") == record["skill_id"]:
                return self._same_source_record(record, existing, owner)
            if Path(str(existing.get("target_path") or "")) == target:
                raise WisdomConflict("publisher source already belongs to another managed skill")
        return record, None

    def _same_source_record(self, record, existing, owner):
        provenance = existing.get("target_provenance")
        same_source = (
            existing.get("owner") == owner
            and existing.get("org_id") == owner["organization_id"]
            and existing.get("slug") == record["slug"]
            and existing.get("target_path") == record["target_path"]
            and existing.get("update_mode") in _UPDATE_MODES
            and isinstance(provenance, dict)
            and provenance.get("kind") == "publisher_source"
            and provenance.get("owner") == owner
            and provenance.get("source_path") == record["target_path"]
        )
        if not same_source:
            raise WisdomConflict("published skill is already managed by another source")
        validate_managed_target(self.host, existing, owner)
        old_version, new_version = existing.get("version"), record.get("version")
        if not _positive_integer(old_version) or new_version < old_version:
            raise WisdomConflict("published skill version cannot move backward")
        revised = {
            **record,
            "update_mode": existing["update_mode"],
            "target_provenance": copy.deepcopy(provenance),
        }
        if new_version == old_version:
            old_revision = existing.get("publisher_revision")
            revision_fields = (
                "version", "content_hash", "manifest_hash", "baseline", "target_path",
                "slug", "org_id", "owner", "state",
            )
            if (
                any(existing.get(key) != revised.get(key) for key in revision_fields)
                or old_revision != record.get("publisher_revision")
            ):
                raise WisdomConflict("published skill version already has different proof")
            return copy.deepcopy(existing), copy.deepcopy(existing)
        return revised, copy.deepcopy(existing)

    @staticmethod
    def _publication_identity(operation, result):
        skill_id = result.get("skill_id") if isinstance(result, dict) else None
        version = result.get("version") if isinstance(result, dict) else None
        proposal = result.get("proposal_id") if isinstance(result, dict) else None
        content_hash = operation.get("hashes", {}).get("content")
        if (
            not isinstance(result, dict) or result.get("state") != "published"
            or not isinstance(skill_id, str) or not skill_id
        ):
            raise WisdomValidationError("Gateway published without exact managed identity")
        if not _positive_integer(version) or (
            proposal is not None and not _positive_integer(proposal)
        ) or not isinstance(content_hash, str):
            raise WisdomValidationError("Gateway published without exact managed identity")
        return {
            "skill_id": skill_id, "version": version, "proposal_id": proposal,
            "content_hash": content_hash,
        }

    @staticmethod
    def _source_snapshot(source: Path, expected_hash: str) -> dict[str, str]:
        if source_fingerprint(source) != expected_hash:
            raise WisdomConflict("published source changed after its reviewed preparation")
        baseline = tree_hashes(source)
        if not isinstance(baseline, dict) or not baseline:
            raise PackagePolicyError("published source bytes are unavailable or unsafe")
        if source_fingerprint(source) != expected_hash or tree_hashes(source) != baseline:
            raise WisdomConflict("published source changed while managed enrollment was prepared")
        return baseline

    def _owner(self):
        owner = self.owner_provider()
        if not isinstance(owner, dict) or owner.keys() != {
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        }:
            raise WisdomAuthError("publisher managed enrollment owner is unavailable")
        if not all(isinstance(value, str) and value for value in owner.values()):
            raise WisdomAuthError("publisher managed enrollment owner is unavailable")
        return copy.deepcopy(owner)

    def _assert_owner(self, expected):
        if self._owner() != expected:
            raise WisdomAuthError("publisher managed enrollment owner changed")

    @staticmethod
    def _current(may_apply):
        if may_apply is not None and may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
