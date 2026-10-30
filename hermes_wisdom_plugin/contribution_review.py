"""Thin owner-private coordinator for manually selected local contributions.

Teammate-usefulness author-copy wording is adapted from work contributed by
`hbizi` under this repository's MIT license.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable

from .contract import author_description_hash, sha256_address
from .editorial import (
    apply_editorial_metadata_to_overlay,
    copy_problem_message,
    copy_provenance_line,
    editorial_copy_complete,
    explicit_editorial_values,
    load_skill_editorial_metadata,
    local_copy_problem,
    overlay_copy_problem,
    propose_share_copy,
    resolve_editorial_metadata,
    share_copy_disclosure_ready,
    share_copy_model_input,
    share_copy_values,
)
from .errors import (
    ShareCopyError,
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomValidationError,
)
from .local_security import model_disclosure_ready, prepared_security_check
from .package import (

    PackagePolicyError,
    PreparedPackage,
    load_manifest,
    prepare_package,
    read_validated_package_files,
    read_validated_source_files,
    source_fingerprint,
    verify_content_files,
)
from .professionalism import review_package, unavailable_review
from .release_scope import active as release_scope

# Host scanner findings that mean the skill text may carry a secret, so it must not
# be sent to the owner's model for share copy. Critical findings of any category
# also block, to stay conservative where the scanner is most certain.
_SHARE_COPY_DISCLOSURE_CATEGORIES = frozenset({"credential_exposure"})
_SHARE_COPY_BLOCKING_SEVERITY = "critical"


OwnerProvider = Callable[[], dict[str, str]]

def _scan_snapshot(host: Any, root: Path, files: list[tuple[str, str, bytes]]) -> dict:
    """Scan exactly the bytes supplied to the model, not a moving source."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="model-input-", dir=root) as directory:
        stage = Path(directory)
        for relative, mode, body in files:
            target = stage.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(body)
            target.chmod(0o700 if mode == "exec" else 0o600)
        return host.scan(stage)


def exact_review_files(overlay: Path) -> tuple[list[dict[str, Any]], str]:
    """Render exact package bytes for a review without making them durable state."""

    raw = read_validated_package_files(overlay)
    entries = []
    for relative, mode, body in raw:
        entry = {
            "path": relative,
            "mode": mode,
            "content_base64": base64.b64encode(body).decode("ascii"),
        }
        try:
            entry["content_utf8"] = body.decode("utf-8")
        except UnicodeDecodeError:
            pass
        entries.append(entry)
    records, content_hash = verify_content_files(raw)
    hashes = {record.path: record.hash for record in records}
    return [{**entry, "hash": hashes[entry["path"]]} for entry in entries], content_hash


def _remove_unreferenced_edit(
    store: Any, draft_id: str, edit_root: Path
) -> None:
    """Remove a failed stage only when durable state proves it is unreferenced."""

    try:
        current = store.draft(draft_id)
    except Exception:  # pylint: disable=broad-exception-caught
        return
    if current is not None:
        overlay_value = current.get("overlay_path")
        if not isinstance(overlay_value, str) or not overlay_value:
            return
        try:
            referenced = Path(overlay_value).resolve()
            candidate_root = edit_root.resolve()
        except (OSError, RuntimeError):
            return
        if referenced == candidate_root or referenced.is_relative_to(candidate_root):
            return
    shutil.rmtree(edit_root, ignore_errors=True)


def _existing_author_description(frontmatter: dict[str, Any]) -> str | None:
    """Prefer owner-facing editorial copy, then the canonical description."""

    _name, editorial_description = explicit_editorial_values(frontmatter)
    if editorial_description:
        return editorial_description
    description = frontmatter.get("description")
    value = description.strip() if isinstance(description, str) else ""
    return value or None


def _draft_author_description(skill_markdown: str, llm: Any) -> str:
    """Draft constrained author copy through the explicitly authorized host route."""

    try:
        result = llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Write one concise, outcome-oriented plain-text description of this "
                        "Hermes skill. Explain in simple terms what this skill does and why it "
                        "would be useful to teammates of the current user. Ground the description "
                        "only in the provided skill; do not add unsupported claims or private "
                        "details. Do not claim quality, safety, usage, popularity, or platform "
                        "verification. Return only the description, at most 600 characters."
                    ),
                },
                {"role": "user", "content": skill_markdown[:24_000]},
            ],
            temperature=0.2,
            max_tokens=220,
            timeout=60,
            purpose="Draft Collective Wisdom author description",
        )
        text = str(getattr(result, "text", "") or "").strip()
    except Exception as exc:
        raise WisdomError(
            "Hermes could not draft a description with the configured model. "
            "Check the model setup and try again."
        ) from exc
    if not text:
        raise PackagePolicyError(
            "the configured model did not return an author description"
        )
    return text


def shown_copy_provenance(draft: dict[str, Any], editorial: dict[str, str]) -> dict[str, Any]:
    """Report Hermes-written copy only while the prepared package still shows it."""

    proposed = draft.get("copy_proposed")
    if not isinstance(proposed, dict):
        return {}
    written = [key for key, value in proposed.items()
               if isinstance(value, str) and editorial.get(key) == value]
    if "editorial_description" in written and not str(
            draft.get("description") or "").startswith(str(proposed["editorial_description"])):
        # The owner rewrote the shared description; it is no longer Hermes' wording.
        written.remove("editorial_description")
    if not written:
        return {}
    original = draft.get("copy_original")
    original = original if isinstance(original, dict) else {}
    return {
        "copy_written_by_hermes": written,
        "copy_provenance_line": copy_provenance_line(written),
        "copy_original": {key: str(original.get(key) or "") for key in written},
    }


def _without_replaced_copy(
    frontmatter: dict[str, Any], fields: list[str],
) -> dict[str, Any]:
    """Treat explicit copy Hermes replaced as absent so the overlay takes the proposal."""

    metadata = frontmatter.get("metadata")
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else None
    if not fields or not isinstance(metadata, dict) or not isinstance(hermes, dict):
        return frontmatter
    return {**frontmatter, "metadata": {**metadata, "hermes": {
        key: value for key, value in hermes.items() if key not in fields
    }}}


class ContributionReviewer:  # pylint: disable=too-few-public-methods
    """Prepare one exact local skill without upload or publication authority."""

    def __init__(
        self,
        state: Any,
        host: Any,
        owner_provider: OwnerProvider,
        *,
        llm: Any | None = None,
        source_store: Any | None = None,
    ) -> None:
        self.store = state
        self.source_store = source_store
        self.host = host
        self.owner_provider = owner_provider
        self.llm = llm

    def _owner(self) -> dict[str, str]:
        owner = self.owner_provider()
        required = {
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        }
        if not isinstance(owner, dict) or set(owner) != required:
            raise WisdomAuthError("Collective Wisdom contribution owner is unavailable")
        if any(
            not isinstance(owner.get(key), str) or not owner[key].strip()
            for key in required
        ):
            raise WisdomAuthError("Collective Wisdom contribution owner is unavailable")
        return dict(owner)

    # Deliberately local: contribution and publication own distinct failure messages.
    # pylint: disable=duplicate-code
    @staticmethod
    def _require_current(may_apply: Callable[[], bool] | None) -> None:
        if may_apply is None:
            return
        try:
            current = may_apply() is True
        except Exception as exc:  # authority callbacks fail closed
            raise WisdomAuthError(
                "Collective Wisdom review is no longer current"
            ) from exc
        if not current:
            raise WisdomAuthError("Collective Wisdom review is no longer current")

    # pylint: enable=duplicate-code
    def _assert_owner(
        self, expected: dict[str, str], may_apply: Callable[[], bool] | None = None,
    ) -> None:
        current = self._owner()
        self._require_current(may_apply)
        if current != expected:
            raise WisdomAuthError("Collective Wisdom contribution owner changed")

    @staticmethod
    def _assert_draft_owner(
        draft: dict[str, Any], expected: dict[str, str]
    ) -> None:
        if draft.get("owner") != expected:
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")

    def prepared_drafts(self) -> list[dict[str, str]]:
        """List this owner's saved draft names for safe, explicit resumption."""
        owner = self._owner()
        rows = self.store.prepared_drafts(owner)
        self._assert_owner(owner)
        return rows

    def source(self, skill_name: str) -> Path:
        """Resolve one unique safe profile-local skill source by its local name."""

        if not isinstance(skill_name, str) or not skill_name.strip():
            raise WisdomNotFound("local skill not found")
        if skill_name != skill_name.strip() or "/" in skill_name or "\\" in skill_name:
            raise WisdomNotFound("local skill not found")
        if skill_name in {".", ".."}:
            raise WisdomNotFound("local skill not found")
        root = Path(getattr(self.host, "skills_root", ""))
        if not root.is_dir() or root.is_symlink():
            raise WisdomNotFound("local skill not found")
        candidates: list[Path] = []
        for skill_md in root.glob("*/SKILL.md"):
            if skill_md.parent.name == skill_name:
                candidates.append(skill_md.parent)
        for skill_md in root.glob("*/*/SKILL.md"):
            if skill_md.parent.name == skill_name:
                candidates.append(skill_md.parent)
        safe: list[Path] = []
        root_resolved = root.resolve()
        for candidate in candidates:
            if candidate.is_symlink() or (candidate / "SKILL.md").is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved != root_resolved and resolved.is_relative_to(root_resolved):
                safe.append(resolved)
        if len(safe) != 1:
            raise WisdomNotFound("local skill not found")
        return safe[0]

    def _result(  # pylint: disable=too-many-locals
        self,
        draft: dict[str, Any],
        *,
        allow_model_review: bool,
        expected_owner: dict[str, str],
        reviewed_hashes: dict[str, str] | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._require_current(may_apply)
        overlay = Path(str(draft["overlay_path"]))
        files, content_hash = exact_review_files(overlay)
        if content_hash != draft["content_hash"]:
            raise WisdomConflict(
                "this source already has an edited draft; review that draft instead"
            )
        current_hashes = {
            "content": str(draft["content_hash"]),
            "author_description": str(draft["description_hash"]),
            "package_manifest": str(draft["manifest_hash"]),
        }
        if (
            reviewed_hashes is not None
            and self._expected_hashes(reviewed_hashes) != current_hashes
        ):
            raise WisdomConflict(
                "Collective Wisdom prepared draft changed after the displayed disclosure"
            )
        scan = _scan_snapshot(
            self.host, self.store.root / "model-inputs",
            [(item["path"], item["mode"], base64.b64decode(item["content_base64"]))
             for item in files],
        )
        self._require_current(may_apply)
        guard = scan.get("guard") if isinstance(scan.get("guard"), dict) else scan
        security = prepared_security_check(
            files, str(draft["description"]), {"guard": guard}
        )
        security["content_hash"] = content_hash
        security["author_description_hash"] = str(draft["description_hash"])
        manifest, _manifest_bytes = load_manifest(overlay / "skill.manifest.json")
        if exact_review_files(overlay)[1] != content_hash:
            raise WisdomConflict("prepared package changed during local scan")
        professionalism = self._professionalism(
            draft,
            files=files,
            security=security,
            allow_model_review=allow_model_review,
            expected_owner=expected_owner,
            may_apply=may_apply,
        )
        self._assert_owner(expected_owner, may_apply)
        # A model response is only an assessment. Re-read exact bytes before returning it.
        _files_after, hash_after = exact_review_files(overlay)
        if hash_after != draft["content_hash"]:
            raise WisdomConflict("prepared package changed during review")
        editorial = load_skill_editorial_metadata(overlay)
        editorial_complete = draft.get("editorial_complete_hash") == content_hash
        copy_problem = None
        if not editorial_complete and not release_scope().model_editorial:
            # This release shares the skill's own name and description as written.
            copy_problem = overlay_copy_problem(overlay)
            editorial_complete = copy_problem is None
        failure = None if editorial_complete else draft.get("editorial_failure")
        return {
            "state": "prepared",
            "draft_id": str(draft["id"]),
            "local_draft_id": str(draft["id"]),
            "skill_id": str(draft["skill_id"]),
            "source_hash": str(draft["source_hash"]),
            "overlay_path": str(overlay),
            "description": str(draft["description"]),
            "drafted_description": str(draft["description"]),
            "system_specification": manifest.requirements.model_dump(mode="json"),
            **editorial,
            "editorial_status": "complete" if editorial_complete else "incomplete",
            "editorial_failure": failure,
            **({"editorial_problem": copy_problem_message(copy_problem)}
               if copy_problem else {}),
            **shown_copy_provenance(draft, editorial),
            "files": files,
            "omitted_paths": [str(path) for path in draft.get("omitted_paths") or []],
            "hashes": {
                "content": str(draft["content_hash"]),
                "author_description": str(draft["description_hash"]),
                "package_manifest": str(draft["manifest_hash"]),
            },
            "local_scan": scan,
            "security_check": security,
            "professionalism_check": professionalism,
            "network_submission": False,
            "notice": (
                "Owner-private local preparation only. "
                "Nothing has been uploaded or published."
            ),
            "next_step": (
                "Review and save the complete local package, then send its exact bytes "
                "for owner-only server review."
            ),
        }

    @staticmethod
    def _expected_hashes(value: object) -> dict[str, str]:
        if not isinstance(value, dict) or set(value) != {
            "content", "author_description", "package_manifest"
        }:
            raise PackagePolicyError("expected prior draft hashes are required")
        if any(not isinstance(item, str) or not item for item in value.values()):
            raise PackagePolicyError("expected prior draft hashes are required")
        return dict(value)

    def reopen(
        self, draft_id: str, *, allow_model_review: bool = False,
        reviewed_hashes: dict[str, str] | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Reopen one owner-bound local prepared draft without submission authority."""

        self._require_current(may_apply)
        expected_owner = self._owner()
        self._require_current(may_apply)
        draft = self.store.draft(draft_id)
        if draft is None or not draft_id.startswith("local:"):
            raise WisdomNotFound("prepared draft not found")
        self._assert_draft_owner(draft, expected_owner)
        if str(draft["state"]) != "prepared":
            raise WisdomConflict(
                "this local draft cannot be edited while it is "
                + str(draft["state"]).replace("_", " ")
            )
        return self._result(
            draft,
            allow_model_review=allow_model_review,
            expected_owner=expected_owner,
            reviewed_hashes=reviewed_hashes,
            may_apply=may_apply,
        )

    def inspect_prepared(
        self, draft_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Export one complete exact payload that can be edited and saved directly."""

        result = self.reopen(draft_id, may_apply=may_apply)
        return {
            "draft_id": result["draft_id"],
            "author_description": result["drafted_description"],
            "files": [
                ({
                    "path": item["path"],
                    **({"mode": "exec"} if item["mode"] == "exec" else {}),
                    "content_utf8": item["content_utf8"],
                } if "content_utf8" in item else {
                    "path": item["path"],
                    **({"mode": "exec"} if item["mode"] == "exec" else {}),
                    "content_base64": item["content_base64"],
                })
                for item in result["files"]
            ],
            "expected_hashes": result["hashes"],
        }

    def save_prepared(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        draft_id: str,
        *,
        author_description: str,
        files: list[dict[str, Any]],
        expected_hashes: dict[str, str],
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Save owner edits locally using an owner- and hash-bound pointer CAS."""

        self._require_current(may_apply)
        expected_owner = self._owner()
        self._require_current(may_apply)
        hashes = self._expected_hashes(expected_hashes)
        local = self.store.draft(draft_id)
        if local is None or not draft_id.startswith("local:"):
            raise WisdomNotFound("prepared draft not found")
        self._assert_draft_owner(local, expected_owner)
        if str(local["state"]) != "prepared":
            raise WisdomConflict(
                "this local draft cannot be edited while it is "
                + str(local["state"]).replace("_", " ")
            )
        if hashes != {
            "content": str(local["content_hash"]),
            "author_description": str(local["description_hash"]),
            "package_manifest": str(local["manifest_hash"]),
        }:
            raise WisdomConflict("prepared draft changed; inspect it again before saving")

        overlay = Path(str(local["overlay_path"]))
        _, actual_content_hash = exact_review_files(overlay)
        _manifest, manifest_bytes = load_manifest(overlay / "skill.manifest.json")
        if (
            actual_content_hash != hashes["content"]
            or author_description_hash(str(local["description"]))
            != hashes["author_description"]
            or sha256_address(manifest_bytes) != hashes["package_manifest"]
        ):
            raise WisdomConflict("prepared draft changed; inspect it again before saving")
        if not isinstance(files, list):
            raise PackagePolicyError("prepared files must be a list")
        requested: dict[str, tuple[str, bytes]] = {}
        for item in files:
            if not isinstance(item, dict):
                raise PackagePolicyError(
                    "every edited file requires a path and UTF-8 content"
                )
            path = item.get("path")
            mode = item.get("mode", "file")
            encoded = item.get("content_base64")
            content = item.get("content_utf8")
            if not isinstance(path, str) or mode not in {"file", "exec"}:
                raise PackagePolicyError(
                    "every edited file requires a path, mode, and exact content"
                )
            if isinstance(encoded, str):
                try:
                    body = base64.b64decode(encoded, validate=True)
                except (TypeError, ValueError) as exc:
                    raise PackagePolicyError("edited file contains invalid base64") from exc
                if isinstance(content, str) and content.encode("utf-8") != body:
                    raise PackagePolicyError("edited text and exact bytes disagree")
            elif isinstance(content, str):
                body = content.encode("utf-8")
            else:
                raise PackagePolicyError(
                    "every edited file requires exact base64 or UTF-8 content"
                )
            if path in requested:
                raise PackagePolicyError(f"duplicate edited file: {path}")
            requested[path] = (mode, body)
        verify_content_files(
            [(path, mode, body) for path, (mode, body) in requested.items()]
        )

        edit_root = self.store.root / "contribution-edits" / uuid.uuid4().hex
        retained = False
        try:
            inputs = self.store.root / "local-edit-inputs"
            inputs.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="edit-", dir=inputs) as raw_stage:
                stage = Path(raw_stage)
                for path, (mode, body) in requested.items():
                    destination = stage.joinpath(*PurePosixPath(path).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.write_bytes(body)
                    destination.chmod(0o700 if mode == "exec" else 0o600)
                package = prepare_package(
                    stage,
                    host=self.host,
                    overlay_root=edit_root,
                    author_description=author_description,
                    owner=expected_owner["account_id"],
                    installation_id=expected_owner["installation_id"],
                )
            candidate = {
                **local,
                "overlay_path": str(package.overlay),
                "draft_commit": package.commit,
                "description": package.description,
                "content_hash": package.content_hash,
                "description_hash": package.description_hash,
                "manifest_hash": package.manifest_hash,
                "editorial_complete_hash": (
                    package.content_hash if editorial_copy_complete(package.overlay, local["slug"])
                    else None
                ),
                "author_edited": True,
            }
            result = self._result(
                candidate, allow_model_review=False, expected_owner=expected_owner,
                may_apply=may_apply,
            )
            self._assert_owner(expected_owner, may_apply)
            self._require_current(may_apply)
            if not self.store.replace_draft(
                draft_id,
                owner=expected_owner,
                hashes=hashes,
                record=candidate,
            ):
                raise WisdomConflict("prepared draft changed; inspect it again before saving")
            retained = True
            return result
        finally:
            if not retained:
                _remove_unreferenced_edit(self.store, draft_id, edit_root)

    def _professionalism(  # pylint: disable=too-many-arguments
        self,
        draft: dict[str, Any],
        *,
        files: list[dict[str, Any]],
        security: dict[str, Any],
        allow_model_review: bool,
        expected_owner: dict[str, str],
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Reuse or explicitly run one durable exact-hash review record."""

        values = {
            "skill_id": str(draft["skill_id"]),
            "owner": expected_owner,
            "source_hash": str(draft["source_hash"]),
            "content_hash": str(draft["content_hash"]),
            "author_description_hash": str(draft["description_hash"]),
            "manifest_hash": str(draft["manifest_hash"]),
        }
        existing = self.store.professionalism(values)
        disclosure_ready = model_disclosure_ready(
            [(item["path"], item["mode"], base64.b64decode(item["content_base64"]))
             for item in files], str(draft["description"])
        )
        if (existing is not None and disclosure_ready
            and security.get("local_status") == "pass"
            and isinstance(existing.get("result"), dict) and
            existing["result"].get("status") in {"pass", "advisory"}):
            return existing["result"]
        package = [
            {"path": item["path"], "content_utf8": item["content_utf8"]}
            for item in files if "content_utf8" in item
        ]
        if any("content_utf8" not in item for item in files):
            result = unavailable_review(
                content_hash=values["content_hash"],
                description_hash=values["author_description_hash"],
            )
            result["availability_reason"] = "opaque_package_content"
            result["summary"] = (
                "Professionalism check unavailable: package contains opaque resources."
            )
            return result
        if not allow_model_review:
            return review_package(
                package=package,
                author_description=str(draft["description"]),
                content_hash=values["content_hash"],
                description_hash=values["author_description_hash"],
                llm=None,
            )
        if security.get("local_status") != "pass" or not disclosure_ready:
            result = unavailable_review(
                content_hash=values["content_hash"],
                description_hash=values["author_description_hash"],
            )
            result["availability_reason"] = "local_disclosure_preflight"
            return result
        self._require_current(may_apply)
        result = review_package(
            package=package,
            author_description=str(draft["description"]),
            content_hash=values["content_hash"],
            description_hash=values["author_description_hash"],
            llm=self.llm,
        )
        self._assert_owner(expected_owner, may_apply)
        if result["status"] in {"pass", "advisory"}:
            self.store.save_professionalism(values, result)
        return result

    def _local_skill_id(self, source: Path) -> str:
        if self.source_store is None:
            return "local:" + sha256_address(str(source).encode())[7:39]
        from .qualification import snapshot_tree  # pylint: disable=import-outside-toplevel
        return self.source_store.register_skill(
            source,
            content_hash=snapshot_tree(source)[0],
            source_kind="local",
            snapshot_text=(source / "SKILL.md").read_text(encoding="utf-8"),
        )

    def _editorial_disclosure_ready(self, source_files, description, frontmatter,
                                    allow_model_review):
        ready = model_disclosure_ready(
            source_files,
            str(description or ""), str(frontmatter.get("metadata") or ""),
        )
        if allow_model_review and ready:
            try:
                guard = _scan_snapshot(
                    self.host, self.store.root / "model-inputs", source_files
                )
                ready = (
                    guard.get("available") is True
                    and guard.get("allowed") is True
                    and not guard.get("findings")
                )
            except Exception:  # pylint: disable=broad-exception-caught
                ready = False
        return ready

    def _share_copy_disclosure_block(self, model_input) -> str | None:
        """Why the skill must not reach the owner's model for share copy, or None.

        Both checks run over ``model_input``, exactly the bytes the model call sends
        (serialized SKILL.md prefix, shareable name and description) staged as SKILL.md
        for the host scanner; unsent package files do not decide this, though the share
        itself scans the whole package. The model call only proposes copy the owner
        reviews, so the question is disclosure: a credential match from the plugin's
        preflight or a credential finding from the host scan blocks it, as does a scan
        that did not run. Other findings (shell snippets, unpinned installs) are not a
        disclosure risk; they still gate the share itself.
        """
        payload = [("SKILL.md", "file", model_input)]
        if not share_copy_disclosure_ready(model_input):
            return "disclosure_blocked"
        try:
            guard = _scan_snapshot(self.host, self.store.root / "model-inputs", payload)
        except Exception:  # pylint: disable=broad-exception-caught
            return "disclosure_unchecked"
        if guard.get("available") is not True:
            return "disclosure_unchecked"
        findings = guard.get("findings")
        if guard.get("allowed") is not True or not isinstance(findings, list) or any(
            not isinstance(finding, dict)
            or finding.get("category") in _SHARE_COPY_DISCLOSURE_CATEGORIES
            or finding.get("severity") == _SHARE_COPY_BLOCKING_SEVERITY
            for finding in findings
        ):
            return "disclosure_blocked"
        return None

    def _propose_share_copy(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, source, source_hash, frontmatter, skill_markdown, allow_model_copy, may_apply,
    ):
        """Manual sharing: the skill's own copy, or model-proposed copy when it cannot share.

        Returns None when the skill's own name and description can be shared as
        written (no model call). Otherwise proposes only the failing fields, or
        raises ShareCopyError with the measured problem before anything is saved.
        """
        problem = local_copy_problem(*share_copy_values(frontmatter))
        if problem is None:
            return None
        if allow_model_copy is False:
            raise ShareCopyError(copy_problem_message(problem, "model_copy_disabled"))
        if self.llm is None:
            raise ShareCopyError(copy_problem_message(problem, "model_unavailable"))
        model_input = share_copy_model_input(frontmatter, skill_markdown)  # scanned and sent
        blocked = self._share_copy_disclosure_block(model_input)
        if source_fingerprint(source) != source_hash:
            raise WisdomConflict("source changed during local disclosure preflight")
        if blocked is not None:
            raise ShareCopyError(copy_problem_message(problem, blocked))
        self._require_current(may_apply)
        proposal = propose_share_copy(frontmatter=frontmatter, skill_markdown=skill_markdown,
                                      llm=self.llm, model_input=model_input)
        self._require_current(may_apply)
        if proposal.get("failure") or not proposal["provenance"]:
            raise ShareCopyError(copy_problem_message(
                problem, proposal.get("failure") or "model_error", proposal.get("rejected")))
        original = dict(zip(("editorial_name", "editorial_description"),
                            share_copy_values(frontmatter)))
        written = list(proposal["written_by_hermes"])
        return {
            "editorial_name": proposal["editorial_name"],
            "editorial_description": proposal["editorial_description"],
            "coworker_benefit": "",
            "changed": False,
            "provenance": proposal["provenance"],
            "written_by_hermes": written,
            "original": {key: original[key][:4096] for key in written},
        }

    def _share_description(self, _skill_id, description, proposed) -> str:
        """The shared description: Hermes' proposal when it replaced the skill's own."""
        if "editorial_description" in proposed["written_by_hermes"]:
            description = proposed["editorial_description"]
        return str(description)

    def _prepare_editorial(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, source, source_hash, skill_id, allow_model_review, may_apply,
        allow_model_copy=None,
    ):
        frontmatter = self.host.parse_skill_frontmatter(source)
        if not isinstance(frontmatter, dict):
            raise WisdomValidationError("local skill frontmatter is unavailable")
        source_files = read_validated_source_files(source)
        skill_markdown = next(body for path, _mode, body in source_files
                              if path == "SKILL.md").decode("utf-8")
        description = _existing_author_description(frontmatter)
        if not release_scope().model_editorial and release_scope().model_copy:
            proposed = self._propose_share_copy(
                source, source_hash, frontmatter, skill_markdown, allow_model_copy, may_apply,
            )
            if proposed is not None:
                return frontmatter, self._share_description(
                    skill_id, description, proposed), proposed
        disclosure_ready = self._editorial_disclosure_ready(
            source_files, description, frontmatter, allow_model_review,
        )
        if source_fingerprint(source) != source_hash:
            raise WisdomConflict("source changed during local disclosure preflight")
        if description is None:
            if not allow_model_review:
                if not release_scope().model_editorial:
                    raise ShareCopyError(copy_problem_message(
                        local_copy_problem(*share_copy_values(frontmatter))
                        or "Description is missing."
                    ))
                raise PackagePolicyError(
                    "local skill requires author copy or explicit model review authorization"
                )
            if not disclosure_ready:
                raise PackagePolicyError(
                    "local disclosure preflight requires private content review"
                )
            self._require_current(may_apply)
            description = _draft_author_description(skill_markdown, self.llm)
            self._require_current(may_apply)
        self._require_current(may_apply)
        editorial = resolve_editorial_metadata(
            frontmatter=frontmatter,
            skill_markdown=skill_markdown,
            llm=self.llm if allow_model_review and disclosure_ready else None,
        )
        if allow_model_review and not disclosure_ready and self.llm is not None:
            editorial["failure"] = "disclosure_blocked"
        if editorial["provenance"]:
            description = editorial["editorial_description"]
        return frontmatter, description, editorial

    def _save_prepared(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
        self, source, source_hash, skill_id, skill_name, expected_owner, existing,
        frontmatter, description, editorial, allow_model_review, may_apply,
    ):
        namespace = self.store.root / "contribution-prepared" / uuid.uuid4().hex
        retained = False

        def apply_editorial(
            overlay: Path,
            *,
            editorial_name: str | None,
            editorial_description: str | None,
        ) -> None:
            apply_editorial_metadata_to_overlay(
                overlay,
                editorial_name=editorial_name,
                editorial_description=editorial_description,
                frontmatter=_without_replaced_copy(frontmatter, written),
                coworker_benefit=(editorial["coworker_benefit"]
                                  if editorial["provenance"] else None),
            )

        # Manual sharing writes only the fields Hermes proposed into the overlay.
        written = list(editorial.get("written_by_hermes") or [])
        proposed = {key: editorial[key] for key in written} if written else (
            {"editorial_name": editorial["editorial_name"],
             "editorial_description": editorial["editorial_description"]}
            if editorial["provenance"] else {})
        try:
            package: PreparedPackage = prepare_package(
                source,
                host=self.host,
                overlay_root=namespace,
                author_description=description,
                owner=expected_owner["account_id"],
                installation_id=expected_owner["installation_id"],
                editorial_name=proposed.get("editorial_name"),
                editorial_description=proposed.get("editorial_description"),
                editorial_applier=apply_editorial,
            )
            if source_fingerprint(source) != source_hash:
                raise WisdomConflict("source changed during preparation")
            self._assert_owner(expected_owner, may_apply)
            draft = {
                "id": existing["id"] if existing else f"local:{skill_id}:{uuid.uuid4().hex}",
                "skill_id": skill_id,
                "slug": skill_name,
                "source_path": str(source),
                "source_hash": source_hash,
                "overlay_path": str(package.overlay),
                "draft_commit": package.commit,
                "state": "prepared",
                "description": package.description,
                "content_hash": package.content_hash,
                "description_hash": package.description_hash,
                "manifest_hash": package.manifest_hash,
                # Bound by source_hash, which covers the same list.
                "omitted_paths": list(package.omitted_paths),
                "editorial_complete_hash": (
                    package.content_hash if editorial_copy_complete(package.overlay, skill_name)
                    else None
                ),
                "editorial_failure": editorial.get("failure"),
                **({"copy_proposed": proposed,
                    "copy_original": dict(editorial.get("original") or {}),
                    "copy_provenance": dict(editorial["provenance"])} if written else {}),
            }
            self._require_current(may_apply)
            if existing is not None:
                old_hashes = {
                    "content": existing["content_hash"],
                    "author_description": existing["description_hash"],
                    "package_manifest": existing["manifest_hash"],
                }
                if not self.store.replace_draft(
                    draft["id"], owner=expected_owner, hashes=old_hashes,
                    record={**draft, "owner": expected_owner},
                    expected_overlay=existing["overlay_path"],
                ):
                    raise WisdomConflict("prepared draft changed during editorial preparation")
            else:
                self.store.save_draft({**draft, "owner": expected_owner})
            retained = True
            saved = self.store.draft(str(draft["id"]))
            if saved is None:
                raise WisdomValidationError("prepared local draft was not persisted")
            self._assert_draft_owner(saved, expected_owner)
            result = self._result(
                saved,
                allow_model_review=allow_model_review,
                expected_owner=expected_owner,
                may_apply=may_apply,
            )
            if source_fingerprint(source) != source_hash:
                raise WisdomConflict("source changed during preparation")
            if existing is not None:
                old_overlay = Path(existing["overlay_path"])
                if old_overlay.parent.parent == self.store.root / "contribution-prepared":
                    shutil.rmtree(old_overlay.parent, ignore_errors=True)
            return result
        finally:
            if not retained:
                shutil.rmtree(namespace, ignore_errors=True)

    def prepare(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
        self, skill_name: str, *, allow_model_review: bool | None = None,
        may_apply: Callable[[], bool] | None = None,
        allow_model_copy: bool | None = None,
    ) -> dict[str, Any]:
        """Prepare locally; explicit model refusal overrides the default route.

        ``allow_model_copy`` governs only manual sharing's proposed name and
        description: False is an explicit refusal, None allows it when a host
        model is available.
        """
        if allow_model_review is None:
            allow_model_review = self.llm is not None
        model_copy = not release_scope().model_editorial and release_scope().model_copy

        self._require_current(may_apply)
        expected_owner = self._owner()
        self._require_current(may_apply)
        source = self.source(skill_name)
        source_hash = source_fingerprint(source)
        self._assert_owner(expected_owner, may_apply)
        self._require_current(may_apply)
        skill_id = self._local_skill_id(source)
        existing = self.store.prepared_draft(skill_id, source_hash, expected_owner)
        if existing is not None:
            self._assert_draft_owner(existing, expected_owner)
            # A saved draft whose own copy cannot be shared is prepared again so
            # Hermes can propose copy (or name why it cannot) instead of reusing it.
            stale_copy = (model_copy and not existing.get("author_edited")
                          and overlay_copy_problem(Path(str(existing["overlay_path"]))))
            if not stale_copy and (
                    not allow_model_review or existing.get("editorial_complete_hash")
                    or existing.get("author_edited")):
                result = self._result(
                    existing,
                    allow_model_review=allow_model_review and not existing.get("author_edited"),
                    expected_owner=expected_owner, may_apply=may_apply,
                )
                if source_fingerprint(source) != source_hash:
                    raise WisdomConflict("source changed during preparation")
                return result

        frontmatter, description, editorial = self._prepare_editorial(
            source, source_hash, skill_id, allow_model_review, may_apply,
            allow_model_copy,
        )
        self._assert_owner(expected_owner, may_apply)
        if existing is not None and not editorial["provenance"]:
            result = self._result(
                existing, allow_model_review=allow_model_review,
                expected_owner=expected_owner, may_apply=may_apply,
            )
            if source_fingerprint(source) != source_hash:
                raise WisdomConflict("source changed during preparation")
            return result
        return self._save_prepared(
            source, source_hash, skill_id, skill_name, expected_owner, existing,
            frontmatter, description, editorial, allow_model_review, may_apply,
        )
