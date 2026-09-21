"""Exact named-skill update checks and owner-bound review tests."""

# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# pylint: disable=wrong-import-order,protected-access,duplicate-code

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from review_test_support import _content, _files, _owner, _spec

from hermes_wisdom_plugin.contract import (
    org_directory_name, sha256_address,
)
from hermes_wisdom_plugin.errors import WisdomValidationError
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.update_review import UpdateReviewer, VerifiedUpdate
from test_install_review import Host


class Client:
    def __init__(self):
        self.files = {1: _files(), 2: self._version_two()}
        self.remote = {
            "skill_id": "skill-1",
            "installed_version": 1,
            "latest_version": 2,
            "update_mode": "MANUAL",
            "skill_state": "active",
            "takedown_generation": 7,
        }
        self.calls = []

    @staticmethod
    def _version_two(spec=None):
        files = _files(spec)
        files[0] = ("SKILL.md", "file", b"# Managed v2\n")
        return files

    def installations(self, installation_id):
        self.calls.append(("installations", installation_id))
        return [dict(self.remote)] if self.remote is not None else []

    def skill(self, skill_id):
        self.calls.append(("skill", skill_id))
        return SimpleNamespace(
            skill={
                "id": "skill-1", "slug": "managed-skill", "state": "active",
                "takedown_generation": self.remote["takedown_generation"],
            },
            versions=[
                {"version": version, "state": "published"}
                for version in sorted(self.files)
            ],
        )

    def version(self, skill_id, version):
        self.calls.append(("version", skill_id, version))
        files = self.files[version]
        content = _content(files)
        manifest = next(body for path, _, body in files if path == "skill.manifest.json")
        spec = json.loads(manifest)["requirements"]
        return SimpleNamespace(
            skill={
                "id": skill_id, "slug": "managed-skill", "state": "active",
                "takedown_generation": self.remote["takedown_generation"],
            },
            version={
                "version": version, "system_spec": spec,
                "content_hash": content.content_hash, "commit": content.commit,
                "package_manifest_hash": sha256_address(manifest),
                "security_check": {"state": "passed"},
            },
        )

    def content(self, skill_id, version, **kwargs):
        self.calls.append(("content", skill_id, version, kwargs))
        files = self.files[version]
        return _content(files), list(files)


class Store:
    def __init__(self, record):
        self.record = record

    def installation(self, skill_id, owner=None):
        if skill_id != self.record["skill_id"]:
            return None
        if owner is not None and owner != self.record["owner"]:
            return None
        return dict(self.record)

    def installations(self, owner=None):
        return [dict(self.record)] if owner in (None, self.record["owner"]) else []


class Receipts:
    def __init__(self, owner):
        self.owner = owner

    def create(self, review):
        return {
            **json.loads(json.dumps(review)),
            "receipt": "wip_" + "b" * 32,
            "operation": "update",
            "owner": self.owner(),
            "created_at": 1.0,
            "expires_at": 2.0,
        }


def _tree(root: Path, files):
    for name, _mode, body in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def _reviewer(tmp_path, *, client=None, host=None):
    owner = _owner(profile_home=str(tmp_path / "profile"))
    target = (tmp_path / "profile" / "skills" / "_wisdom"
              / org_directory_name(owner["organization_id"]) / "managed-skill")
    _tree(target, _files())
    baseline = {name: sha256_address(body) for name, _mode, body in _files()}
    record = {
        "skill_id": "skill-1", "org_id": owner["organization_id"],
        "slug": "managed-skill", "version": 1,
        "content_hash": _content(_files()).content_hash,
        "baseline": baseline, "target_path": str(target),
        "update_mode": "MANUAL", "state": "active", "owner": owner,
    }
    store = Store(record)
    host = host or Host(tmp_path / "profile")
    value = UpdateReviewer(
        client or Client(), store, Receipts(lambda: dict(owner)), host,
        lambda: dict(owner),
    )
    return value, store, target


@pytest.mark.parametrize(
    ("remote", "state"),
    [
        ({"latest_version": 1, "skill_state": "active"}, "current"),
        ({"latest_version": 2, "skill_state": "active"}, "update_available"),
        ({"latest_version": 2, "skill_state": "archived"}, "archived"),
        ({"latest_version": 2, "skill_state": "taken_down"}, "taken_down"),
        (None, "not_recorded"),
        ({"latest_version": None, "skill_state": "active"}, "unverifiable"),
    ],
)
def test_check_reports_authoritative_state_without_package_or_mutation(tmp_path, remote, state):
    client = Client()
    if remote is None:
        client.remote = None
    else:
        client.remote.update(remote)
    reviewer, _store, target = _reviewer(tmp_path, client=client)
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*") if path.is_file()
    }

    result = reviewer.check("skill-1")

    assert result["state"] == state
    assert not any(call[0] in {"skill", "version", "content"} for call in client.calls)
    assert before == {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("operation", ["check", "plan"])
def test_update_rejects_record_redirected_to_unmanaged_skill(tmp_path, operation):
    value, store, target = _reviewer(tmp_path)
    unrelated = value.host.skills_root / "user-owned"
    target.rename(unrelated)
    store.record["target_path"] = str(unrelated)
    original = (unrelated / "SKILL.md").read_bytes()

    with pytest.raises(PackagePolicyError, match="managed.*target"):
        getattr(value, operation)("skill-1")

    assert (unrelated / "SKILL.md").read_bytes() == original
    assert value.client.calls == []


def test_publisher_source_update_uses_original_path_and_blocks_later_local_edits(tmp_path):
    reviewer, store, canonical = _reviewer(tmp_path)
    target = reviewer.host.skills_root / "authored" / "managed-skill"
    target.parent.mkdir(parents=True)
    canonical.rename(target)
    store.record.update({
        "target_path": str(target),
        "manifest_hash": "sha256:" + "b" * 64,
    })
    store.record["target_provenance"] = {
        "kind": "publisher_source", "owner": store.record["owner"],
        "source_path": str(target), "source_hash": "sha256:" + "a" * 64,
        "publication": {
            "operation_id": "publication-1", "remote_draft_id": "draft-1",
            "proposal_id": 1, "skill_id": "skill-1", "version": 1,
            "content_hash": store.record["content_hash"],
            "commit": "sha256:" + "c" * 64,
            "manifest_hash": store.record["manifest_hash"],
        },
    }

    clean = reviewer.plan("skill-1")
    assert clean["target_path"] == str(target)
    assert clean["target_provenance"] == store.record["target_provenance"]
    assert clean["modified"] is False

    (target / "SKILL.md").write_text("# Changed after publication\n", encoding="utf-8")
    blocked = reviewer.plan("skill-1")
    assert blocked["target_path"] == str(target)
    assert blocked["modified"] is True
    assert blocked["allowed"] is False
    assert blocked["preservation"] == {"action": "block", "destination": None}


def test_plan_binds_prior_authority_incoming_bytes_and_clean_local_snapshot(tmp_path):
    reviewer, store, target = _reviewer(tmp_path)

    plan = reviewer.plan("skill-1")

    assert plan["operation"] == "update"
    assert plan["from_version"] == 1 and plan["version"] == 2
    assert plan["previous_installation"] == store.record
    assert plan["local_snapshot"] == store.record["baseline"]
    assert plan["preservation"] == {"action": "none", "destination": None}
    assert plan["authority"] == {
        "installed_version": 1, "latest_version": 2,
        "takedown_generation": 7, "update_mode": "MANUAL", "skill_state": "active",
    }
    assert plan["content_hash"] == _content(Client._version_two()).content_hash
    assert plan["modified"] is False
    assert (target / "SKILL.md").read_bytes() == b"# Managed\n"


def test_modified_plan_defaults_to_block_and_explicit_plan_reserves_unmanaged_copy(tmp_path):
    reviewer, _store, target = _reviewer(tmp_path)
    (target / "SKILL.md").write_bytes(b"# locally edited\n")

    blocked = reviewer.plan("skill-1")
    assert blocked["modified"] is True
    assert blocked["allowed"] is False
    assert blocked["preservation"]["action"] == "block"

    reviewed = reviewer.plan("skill-1", preserve_modified=True)
    destination = Path(reviewed["preservation"]["destination"])
    assert reviewed["allowed"] is True
    assert reviewed["preservation"]["action"] == "preserve_unmanaged_copy"
    assert destination.parent == reviewer.host.skills_root
    assert destination.name.startswith("managed-skill-local-before-v2-")
    assert not destination.exists()
    assert reviewed["local_snapshot"]["SKILL.md"] == sha256_address(b"# locally edited\n")


def test_sensitive_and_partial_actions_are_bound_into_review_not_apply_flags(tmp_path):
    client = Client()
    sensitive = _spec(credentials=["TEAM_TOKEN"])
    client.files[2] = client._version_two(sensitive)
    reviewer, _store, _target = _reviewer(tmp_path, client=client)

    blocked = reviewer.plan("skill-1")
    assert "new credentials: TEAM_TOKEN" in blocked["sensitive_expansion"]
    assert blocked["allowed"] is False

    sensitive_only = reviewer.plan("skill-1", accept_sensitive=True)
    assert sensitive_only["policy"]["accepted_sensitive"] is True
    assert sensitive_only["allowed"] is False

    accepted = reviewer.plan(
        "skill-1", accept_sensitive=True, accept_partial=True
    )
    assert accepted["policy"]["accepted_partial"] is True
    assert accepted["allowed"] is True


@pytest.mark.parametrize("scan_allowed", [False, None])
def test_manual_update_can_bind_fresh_warning_acceptance(tmp_path, scan_allowed):
    host = Host(tmp_path / "profile", scan_allowed=scan_allowed)
    host.findings = [{"kind": "suspicious", "file": "SKILL.md"}]
    reviewer, _store, _target = _reviewer(tmp_path, host=host)

    blocked = reviewer.plan("skill-1")
    assert blocked["allowed"] is False
    accepted = reviewer.plan("skill-1", accept_warnings=True)

    warning = accepted["policy"]["warning_acceptance"]
    assert accepted["allowed"] is True
    assert warning["accepted"] is True
    assert warning["operation"] == "update"
    assert reviewer.verify(accepted).plan == accepted


@pytest.mark.parametrize("mode", ["AUTO_WITH_NOTICE", "REQUIRED"])
def test_automatic_update_cannot_accept_host_warnings(tmp_path, mode):
    client = Client()
    client.remote["update_mode"] = mode
    host = Host(tmp_path / "profile", scan_allowed=False)
    host.findings = [{"kind": "suspicious", "file": "SKILL.md"}]
    reviewer, _store, _target = _reviewer(tmp_path, client=client, host=host)

    with pytest.raises(PackagePolicyError, match="requires manual review"):
        reviewer.plan_automatic("skill-1")
    with pytest.raises(WisdomValidationError, match="cannot accept manual"):
        reviewer.plan("skill-1", automatic=True, accept_warnings=True)


def test_verify_rechecks_owner_authority_local_snapshot_and_exact_target_bytes(tmp_path):
    client = Client()
    reviewer, _store, target = _reviewer(tmp_path, client=client)
    plan = reviewer.plan("skill-1")

    verified = reviewer.verify(plan)
    assert isinstance(verified, VerifiedUpdate)
    assert verified.previous_snapshot == plan["local_snapshot"]

    (target / "SKILL.md").write_bytes(b"drift")
    with pytest.raises(PackagePolicyError, match="local bytes changed"):
        reviewer.verify(plan)

    (target / "SKILL.md").write_bytes(b"# Managed\n")
    client.remote["update_mode"] = "REQUIRED"
    with pytest.raises(PackagePolicyError, match="authority or policy changed"):
        reviewer.verify(plan)


def test_update_receipt_cannot_be_verified_as_install(tmp_path):
    reviewer, _store, _target = _reviewer(tmp_path)
    plan = reviewer.plan("skill-1")
    plan["operation"] = "install"
    with pytest.raises(WisdomValidationError, match="update review"):
        reviewer.verify(plan)
