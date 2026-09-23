"""Manual install review extraction and adaptation tests."""

# Isolated owner fixtures intentionally match the store's enrolment contract.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# pylint: disable=duplicate-code

# Ported 0924f7946b cases: exact-version URLs, latest selection,
# manifest/spec binding, and apply-time hash/generation rechecks.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from review_test_support import _content, _files, _owner, _spec

from hermes_wisdom_plugin.compatibility import LocalCapabilities
from hermes_wisdom_plugin.contract import (
    RuntimeRequirement,
    sha256_address,
)
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomNotFound, WisdomValidationError
from hermes_wisdom_plugin.install_review import InstallReviewer, VerifiedInstall
from hermes_wisdom_plugin.package import PackagePolicyError


class Client:
    def __init__(self):
        self.spec = _spec()
        self.files = _files(self.spec)
        self.skill_value = {
            "id": "skill-1",
            "slug": "managed-skill",
            "state": "active",
            "takedown_generation": 7,
        }
        self.version_values = [
            {"version": 1, "state": "published"},
            {"version": 2, "state": "published"},
        ]
        content = _content(self.files)
        manifest = next(body for path, _, body in self.files if path == "skill.manifest.json")
        self.version_value = {
            "system_spec": self.spec.model_dump(mode="json"),
            "content_hash": content.content_hash,
            "commit": content.commit,
            "package_manifest_hash": sha256_address(manifest),
            "security_check": {"state": "passed"},
        }
        self.calls = []
        self.after_skill = None

    def skill(self, skill_id):
        self.calls.append(("skill", skill_id))
        if self.after_skill:
            self.after_skill()
        return SimpleNamespace(skill=dict(self.skill_value), versions=self.version_values)

    def version(self, skill_id, version):
        self.calls.append(("version", skill_id, version))
        return SimpleNamespace(
            skill=dict(self.skill_value),
            version={**self.version_value, "version": version},
        )

    def content(self, skill_id, version, **kwargs):
        self.calls.append(("content", skill_id, version, kwargs))
        return _content(self.files), list(self.files)


class Store:
    def __init__(self):
        self.rows = []

    def installation(self, skill_id, owner=None):
        return next(
            (
                dict(row)
                for row in self.rows
                if row["skill_id"] == skill_id
                and (owner is None or row.get("owner") == owner)
            ),
            None,
        )

    def installations(self, owner=None):  # pylint: disable=unused-argument
        return [dict(row) for row in self.rows]


class Host:
    def __init__(self, root, *, scan_allowed=True, blocked=False):
        self.profile_home = root
        self.skills_root = root / "skills"
        self.scan_allowed = scan_allowed
        self.blocked = blocked
        self.findings = []
        self.scanned = []
        self.targets = []

    def validate_target(self, path):
        self.targets.append(path)

    def capabilities(self, _specification):
        return LocalCapabilities(
            hermes_version="1.0.0",
            os="darwin",
            architecture="arm64",
            runtime={
                "shell": True,
                "browser": False,
                "code": True,
                "sandbox": not self.blocked,
            },
        )

    def scan(self, directory):
        self.scanned.append(directory)
        return {
            "available": True,
            "allowed": self.scan_allowed,
            "verdict": "safe" if self.scan_allowed is not False else "dangerous",
            "summary": "host policy",
            "findings": list(self.findings),
        }


class Receipts:
    def __init__(self, owner):
        self.owner = owner

    def create(self, review):
        review = json.loads(json.dumps(review))
        return {
            **review,
            "receipt": "wip_" + "a" * 32,
            "operation": "install",
            "owner": self.owner(),
            "created_at": 1.0,
            "expires_at": 2.0,
        }


def _reviewer(tmp_path, *, client=None, store=None, host=None, owner=None):
    value = owner or _owner()
    provider = value if callable(value) else lambda: value
    return InstallReviewer(
        client or Client(),
        store or Store(),
        Receipts(provider),
        host or Host(tmp_path),
        provider,
    )


def test_install_plan_resolves_exact_catalog_slug_to_id(tmp_path: Path):
    class SlugClient(Client):
        def skill(self, skill_id):
            if skill_id == "managed-skill":
                raise WisdomNotFound("Collective Wisdom item not found")
            return super().skill(skill_id)

        def list_skills(self, *, cursor=None):
            assert cursor is None
            item = SimpleNamespace(model_dump=lambda *, mode: {
                "id": "skill-1", "slug": "managed-skill",
            })
            return SimpleNamespace(skills=[item], next_cursor=None)

    client = SlugClient()
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan("managed-skill@v1")
    assert (plan["skill_id"], plan["slug"], plan["version"]) == (
        "skill-1", "managed-skill", 1,
    )
    assert reviewer.verify(plan).plan["skill_id"] == "skill-1"
    client.skill_value["slug"] = "renamed"
    content_calls = sum(call[0] == "content" for call in client.calls)
    with pytest.raises(WisdomValidationError, match="slug changed"):
        reviewer.plan("managed-skill")
    assert sum(call[0] == "content" for call in client.calls) == content_calls


def test_install_plan_rejects_ambiguous_slug_before_receipt(tmp_path: Path):
    class DuplicateSlugClient(Client):
        def skill(self, skill_id):
            del skill_id
            raise WisdomNotFound("Collective Wisdom item not found")

        def list_skills(self, *, cursor=None):
            assert cursor is None
            items = [
                SimpleNamespace(model_dump=lambda *, mode, id=skill_id: {
                    "id": id, "slug": "managed-skill",
                })
                for skill_id in ("skill-1", "skill-2")
            ]
            return SimpleNamespace(skills=items, next_cursor=None)

    with pytest.raises(WisdomNotFound, match="Wisdom skill not found"):
        _reviewer(tmp_path, client=DuplicateSlugClient()).plan("managed-skill")


def test_plan_preserves_exact_version_url_and_complete_review(tmp_path: Path):
    reviewer = _reviewer(tmp_path)
    plan = reviewer.plan(
        "https://portal.example/wisdom/skills/skill-1?version=1",
        update_mode="MANUAL",
    )
    assert (plan["skill_id"], plan["version"]) == ("skill-1", 1)
    assert plan["hashes"] == {
        "content": plan["content_hash"],
        "manifest": plan["manifest_hash"],
    }
    assert [item["path"] for item in plan["files"]] == [
        "SKILL.md",
        "skill.manifest.json",
    ]
    assert plan["server_checks"] == {"security_check": {"state": "passed"}}
    assert plan["permitted_actions"] == ["confirm_install", "recheck"]


def test_plan_uses_latest_published_and_rejects_bad_selector(tmp_path: Path):
    client = Client()
    client.version_values.append({"version": 99, "state": "draft"})
    assert _reviewer(tmp_path, client=client).plan("skill-1")["version"] == 2
    with pytest.raises(PackagePolicyError, match="version selector"):
        _reviewer(tmp_path).plan("skill-1@vlatest")


def test_verify_redownloads_exact_bytes_and_returns_baseline(tmp_path: Path):
    reviewer = _reviewer(tmp_path)
    plan = reviewer.plan("skill-1@v1")
    verified = reviewer.verify(plan)
    assert isinstance(verified, VerifiedInstall)
    assert verified.plan == plan
    assert verified.files == _files()
    assert verified.baseline == {
        path: sha256_address(body) for path, _mode, body in _files()
    }


@pytest.mark.parametrize("field", ["content_hash", "commit", "package_manifest_hash"])
def test_plan_binds_gateway_version_metadata_to_exact_content(tmp_path: Path, field):
    client = Client()
    client.version_value[field] = "sha256:" + "0" * 64
    with pytest.raises(WisdomValidationError, match="exact package content"):
        _reviewer(tmp_path, client=client).plan("skill-1")


def test_manifest_requirements_must_match_version_metadata(tmp_path: Path):
    client = Client()
    client.version_value["system_spec"] = _spec(
        runtime=RuntimeRequirement(browser=True)
    ).model_dump(mode="json")
    with pytest.raises(WisdomValidationError, match="exact package manifest"):
        _reviewer(tmp_path, client=client).plan("skill-1")


@pytest.mark.parametrize("scan_allowed", [True, None, False])
def test_scan_tristate_requires_exact_warning_acceptance_and_unavailable_blocks(
    tmp_path: Path, scan_allowed
):
    host = Host(tmp_path, scan_allowed=scan_allowed)
    reviewer = _reviewer(tmp_path, host=host)
    plan = reviewer.plan("skill-1", accept_warnings=scan_allowed is not True)
    assert plan["local_scan"]["allowed"] is scan_allowed
    assert plan["policy"]["warning_acceptance"]["accepted"] is (scan_allowed is not True)
    assert plan["allowed"] is True
    staged = host.scanned[0]
    assert staged.parent == host.skills_root / ".hub" / "collective-wisdom"
    assert not staged.exists()
    assert reviewer.verify(plan).plan == plan


def test_install_warning_cannot_be_accepted_after_review_or_reused_for_new_findings(
    tmp_path: Path,
):
    host = Host(tmp_path, scan_allowed=False)
    reviewer = _reviewer(tmp_path, host=host)
    blocked = reviewer.plan("skill-1")
    assert blocked["allowed"] is False
    with pytest.raises(PackagePolicyError, match="explicit warning acknowledgement"):
        reviewer.verify(blocked)

    accepted = reviewer.plan("skill-1", accept_warnings=True)
    host.findings.append({"kind": "new-warning"})
    with pytest.raises(PackagePolicyError, match="findings changed"):
        reviewer.verify(accepted)


@pytest.mark.parametrize(
    "path,body",
    [
        ("assets/.env", b"\xffpassword=" + b"a" * 24),
        ("assets/key.bin", b"\xff-----BEGIN PRIVATE KEY-----\nopaque"),
        ("assets/blob.bin", b"\xffghp_" + b"a" * 36),
    ],
)
def test_install_review_hard_blocks_credentials_in_opaque_bytes(
    tmp_path: Path, path: str, body: bytes
):
    client = Client()
    client.files.append((path, "file", body))
    content = _content(client.files)
    client.version_value.update(
        content_hash=content.content_hash,
        commit=content.commit,
    )
    with pytest.raises(PackagePolicyError, match="credential or private-key"):
        _reviewer(tmp_path, client=client).plan("skill-1", accept_warnings=True)


def test_org_scoped_target_and_existing_owner_rules(tmp_path: Path):
    owner = _owner(organization_id="org/id")
    host = Host(tmp_path)
    _reviewer(tmp_path, host=host, owner=owner).plan("skill-1")
    assert host.targets[0].parent.parent.name == "_wisdom"
    assert host.targets[0].parent.name.startswith("org-")
    assert host.targets[0].name == "managed-skill"

    for row_owner in (owner, _owner(account_id="other"), None):
        store = Store()
        store.rows.append(
            {
                "skill_id": "skill-1",
                "state": "active",
                "target_path": str(host.targets[0]),
                "owner": row_owner,
            }
        )
        with pytest.raises(PackagePolicyError, match="already managed"):
            _reviewer(tmp_path, store=store, owner=owner).plan("skill-1")


def test_verify_allows_exact_owner_record_created_after_review(tmp_path: Path):
    owner = _owner()
    store = Store()
    reviewer = _reviewer(tmp_path, store=store, owner=owner)
    plan = reviewer.plan("skill-1")
    store.rows.append(
        {
            "skill_id": "skill-1",
            "state": "active",
            "target_path": str(tmp_path / "skills" / "renamed"),
            "owner": owner,
        }
    )
    assert reviewer.verify(plan).plan == plan


def test_owner_change_and_redirected_skills_root_fail_closed(tmp_path: Path):
    current = _owner()
    client = Client()
    client.after_skill = lambda: current.update(account_id="other")
    with pytest.raises(WisdomAuthError, match="owner changed"):
        _reviewer(tmp_path, client=client, owner=lambda: dict(current)).plan("skill-1")

    outside = tmp_path / "outside"
    outside.mkdir()
    host = Host(tmp_path)
    host.skills_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackagePolicyError, match="staging is unsafe"):
        _reviewer(tmp_path, host=host).plan("skill-1")
