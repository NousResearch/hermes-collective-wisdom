"""Small shared builders for native workflow tests."""

from copy import deepcopy
from unittest.mock import Mock

from hermes_wisdom_plugin.native_context import NativeInvocation


def owner(**changes):
    """Return a fresh realistic native owner binding."""
    value = {
        "profile_name": "default",
        "profile_home": "/tmp/profile",
        "account_id": "member",
        "organization_id": "org",
        "base_url": "https://g.invalid",
        "installation_id": "install",
    }
    value.update(changes)
    return value


def direct_owner():
    """Return the owner shared by direct-review and slash workflow fixtures."""
    return owner(
        profile_home="/profiles/default", account_id="account-1",
        organization_id="org-1", base_url="https://gateway.invalid",
        installation_id="install-1",
    )


def native_invocation(adapter=None, **changes):
    """Return a fresh private Slack invocation with explicit overrides."""
    values = {
        "platform": "slack",
        "profile": "default",
        "chat_type": "dm",
        "chat_id": "D1",
        "actor_id": "U1",
        "actor_name": "Alex",
        "thread_id": "T1",
        "scope_id": "W1",
        "message_id": "request-1",
        "session_key": "slack:W1:D1:U1",
        "adapter": adapter if adapter is not None else object(),
    }
    values.update(changes)
    return NativeInvocation(**values)


def publication_review(draft_id="draft-1", **changes):
    """Build a fresh complete publication review; callers state deviations."""
    review = {
        "draft_id": draft_id,
        "editorial_name": "Reviewed publication",
        "receipt": "publication-receipt",
        "description": "Exact description",
        "files": [{"path": "SKILL.md", "hash": "sha256:" + "d" * 64}],
        "hashes": {
            "content": "sha256:" + "f" * 64,
            "author_description": "sha256:" + "a" * 64,
            "package_manifest": "sha256:" + "b" * 64,
        },
        "security_check": {"status": "pass"},
        "professionalism_check": {"status": "pass"},
        "publication_mode": "moderated",
        "policy_version": 7,
    }
    review.update(deepcopy(changes))
    return review


def inspect_for_confirmation(controller, record_id, service, caller):
    """Enter an exceptional install review and traverse its exact pages."""
    record = controller.act(record_id, "install", service, caller)
    while True:
        with controller.displaying(record):
            pass  # Simulate successful SDK display in controller-level tests.
        following = next((a for a in controller.view(record).actions if a.label == "Next"), None)
        if following is None:
            return record
        record = controller.act(record_id, following.action, service, caller)


class RecordingPublicationService:
    """Supply exact reviewed files while recording forbidden mutation."""

    def __init__(self, files):
        self.files = files
        self.publication_submit = Mock(side_effect=AssertionError("Inspection must not publish"))
        self.owner = owner()

    def native_owner(self):
        """Return the bound owner as fresh data."""
        return dict(self.owner)

    def publication_review(self, draft_id):
        """Return the exact files supplied to this recording service."""
        return publication_review(
            draft_id,
            description="Useful",
            files=self.files,
            hashes={"content": "b" * 64},
        )
