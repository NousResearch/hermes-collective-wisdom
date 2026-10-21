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
    canonical_json_bytes,
    sha256_address,
)
from hermes_wisdom_plugin.errors import (
    WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError,
)
from hermes_wisdom_plugin.install_review import InstallReviewer, VerifiedInstall
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.setup_document import PREFIX, SUFFIX


def test_install_review_binds_explicit_setup_commands(tmp_path: Path):
    """A one-click install can only run setup commands in its exact review."""
    client = Client()
    guidance = {
        "schema_version": 1, "execution_requires_user_approval": True,
        "requirements": [], "setup_instructions": [],
        "setup_commands": ["python scripts/setup.py --local"],
        "credential_handoff": [], "compatibility_limits": [],
        "verification_step": "Check installed skill.",
        "removed_or_generalized": [], "related_skills": [],
    }
    body = PREFIX.encode() + canonical_json_bytes(guidance) + SUFFIX.encode()
    client.files.append(("refs/wisdom-setup.md", "file", body))
    content = _content(client.files)
    client.version_value["content_hash"] = content.content_hash
    client.version_value["commit"] = content.commit
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan("skill-1@v1")
    assert plan["setup_commands"] == ["python scripts/setup.py --local"]
    assert reviewer.verify(plan).plan["setup_commands"] == plan["setup_commands"]
    client.files[-1] = ("refs/wisdom-setup.md", "file", body.replace(b"--local", b"--global"))
    with pytest.raises((WisdomValidationError, PackagePolicyError)):
        reviewer.verify(plan)


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

    def list_skills(self, *, cursor=None):
        assert cursor is None
        item = SimpleNamespace(model_dump=lambda *, mode: {
            "id": self.skill_value["id"], "slug": self.skill_value["slug"]})
        return SimpleNamespace(skills=[item], next_cursor=None)

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

    with pytest.raises(WisdomConflict, match="name is ambiguous"):
        _reviewer(tmp_path, client=DuplicateSlugClient()).plan("managed-skill")


def test_install_name_wins_over_another_skills_matching_id(tmp_path: Path):
    class CollisionClient(Client):
        def skill(self, skill_id):
            if skill_id == "managed-skill":
                self.calls.append(("skill", skill_id))
                return SimpleNamespace(skill={**self.skill_value, "id": skill_id,
                                              "slug": "another-skill"},
                                       versions=self.version_values)
            return super().skill(skill_id)

        def list_skills(self, *, cursor=None):
            assert cursor is None
            rows = [{"id": "skill-1", "slug": "managed-skill"},
                    {"id": "managed-skill", "slug": "another-skill"}]
            return SimpleNamespace(skills=[SimpleNamespace(
                model_dump=lambda *, mode, row=row: row) for row in rows],
                next_cursor=None)

    plan = _reviewer(tmp_path, client=CollisionClient()).plan("managed-skill@v1")
    assert (plan["skill_id"], plan["slug"]) == ("skill-1", "managed-skill")


def _legacy_client(spec, skill_md=b"# Managed\n"):
    """A published package whose manifest pins facts its SKILL.md may not declare."""
    client = Client()
    client.spec = spec
    client.files = [(path, mode, skill_md if path == "SKILL.md" else body)
                    for path, mode, body in _files(spec)]
    content = _content(client.files)
    manifest = next(body for path, _, body in client.files if path == "skill.manifest.json")
    client.version_value.update(
        system_spec=spec.model_dump(mode="json"), content_hash=content.content_hash,
        commit=content.commit, package_manifest_hash=sha256_address(manifest),
    )
    return client


class LinuxHost(Host):
    def capabilities(self, _specification):
        return LocalCapabilities(
            hermes_version="1.0.0", os="linux", architecture="x86_64",
            runtime={"shell": True, "browser": False, "code": True, "sandbox": True},
        )


def test_legacy_arm64_pin_installs_on_x86_64_with_a_note(tmp_path: Path):
    """A sharer-machine architecture pin never blocks; plan and verify agree."""
    client = _legacy_client(_spec(platforms=["macOS"], architectures=["arm64"]))
    reviewer = _reviewer(tmp_path, client=client, host=LinuxHost(tmp_path))
    plan = reviewer.plan("skill-1@v1")
    assert plan["allowed"] is True
    assert plan["compatibility"]["outcome"] == "compatible"
    assert plan["compatibility"]["blocked"] == []
    assert plan["compatibility"]["notes"] == [
        "Shared with platforms: macOS; this machine is Linux",
        "Shared with architectures: arm64; this machine is x86_64",
    ]
    assert plan["permitted_actions"] == ["confirm_install", "recheck"]
    assert reviewer.verify(plan).plan == plan


def test_skill_md_platform_declaration_blocks_on_another_os(tmp_path: Path):
    """SKILL.md platforms: [macos] blocks on Linux with the exact reason, at plan and verify."""
    skill_md = b"---\nname: managed-skill\ndescription: Mac only.\nplatforms: [macos]\n---\n# M\n"
    client = _legacy_client(_spec(platforms=["macOS"]), skill_md)
    reviewer = _reviewer(tmp_path, client=client, host=LinuxHost(tmp_path))
    plan = reviewer.plan("skill-1@v1")
    assert plan["allowed"] is False
    assert plan["compatibility"]["blocked"] == ["requires macOS; this machine is Linux"]
    assert plan["permitted_actions"] == ["recheck"]
    with pytest.raises(PackagePolicyError, match="blocked compatibility"):
        reviewer.verify(plan)


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


def test_downloaded_package_with_generated_bytecode_is_refused_exactly(tmp_path: Path):
    """Sharing leaves __pycache__ out, so a download carrying it is refused, naming it."""
    client = Client()
    client.files.append(("scripts/__pycache__/run.cpython-311.pyc", "file", b"\x00"))
    client.version_value["content_hash"] = _content(client.files).content_hash
    with pytest.raises(WisdomValidationError) as refused:
        _reviewer(tmp_path, client=client).plan("skill-1")
    assert str(refused.value) == (
        "Gateway returned invalid package content: "
        "package path 'scripts/__pycache__/run.cpython-311.pyc' is refused: directory "
        "'__pycache__' is generated Python bytecode; Hermes does not treat it as skill "
        "content, so it would not reach the agents that install this skill"
    )
