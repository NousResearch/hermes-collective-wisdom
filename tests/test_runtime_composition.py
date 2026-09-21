"""Real runtime/service composition with only account, host and Gateway faked."""
# pylint: disable=missing-function-docstring,too-few-public-methods

from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState
from test_contribution_review import reviewer
from test_direct_services import Account, Capability, Identity
from test_publication import RecordingPublicationClient
from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.contract import org_directory_name
from hermes_wisdom_plugin.commands import parse_slash, run_slash
from hermes_wisdom_plugin.runtime import InvocationService


class Gateway(RecordingPublicationClient):
    """In-memory enrollment/publication boundary; never contacts a service."""

    def capability(self):
        return Capability(features=["wisdom"])

    def register_identity(self, installation_id):
        return Identity(installation_id=installation_id, state="active")


class Model(SimpleNamespace):
    """Small Gateway response fixture with the production serialization seam."""

    def model_dump(self, *, mode):
        assert mode == "json"
        return deepcopy(self.value)


def test_catalog_browse_renders_owner_local_managed_install_status(tmp_path, monkeypatch):
    profile = ("fixture", tmp_path / "profile")
    root = tmp_path / "runtime-state"
    state = JsonState(root)
    service = InvocationService(root, profile, state=state)
    owner = {
        "profile_name": profile[0], "profile_home": str(profile[1]),
        "account_id": "account-1", "organization_id": "org-1",
        "base_url": "https://gateway.example", "installation_id": "install-1",
    }
    target = (
        profile[1] / "skills" / "_wisdom" / org_directory_name(owner["organization_id"])
        / "incident-handoff"
    )
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Managed\n", encoding="utf-8")
    service.installed.record_install({
        "skill_id": "skill-1", "slug": "incident-handoff", "version": 1,
        "content_hash": "sha256:one", "baseline": {"SKILL.md": "sha256:one"},
        "target_path": str(target), "update_mode": "MANUAL", "state": "active",
        "owner": owner,
    })
    item = Model(value={
        "id": "skill-1", "slug": "incident-handoff", "latest_version": 2,
        "author_description": "Summarize an incident",
    })
    service.account = SimpleNamespace(
        portal_base_url="https://portal.example", organization_id="org-1"
    )
    service.client = SimpleNamespace(
        list_skills=lambda **_kwargs: Model(
            value={"skills": [item.value], "next_cursor": None},
            skills=[item], next_cursor=None,
        ),
        skill=lambda _skill_id: None,
        version=lambda _skill_id, _version: None,
    )
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "_installation_owner", lambda: owner)
    monkeypatch.setattr(service, "check_owner", lambda: owner)
    monkeypatch.setattr("hermes_wisdom_plugin.runtime.current_profile", lambda: profile)

    rows = service.search_skills()
    rendered = run_slash(parse_slash("browse"), service)

    assert rows[0]["installation_status"] == {
        "state": "installed", "text": "Installed v1 · update to v2 available",
        "installed_version": 1, "latest_version": 2,
    }
    assert "Installed v1 · update to v2 available" in rendered


@pytest.mark.parametrize(
    "mode,expected", [("open", "published"), ("moderated", "pending_moderation")],
)
def test_runtime_publication(tmp_path, monkeypatch, mode, expected):
    # pylint: disable=too-many-locals
    # Keep the complete workflow visible; only external boundaries are substituted.
    prepared, source, owner = reviewer(tmp_path)
    profile = ("fixture", source.parent.parent)
    account = Account()
    account.profile_home = str(profile[1])
    account.account_id = owner["account_id"]
    account.organization_id = owner["organization_id"]
    gateway = Gateway(owner, mode=mode)
    root = tmp_path / "runtime-state"
    service = InvocationService(root, profile, state=JsonState(root))
    service.account = account
    service.client = gateway
    monkeypatch.setattr("hermes_wisdom_plugin.runtime.current_profile", lambda: profile)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.install_host.InstallationHost", lambda _home: prepared.host,
    )
    original = (source / "SKILL.md").read_bytes()

    assert service.status()["configured"] is False
    assert service.setup(accept_disclosure=True)["configured"] is True
    assert service._publication_service().enrollment is not None  # pylint: disable=protected-access
    local = service.suggest("reviewed-skill")
    review = service.publication_review(local["draft_id"])
    assert review["files"] == local["files"]
    assert gateway.state.calls == Counter()
    result = service.publication_submit(review["receipt"])
    assert result["publication_state"] == expected
    assert gateway.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)
    assert (source / "SKILL.md").read_bytes() == original

    reopened = InvocationService(root, profile, state=JsonState(root))
    reopened.account = account
    reopened.client = gateway
    assert reopened.status()["configured"] is True
    monkeypatch.setattr(
        "hermes_wisdom_plugin.runtime.current_profile", lambda: ("other", profile[1]),
    )
    with pytest.raises(WisdomAuthError, match="profile changed"):
        reopened.publication_submit(review["receipt"])
    assert gateway.state.calls["publish"] == 1
