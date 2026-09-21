"""Owner-scoped, advisory catalog installation status."""

from pathlib import Path
from types import SimpleNamespace

from installed_test_support import installed_store

from hermes_wisdom_plugin.catalog_status import CatalogInstallationStatus
from hermes_wisdom_plugin.contract import org_directory_name
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.package_application import tree_hashes


def _owner(profile: Path, *, account="account-1", organization="org-1"):
    return {
        "profile_name": "fixture", "profile_home": str(profile),
        "account_id": account, "organization_id": organization,
        "base_url": "https://gateway.example", "installation_id": "install-1",
    }


def _record(owner, target, **overrides):
    value = {
        "skill_id": "skill-1", "slug": "incident-handoff", "version": 1,
        "content_hash": "sha256:one", "baseline": {"SKILL.md": "sha256:one"},
        "target_path": str(target), "update_mode": "MANUAL", "state": "active",
        "owner": owner,
    }
    value.update(overrides)
    return value


def _managed_target(profile, owner, slug="incident-handoff"):
    return profile / "skills" / "_wisdom" / org_directory_name(owner["organization_id"]) / slug


def _resolver(monkeypatch, result):
    monkeypatch.setattr(
        "hermes_wisdom_plugin.catalog_status.import_module",
        lambda _name: SimpleNamespace(_find_skill_dir=lambda _slug: result),
    )


def test_no_record_distinguishes_absent_and_unmanaged_profile_copy(tmp_path, monkeypatch):
    """An authored copy is available locally but has no managed version claim."""
    profile = tmp_path / "profile"
    owner = _owner(profile)
    _resolver(monkeypatch, None)
    status = CatalogInstallationStatus(installed_store(tmp_path / "state"), owner, profile)

    assert status.for_skill("skill-1", "incident-handoff", 2)["text"] == "Not installed"

    authored = profile / "skills" / "ops" / "incident-handoff"
    authored.mkdir(parents=True)
    (authored / "SKILL.md").write_text("# Owner copy\n", encoding="utf-8")
    _resolver(monkeypatch, authored)
    status = CatalogInstallationStatus(installed_store(tmp_path / "state"), owner, profile)

    result = status.for_skill("skill-1", "incident-handoff", 2)
    assert result["state"] == "unmanaged_local"
    assert result["text"] == "Local copy · not managed by Wisdom"
    assert result["installed_version"] is None


def test_missing_or_wrong_owner_managed_record_is_never_claimed_installed(tmp_path, monkeypatch):
    """Missing files and foreign ownership cannot establish an installed version."""
    profile = tmp_path / "profile"
    owner_a = _owner(profile)
    owner_b = _owner(profile, account="account-2", organization="org-2")
    store = installed_store(tmp_path / "state")
    missing = _managed_target(profile, owner_a)
    missing.mkdir(parents=True)
    (missing / "SKILL.md").write_text("# Managed\n", encoding="utf-8")
    store.record_install(_record(
        owner_a, missing,
        baseline={"SKILL.md": "sha256:one", "references/missing.md": "sha256:missing"},
    ))
    _resolver(monkeypatch, None)

    unavailable = CatalogInstallationStatus(store, owner_a, profile).for_skill(
        "skill-1", "incident-handoff", 2
    )
    isolated = CatalogInstallationStatus(store, owner_b, profile).for_skill(
        "skill-1", "incident-handoff", 2
    )

    assert unavailable["text"] == "Installation status unavailable"
    assert isolated["text"] == "Not installed"


def test_local_edits_remain_installed_with_integrity_checked_separately(tmp_path):
    """Presence uses the recorded version, matching the existing installed listing."""
    profile = tmp_path / "profile"
    owner = _owner(profile)
    target = _managed_target(profile, owner)
    target.mkdir(parents=True)
    skill = target / "SKILL.md"
    skill.write_text("# Original installed skill\n", encoding="utf-8")
    store = installed_store(tmp_path / "state")
    store.record_install(_record(owner, target, baseline=tree_hashes(target)))
    skill.write_text("# Owner-local refinement\n", encoding="utf-8")

    catalog = CatalogInstallationStatus(store, owner, profile).for_skill(
        "skill-1", "incident-handoff", 2,
    )
    executor = InstallExecutor(
        SimpleNamespace(installations=lambda _identity: []), store,
        None, None, None, lambda: owner,
    )
    inspected = executor.installed()[0]

    assert catalog["state"] == "installed"
    assert catalog["installed_version"] == inspected["version"] == 1
    assert inspected["state"] == "active" and inspected["modified"] is True
    assert "modified" not in catalog and "ready_to_use" not in catalog


def test_malformed_remote_slug_cannot_be_resolved_as_a_path(tmp_path, monkeypatch):
    """Reject traversal before calling the local skill resolver."""
    profile = tmp_path / "profile"
    owner = _owner(profile)
    calls = []
    monkeypatch.setattr(
        "hermes_wisdom_plugin.catalog_status.import_module",
        lambda _name: SimpleNamespace(
            _find_skill_dir=lambda slug: calls.append(slug) or tmp_path
        ),
    )
    status = CatalogInstallationStatus(installed_store(tmp_path / "state"), owner, profile)

    result = status.for_skill("skill-1", "../../outside", 2)

    assert result["text"] == "Installation status unavailable"
    assert not calls


def test_unavailable_local_aggregate_fails_unknown_not_not_installed(tmp_path):
    """Unreadable local state is unknown, not evidence of absence."""
    def broken_installations(*, owner):
        del owner
        raise RuntimeError("state unavailable")

    profile = tmp_path / "profile"
    store = SimpleNamespace(installations=broken_installations)
    result = CatalogInstallationStatus(store, _owner(profile), profile).for_skill(
        "skill-1", "incident-handoff", 2
    )

    assert result["text"] == "Installation status unavailable"


def test_unavailable_host_skill_inspection_fails_unknown(tmp_path, monkeypatch):
    """Missing host inspection support does not manufacture a Not installed label."""
    profile = tmp_path / "profile"
    monkeypatch.setattr(
        "hermes_wisdom_plugin.catalog_status.import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("host helper unavailable")),
    )
    status = CatalogInstallationStatus(
        installed_store(tmp_path / "state"), _owner(profile), profile
    )

    result = status.for_skill("skill-1", "incident-handoff", 2)

    assert result["text"] == "Installation status unavailable"
