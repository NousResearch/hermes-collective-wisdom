"""Read-only inspection of exact managed installation bytes."""

# pylint: disable=import-outside-toplevel,missing-class-docstring
# pylint: disable=missing-function-docstring,redefined-outer-name

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_wisdom_plugin.compatibility import LocalCapabilities
from hermes_wisdom_plugin.contract import canonical_json_bytes, parse_manifest_bytes
from hermes_wisdom_plugin.package import PackagePolicyError, verify_content_files
from hermes_wisdom_plugin.setup_document import PREFIX, SETUP_PATH, SUFFIX
from hermes_wisdom_plugin.store import WisdomStore


OWNER = {
    "profile_name": "test",
    "profile_home": "/profiles/test",
    "account_id": "member",
    "organization_id": "org",
    "base_url": "https://gateway.invalid",
    "installation_id": "hwi_test",
}


class Host:
    def __init__(self, root: Path):
        self.skills_root = root
        self.environment = set()

    def validate_target(self, path: Path):
        assert path.resolve().is_relative_to(self.skills_root.resolve())

    def capabilities(self, _spec):
        return LocalCapabilities(
            hermes_version="1.0.0", os="darwin", architecture="arm64",
            network_access=True, runtime={"sandbox": True}, hardware=frozenset({"cpu", "arm64"}),
        )

    def environment_present(self, name: str) -> bool:
        return name in self.environment


def _setup_bytes() -> bytes:
    value = {
        "schema_version": 1,
        "execution_requires_user_approval": True,
        "requirements": [
            {"kind": "env_var", "name": "EXAMPLE_TOKEN", "purpose": "Authenticate"},
            {"kind": "command", "name": "sh", "purpose": "Run checks"},
            {"kind": "account", "name": "example", "purpose": "Use an account"},
        ],
        "setup_instructions": ["Configure the example client."],
        "credential_handoff": [],
        "compatibility_limits": [],
        "verification_step": "Check the example client.",
        "removed_or_generalized": [],
        "related_skills": [],
    }
    return PREFIX.encode() + canonical_json_bytes(value) + SUFFIX.encode()


@pytest.fixture
def installed(tmp_path):
    store = WisdomStore(tmp_path / "state")
    root = tmp_path / "skills"
    target = root / "_wisdom" / "org" / "managed"
    target.mkdir(parents=True)
    manifest = canonical_json_bytes({
        "schema_version": 1,
        "name": "managed",
        "requirements": {
            "hermes": {"minimum_version": "0.1.0"},
            "platforms": [], "architectures": [], "model": {}, "tools": [], "plugins": [],
            "credentials": [], "connections": [], "filesystem": {}, "network": {},
            "runtime": {}, "hardware": [], "known_limitations": [],
        },
    })
    assert parse_manifest_bytes(manifest).name == "managed"
    files = [("SKILL.md", "file", b"# Managed\n"),
             ("skill.manifest.json", "file", manifest),
             (SETUP_PATH, "file", _setup_bytes())]
    for name, _mode, body in files:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    records, content_hash = verify_content_files(files)
    baseline = {record.path: record.hash for record in records}
    store.record_install({
        "skill_id": "skill-1", "org_id": "org", "slug": "managed", "version": 1,
        "content_hash": content_hash, "baseline": baseline, "target_path": str(target),
        "update_mode": "MANUAL", "state": "active", "owner": OWNER,
    })
    return store, Host(root), target


def test_inspection_checks_prerequisites_without_execution(installed, monkeypatch):
    from hermes_wisdom_plugin.installed_setup import inspect_installed_setup

    store, host, target = installed
    marker = target.parent / "must-not-exist"
    monkeypatch.setattr(os, "system", lambda *_args: pytest.fail("inspection executed a command"))

    result = inspect_installed_setup(store, host, OWNER, "skill-1", version=1)

    assert result["files_installed"] is True
    assert result["execution_authorized"] is False
    assert result["ready_to_use"] is None
    assert result["state"] == "setup_required"
    assert [item["status"] for item in result["prerequisites"]] == [
        "missing", "present", "acknowledgement_required"
    ]
    assert not marker.exists()


@pytest.mark.parametrize("change", ["bytes", "version", "owner", "pending", "hardlink", "extra"])
def test_inspection_rejects_changed_unowned_or_unsettled_install(installed, tmp_path, change):
    from hermes_wisdom_plugin.installed_setup import inspect_installed_setup

    store, host, target = installed
    owner = dict(OWNER)
    version = 1
    if change == "bytes":
        (target / "SKILL.md").write_text("changed", encoding="utf-8")
    elif change == "version":
        version = 2
    elif change == "owner":
        owner["account_id"] = "other"
    elif change == "pending":
        store.journal("uninstall", "skill-1", "staged", {"owner": OWNER})
    elif change == "hardlink":
        os.link(target / SETUP_PATH, tmp_path / "duplicate")
    else:
        (target / "refs" / "unexpected").mkdir()

    with pytest.raises(PackagePolicyError):
        inspect_installed_setup(store, host, owner, "skill-1", version=version)


def test_missing_guidance_is_explicitly_unverified(installed):
    from hermes_wisdom_plugin.installed_setup import inspect_installed_setup

    store, host, target = installed
    row = store.installation("skill-1", owner=OWNER)
    (target / SETUP_PATH).unlink()
    files = [(name, "file", (target / name).read_bytes()) for name in (
        "SKILL.md", "skill.manifest.json"
    )]
    records, content_hash = verify_content_files(files)
    store.record_install({**row, "content_hash": content_hash,
                          "baseline": {item.path: item.hash for item in records}})

    result = inspect_installed_setup(store, host, OWNER, "skill-1")
    assert result["state"] == "guidance_unavailable"
    assert result["guidance"] is None
    assert result["ready_to_use"] is None


def test_setup_readiness_never_carries_across_an_updated_version(installed):
    from hermes_wisdom_plugin.installed_setup import inspect_installed_setup

    store, host, _target = installed
    old = store.installation("skill-1", owner=OWNER)
    host.environment.add("EXAMPLE_TOKEN")
    for phase, index in (("prerequisite", 2), ("setup", 0), ("verify", 0)):
        operation_id = store.journal(
            "wisdom_setup", "skill-1", "passed", {
                "owner": OWNER, "version": 1, "content_hash": old["content_hash"],
                "step": {"phase": phase, "index": index},
            }
        )
        store.advance(operation_id, "passed", done=True)
    assert inspect_installed_setup(store, host, OWNER, "skill-1")["ready_to_use"] is True

    store.record_install({**old, "version": 2})
    updated = inspect_installed_setup(store, host, OWNER, "skill-1", version=2)
    assert not updated["setup_progress"]
    assert updated["ready_to_use"] is None
    assert updated["state"] == "setup_required"
