"""Disposable Gateway/service fixtures; no host SDK or live connectors required."""
# Test doubles implement the workflow protocol, not a separate public API.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from hermes_wisdom_plugin.client import (
    AgentLedNotificationDefaults, AgentLedPolicyResponse, WisdomMuteResponse,
)
from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.runtime import InvocationService


def agent_policy(**changes):
    """Disposable org-1 policy shared by generation and SDK journeys."""
    defaults = AgentLedNotificationDefaults(
        skill_ready_to_share=changes.get("candidate", True),
        teammate_published=changes.get("shared", True),
        update_available=changes.get("update", True),
    )
    return AgentLedPolicyResponse(
        org_id="org-1", usage_evidence_window_days=7,
        min_aggregate_invocations=3, consecutive_day_usage_counts=True,
        repeated_edits_count=True, max_recommendations_per_user_per_week=3,
        publication_mode="moderated", install_popularity_threshold=2,
        notification_defaults=defaults, manager_review_email_cadence="immediate",
        not_now_suppression_days=7, version=1, updated_by_user_id="manager",
    )


class CatalogClient:
    """Known disposable Gateway authority fixture; package work remains real."""

    # Directory-loaded plugins have a distinct contract class; the SDK harness binds it.
    response_type = ClientDeliveryResponse
    owner_user_id = "member-1"
    display_org_id = "org-1"
    expected_organization_id = "org-1"

    def __init__(self, packages):
        self.packages = packages
        self.latest = 1
        self.remote_version = None
        self.posts = []
        self.identity = {"owner": "member-1"}

    def close(self):
        return None

    def skill(self, skill_id):
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            versions=[
                {"version": value, "state": "published"}
                for value in range(self.latest, 0, -1)
            ],
        )

    def version(self, skill_id, version):
        return SimpleNamespace(
            skill={"id": skill_id, "slug": "managed", "state": "active", "takedown_generation": 0},
            version={
                "version": version,
                "content_hash": self.packages[version][1],
                "commit": f"synthetic-{version}",
                "system_spec": self.packages[version][3],
            },
        )

    def content(self, _skill_id, version, **_kwargs):
        files, content_hash, _hashes, _spec = self.packages[version]
        return SimpleNamespace(content_hash=content_hash, commit=f"synthetic-{version}"), files

    def installations(self, _installation_id):
        if self.remote_version is None:
            return []
        return [{
            "skill_id": "skill-1", "installed_version": self.remote_version,
            "latest_version": self.latest, "update_mode": "MANUAL",
            "skill_state": "active", "takedown_generation": 0,
        }]

    def record_install(self, **values):
        self.posts.append(dict(values))
        self.remote_version = values["version"]
        return {"effective_update_mode": "MANUAL"}

    def agent_led_policy(self):
        return agent_policy()

    def recommendation_mute(self):
        return WisdomMuteResponse(
            org_id="org-1", muted=False, duration=None, muted_until=None,
            forever=False, revision=1,
        )

    def recommendation_suppressions(self, _keys):
        return []

    def claim_notification_delivery(self, request_id, reference):
        return self.response_type(
            org_id="org-1", recipient_user_id="member-1", event_id=f"delivery-{request_id}",
            request_id=request_id, reference=reference, state="claimed", reason=None,
            lease_until=(datetime.now(timezone.utc) + timedelta(minutes=2))
            .isoformat().replace("+00:00", "Z"),
        )

    def settle_notification_delivery(
        self, event_id, request_id, reference, *, outcome, receipt,
    ):
        del receipt
        assert event_id == f"delivery-{request_id}"
        return self.response_type(
            org_id="org-1", recipient_user_id="member-1", event_id=event_id,
            request_id=request_id, reference=reference, state=outcome,
            reason=None, lease_until=None,
        )


class Service(InvocationService):
    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, state_root, profile, owner, installer, install_reviewer,
        updater, update_reviewer, setup_controller, store
    ):
        super().__init__(state_root, ("default", profile))
        self.client = installer.client
        self._owner = owner
        self._installer = installer
        self._install_reviewer = install_reviewer
        self._update_pair = (update_reviewer, updater)
        self._setup_controller = setup_controller
        self._journey_store = store

    @property
    def store(self):
        return self._journey_store

    def check_owner(self):
        if self._closed:
            raise PermissionError("service closed")
        return dict(self._owner)

    def _installation_owner(self):
        return dict(self._owner)

    def _update_services(self):
        return self._update_pair

    def _installation_services(self):
        return self._install_reviewer, self._installer

    def _setup_services(self):
        return self._setup_controller

    def native_skill_inspect(self, skill_id, *, version):
        detail = self.client.version(skill_id, version)
        return {
            "kind": "skill", "skill_id": skill_id, "version": version,
            "facts": {"skill": detail.skill, "version": detail.version},
        }

    def installed_setup_inspect(self, skill_id, *, version=None):
        if version == 1:
            return {
                "skill_id": skill_id, "version": version, "state": "ready",
                "ready_to_use": True, "setup_progress": [],
            }
        return self._setup_controller.inspect(skill_id, version=version)

    def search_skills(self, query=None):
        assert query in {None, "incident"}
        return [{
            "id": "skill-1", "slug": "managed", "latest_version": 1,
            "author_description": "Exact SDK catalog description.",
            "security_check": {"status": "pass"},
            "professionalism_check": {"status": "pass"},
        }]

    def resolve_skill(self, reference):
        assert reference in {"skill-1", "managed"}
        return self.show("skill-1")

    def show(self, skill_id):
        assert skill_id == "skill-1"
        return {
            "skill": {"id": skill_id, "slug": "managed", "state": "active"},
            "versions": [{"version": 1, "state": "published", "created_at": "2026-09-14"}],
            "latest_version_detail": {"version": {
                "version": 1, "author_description": "Exact SDK catalog description.",
                "system_spec": self._installer.client.packages[1][3],
                "security_check": {"status": "pass"},
                "professionalism_check": {"status": "pass"},
            }},
            "portal_url": "https://portal.nousresearch.com/wisdom/skills/skill-1?org_id=org-1",
        }

    def versions(self, skill_id):
        return self.show(skill_id)["versions"]

    def publication_review(
        self, draft_id, *, may_apply=None, accept_warnings=False
    ):
        del may_apply
        assert accept_warnings is False
        assert draft_id == "local-draft"
        return {
            "draft_id": draft_id, "editorial_name": "SDK publication",
            "receipt": "publication-receipt", "description": "Exact SDK publication review.",
            "files": [{"path": "SKILL.md", "hash": "e" * 64}],
            "hashes": {
                key: "sha256:" + character * 64
                for key, character in (
                    ("content", "f"), ("author_description", "a"),
                    ("package_manifest", "b"),
                )
            },
            "security_check": {"status": "pass", "summary": "No known matches detected."},
            "professionalism_check": {"status": "pass"},
            "publication_mode": "moderated", "policy_version": 7,
            "notice": "Nothing has been uploaded.",
        }

    def publication_submit(self, receipt, *, may_apply=None):
        assert may_apply is not None and may_apply() is True
        assert receipt == "publication-receipt"
        return {
            "operation_id": "publication-operation", "state": "done",
            "publication_state": "pending_moderation", "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.nousresearch.com/wisdom/review/remote-draft?org_id=org-1",
        }

    def publication_reopen(self, operation_id, *, may_apply=None):
        assert may_apply is not None and may_apply() is True
        assert operation_id == "publication-operation"
        return {
            "operation_id": operation_id, "state": "done", "publication_state": "published",
            "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.nousresearch.com/wisdom/skills/skill-1?org_id=org-1",
        }
