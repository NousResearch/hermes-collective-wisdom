"""Real runtime/service composition with only account, host and Gateway faked."""
# pylint: disable=missing-function-docstring,too-few-public-methods

from collections import Counter

import pytest

from installed_test_support import JsonState
from test_contribution_review import reviewer
from test_direct_services import Account, Capability, Identity
from test_publication import RecordingPublicationClient
from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.runtime import InvocationService


class Gateway(RecordingPublicationClient):
    """In-memory enrollment/publication boundary; never contacts a service."""

    def capability(self):
        return Capability(features=["wisdom"])

    def register_identity(self, installation_id):
        return Identity(installation_id=installation_id, state="active")


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
