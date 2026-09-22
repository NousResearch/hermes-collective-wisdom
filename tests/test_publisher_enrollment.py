"""Publication-owned source enrollment into the managed lifecycle."""
# pylint: disable=missing-function-docstring,too-few-public-methods

from __future__ import annotations

import copy
import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from installed_test_support import installed_store
from review_test_support import _content
from test_contribution_review import reviewer
from test_install_review import Host as UpdateHost
from test_publication import RecordingPublicationClient
from test_update_review import Receipts as UpdateReceipts

from hermes_wisdom_plugin.compatibility import LocalCapabilities
from hermes_wisdom_plugin.contract import sha256_address
from hermes_wisdom_plugin.direct_services import PublicationService
from hermes_wisdom_plugin.errors import (
    WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError,
)
from hermes_wisdom_plugin.package import (
    PackagePolicyError, read_validated_package_files, source_fingerprint,
)
from hermes_wisdom_plugin.package_application import tree_hashes
from hermes_wisdom_plugin.publisher_enrollment import PublisherEnrollment
from hermes_wisdom_plugin.update_review import UpdateReviewer
from hermes_wisdom_plugin.wire import build_commit


class Host:
    """Disposable profile-local target validator."""
    def __init__(self, profile_home: Path) -> None:
        self.profile_home = profile_home
        self.skills_root = profile_home / "skills"

    def validate_target(self, target: Path) -> None:
        assert target.is_relative_to(self.skills_root)


class PublicationClient(RecordingPublicationClient):  # pylint: disable=too-many-instance-attributes
    """Gateway publication and installation projections, without extra response fields."""
    effective_mode = "MANUAL"

    def __init__(self, owner, files, commit):
        super().__init__(owner)
        self.files = files
        self.commit = commit
        self.current_version = 1
        self.revisions = {1: (list(files), commit, owner["account_id"])}
        self.skill_creator = owner["account_id"]
        self.installation_rows = []
        self.record_calls = []

    def add_version(self, version, files, commit, *, publisher=None):
        self.current_version = version
        self.files = list(files)
        self.commit = commit
        self.revisions[version] = (
            list(files), commit, publisher or self.state.owner["account_id"],
        )

    def skill(self, skill_id):
        assert skill_id == "published-skill-1"
        return SimpleNamespace(
            skill={
                "id": skill_id,
                "slug": "reviewed-skill",
                "state": "active",
                "created_by_user_id": self.skill_creator,
                "takedown_generation": 3,
            },
            versions=[{
                "version": version,
                "content_hash": _content(files).content_hash,
                "commit": commit,
                "published_by_user_id": publisher,
            } for version, (files, commit, publisher) in sorted(self.revisions.items())],
        )

    def list_skills(self, *, cursor=None, limit=None):
        assert cursor is None and limit is None
        return SimpleNamespace(
            skills=[SimpleNamespace(model_dump=lambda **_kwargs: {
                "id": "published-skill-1", "slug": "reviewed-skill", "state": "active",
                "created_by_user_id": self.skill_creator,
                "latest_version": self.current_version,
            })],
            next_cursor=None,
        )

    def version(self, skill_id, version):
        files, commit, publisher = self.revisions[version]
        content = _content(files)
        manifest = next(body for path, _mode, body in files if path == "skill.manifest.json")
        return SimpleNamespace(
            skill={
                "id": skill_id, "state": "active",
                "takedown_generation": 3,
            },
            version={
                "version": version,
                "published_by_user_id": publisher,
                "content_hash": content.content_hash,
                "commit": commit,
                "package_manifest_hash": sha256_address(manifest),
                "system_spec": json.loads(manifest)["requirements"],
            },
        )

    def content(self, skill_id, version, **kwargs):
        del skill_id, kwargs
        files, commit, _publisher = self.revisions[version]
        content = _content(files)
        return SimpleNamespace(
            commit=commit, content_hash=content.content_hash,
        ), list(files)

    def publish(self, draft_id, **kwargs):
        result = super().publish(draft_id, **kwargs)
        if result["state"] == "published":
            self.add_version(
                self.current_version, self.files, self.state.drafts[draft_id].draftCommit,
            )
            result["version"] = self.current_version
        return result

    def installations(self, installation_id):
        assert installation_id == self.state.owner["installation_id"]
        return [dict(row) for row in self.installation_rows]

    def record_install(self, **values):
        self.record_calls.append(dict(values))
        self.installation_rows = [{
            "skill_id": values["skill_id"],
            "installed_version": values["version"],
            "latest_version": values["version"],
            "update_mode": self.effective_mode,
            "skill_state": "active",
            "takedown_generation": values["takedown_generation"],
        }]
        return SimpleNamespace(effective_update_mode=self.effective_mode)


def _enrollment_fixture(tmp_path: Path):
    prepared_reviewer, source, owner = reviewer(tmp_path)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    draft = prepared_reviewer.store.draft(prepared["draft_id"])
    assert draft is not None
    client = PublicationClient(
        owner, read_validated_package_files(Path(draft["overlay_path"])),
        draft["draft_commit"],
    )
    installed = installed_store(tmp_path / "installed-state")
    enrollment = PublisherEnrollment(
        prepared_reviewer.store, installed, client, Host(Path(owner["profile_home"])),
        lambda: dict(owner),
    )
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner),
    )
    published = service.submit(service.review(prepared["draft_id"])["receipt"])
    operation = prepared_reviewer.store.publication(published["operation_id"])
    assert operation is not None
    return enrollment, operation, published["publication_result"], installed, client, source, owner


def _named_recovery_fixture(tmp_path: Path):
    prepared_reviewer, source, owner = reviewer(tmp_path)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    draft = prepared_reviewer.store.draft(prepared["draft_id"])
    assert draft is not None
    client = PublicationClient(
        owner, read_validated_package_files(Path(draft["overlay_path"])),
        draft["draft_commit"],
    )
    initial = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner),
    )
    published = initial.submit(initial.review(prepared["draft_id"])["receipt"])
    installed = installed_store(tmp_path / "installed-state")
    enrollment = PublisherEnrollment(
        prepared_reviewer.store, installed, client, Host(Path(owner["profile_home"])),
        lambda: dict(owner),
    )
    recovered = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner), enrollment,
    )
    return recovered, prepared_reviewer, published, installed, client, source, owner


def test_direct_publication_enrolls_untouched_author_source_in_place(tmp_path: Path, monkeypatch):
    # Preparation and publication need not construct their commits in the same second.
    monkeypatch.setattr(
        "hermes_wisdom_plugin.package.build_commit",
        partial(build_commit, ts="2026-01-01T00:00:00Z"),
    )
    prepared_reviewer, source, owner = reviewer(tmp_path)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    draft = prepared_reviewer.store.draft(prepared["draft_id"])
    assert draft is not None
    files = read_validated_package_files(Path(draft["overlay_path"]))
    client = PublicationClient(owner, files, draft["draft_commit"])
    installed = installed_store(tmp_path / "installed-state")
    enrollment = PublisherEnrollment(
        prepared_reviewer.store, installed, client, Host(Path(owner["profile_home"])),
        lambda: dict(owner),
    )
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner),
        enrollment,
    )
    before = tree_hashes(source)

    result = service.submit(service.review(prepared["draft_id"])["receipt"])

    assert result["publication_state"] == "published"
    assert result["managed_enrollment"] == {
        "state": "managed", "skill_id": "published-skill-1", "version": 1,
        "path": str(source), "effective_update_mode": "MANUAL",
        "detail": (
            "reviewed-skill is now Managed v1. "
            "Update mode: MANUAL (manual/review required)."
        ),
    }
    assert result["detail"] == result["managed_enrollment"]["detail"]
    assert result["submitted_request"]["commit"] != draft["draft_commit"]
    assert tree_hashes(source) == before
    record = installed.installation("published-skill-1", owner=owner)
    assert record is not None
    assert record["target_path"] == str(source)
    assert record["baseline"] == before
    assert record["content_hash"] == prepared["hashes"]["content"]
    assert record["target_provenance"]["kind"] == "publisher_source"
    assert record["target_provenance"]["source_hash"] == draft["source_hash"]
    assert record["target_provenance"]["publication"] == {
        "operation_id": result["operation_id"],
        "remote_draft_id": "draft-1",
        "proposal_id": 11,
        "skill_id": "published-skill-1",
        "version": 1,
        "content_hash": prepared["hashes"]["content"],
        "commit": result["submitted_request"]["commit"],
        "manifest_hash": prepared["hashes"]["package_manifest"],
    }
    assert client.record_calls == [{
        "skill_id": "published-skill-1", "installation_id": owner["installation_id"],
        "version": 1, "takedown_generation": 3, "update_mode": "MANUAL",
    }]


# This end-to-end journey intentionally retains all lifecycle assertions together.
# pylint: disable=too-many-locals,too-many-statements
@pytest.mark.parametrize(
    ("system", "architecture"), [("Linux", "x86_64"), ("Darwin", "arm64")],
)
def test_author_v2_advances_same_source_then_teammate_v3_uses_managed_review(
    tmp_path: Path, monkeypatch, system: str, architecture: str,
):
    monkeypatch.setattr("hermes_wisdom_plugin.package.platform.system", lambda: system)
    monkeypatch.setattr("hermes_wisdom_plugin.package.platform.machine", lambda: architecture)
    prepared_reviewer, source, owner = reviewer(tmp_path)
    prepared_v1 = prepared_reviewer.prepare("reviewed-skill")
    draft_v1 = prepared_reviewer.store.draft(prepared_v1["draft_id"])
    assert draft_v1 is not None
    files_v1 = read_validated_package_files(Path(draft_v1["overlay_path"]))
    client = PublicationClient(owner, files_v1, draft_v1["draft_commit"])
    client.effective_mode = "AUTO_WITH_NOTICE"
    installed = installed_store(tmp_path / "installed-state")
    enrollment = PublisherEnrollment(
        prepared_reviewer.store, installed, client, Host(Path(owner["profile_home"])),
        lambda: dict(owner),
    )
    service = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner), enrollment,
    )
    source_before_v1 = tree_hashes(source)
    source_v1_bytes = (source / "SKILL.md").read_bytes()

    published_v1 = service.submit(service.review(prepared_v1["draft_id"])["receipt"])
    record_v1 = installed.installation("published-skill-1", owner=owner)
    assert record_v1 is not None
    original_provenance = record_v1["target_provenance"]
    assert original_provenance["source_hash"] == source_fingerprint(source)
    assert original_provenance["publication"]["version"] == 1
    assert tree_hashes(source) == source_before_v1

    (source / "SKILL.md").write_text(
        "---\nname: reviewed-skill\ndescription: Review incident notes.\n---\n"
        "# Incident review v2\nUse the improved checklist.\n",
        encoding="utf-8",
    )
    prepared_v2 = prepared_reviewer.prepare("reviewed-skill")
    draft_v2 = prepared_reviewer.store.draft(prepared_v2["draft_id"])
    assert draft_v2 is not None
    files_v2 = read_validated_package_files(Path(draft_v2["overlay_path"]))
    client.add_version(2, files_v2, draft_v2["draft_commit"])
    source_before_v2_link = tree_hashes(source)
    record_install = client.record_install

    def register_v2_then_lose_ack(**values):
        record_install(**values)
        raise TimeoutError("lost v2 registration acknowledgement")

    client.record_install = register_v2_then_lose_ack

    published_v2 = service.submit(service.review(prepared_v2["draft_id"])["receipt"])

    assert published_v2["operation_id"] != published_v1["operation_id"]
    assert prepared_v2["hashes"]["content"] != prepared_v1["hashes"]["content"]
    assert draft_v2["source_hash"] != draft_v1["source_hash"]
    assert published_v2["managed_enrollment"]["state"] == "not_linked"
    assert installed.installation("published-skill-1", owner=owner) == record_v1
    publish_calls = client.state.calls["publish"]
    client.record_install = record_install
    recovered_v2 = service.enroll("reviewed-skill")
    assert service.enroll("reviewed-skill")["managed_enrollment"] == (
        recovered_v2["managed_enrollment"]
    )
    assert recovered_v2["managed_enrollment"]["version"] == 2
    assert client.state.calls["publish"] == publish_calls
    assert tree_hashes(source) == source_before_v2_link
    record_v2 = installed.installation("published-skill-1", owner=owner)
    assert record_v2 is not None
    assert record_v2["version"] == 2
    assert record_v2["content_hash"] == prepared_v2["hashes"]["content"]
    assert record_v2["manifest_hash"] == prepared_v2["hashes"]["package_manifest"]
    assert record_v2["baseline"] == source_before_v2_link
    assert record_v2["target_provenance"] == original_provenance
    assert record_v2["update_mode"] == "AUTO_WITH_NOTICE"
    assert client.record_calls[-1]["version"] == 2
    assert client.record_calls[-1]["update_mode"] == "AUTO_WITH_NOTICE"

    files_v3 = [
        (path, mode, b"# Teammate v3\n" if path == "SKILL.md" else body)
        for path, mode, body in files_v2
    ]
    client.add_version(3, files_v3, "sha256:" + "3" * 64, publisher="member-2")
    client.installation_rows[0]["latest_version"] = 3
    update_host = UpdateHost(Path(owner["profile_home"]))
    update_host.capabilities = lambda _specification: LocalCapabilities(
        hermes_version="2.0.0", os=system, architecture=architecture,
        runtime={"shell": True, "browser": False, "code": True, "sandbox": True},
    )
    update = UpdateReviewer(
        client, installed, UpdateReceipts(lambda: dict(owner)), update_host,
        lambda: dict(owner),
    )

    checked = update.check("published-skill-1")
    clean = update.plan(
        "published-skill-1", accept_sensitive=True, accept_partial=True,
    )
    assert checked["state"] == "update_available"
    assert checked["installed_version"] == 2 and checked["latest_version"] == 3
    assert clean["from_version"] == 2 and clean["version"] == 3
    assert clean["target_path"] == str(source)
    assert clean["target_provenance"] == original_provenance
    assert clean["modified"] is False
    assert clean["allowed"] is True, clean

    (source / "SKILL.md").write_text("# local edit after v2\n", encoding="utf-8")
    blocked = update.plan(
        "published-skill-1", accept_sensitive=True, accept_partial=True,
    )
    assert blocked["modified"] is True
    assert blocked["allowed"] is False
    assert blocked["preservation"] == {"action": "block", "destination": None}

    (source / "SKILL.md").write_bytes(source_v1_bytes)
    stale_v1 = prepared_reviewer.store.publication(published_v1["operation_id"])
    assert stale_v1 is not None
    with pytest.raises(WisdomConflict, match="cannot move backward"):
        enrollment.link(stale_v1, published_v1["publication_result"])
    assert installed.installation("published-skill-1", owner=owner) == record_v2


# pylint: enable=too-many-locals,too-many-statements
def test_explicit_enrollment_recovers_historical_published_operation_by_exact_read_proof(
    tmp_path: Path,
):
    prepared_reviewer, source, owner = reviewer(tmp_path)
    prepared = prepared_reviewer.prepare("reviewed-skill")
    draft = prepared_reviewer.store.draft(prepared["draft_id"])
    assert draft is not None
    client = PublicationClient(
        owner, read_validated_package_files(Path(draft["overlay_path"])),
        draft["draft_commit"],
    )
    initial = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner),
    )
    published = initial.submit(initial.review(prepared["draft_id"])["receipt"])
    prepared_reviewer.store.update_publication(
        published["operation_id"], publication_result=None, managed_enrollment=None,
    )
    installed = installed_store(tmp_path / "installed-state")
    enrollment = PublisherEnrollment(
        prepared_reviewer.store, installed, client, Host(Path(owner["profile_home"])),
        lambda: dict(owner),
    )
    recovered = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client, lambda: dict(owner), enrollment,
    )

    result = recovered.enroll("reviewed-skill")

    assert result["publication_state"] == "published"
    assert result["managed_enrollment"]["state"] == "managed"
    assert result["managed_enrollment"]["path"] == str(source)
    assert installed.installation("published-skill-1", owner=owner) is not None
    assert client.state.calls["publish"] == 1


def test_named_enrollment_requires_retained_proof_for_current_source(tmp_path: Path):
    recovered, _reviewer, _published, installed, client, source, owner = (
        _named_recovery_fixture(tmp_path)
    )
    (source / "SKILL.md").write_text("# locally changed\n", encoding="utf-8")
    before = tree_hashes(source)

    with pytest.raises(WisdomNotFound, match="current local skill 'reviewed-skill'"):
        recovered.enroll("reviewed-skill")

    assert tree_hashes(source) == before
    assert client.state.calls["publish"] == 1
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


def test_named_enrollment_rejects_unknown_local_skill_without_effects(tmp_path: Path):
    recovered, _reviewer, _published, installed, client, source, owner = (
        _named_recovery_fixture(tmp_path)
    )
    before = tree_hashes(source)

    with pytest.raises(WisdomNotFound, match="local skill not found"):
        recovered.enroll("unknown-skill")

    assert tree_hashes(source) == before
    assert client.state.calls["publish"] == 1
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


def test_named_enrollment_rejects_ambiguous_exact_publications(tmp_path: Path):
    recovered, prepared_reviewer, published, installed, client, source, owner = (
        _named_recovery_fixture(tmp_path)
    )
    operation = prepared_reviewer.store.publication(published["operation_id"])
    assert operation is not None
    distinct = copy.deepcopy(operation)
    distinct.update({"id": "distinct-publication", "remote_draft_id": "distinct-draft"})
    with prepared_reviewer.store.change() as state:
        state["publications"][distinct["id"]] = distinct
    before = tree_hashes(source)

    with pytest.raises(WisdomConflict, match="Multiple retained publications.*reviewed-skill"):
        recovered.enroll("reviewed-skill")

    assert tree_hashes(source) == before
    assert client.state.calls["publish"] == 1
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


def test_named_enrollment_keeps_exact_remote_proof_checks(tmp_path: Path):
    recovered, _reviewer, _published, installed, client, source, owner = (
        _named_recovery_fixture(tmp_path)
    )
    original = client.version

    def incorrect_version(skill_id, version):
        detail = original(skill_id, version)
        detail.version["content_hash"] = "sha256:" + "0" * 64
        return detail

    client.version = incorrect_version
    before = tree_hashes(source)

    with pytest.raises(WisdomValidationError, match="proof differs"):
        recovered.enroll("reviewed-skill")

    assert tree_hashes(source) == before
    assert client.state.calls["publish"] == 1
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


def test_named_enrollment_keeps_retained_proof_owner_isolated(tmp_path: Path):
    recovered, prepared_reviewer, _published, installed, client, source, owner = (
        _named_recovery_fixture(tmp_path)
    )
    other_owner = {**owner, "account_id": "other-member"}
    recovered = PublicationService(
        prepared_reviewer.store, prepared_reviewer, client,
        lambda: dict(other_owner), recovered.enrollment,
    )
    before = tree_hashes(source)

    with pytest.raises(WisdomNotFound, match="current local skill 'reviewed-skill'"):
        recovered.enroll("reviewed-skill")

    assert tree_hashes(source) == before
    assert client.state.calls["publish"] == 1
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


@pytest.mark.parametrize("field", [
    "published_by_user_id", "package_manifest_hash", "content_hash", "commit",
])
def test_publication_link_requires_exact_version_proof(tmp_path: Path, field):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    original = client.version

    def incorrect_version(skill_id, version):
        detail = original(skill_id, version)
        detail.version[field] = "wrong-proof"
        return detail

    client.version = incorrect_version
    with pytest.raises(WisdomValidationError):
        enrollment.link(operation, result)
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


@pytest.mark.parametrize("mode", ["MANUAL", "AUTO_WITH_NOTICE", "REQUIRED"])
def test_enrollment_replay_preserves_gateway_effective_mode(tmp_path: Path, mode):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    client.effective_mode = mode
    first = enrollment.link(operation, result)
    assert enrollment.link(operation, result) == first
    assert len(client.record_calls) == 1
    record = installed.installation("published-skill-1", owner=owner)
    assert record is not None and record["update_mode"] == mode


@pytest.mark.parametrize("missing_revision", [False, True])
def test_same_version_requires_exact_current_publication_proof(
    tmp_path: Path, missing_revision,
):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    enrollment.link(operation, result)
    before = installed.installation("published-skill-1", owner=owner)
    assert before is not None
    changed = copy.deepcopy(operation)
    if missing_revision:
        before.pop("publisher_revision")
        installed.record_install(before)
    else:
        changed["id"] = "different-reviewed-operation"
        changed["remote_draft_id"] = "different-remote-draft"

    with pytest.raises(WisdomConflict, match="same|different proof"):
        enrollment.link(changed, result)

    assert installed.installation("published-skill-1", owner=owner) == before
    assert len(client.record_calls) == 1


@pytest.mark.parametrize(
    ("generation", "skill_state"),
    [
        (2, "active"),
        (4, "active"),
        (None, "active"),
        (True, "active"),
        (3, "inactive"),
        (3, "unavailable"),
        (3, None),
    ],
)
def test_existing_gateway_row_requires_active_exact_current_generation(
    tmp_path: Path, generation, skill_state,
):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    row = {
        "skill_id": "published-skill-1",
        "installed_version": 1,
        "latest_version": 1,
        "update_mode": "MANUAL",
        "skill_state": skill_state,
        "takedown_generation": generation,
    }
    client.installation_rows = [row]

    with pytest.raises(WisdomConflict, match="Gateway.*managed publication"):
        enrollment.link(operation, result)

    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


def test_existing_gateway_row_missing_generation_fails_closed(tmp_path: Path):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    client.installation_rows = [{
        "skill_id": "published-skill-1",
        "installed_version": 1,
        "latest_version": 1,
        "update_mode": "MANUAL",
        "skill_state": "active",
    }]

    with pytest.raises(WisdomConflict, match="Gateway.*managed publication"):
        enrollment.link(operation, result)

    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=owner) is None


@pytest.mark.parametrize("recover", [False, True])
def test_version_publisher_can_manage_a_skill_created_by_a_teammate(tmp_path: Path, recover):
    enrollment, operation, result, installed, client, source, owner = _enrollment_fixture(
        tmp_path
    )
    client.skill_creator = "other-member"
    linked = enrollment.link_existing(operation) if recover else enrollment.link(operation, result)
    assert linked["state"] == "managed"
    record = installed.installation("published-skill-1", owner=owner)
    assert record is not None and record["target_path"] == str(source)


def test_source_change_during_remote_registration_blocks_local_enrollment(tmp_path: Path):
    enrollment, operation, result, installed, client, source, _owner = _enrollment_fixture(
        tmp_path
    )
    record_install = client.record_install

    def register_then_edit(**values):
        acknowledged = record_install(**values)
        (source / "SKILL.md").write_text("# changed during registration\n", encoding="utf-8")
        return acknowledged

    client.record_install = register_then_edit

    with pytest.raises(WisdomConflict, match="published source changed"):
        enrollment.link(operation, result)
    assert installed.installation("published-skill-1", owner=_owner) is None


def test_owner_change_during_remote_registration_blocks_local_enrollment(tmp_path: Path):
    enrollment, operation, result, installed, client, _source, owner = _enrollment_fixture(
        tmp_path
    )
    current_owner = dict(owner)
    enrollment.owner_provider = lambda: dict(current_owner)
    record_install = client.record_install

    def register_then_change_owner(**values):
        acknowledged = record_install(**values)
        current_owner["account_id"] = "other-member"
        return acknowledged

    client.record_install = register_then_change_owner

    with pytest.raises(WisdomAuthError, match="owner changed"):
        enrollment.link(operation, result)
    assert installed.installation("published-skill-1", owner=owner) is None


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_malicious_link_added_after_publication_review_blocks_enrollment(
    tmp_path: Path, attack: str,
):
    enrollment, operation, result, installed, client, source, _owner = _enrollment_fixture(
        tmp_path
    )
    outside = tmp_path / "outside"
    if attack == "symlink":
        outside.write_text("outside", encoding="utf-8")
        (source / "malicious-link").symlink_to(outside)
    else:
        os.link(source / "SKILL.md", outside)

    with pytest.raises(PackagePolicyError):
        enrollment.link(operation, result)
    assert not client.record_calls
    assert installed.installation("published-skill-1", owner=_owner) is None
