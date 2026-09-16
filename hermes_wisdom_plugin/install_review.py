"""Hash-bound preparation and verification for manual Wisdom installs.

The reference resolver and core plan/apply rechecks are extracted from Agent
0924f7946b. Filesystem mutation and Gateway installation recording remain in a
separate executor; this module only prepares an immutable owner-bound review.
"""

from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from .compatibility import evaluate
from .contract import (
    SystemSpecification,
    org_directory_name,
    parse_manifest_bytes,
    sha256_address,
)
from .errors import WisdomAuthError, WisdomValidationError
from .package import PackagePolicyError, verify_content_files


_OWNER_FIELDS = (
    "profile_name", "profile_home", "account_id",
    "organization_id", "base_url", "installation_id",
)
_UPDATE_MODES = frozenset({"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"})
_REVIEW_FIELDS = (
    "skill_id",
    "slug",
    "version",
    "content_hash",
    "manifest_hash",
    "takedown_generation",
    "update_mode",
    "manifest",
    "files",
    "compatibility",
    "server_checks",
    "local_scan",
    "policy",
    "permitted_actions",
    "hashes",
    "allowed",
)


@dataclass(frozen=True, slots=True)
class VerifiedInstall:
    """Exact reviewed package ready for a separate transactional executor."""

    plan: dict[str, Any]
    files: list[tuple[str, str, bytes]]
    baseline: dict[str, str]


class InstallReviewer:  # pylint: disable=too-few-public-methods
    """Prepare and recheck immutable manual-install consent facts."""

    def __init__(
        self,
        client: Any,
        store: Any,
        receipts: Any,
        host: Any,
        owner: Callable[[], dict[str, Any]],
    ) -> None:
        if not callable(owner):
            raise TypeError("owner must be callable")
        self.client = client
        self.store = store
        self.receipts = receipts
        self.host = host
        self.owner = owner

    @staticmethod
    def _resolve_reference(reference: str) -> tuple[str, int | None]:
        if not isinstance(reference, str) or not reference.strip():
            raise PackagePolicyError("invalid Wisdom install reference")
        parsed = urlparse(reference)
        raw = (
            parsed.path.rstrip("/").split("/")[-1]
            if parsed.scheme in {"http", "https"}
            else reference
        )
        if parsed.scheme in {"http", "https"}:
            selected = parse_qs(parsed.query).get("version", [])
            if selected:
                raw_version = selected[-1]
                if not raw_version.isdigit() or int(raw_version) < 1:
                    raise PackagePolicyError("invalid Wisdom version selector")
                if not raw:
                    raise PackagePolicyError("invalid Wisdom install reference")
                return raw, int(raw_version)
        if "@v" in raw:
            skill_id, raw_version = raw.rsplit("@v", 1)
            if (
                not skill_id
                or not raw_version.isdigit()
                or int(raw_version) < 1
            ):
                raise PackagePolicyError("invalid Wisdom version selector")
            return skill_id, int(raw_version)
        if not raw:
            raise PackagePolicyError("invalid Wisdom install reference")
        return raw, None

    def _owner_snapshot(self) -> dict[str, Any]:
        try:
            value = self.owner()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise WisdomAuthError("Installation owner is unavailable") from exc
        if not isinstance(value, dict) or any(
            not isinstance(value.get(field), str) or not value[field]
            for field in _OWNER_FIELDS
        ):
            raise WisdomAuthError("Installation owner is unavailable")
        return copy.deepcopy(value)

    def _check_owner(self, expected: dict[str, Any]) -> None:
        if self._owner_snapshot() != expected:
            raise WisdomAuthError("Installation owner changed during review")

    def _request(self, expected: dict[str, Any], operation: Callable[[], Any]) -> Any:
        self._check_owner(expected)
        try:
            return operation()
        finally:
            self._check_owner(expected)

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise WisdomValidationError(f"Gateway returned an invalid {field}")
        return value

    @staticmethod
    def _generation(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WisdomValidationError(
                "Gateway omitted the install authorization generation"
            )
        return value

    @staticmethod
    def _update_mode(value: object) -> str | None:
        if value is not None and value not in _UPDATE_MODES:
            raise PackagePolicyError("invalid Wisdom update mode")
        return value if isinstance(value, str) else None

    def _reject_existing(
        self,
        skill_id: str,
        owner: dict[str, Any],
        slug: str | None = None,
        *,
        check_existing: bool = True,
    ) -> None:
        try:
            existing = self.store.installation(skill_id, owner)
            any_owner = self.store.installation(skill_id)
            installations = self.store.installations()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise PackagePolicyError("managed install state is unavailable") from exc
        if existing is not None and not isinstance(existing, dict):
            raise PackagePolicyError("managed install state is invalid")
        if existing and existing.get("state") == "active" and check_existing:
            raise PackagePolicyError(
                "skill is already managed; use the update workflow"
            )
        if any_owner is not None and not isinstance(any_owner, dict):
            raise PackagePolicyError("managed install state is invalid")
        if any_owner and any_owner.get("owner") != owner:
            raise PackagePolicyError(
                "skill is already managed by another installation owner"
            )
        if not isinstance(installations, list):
            raise PackagePolicyError("managed install state is invalid")
        if slug is None or not check_existing:
            return
        target = (
            self.host.skills_root
            / "_wisdom"
            / org_directory_name(owner["organization_id"])
            / slug
        )
        self.host.validate_target(target)
        target_resolved = target.resolve()
        for item in installations:
            if not isinstance(item, dict):
                raise PackagePolicyError("managed install state is invalid")
            raw_target = item.get("target_path")
            if (
                item.get("state") == "active"
                and isinstance(raw_target, str)
                and Path(raw_target).expanduser().resolve() == target_resolved
            ):
                raise PackagePolicyError(
                    "install target belongs to another managed skill"
                )
        if target.exists():
            raise PackagePolicyError(
                "install target already exists outside this managed installation"
            )

    @staticmethod
    def _published_versions(values: object) -> list[int]:
        if not isinstance(values, list):
            raise WisdomValidationError("Gateway returned an invalid version index")
        versions: list[int] = []
        for item in values:
            if not isinstance(item, dict):
                raise WisdomValidationError("Gateway returned an invalid version index")
            if item.get("state", "published") != "published":
                continue
            versions.append(InstallReviewer._positive_int(item.get("version"), "version"))
        if not versions:
            raise PackagePolicyError("Wisdom skill has no published versions")
        return versions

    def _stage_and_scan(  # pylint: disable=too-many-locals
        self, files: list[tuple[str, str, bytes]]
    ) -> dict[str, object]:
        skills_root = self.host.skills_root
        try:
            profile_home = getattr(self.host, "profile_home", skills_root.parent)
            if not isinstance(profile_home, Path):
                raise PackagePolicyError("Wisdom review staging is unsafe")
            for directory in (profile_home, skills_root):
                if directory.is_symlink() or (
                    hasattr(directory, "is_junction") and directory.is_junction()
                ):
                    raise PackagePolicyError("Wisdom review staging is unsafe")
            skills_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            hub = skills_root / ".hub"
            staging_root = hub / "collective-wisdom"
            for directory in (hub, staging_root):
                if directory.is_symlink() or (
                    hasattr(directory, "is_junction") and directory.is_junction()
                ):
                    raise PackagePolicyError("Wisdom review staging is unsafe")
                directory.mkdir(exist_ok=True, mode=0o700)
            staging_root.chmod(0o700)
            with tempfile.TemporaryDirectory(prefix="review-", dir=staging_root) as raw:
                staging = Path(raw)
                for relative, _mode, body in files:
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.write_bytes(body)
                    destination.chmod(0o600)
                result = self.host.scan(staging)
        except OSError as exc:
            raise PackagePolicyError("Wisdom review staging is unavailable") from exc
        if not isinstance(result, dict) or set(result) != {
            "allowed",
            "verdict",
            "summary",
            "findings",
        }:
            raise PackagePolicyError("Hermes host skill scan returned an invalid result")
        allowed = result.get("allowed")
        if allowed is not None and not isinstance(allowed, bool):
            raise PackagePolicyError("Hermes host skill scan returned an invalid result")
        return copy.deepcopy(result)

    @staticmethod
    def _manifest(files: list[tuple[str, str, bytes]]) -> tuple[Any, bytes]:
        bodies = [body for path, _mode, body in files if path == "skill.manifest.json"]
        if len(bodies) != 1:
            raise WisdomValidationError("version content has no unique package manifest")
        body = bodies[0]
        try:
            return parse_manifest_bytes(body), body
        except (UnicodeError, ValueError, ValidationError) as exc:
            raise WisdomValidationError("version package manifest is invalid") from exc

    def _prepare(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        skill_id: str,
        selected_version: int | None,
        update_mode: str | None,
        owner: dict[str, Any],
        *,
        check_existing: bool,
    ) -> tuple[dict[str, Any], list[tuple[str, str, bytes]], dict[str, str]]:
        self._reject_existing(skill_id, owner, check_existing=check_existing)
        detail = self._request(owner, lambda: self.client.skill(skill_id))
        skill = getattr(detail, "skill", None)
        versions = getattr(detail, "versions", None)
        if not isinstance(skill, dict) or skill.get("state") != "active":
            raise PackagePolicyError("only active Wisdom skills can be installed")
        returned_id = skill.get("id")
        if returned_id is not None and returned_id != skill_id:
            raise WisdomValidationError("Gateway returned a different Wisdom skill")
        slug = skill.get("slug") or skill_id
        if not isinstance(slug, str) or not slug:
            raise WisdomValidationError("Gateway returned an invalid skill slug")
        self._reject_existing(
            skill_id, owner, slug, check_existing=check_existing
        )
        published_versions = self._published_versions(versions)
        version_number = selected_version or max(published_versions)
        if version_number not in published_versions:
            raise PackagePolicyError("selected Wisdom version is not published")
        version_detail = self._request(
            owner, lambda: self.client.version(skill_id, version_number)
        )
        version_skill = getattr(version_detail, "skill", None)
        version_metadata = getattr(version_detail, "version", None)
        if not isinstance(version_skill, dict) or not isinstance(version_metadata, dict):
            raise WisdomValidationError("Gateway returned invalid version metadata")
        if (
            version_skill.get("id", skill_id) != skill_id
            or version_skill.get("state") != "active"
            or version_skill.get("takedown_generation") != skill.get("takedown_generation")
        ):
            raise WisdomValidationError("Gateway version skill metadata changed")
        metadata_version = version_metadata.get("version")
        if metadata_version is not None and metadata_version != version_number:
            raise WisdomValidationError("Gateway returned a different Wisdom version")
        generation = self._generation(skill.get("takedown_generation"))
        content, raw_files = self._request(
            owner,
            lambda: self.client.content(
                skill_id,
                version_number,
                installation_id=owner["installation_id"],
                takedown_generation=generation,
            ),
        )
        if not isinstance(raw_files, list):
            raise WisdomValidationError("Gateway returned invalid package content")
        files = list(raw_files)
        try:
            records, content_hash = verify_content_files(files)
        except (TypeError, ValueError) as exc:
            raise WisdomValidationError("Gateway returned invalid package content") from exc
        response_hash = getattr(content, "content_hash", None)
        if not isinstance(response_hash, str) or response_hash != content_hash:
            raise WisdomValidationError("Gateway package content hash does not match")
        manifest, manifest_body = self._manifest(files)
        manifest_hash = sha256_address(manifest_body)
        response_commit = getattr(content, "commit", None)
        if (
            version_metadata.get("content_hash") != content_hash
            or not isinstance(response_commit, str)
            or version_metadata.get("commit") != response_commit
            or (
                version_metadata.get("package_manifest_hash") is not None
                and version_metadata.get("package_manifest_hash") != manifest_hash
            )
        ):
            raise WisdomValidationError(
                "version metadata does not match the exact package content"
            )
        declared_specification = version_metadata.get("system_spec")
        try:
            declared = SystemSpecification.model_validate(declared_specification)
        except (TypeError, ValueError, ValidationError) as exc:
            raise WisdomValidationError(
                "version metadata does not match the exact package manifest"
            ) from exc
        if declared != manifest.requirements:
            raise WisdomValidationError(
                "version metadata does not match the exact package manifest"
            )
        local = self.host.capabilities(manifest.requirements)
        evaluated = evaluate(manifest.requirements, local)
        compatibility = {
            "outcome": evaluated.outcome,
            "satisfied": list(evaluated.satisfied),
            "setup_actions": list(evaluated.setup_actions),
            "limitations": list(evaluated.limitations),
            "blocked": list(evaluated.blocked),
        }
        local_scan = self._stage_and_scan(files)
        scan_allowed = local_scan["allowed"]
        allowed = (
            compatibility["outcome"] != "blocked_pending_action"
            and scan_allowed is not False
        )
        sizes = {path: len(body) for path, _mode, body in files}
        inventory = [
            {
                "path": record.path,
                "mode": record.mode,
                "hash": record.hash,
                "size": sizes[record.path],
            }
            for record in records
        ]
        server_checks = {
            key: copy.deepcopy(version_metadata.get(key))
            for key in (
                "security_check",
                "professionalism_check",
                "scan",
                "verified_facts",
            )
            if key in version_metadata
        }
        review = {
            "skill_id": skill_id,
            "slug": slug,
            "version": version_number,
            "content_hash": content_hash,
            "manifest_hash": manifest_hash,
            "takedown_generation": generation,
            "update_mode": update_mode,
            "manifest": manifest.model_dump(mode="json"),
            "files": inventory,
            "compatibility": compatibility,
            "server_checks": server_checks,
            "local_scan": local_scan,
            "policy": {
                "requested_update_mode": update_mode,
                "requires_explicit_confirmation": True,
                "scan_requires_confirmation": scan_allowed is None,
                "compatibility_requires_confirmation": compatibility["outcome"]
                in {"partial", "compatible_after_setup"},
            },
            "permitted_actions": (
                ["confirm_install", "recheck"] if allowed else ["recheck"]
            ),
            "hashes": {"content": content_hash, "manifest": manifest_hash},
            "allowed": allowed,
        }
        baseline = {record.path: record.hash for record in records}
        self._check_owner(owner)
        return review, files, baseline

    def plan(self, reference: str, update_mode: str | None = None) -> dict[str, Any]:
        """Prepare exact install facts and persist an owner-bound review receipt."""

        mode = self._update_mode(update_mode)
        skill_id, version = self._resolve_reference(reference)
        owner = self._owner_snapshot()
        review, _files, _baseline = self._prepare(
            skill_id, version, mode, owner, check_existing=True
        )
        self._check_owner(owner)
        try:
            plan = self.receipts.create(review)
        except (WisdomAuthError, WisdomValidationError):
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise WisdomValidationError("Install review could not be persisted") from exc
        if not isinstance(plan, dict) or plan.get("owner") != owner:
            raise WisdomValidationError("Install receipt has an invalid owner binding")
        self._check_owner(owner)
        return plan

    def verify(self, plan: dict[str, Any]) -> VerifiedInstall:
        """Re-download and recheck every reviewed fact before local mutation."""

        if not isinstance(plan, dict) or plan.get("operation") != "install":
            raise WisdomValidationError("Invalid install review")
        expected_owner = plan.get("owner")
        if not isinstance(expected_owner, dict):
            raise WisdomAuthError("Install review has no owner binding")
        owner = self._owner_snapshot()
        if owner != expected_owner:
            raise WisdomAuthError("Install review does not belong to this enrolment")
        skill_id = plan.get("skill_id")
        slug = plan.get("slug")
        if not isinstance(skill_id, str) or not skill_id or not isinstance(slug, str):
            raise WisdomValidationError("Invalid install review")
        version = self._positive_int(plan.get("version"), "version")
        self._generation(plan.get("takedown_generation"))
        mode = self._update_mode(plan.get("update_mode"))
        current, files, baseline = self._prepare(
            skill_id, version, mode, owner, check_existing=False
        )
        reviewed = {field: plan.get(field) for field in _REVIEW_FIELDS}
        if reviewed != current:
            raise WisdomValidationError(
                "Install review changed; create a new review before installing"
            )
        if current["local_scan"]["allowed"] is False:
            raise PackagePolicyError("Hermes host skill policy blocks installation")
        if current["compatibility"]["outcome"] == "blocked_pending_action":
            raise PackagePolicyError(
                "blocked compatibility requirements prevent installation"
            )
        if not current["allowed"]:
            raise PackagePolicyError("reviewed installation is not allowed")
        self._check_owner(owner)
        return VerifiedInstall(
            plan=copy.deepcopy(plan),
            files=[(path, mode_value, bytes(body)) for path, mode_value, body in files],
            baseline=baseline,
        )
