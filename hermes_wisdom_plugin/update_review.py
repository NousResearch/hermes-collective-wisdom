"""Read-only version checks and exact owner-bound manual update reviews."""

# Keep exact authority and phase predicates together for an auditable recovery path.
# pylint: disable=duplicate-code

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .contract import SystemSpecification, parse_manifest_bytes
from .errors import WisdomAuthError, WisdomValidationError
from .install_execution import (
    _tree_hashes,
)
from .install_review import InstallReviewer
from .managed_target import (
    REMOVED_TEXT, RemovedOutsideWisdom, local_provenance, removed_outside_wisdom,
    validate_managed_target,
)
from .package import PackagePolicyError
from .qualification import _eligible_path

_UPDATE_MODES = frozenset({"MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"})
_INCOMING_FIELDS = (
    "slug", "version", "content_hash", "manifest_hash", "takedown_generation",
    "update_mode", "manifest", "files", "compatibility", "server_checks",
    "local_scan", "security_check", "hashes", "setup_commands",
)


def _host_skill_resolver(skill_name: str) -> Path | None:
    """Resolve the active profile's local skill through the current host API."""

    try:
        resolver = getattr(import_module("tools.skill_usage"), "_find_skill_dir")
        return resolver(skill_name)
    except (AttributeError, ImportError, RuntimeError, ValueError, OSError):
        return None


class UpdateAuthorityRejected(PackagePolicyError):
    """Gateway definitively removed this enrolment's update authority."""


@dataclass(frozen=True, slots=True)
class VerifiedUpdate:
    """A fully reauthorized update plus its exact prior and incoming trees."""

    plan: dict[str, Any]
    files: list[tuple[str, str, bytes]]
    baseline: dict[str, str]
    previous_snapshot: dict[str, str]


def sensitive_expansion(
    previous: SystemSpecification, incoming: SystemSpecification
) -> list[str]:
    """Describe newly requested sensitive capabilities without granting them."""

    reasons: list[str] = []

    def added(label: str, old: list[str], new: list[str]) -> None:
        values = sorted(set(new) - set(old))
        if values:
            reasons.append(f"new {label}: {', '.join(values)}")

    added("credentials", previous.credentials, incoming.credentials)
    added("connections", previous.connections, incoming.connections)
    added("filesystem reads", previous.filesystem.read, incoming.filesystem.read)
    added("filesystem writes", previous.filesystem.write, incoming.filesystem.write)
    added(
        "network destinations",
        previous.network.destinations,
        incoming.network.destinations,
    )
    added("hardware requirements", previous.hardware, incoming.hardware)
    old_tools = {item.name: item for item in previous.tools}
    for tool in incoming.tools:
        before = old_tools.get(tool.name)
        if tool.requires_admin and (before is None or not before.requires_admin):
            reasons.append(f"new privileged tool requirement: {tool.name}")
    for field in ("shell", "browser", "code"):
        if getattr(incoming.runtime, field) and not getattr(previous.runtime, field):
            reasons.append(f"new {field} permission")
    if previous.runtime.sandbox and not incoming.runtime.sandbox:
        reasons.append("sandbox no longer required")
    return reasons


def validate_update_target(
    host: Any, record: dict, owner: dict, *, require_tree: bool = True,
) -> Path:
    """Require canonical or exact publisher-source provenance before access."""
    return validate_managed_target(host, record, owner, require_tree=require_tree)


class UpdateReviewer:  # pylint: disable=too-many-instance-attributes
    """Check and review one already-managed skill without implicit mutation."""

    def __init__(
        self,
        client: Any,
        store: Any,
        receipts: Any,
        host: Any,
        owner: Callable[[], dict[str, Any]],
    ) -> None:
        self.client = client
        self.store = store
        self.receipts = receipts
        self.host = host
        self.owner = owner
        self.skill_resolver = _host_skill_resolver
        self.install_reviewer = InstallReviewer(client, store, receipts, host, owner)

    def _owner(self) -> dict[str, Any]:
        return self.install_reviewer._owner_snapshot()  # pylint: disable=protected-access

    def _installation(
        self, skill_id: str, owner: dict[str, Any], *, slug: str | None = None,
        require_tree: bool = True,
    ) -> dict[str, Any]:
        installation = self.store.installation(skill_id, owner=owner)
        if not isinstance(installation, dict) or installation.get("state") != "active":
            any_owner = self.store.installation(skill_id)
            if isinstance(any_owner, dict) and any_owner.get("owner") != owner:
                raise WisdomAuthError("managed installation belongs to another enrolment")
            if not isinstance(slug, str) or not slug:
                raise PackagePolicyError(
                    "no active managed installation was found; use the catalog skill name "
                    "to review an existing local copy"
                )
            target = _eligible_path(
                slug, skills_root=Path(self.host.skills_root),
                skill_resolver=self.skill_resolver,
            )
            if target is None:
                raise PackagePolicyError(
                    "no local copy exists for this Wisdom skill; "
                    "update will not install it implicitly"
                )
            baseline = _tree_hashes(target)
            if not baseline:
                raise PackagePolicyError("local skill bytes are unavailable or unsafe")
            self._reject_local_collision(skill_id, target)
            installation = {
                "skill_id": skill_id, "org_id": owner["organization_id"],
                "slug": slug, "version": None, "content_hash": None,
                "baseline": baseline, "target_path": str(target),
                "target_provenance": local_provenance(owner=owner, source_path=target),
                "update_mode": "MANUAL", "state": "active", "owner": owner,
            }
        if installation.get("owner") != owner:
            raise WisdomAuthError("managed installation belongs to another enrolment")
        if require_tree and removed_outside_wisdom(self.host, installation, owner):
            raise RemovedOutsideWisdom(str(installation.get("slug")), installation.get("version"))
        validate_update_target(
            self.host, installation, owner, require_tree=require_tree,
        )
        return copy.deepcopy(installation)

    def _reject_local_collision(self, skill_id, target):
        for row in self.store.installations():
            if (
                isinstance(row, dict) and row.get("state") == "active"
                and row.get("skill_id") != skill_id
                and row.get("target_path") == str(target)
            ):
                raise PackagePolicyError(
                    "local skill target belongs to another managed skill"
                )

    @staticmethod
    def _unknown_local(installation: dict[str, Any]) -> bool:
        provenance = installation.get("target_provenance")
        return (isinstance(provenance, dict) and provenance.get("kind") == "local_source"
                and installation.get("version") is None)

    def _catalog_authority(
        self, skill_id: str, owner: dict[str, Any], *, installed_version: int | None = None,
    ) -> dict[str, Any]:
        detail = self.install_reviewer._request(  # pylint: disable=protected-access
            owner, lambda: self.client.skill(skill_id)
        )
        skill = getattr(detail, "skill", None)
        versions = getattr(detail, "versions", None)
        if (
            not isinstance(skill, dict) or skill.get("id", skill_id) != skill_id
            or skill.get("state") != "active"
        ):
            raise PackagePolicyError(
                "only an active published Wisdom skill can update a local copy"
            )
        published = self.install_reviewer._published_versions(versions)  # pylint: disable=protected-access
        generation = self.install_reviewer._generation(  # pylint: disable=protected-access
            skill.get("takedown_generation")
        )
        return {
            "installed_version": installed_version, "latest_version": max(published),
            "takedown_generation": generation, "update_mode": "MANUAL",
            "skill_state": "active", "source": "catalog",
        }

    def _authority_detail(
        self, skill_id: str, owner: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        self.install_reviewer._check_owner(owner)  # pylint: disable=protected-access
        values = self.client.installations(owner["installation_id"])
        self.install_reviewer._check_owner(owner)  # pylint: disable=protected-access
        if not isinstance(values, list):
            return "unverifiable", None
        matches = [
            item for item in values
            if isinstance(item, dict) and item.get("skill_id") == skill_id
        ]
        if len(matches) > 1:
            return "unverifiable", None
        if not matches:
            return "absent", None
        authority = copy.deepcopy(matches[0])
        if self._authority_snapshot(authority) is None:
            return "unverifiable", None
        return "verified", authority

    @staticmethod
    def _authority_snapshot(  # pylint: disable=too-many-boolean-expressions
        value: dict[str, Any]
    ) -> dict[str, Any] | None:
        installed = value.get("installed_version")
        latest = value.get("latest_version")
        generation = value.get("takedown_generation")
        mode = value.get("update_mode")
        state = value.get("skill_state")
        if (
            not isinstance(installed, int) or isinstance(installed, bool) or installed < 1
            or not isinstance(latest, int) or isinstance(latest, bool) or latest < 1
            or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
            or mode not in _UPDATE_MODES
            or not isinstance(state, str) or not state
        ):
            return None
        return {
            "installed_version": installed,
            "latest_version": latest,
            "takedown_generation": generation,
            "update_mode": mode,
            "skill_state": state,
        }

    def check(self, skill_id: str, *, slug: str | None = None) -> dict[str, Any]:
        """Compare local and authoritative versions without package/feed side effects."""

        owner = self._owner()
        try:
            installation = self._installation(skill_id, owner, slug=slug)
        except RemovedOutsideWisdom as removed:
            return {
                "skill_id": skill_id, "slug": removed.slug, "state": "removed",
                "reason": REMOVED_TEXT, "installed_version": removed.version,
                "latest_version": None,
            }
        if self._unknown_local(installation):
            authority = self._catalog_authority(skill_id, owner)
            return {
                "skill_id": skill_id, "slug": installation["slug"],
                "installed_version": None, "latest_version": authority["latest_version"],
                "content_hash": None, "update_mode": "MANUAL",
                "state": "update_available", "local_baseline": "unknown",
                "preservation_required": True,
            }
        detail, authority = self._authority_detail(skill_id, owner)
        base = {
            "skill_id": skill_id,
            "installed_version": installation["version"],
            "content_hash": installation["content_hash"],
            "update_mode": installation["update_mode"],
        }
        if detail == "absent":
            authority = self._catalog_authority(
                skill_id, owner, installed_version=installation["version"],
            )
            detail = "verified"
        if detail != "verified" or authority is None:
            return {**base, "state": "unverifiable", "latest_version": None}
        state = authority.get("skill_state")
        if state != "active":
            return {
                **base,
                "state": state if state in {"archived", "taken_down"} else "unverifiable",
                "latest_version": authority.get("latest_version"),
            }
        checked = self._authority_snapshot(authority)
        if checked is None or checked["installed_version"] != installation["version"]:
            return {
                **base, "state": "unverifiable",
                "latest_version": authority.get("latest_version"),
            }
        return {
            **base,
            "state": (
                "update_available"
                if checked["latest_version"] > installation["version"]
                else "current"
            ),
            "latest_version": checked["latest_version"],
            "effective_update_mode": checked["update_mode"],
            "takedown_generation": checked["takedown_generation"],
        }

    def _planning_authority(self, skill_id, owner, installation, *, automatic):
        if self._unknown_local(installation):
            if automatic:
                raise PackagePolicyError(
                    "automatic updates require an existing managed installation"
                )
            return self._catalog_authority(skill_id, owner)
        detail, raw = self._authority_detail(skill_id, owner)
        if detail == "absent" and not automatic:
            authority = self._catalog_authority(
                skill_id, owner, installed_version=installation["version"],
            )
        else:
            authority = self._authority_snapshot(raw or {})
        if authority is None or authority["skill_state"] != "active":
            raise PackagePolicyError("Gateway does not authorize this managed update")
        if authority["installed_version"] != installation["version"]:
            raise PackagePolicyError("local and Gateway installed versions do not agree")
        if authority["latest_version"] <= installation["version"]:
            raise PackagePolicyError("managed installation is already current")
        return authority

    def plan(  # pylint: disable=too-many-locals,too-many-arguments
        self,
        skill_id: str,
        *,
        slug: str | None = None,
        preserve_modified: bool = False,
        accept_sensitive: bool = False,
        accept_partial: bool = False,
        accept_warnings: bool = False,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Create an exact review; acknowledgements become immutable receipt facts."""

        owner = self._owner()
        installation = self._installation(skill_id, owner, slug=slug)
        local_source = self._unknown_local(installation)
        authority = self._planning_authority(skill_id, owner, installation, automatic=automatic)
        if automatic and authority["update_mode"] not in {"AUTO_WITH_NOTICE", "REQUIRED"}:
            raise PackagePolicyError("manual update policy requires explicit review")
        if automatic and any((
            accept_sensitive, accept_partial, preserve_modified, accept_warnings,
        )):
            raise WisdomValidationError("automatic policy cannot accept manual acknowledgements")
        incoming, _files, baseline = self.install_reviewer._prepare(  # pylint: disable=protected-access
            skill_id,
            authority["latest_version"],
            authority["update_mode"],
            owner,
            check_existing=False,
            accept_warnings=accept_warnings,
            warning_operation="update",
        )
        target = Path(installation["target_path"])
        local_snapshot = _tree_hashes(target)
        if local_snapshot is None:
            raise PackagePolicyError("managed local bytes are unavailable or unsafe")
        modified = local_source or local_snapshot != installation.get("baseline")
        sensitive = self._sensitive(target, incoming["manifest"])
        outcome = incoming["compatibility"]["outcome"]
        partial = outcome in {"partial", "compatible_after_setup"}
        preserve_edited = preserve_modified or (
            automatic and authority["update_mode"] == "REQUIRED" and modified
        )
        preservation = self._preservation(
            installation,
            authority["latest_version"],
            modified=modified,
            accepted=preserve_edited,
        )
        manual_allowed = bool(
            incoming["allowed"]
            and (not modified or preserve_modified)
            and (not sensitive or accept_sensitive)
            and (not partial or accept_partial)
        )
        automatic_allowed = bool(
            incoming["allowed"]
            and outcome == "compatible"
            and incoming["local_scan"].get("allowed") is True
            and not incoming["local_scan"].get("findings")
            and not sensitive
            and (not modified or authority["update_mode"] == "REQUIRED")
        )
        allowed = automatic_allowed if automatic else manual_allowed
        if automatic and not allowed:
            raise PackagePolicyError(
                "automatic update requires manual review for compatibility, security, "
                "or local edits"
            )
        review = {
            **{field: copy.deepcopy(incoming[field]) for field in _INCOMING_FIELDS},
            "skill_id": skill_id,
            "from_version": installation.get("version"),
            "previous_content_hash": installation.get("content_hash"),
            "previous_baseline": copy.deepcopy(installation["baseline"]),
            "previous_installation": installation,
            "target_path": str(target),
            "target_provenance": copy.deepcopy(installation.get("target_provenance")),
            "local_snapshot": local_snapshot,
            "baseline": baseline,
            "authority": authority,
            "modified": modified,
            "local_baseline": "unknown" if local_source else "published",
            "sensitive_expansion": sensitive,
            "preservation": preservation,
            "policy": {
                **copy.deepcopy(incoming["policy"]),
                "manual_only": not automatic,
                "accepted_sensitive": bool(accept_sensitive),
                "accepted_partial": bool(accept_partial),
                "accepted_warnings": bool(accept_warnings),
                "accepted_modified_preservation": bool(preserve_edited),
            },
            "authorization": {
                "kind": "automatic_policy" if automatic else "manual_receipt",
                "mode": authority["update_mode"],
            },
            "user_confirmation": False if automatic else None,
            "allowed": allowed,
            "permitted_actions": (
                ["apply_automatic", "recheck"]
                if automatic
                else ["confirm_update", "recheck"] if allowed else ["recheck"]
            ),
        }
        self.install_reviewer._check_owner(owner)  # pylint: disable=protected-access
        plan = self.receipts.create(review)
        if plan.get("operation") != "update" or plan.get("owner") != owner:
            raise WisdomValidationError("Update receipt has an invalid owner binding")
        return plan

    def plan_automatic(self, skill_id: str) -> dict[str, Any]:
        """Create a fresh policy-authorized plan, distinct from human confirmation."""

        return self.plan(skill_id, automatic=True)

    def _preservation(
        self, installation: dict[str, Any], version: int, *, modified: bool, accepted: bool
    ) -> dict[str, Any]:
        if not modified:
            return {"action": "none", "destination": None}
        if not accepted:
            return {"action": "block", "destination": None}
        root = Path(self.host.skills_root).expanduser().absolute()
        slug = str(installation["slug"])
        destination = root / (
            f"{slug}-local-before-v{version}-{uuid.uuid4().hex[:12]}"
        )
        if destination.parent != root or destination.exists() or destination.is_symlink():
            raise PackagePolicyError("edited-copy preservation destination is unavailable")
        self.host.validate_target(Path(installation["target_path"]))
        return {"action": "preserve_unmanaged_copy", "destination": str(destination)}

    @staticmethod
    def _sensitive(target: Path, incoming_manifest: dict[str, Any]) -> list[str]:
        try:
            previous = parse_manifest_bytes((target / "skill.manifest.json").read_bytes())
            incoming = SystemSpecification.model_validate(incoming_manifest["requirements"])
        except (OSError, KeyError, TypeError, ValueError, ValidationError):
            return [
                "existing managed manifest is invalid; requirement expansion cannot be proven safe"
            ]
        return sensitive_expansion(previous.requirements, incoming)

    def _review_installation(self, plan, owner, recovery_phase):
        previous = plan.get("previous_installation")
        if not isinstance(previous, dict):
            raise WisdomValidationError("Invalid previous update installation")
        current = self.store.installation(plan["skill_id"], owner=owner)
        if (recovery_phase is not None and self._unknown_local(previous)
                and (current is None or current.get("state") != "active")):
            # The reviewed original can be in recovery while its replacement is not
            # yet recorded. Use the receipt's exact path/baseline, never rediscover it.
            any_owner = self.store.installation(plan["skill_id"])
            if any_owner is not None and any_owner.get("owner") != owner:
                raise WisdomAuthError("managed installation belongs to another enrolment")
            validate_update_target(self.host, previous, owner, require_tree=False)
            self._reject_local_collision(plan["skill_id"], Path(previous["target_path"]))
            return copy.deepcopy(previous)
        return self._installation(
            plan["skill_id"], owner, slug=previous.get("slug"),
            require_tree=recovery_phase is None,
        )

    def _verify_authority(self, plan, owner, recovery_phase):
        expected = plan.get("authority")
        if not isinstance(expected, dict):
            raise WisdomValidationError("Invalid update review authority")
        if expected.get("source") == "catalog":
            return self._verify_catalog_authority(plan, owner, recovery_phase)
        return self._verify_gateway_authority(plan, owner, recovery_phase)

    def _verify_catalog_authority(self, plan, owner, recovery_phase):
        expected = plan["authority"]
        current = self._catalog_authority(
            plan["skill_id"], owner, installed_version=plan.get("from_version"),
        )
        current["update_mode"] = expected["update_mode"]
        if "source" not in expected:
            current.pop("source")
        if recovery_phase in {"files_committed", "local_committed"}:
            current["latest_version"] = expected["latest_version"]
        if current != expected:
            raise PackagePolicyError(
                "published update changed after planning; create a new review"
            )
        return expected, False

    def _verify_gateway_authority(self, plan, owner, recovery_phase):
        skill_id = plan["skill_id"]
        detail, authority_raw = self._authority_detail(skill_id, owner)
        if detail == "absent":
            if plan["authorization"]["kind"] == "manual_receipt":
                return self._verify_catalog_authority(plan, owner, recovery_phase)
            raise UpdateAuthorityRejected(
                "Gateway no longer records an active installation for this skill"
            )
        if detail != "verified":
            raise PackagePolicyError("Gateway update authority is unverifiable")
        authority = self._authority_snapshot(authority_raw or {})
        if authority is not None and authority["skill_state"] != "active":
            raise UpdateAuthorityRejected(
                "Gateway no longer authorizes this managed skill"
            )
        expected_authority = plan.get("authority")
        if not isinstance(expected_authority, dict):
            raise WisdomValidationError("Invalid update review authority")
        expected_remote = dict(expected_authority)
        remote_committed = False
        if authority is not None and authority.get("installed_version") == plan.get("version"):
            if recovery_phase not in {"local_committed", "outcome_recorded"}:
                raise PackagePolicyError(
                    "Gateway target version is inconsistent with local recovery phase"
                )
            expected_remote["installed_version"] = plan.get("version")
            # recordInstall may clamp the requested mode to current organization policy.
            expected_remote["update_mode"] = authority.get("update_mode")
            # A newer publication does not undo an already committed target version.
            expected_remote["latest_version"] = authority.get("latest_version")
            remote_committed = True
        if authority is None or authority != expected_remote:
            raise PackagePolicyError(
                "update authority or policy changed after planning; create a new review"
            )
        return expected_authority, remote_committed

    def verify(  # pylint: disable=too-many-locals,too-many-branches
        self, plan: dict[str, Any], *, recovery_phase: str | None = None
    ) -> VerifiedUpdate:
        """Reauthorize all receipt facts and reject local or package drift."""

        if not isinstance(plan, dict) or plan.get("operation") != "update":
            raise WisdomValidationError("Invalid update review")
        owner = self._owner()
        if plan.get("owner") != owner:
            raise WisdomAuthError("Update review does not belong to this enrolment")
        skill_id = plan.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            raise WisdomValidationError("Invalid update review")
        expected_previous = plan.get("previous_installation")
        installation = self._review_installation(plan, owner, recovery_phase)
        target = Path(str(plan.get("target_path") or ""))
        self._verify_local_recovery_state(
            plan, installation, expected_previous, target, recovery_phase
        )
        expected_authority, remote_committed = self._verify_authority(
            plan, owner, recovery_phase,
        )
        if remote_committed:
            self.install_reviewer._check_owner(owner)  # pylint: disable=protected-access
            return VerifiedUpdate(
                plan=copy.deepcopy(plan), files=[], baseline=dict(plan["baseline"]),
                previous_snapshot=dict(plan["local_snapshot"]),
            )
        incoming, files, baseline = self.install_reviewer._prepare(  # pylint: disable=protected-access
            skill_id,
            int(plan["version"]),
            str(expected_authority["update_mode"]),
            owner,
            check_existing=False,
            accept_warnings=bool(
                isinstance(plan.get("policy"), dict)
                and isinstance(plan["policy"].get("warning_acceptance"), dict)
                and plan["policy"]["warning_acceptance"].get("accepted") is True
            ),
            warning_operation="update",
        )
        if any(plan.get(field) != incoming.get(field) for field in _INCOMING_FIELDS):
            raise WisdomValidationError(
                "Update package or review changed; create a new update plan"
            )
        if plan.get("baseline") != baseline:
            raise WisdomValidationError(
                "Update package file identity changed; create a new update plan"
            )
        automatic = plan.get("authorization", {}).get("kind") == "automatic_policy"
        expected_actions = (
            ["apply_automatic", "recheck"]
            if automatic else ["confirm_update", "recheck"]
        )
        if not plan.get("allowed") or plan.get("permitted_actions") != expected_actions:
            raise PackagePolicyError("reviewed update requires a new explicit action")
        self.install_reviewer._check_owner(owner)  # pylint: disable=protected-access
        return VerifiedUpdate(
            plan=copy.deepcopy(plan),
            files=[(path, mode, bytes(body)) for path, mode, body in files],
            baseline=dict(baseline),
            previous_snapshot=dict(plan["local_snapshot"]),
        )

    @staticmethod
    def _verify_local_recovery_state(
        plan: dict[str, Any], installation: dict[str, Any], expected_previous: Any,
        target: Path, recovery_phase: str | None,
    ) -> None:
        if recovery_phase is None:
            if _tree_hashes(target) != plan.get("local_snapshot"):
                raise PackagePolicyError("managed local bytes changed after update review")
            if installation != expected_previous:
                raise PackagePolicyError("managed installation changed after update review")
            return
        allowed_hash = {
            "prepared": plan.get("local_snapshot"), "old_preserved": None,
            "files_committed": plan.get("baseline"),
            "local_committed": plan.get("baseline"),
        }.get(recovery_phase, object())
        if _tree_hashes(target) != allowed_hash:
            raise PackagePolicyError("managed local bytes do not match the update recovery phase")
        version = installation.get("version")
        if version == plan.get("from_version"):
            if installation != expected_previous:
                raise PackagePolicyError("managed installation changed after update review")
            return
        expected = {
            "skill_id": plan.get("skill_id"),
            "org_id": plan.get("owner", {}).get("organization_id"),
            "slug": plan.get("slug"), "version": plan.get("version"),
            "content_hash": plan.get("content_hash"), "baseline": plan.get("baseline"),
            "target_path": plan.get("target_path"), "state": "active",
            "owner": plan.get("owner"),
        }
        if (
            version != plan.get("version")
            or recovery_phase not in {"files_committed", "local_committed"}
            or installation.get("update_mode") not in _UPDATE_MODES
            or any(installation.get(key) != value for key, value in expected.items())
        ):
            raise PackagePolicyError("managed installation changed during update recovery")
