"""Strict target provenance shared by managed lifecycle consumers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from .contract import org_directory_name
from .package import PackagePolicyError, source_fingerprint
from .package_application import reject_existing_links, reject_symlink_chain, within

_PUBLISHER_KIND = "publisher_source"


def validate_managed_target(
    host: Any,
    record: dict[str, Any],
    owner: dict[str, str],
    *,
    require_tree: bool = True,
) -> Path:
    """Validate canonical installs or one exact publication-owned source."""

    if record.get("owner") != owner:
        raise PackagePolicyError("managed installation belongs to another enrolment")
    root = Path(host.skills_root).expanduser().absolute()
    reject_existing_links(root)
    target = Path(str(record.get("target_path") or ""))
    slug = _safe_segment(record.get("slug"), "skill slug")
    provenance = record.get("target_provenance")
    if provenance is None:
        expected = root / "_wisdom" / org_directory_name(owner["organization_id"]) / slug
        if target != expected:
            raise PackagePolicyError("managed installation target is invalid")
    else:
        _validate_publisher_provenance(record, provenance, owner, target)
        within(target, root, "publisher source target")
        if target == root or target.parts[:len(root.parts)] != root.parts:
            raise PackagePolicyError("publisher source target is invalid")
    reject_symlink_chain(root, target)
    host.validate_target(target)
    baseline = record.get("baseline")
    if not isinstance(baseline, dict) or not baseline:
        raise PackagePolicyError("managed installation baseline is invalid")
    if provenance is not None:
        _validate_baseline(baseline)
    if require_tree:
        source_fingerprint(target)
    return target


def publisher_provenance(
    *,
    owner: dict[str, str],
    source_path: Path,
    source_hash: str,
    publication: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact immutable provenance marker for an author-owned source."""

    return {
        "kind": _PUBLISHER_KIND,
        "owner": dict(owner),
        "source_path": str(source_path),
        "source_hash": source_hash,
        "publication": dict(publication),
    }


def _validate_publisher_provenance(record, provenance, owner, target) -> None:
    if not isinstance(provenance, dict) or set(provenance) != {
        "kind", "owner", "source_path", "source_hash", "publication",
    }:
        raise PackagePolicyError("publisher source provenance is invalid")
    publication = provenance.get("publication")
    provenance_matches = (
        provenance.get("kind") == _PUBLISHER_KIND
        and provenance.get("owner") == owner
        and provenance.get("source_path") == str(target)
        and _sha256(provenance.get("source_hash"))
    )
    if (
        not provenance_matches
        or not isinstance(publication, dict)
        or publication.get("skill_id") != record.get("skill_id")
    ):
        raise PackagePolicyError("publisher source provenance is invalid")
    required_text = (
        "operation_id", "remote_draft_id", "skill_id", "content_hash",
        "commit", "manifest_hash",
    )
    if any(
        not isinstance(publication.get(key), str) or not publication[key]
        for key in required_text
    ):
        raise PackagePolicyError("publisher publication identity is invalid")
    proposal_id = publication.get("proposal_id")
    version = publication.get("version")
    valid_version = isinstance(version, int) and not isinstance(version, bool) and version >= 1
    valid_hashes = (
        _sha256(publication.get("content_hash"))
        and _sha256(publication.get("manifest_hash"))
    )
    valid_proposal = proposal_id is None or (
        isinstance(proposal_id, int)
        and not isinstance(proposal_id, bool)
        and proposal_id >= 1
    )
    if not valid_version or not valid_hashes or not valid_proposal:
        raise PackagePolicyError("publisher publication identity is invalid")


def _validate_baseline(baseline: dict[Any, Any]) -> None:
    for name, digest in baseline.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise PackagePolicyError("managed installation baseline is invalid")
        relative = PurePosixPath(name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise PackagePolicyError("managed installation baseline is invalid")
        raw = digest.removeprefix("exec:")
        if not _sha256(raw):
            raise PackagePolicyError("managed installation baseline is invalid")


def _safe_segment(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise PackagePolicyError(f"{label} is invalid")
    return value


def _sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])
