"""Durable native update review state-machine tests."""

# Test doubles and behavior-named tests intentionally favor explicit state.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-instance-attributes
# The durable controller regression matrix is intentionally kept in one module.
# pylint: disable=too-many-lines
# White-box CAS tests exercise controller transition primitives directly.
# pylint: disable=protected-access

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
from pathlib import Path
from typing import Any

import pytest

from hermes_wisdom_plugin.client import WisdomMuteResponse
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import text as telegram_text
from hermes_wisdom_plugin.store import WisdomStore


OWNER = {
    "profile_name": "default", "profile_home": "/tmp/profile",
    "account_id": "member", "organization_id": "org", "base_url": "https://g.invalid",
    "installation_id": "install",
}


def invocation(**changes):
    values = {
        "platform": "slack", "profile": "default", "chat_type": "dm",
        "chat_id": "D1", "actor_id": "U1", "actor_name": "Shannon",
        "thread_id": "T1", "scope_id": "W1", "message_id": "request-1",
        "session_key": "slack:W1:D1:U1", "adapter": object(),
    }
    values.update(changes)
    return NativeInvocation(**values)


class Service:
    def __init__(self):
        self.applies = 0
        self.plans = 0
        self.owner = dict(OWNER)
        self.modified = False
        self.sensitive = []
        self.compatibility = "compatible"
        self.plan_calls = []
        self.inspect_error: Exception | None = None
        self.ready_to_use: bool | None = None
        self.setup_state = "setup_required"
        self.setup_result = {"state": "passed"}
        self.setup_applies = 0
        self.setup_plans = 0
        self.setup_instruction = "Configure it"
        self.mode = "agent"
        self.mode_sets = []
        self.mode_writable = True
        self.store: Any = None
        self.client: Any = None

    def notification_mode(self):
        return self.mode

    def notification_mode_writable(self):
        return self.mode_writable

    def set_notification_mode(self, value):
        self.mode_sets.append(value)
        self.mode = value

    def require_setup(self):
        return None

    def native_owner(self):
        return dict(self.owner)

    def update_plan(self, skill_id, **choices):
        self.plans += 1
        self.plan_calls.append(dict(choices))
        partial = self.compatibility in {"partial", "compatible_after_setup"}
        allowed = bool(
            (not self.modified or choices.get("preserve_modified"))
            and (not self.sensitive or choices.get("accept_sensitive"))
            and (not partial or choices.get("accept_partial"))
        )
        preservation = {
            "action": "preserve_unmanaged_copy",
            "destination": "/tmp/profile/skills/managed-local-before-v2-reviewed",
        } if self.modified and choices.get("preserve_modified") else {
            "action": "block" if self.modified else "none", "destination": None,
        }
        return {
            "operation": "update", "receipt": f"receipt-{self.plans}",
            "skill_id": skill_id, "slug": "managed", "from_version": 1, "version": 2,
            "previous_content_hash": "a" * 64, "content_hash": "b" * 64,
            "manifest_hash": "c" * 64, "update_mode": "MANUAL", "allowed": allowed,
            "modified": self.modified, "sensitive_expansion": list(self.sensitive),
            "compatibility": {"outcome": self.compatibility},
            "local_scan": {"allowed": True, "status": "pass", "summary": "No issues detected"},
            "server_checks": {"status": "pass"},
            "files": [{"path": "SKILL.md", "hash": "d" * 64, "size": 8}],
            "preservation": preservation,
            "policy": choices, "owner": dict(self.owner),
        }

    def native_update_inspect(self, receipt):
        assert receipt.startswith("receipt-")
        return [{"path": "SKILL.md", "content": "# exact\n", "hash": "d" * 64}]

    def update_apply(self, receipt):
        self.applies += 1
        return {
            "updated": True, "skill_id": "skill-1", "from_version": 1, "version": 2,
            "content_hash": "b" * 64, "receipt": receipt,
            "setup_execution_authorized": False, "ready_to_use": None,
        }

    def installed_setup_inspect(self, skill_id, *, version=None):
        if self.inspect_error:
            raise self.inspect_error
        return {
            "skill_id": skill_id, "version": version, "content_hash": "b" * 64,
            "guidance": {"setup_instructions": ["Configure it"], "verification_step": "Verify it"},
            "state": self.setup_state, "ready_to_use": self.ready_to_use,
        }

    def installed_setup_plan(self, skill_id, **values):
        self.setup_plans += 1
        return {
            "operation": "installed_setup", "receipt": f"setup-receipt-{self.setup_plans}",
            "skill_id": skill_id, "version": values["version"],
            "content_hash": "b" * 64, "owner": dict(self.owner),
            "step": {"phase": values["phase"], "index": values["index"],
                     "command": values["command"]},
            "setup_instruction": self.setup_instruction, "allowed": True,
        }

    def installed_setup_apply(self, _receipt):
        self.setup_applies += 1
        return dict(self.setup_result)

    def installed_setup_recover(self, _skill_id, operation_id, *, acknowledged=False):
        return {
            "state": "abandoned" if acknowledged else "unknown",
            "operation_id": operation_id,
            **({} if acknowledged else {"recovery_review": True}),
        }

    def search_skills(self, query=None):
        assert query in {None, "incident"}
        return [{
            "id": f"skill-{index}", "slug": f"shared-{index}",
            "latest_version": index, "author_description": f"Description {index}",
            "security_check": {"status": "pass"},
            "professionalism_check": {"status": "pass"},
        } for index in range(1, 7)]

    def resolve_skill(self, reference):
        return {
            "skill": {"id": reference, "slug": "shared-skill"},
            "versions": [{"version": 2, "state": "published"}],
            "latest_version_detail": {"version": {
                "version": 2, "author_description": "Exact shared description",
                "verified_facts": {"scan_verdict": "pass"},
                "system_spec": {"tools": [{"name": "terminal"}]},
                "security_check": {"status": "pass"},
                "professionalism_check": {"status": "pass"},
            }},
            "portal_url": "https://portal.example/wisdom/skills/skill-1?org_id=org",
        }

    def show(self, skill_id):
        return self.resolve_skill(skill_id)

    def versions(self, _skill_id):
        return [
            {"version": value, "state": "published", "created_at": f"2026-09-{value:02d}",
             "security_check": {"status": "pass"},
             "professionalism_check": {"status": "pass"}}
            for value in range(6, 0, -1)
        ]


def test_settings_mode_uses_owner_bound_setter_once_and_keeps_manual_review(
    tmp_path: Path,
):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"},
        "owner_user_id": "member",
        "display_org_id": "org",
        "expected_organization_id": "org",
        "recommendation_mute": lambda _self: (_ for _ in ()).throw(
            ConnectionError("offline")
        ),
    })()

    record = controller.request_settings(service, invocation())
    controller.bind_delivery(record["id"], invocation(), "card-1")
    view = controller.view(record)
    assert view.summary == "Notification settings"
    assert [action.label for action in view.actions] == [
        "Agent decides", "Fixed notifications",
    ]
    assert "Gateway status: unavailable" in view.items[1].detail

    completed = controller.act(
        service, record["id"], invocation(message_id="card-1"), "settings.mode.fixed"
    )
    assert completed["state"] == "completed"
    assert service.mode == "fixed" and service.mode_sets == ["fixed"]
    assert controller.view(completed).summary == "Notification settings updated"

    duplicate = controller.act(
        service, record["id"], invocation(message_id="card-1"), "settings.mode.fixed"
    )
    assert duplicate["state"] == "completed"
    assert service.mode_sets == ["fixed"]

    manual = controller.request(service, invocation(message_id="manual-request"), "skill-1")
    assert manual["state"] == "pending" and manual["operation"] == "update"


def test_settings_mute_choice_uses_existing_control_and_syncs_once(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))

    class SettingsClient:
        identity = {"owner": "member"}
        owner_user_id = "member"
        display_org_id = "org"
        expected_organization_id = "org"

        def __init__(self):
            self.set_calls = []

        def recommendation_mute(self):
            return WisdomMuteResponse(
                org_id="org", muted=False, duration=None, muted_until=None,
                forever=False, revision=1,
            )

        def set_recommendation_mute(
            self, duration, *, expected_revision, mutation_id, requested_at,
        ):
            self.set_calls.append((duration, expected_revision, mutation_id, requested_at))
            until = (
                datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
                + timedelta(days=7)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return WisdomMuteResponse(
                org_id="org", muted=True, duration=duration, muted_until=until,
                forever=False, revision=2, mutation_id=mutation_id,
            )

    service = Service()
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = SettingsClient()
    record = controller.request_settings(service, invocation())
    controller.bind_delivery(record["id"], invocation(), "card-1")

    assert [action.label for action in controller.view(record).actions] == [
        "Agent decides", "Fixed notifications", "Mute 1 day", "Mute 1 week",
        "Mute 30 days", "Mute forever", "Unmute",
    ]
    completed = controller.act(
        service, record["id"], invocation(message_id="card-1"), "settings.mute.1w"
    )
    assert completed["state"] == "completed"
    assert completed["result"]["mute_sync"]["preference_sync"] == "synced"
    assert len(service.client.set_calls) == 1

    controller.act(
        service, record["id"], invocation(message_id="card-1"), "settings.mute.1w"
    )
    assert len(service.client.set_calls) == 1


def test_settings_mode_rejects_card_when_live_mode_changed_after_open(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"},
        "owner_user_id": "member",
        "display_org_id": "org",
        "expected_organization_id": "org",
        "recommendation_mute": lambda _self: (_ for _ in ()).throw(
            ConnectionError("offline")
        ),
    })()
    record = controller.request_settings(service, invocation())
    controller.bind_delivery(record["id"], invocation(), "card-1")

    service.mode = "fixed"
    with pytest.raises(PermissionError, match="changed.*Open /wisdom settings again"):
        controller.act(
            service, record["id"], invocation(message_id="card-1"),
            "settings.mode.fixed",
        )

    assert not service.mode_sets
    assert controller.load(record["id"])["state"] == "needs_review"


@pytest.mark.parametrize("hazard", ["owner", "superseded"])
def test_settings_mode_rechecks_authority_immediately_before_setter(
    tmp_path: Path, hazard: str,
):
    controller = NativeReviewController(WisdomStore(tmp_path))

    class HazardService(Service):
        def __init__(self):
            super().__init__()
            self.armed = False

        def notification_mode(self):
            if self.armed and hazard == "owner":
                self.owner = {
                    "installation_id": "install-2", "organization_id": "org"
                }
            elif self.armed:
                with controller.store.transaction() as database:
                    database.execute(
                        "UPDATE wisdom_consent SET state='superseded' WHERE id=?",
                        (record["id"],),
                    )
            return "agent"

    service = HazardService()
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"}, "owner_user_id": "member",
        "display_org_id": "org", "expected_organization_id": "org",
        "recommendation_mute": lambda _self: (_ for _ in ()).throw(
            ConnectionError("offline")
        ),
    })()
    record = controller.request_settings(service, invocation())
    controller.bind_delivery(record["id"], invocation(), "card-1")
    service.armed = True
    with pytest.raises(PermissionError):
        controller.act(
            service, record["id"], invocation(message_id="card-1"),
            "settings.mode.fixed",
        )

    assert not service.mode_sets


def test_settings_mode_uncertain_setter_failure_is_never_replayed(tmp_path: Path):
    class UncertainService(Service):
        def set_notification_mode(self, value):
            self.mode_sets.append(value)
            self.mode = value
            raise RuntimeError("setter acknowledgement lost")

    controller = NativeReviewController(WisdomStore(tmp_path))
    service = UncertainService()
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"}, "owner_user_id": "member",
        "display_org_id": "org", "expected_organization_id": "org",
        "recommendation_mute": lambda _self: (_ for _ in ()).throw(
            ConnectionError("offline")
        ),
    })()
    record = controller.request_settings(service, invocation())
    controller.bind_delivery(record["id"], invocation(), "card-1")

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        controller.act(
            service, record["id"], invocation(message_id="card-1"),
            "settings.mode.fixed",
        )
    assert controller.load(record["id"])["state"] == "needs_review"

    with pytest.raises(PermissionError, match="no longer"):
        controller.act(
            service, record["id"], invocation(message_id="card-1"),
            "settings.mode.fixed",
        )
    assert service.mode_sets == ["fixed"]


@pytest.mark.parametrize(
    "state", ["needs_review", "expired", "superseded", "applying", "completed"]
)
def test_settings_nonpending_cards_never_render_mutation_actions(
    tmp_path: Path, state: str,
):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    service.mode = "fixed"
    service.mode_writable = False
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"},
        "owner_user_id": "member",
        "display_org_id": "org",
        "expected_organization_id": "org",
        "recommendation_mute": lambda _self: WisdomMuteResponse(
            org_id="org", muted=True, duration="forever", muted_until=None,
            forever=True, revision=4,
        ),
    })()
    record = controller.request_settings(service, invocation())
    with controller.store.transaction() as database:
        database.execute(
            "UPDATE wisdom_consent SET state=? WHERE id=?", (state, record["id"])
        )

    view = controller.view(controller.load(record["id"]))

    assert not view.actions
    assert "proactive generation and delivery are off" in view.items[0].detail.lower()
    assert "Current Gateway state: muted forever" in view.items[1].detail


def test_read_only_settings_card_omits_mode_controls_but_keeps_available_mute(
    tmp_path: Path,
):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    service.mode_writable = False
    service.store = controller.store
    service.store.activate_installation_identity("install", "org")
    service.client = type("SettingsClient", (), {
        "identity": {"owner": "member"},
        "owner_user_id": "member", "display_org_id": "org",
        "expected_organization_id": "org",
        "recommendation_mute": lambda _self: WisdomMuteResponse(
            org_id="org", muted=False, duration=None, muted_until=None,
            forever=False, revision=1,
        ),
    })()

    view = controller.view(controller.request_settings(service, invocation()))

    assert [action.label for action in view.actions] == [
        "Mute 1 day", "Mute 1 week", "Mute 30 days", "Mute forever", "Unmute",
    ]
    assert "Current Gateway state: not muted" in view.items[1].detail


def test_pending_projection_is_bounded_and_newest_first(tmp_path: Path):
    now = iter(range(1000, 1205))
    controller = NativeReviewController(WisdomStore(tmp_path), clock=lambda: float(next(now)))
    service = Service()
    records = [
        controller.request_install_modes(
            service,
            invocation(
                session_key=f"slack:W1:D{index}:U1", chat_id=f"D{index}",
                message_id=f"request-{index}",
            ),
            f"skill-{index}",
        )
        for index in range(101)
    ]

    projected = controller.pending(OWNER)

    assert [row["id"] for row in projected] == [
        row["id"] for row in reversed(records[-100:])
    ]


def test_reference_controls_exact_inspection_defer_and_manual_reopen(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path), clock=lambda: 100.0)
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    view = controller.view(record)
    assert [action.label for action in view.actions] == [
        "Show checks", "Not Now", "Review first", "Update"
    ]
    assert "Nothing changes until you use the confirmation control." in view.detail
    assert "Hermes recommendation:" not in view.detail

    inspected = controller.act(
        service, record["id"], invocation(message_id="card-1"), "review.0"
    )
    inspected_view = controller.view(inspected)
    assert inspected_view.title == "Review proposed package"
    assert "# exact" in inspected_view.detail
    assert [action.label for action in inspected_view.actions][-1] == "Approve exact package"
    assert inspected_view.notice == (
        "Nothing is uploaded by reviewing. Setup and verification require separate permission."
    )

    deferred = controller.act(
        service, record["id"], invocation(message_id="card-1"), "defer"
    )
    assert deferred["state"] == "deferred"
    assert controller.view(deferred).summary == "Deferred on this surface"
    reopened = controller.request(service, invocation(message_id="request-2"), "skill-1")
    assert reopened["id"] != record["id"]
    assert controller.load(record["id"])["state"] == "superseded"
    assert reopened["state"] == "pending"


def test_catalog_browse_show_versions_and_install_controls_match_reference(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    route = invocation(thread_id="")
    record = controller.request_catalog(service, route, "browse", "incident")
    controller.bind_delivery(record["id"], route, "card-1")

    browse = controller.view(record)
    assert browse.title == "Shared skills"
    assert browse.summary == "Results for “incident” · page 1"
    assert len(browse.items) == 5
    assert [action.label for action in browse.items[0].actions] == ["View", "Install"]
    assert [action.label for action in browse.actions] == ["Next"]

    callback = invocation(thread_id="", message_id="card-1")
    second = controller.act(service, record["id"], callback, "catalog.page.1")
    assert [action.label for action in controller.view(second).actions] == ["Previous"]

    shown = controller.act(service, record["id"], callback, "catalog.show.5")
    shown_view = controller.view(shown)
    assert shown_view.title == "shared-skill"
    assert "Exact shared description" in shown_view.summary
    assert [action.label for action in shown_view.actions] == [
        "Versions", "View in Portal", "Install"
    ]
    assert shown_view.actions[1].url == (
        "https://portal.example/wisdom/skills/skill-1?org_id=org"
    )

    versions = controller.act(service, record["id"], callback, "catalog.versions.0")
    versions_view = controller.view(versions)
    assert versions_view.title == "shared-skill versions"
    assert len(versions_view.items) == 5
    assert [action.label for action in versions_view.items[0].actions] == [
        "View in Portal", "Install"
    ]
    assert versions_view.items[0].actions[0].url == (
        "https://portal.example/wisdom/skills/skill-1?org_id=org&version=6"
    )
    assert [action.label for action in versions_view.actions] == ["Next"]

    install = controller.act(service, record["id"], callback, "catalog.install.0")
    assert install["operation"] == "install_modes"
    assert install["plan"]["reference"] == "skill-6@v6"


def test_confirmation_is_idempotent_and_delivery_failure_does_not_reapply(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    completed = controller.act(service, record["id"], invocation(message_id="card-1"), "confirm")
    assert completed["state"] == "completed"
    assert completed["result"]["setup_continuation"]["state"] == "queued"
    assert service.applies == 1

    controller.delivery_failed(record["id"], "edit failed")
    duplicate = controller.act(
        service, record["id"], invocation(message_id="card-1"), "confirm"
    )
    assert duplicate["state"] == "completed"
    assert duplicate["delivery_state"] == "edit_failed"
    assert service.applies == 1


def test_actor_route_profile_message_expiry_and_unknown_send_are_denied(tmp_path: Path):
    now = [100.0]
    controller = NativeReviewController(WisdomStore(tmp_path), clock=lambda: now[0], ttl_seconds=5)
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    for changed in (
        {"actor_id": "U2", "message_id": "card-1"},
        {"chat_id": "D2", "message_id": "card-1"},
        {"scope_id": "W2", "message_id": "card-1"},
        {"profile": "other", "message_id": "card-1"},
        {"thread_id": "T2", "message_id": "card-1"},
        {"message_id": "other-card"},
    ):
        with pytest.raises(PermissionError):
            controller.act(service, record["id"], invocation(**changed), "confirm")
    now[0] = 106.0
    with pytest.raises(PermissionError, match="expired"):
        controller.act(service, record["id"], invocation(message_id="card-1"), "confirm")

    other = controller.request(service, invocation(message_id="request-2"), "skill-1")
    controller.delivery_uncertain(other["id"], "send acknowledgement unknown")
    with pytest.raises(PermissionError, match="not confirmed"):
        controller.act(service, other["id"], invocation(message_id="anything"), "confirm")


def test_same_organization_account_or_installation_change_revokes_control(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")

    for key, value in (("account_id", "other-member"), ("installation_id", "other-install")):
        service.owner = {**OWNER, key: value}
        with pytest.raises(PermissionError, match="another enrolment"):
            controller.act(
                service, record["id"], invocation(message_id="card-1"), "confirm"
            )
        service.owner = dict(OWNER)

    controller.act(service, record["id"], invocation(message_id="card-1"), "confirm")
    service.owner = dict(OWNER)
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None
    service.owner = {**OWNER, "account_id": "other-member"}
    with pytest.raises(PermissionError, match="another enrolment"):
        controller.create_setup(
            service, claim, invocation(), phase="setup", index=0,
            command="printf denied", explanation="Must retain the original owner.",
        )

    service.owner = dict(OWNER)
    controller.finish_handoff(
        claim["id"], claim["lease_token"], state="pending",
        error="owner_changed_during_setup_review",
    )
    service.owner = {**OWNER, "account_id": "other-member"}
    with pytest.raises(PermissionError, match="another enrolment"):
        controller.claim_handoff(service, invocation())


def test_separate_acknowledgements_create_fresh_exact_reviews(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path), clock=lambda: 100.0)
    service = Service()
    service.modified = True
    service.sensitive = ["new credentials: TEAM_TOKEN"]
    service.compatibility = "compatible_after_setup"
    original = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(original["id"], invocation(), "card-1")

    assert [action.label for action in controller.view(original).actions] == [
        "Show checks",
        "Preserve my modified copy as an unmanaged fork first.",
        "I explicitly accept the new sensitive requirements.",
        "I reviewed and accept the compatibility actions.",
        "Not Now",
        "Review first",
    ]
    inspected = controller.act(
        service, original["id"], invocation(message_id="card-1"), "inspect"
    )
    assert inspected["id"] == original["id"]
    assert service.plan_calls == [{}]

    current = original
    for action, expected in (
        ("ack.preserve", {
            "preserve_modified": True, "accept_sensitive": False, "accept_partial": False,
        }),
        ("ack.sensitive", {
            "preserve_modified": True, "accept_sensitive": True, "accept_partial": False,
        }),
        ("ack.partial", {
            "preserve_modified": True, "accept_sensitive": True, "accept_partial": True,
        }),
    ):
        current = controller.act(
            service, current["id"], invocation(message_id="card-1"), action
        )
        assert current["id"] != original["id"]
        assert current["plan"]["policy"] == expected
        assert current["message_id"] == "card-1"
        original = current

    assert current["plan"]["allowed"] is True
    assert current["plan"]["preservation"]["destination"] in controller.view(current).detail


def test_defer_command_reopen_and_expired_recheck_create_bound_successors(tmp_path: Path):
    now = [100.0]
    controller = NativeReviewController(
        WisdomStore(tmp_path), clock=lambda: now[0], ttl_seconds=5
    )
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    deferred = controller.act(
        service, record["id"], invocation(message_id="card-1"), "defer"
    )
    assert deferred["state"] == "deferred"
    # The pinned source has no deferred-card Reopen control. Explicitly invoking
    # the original /wisdom update flow creates the successor instead.
    assert [(action.label, action.action) for action in controller.view(deferred).actions] == [
        ("Show checks", "checks.show")
    ]
    shown = controller.act(
        service, record["id"], invocation(message_id="card-1"), "checks.show"
    )
    assert shown["state"] == "deferred" and shown["checks_expanded"] is True
    assert service.applies == 0 and service.plan_calls == [{}]

    reopened = controller.request(service, invocation(message_id="request-2"), "skill-1")
    assert reopened["id"] != record["id"]
    controller.bind_delivery(reopened["id"], invocation(message_id="request-2"), "card-2")
    reopened = controller.load(reopened["id"])
    assert reopened["message_id"] == "card-2"
    assert controller.load(record["id"])["state"] == "superseded"

    now[0] = 106.0
    refreshed = controller.act(
        service, reopened["id"], invocation(message_id="card-2"), "recheck"
    )
    assert refreshed["id"] != reopened["id"]
    assert refreshed["state"] == "pending"
    assert refreshed["message_id"] == "card-2"
    assert controller.load(reopened["id"])["state"] == "expired"
    with pytest.raises(PermissionError, match="another session"):
        controller.act(
            service, refreshed["id"],
            invocation(actor_id="U2", message_id="card-1"), "confirm",
        )


def test_post_apply_inspection_failure_recovers_same_receipt_without_reapply(tmp_path: Path):
    root = tmp_path / "state"
    controller = NativeReviewController(WisdomStore(root))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    service.inspect_error = RuntimeError("inspection unavailable")

    with pytest.raises(RuntimeError, match="inspection unavailable"):
        controller.act(
            service, record["id"], invocation(message_id="card-1"), "confirm"
        )
    failed = controller.load(record["id"])
    assert failed["state"] == "needs_review"
    assert [(action.label, action.action) for action in controller.view(failed).actions][-1] == (
        "Recover operation", "recover"
    )

    service.inspect_error = None
    restarted = NativeReviewController(WisdomStore(root))
    completed = restarted.act(
        service, record["id"], invocation(message_id="card-1"), "recover"
    )
    assert completed["state"] == "completed"
    assert completed["plan"]["receipt"] == record["plan"]["receipt"]
    assert service.applies == 2  # Same receipt is reconciled by the executor-facing API.


def test_setup_status_persists_fresh_ready_projection(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    completed = controller.act(
        service, record["id"], invocation(message_id="card-1"), "confirm"
    )
    assert controller.view(completed).summary == "Setup queued"

    service.ready_to_use = True
    service.setup_state = "verified"
    ready = controller.act(
        service, record["id"], invocation(message_id="card-1"), "setup.status"
    )
    assert ready["setup_continuation"]["state"] == "ready"
    assert ready["setup_continuation"]["inspection"]["state"] == "verified"
    assert controller.view(ready).summary == "Ready"
    assert NativeReviewController(WisdomStore(tmp_path)).load(record["id"])[
        "setup_continuation"
    ]["state"] == "ready"


def test_delivery_attempt_fencing_rejects_late_failure(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    competitor = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    revision, first_attempt = controller.delivery_token(record["id"])
    confirmed = controller.bind_delivery(
        record["id"], invocation(), "card-1",
        expected_revision=revision, attempt=first_attempt,
    )
    assert confirmed["delivery_revision"] == revision + 1

    edit_revision, edit_attempt = competitor.delivery_token(record["id"])
    competitor.delivery_confirmed(
        record["id"], expected_revision=edit_revision, attempt=edit_attempt
    )
    with pytest.raises(PermissionError, match="delivery state changed"):
        controller.delivery_failed(
            record["id"], "late transport failure",
            expected_revision=edit_revision, attempt=edit_attempt,
        )
    latest = controller.load(record["id"])
    assert latest["delivery_state"] == "confirmed"
    assert latest["delivery_revision"] == edit_revision + 1
    assert latest["delivery_error"] is None


def test_delivery_lost_cas_accepts_only_an_already_confirmed_same_fact(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    competitor = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    revision, attempt = controller.delivery_token(record["id"])
    competitor.bind_delivery(
        record["id"], invocation(), "card-1",
        expected_revision=revision, attempt=attempt,
    )

    same = controller.bind_delivery(
        record["id"], invocation(), "card-1",
        expected_revision=revision, attempt=attempt,
    )

    assert same["delivery_state"] == "confirmed"
    assert same["message_id"] == "card-1"
    assert same["delivery_revision"] == revision + 1


def test_competing_store_callbacks_cannot_apply_one_review_twice(tmp_path: Path):
    first = NativeReviewController(WisdomStore(tmp_path))
    second = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    entered = threading.Event()
    release = threading.Event()
    original_apply = service.update_apply

    def held_apply(receipt):
        result = original_apply(receipt)
        entered.set()
        assert release.wait(timeout=2)
        return result

    service.update_apply = held_apply
    record = first.request(service, invocation(), "skill-1")
    first.bind_delivery(record["id"], invocation(), "card-1")
    callback = invocation(message_id="card-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        winning = pool.submit(first.act, service, record["id"], callback, "confirm")
        assert entered.wait(timeout=2)
        with pytest.raises(PermissionError, match="no longer pending"):
            second.act(service, record["id"], callback, "confirm")
        release.set()
        assert winning.result(timeout=2)["state"] == "completed"
    assert service.applies == 1


def test_waiting_for_model_is_projected_from_durable_handoff(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    parent = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(parent["id"], invocation(), "card-1")
    completed = controller.act(
        service, parent["id"], invocation(message_id="card-1"), "confirm"
    )
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None
    controller.finish_handoff(
        claim["id"], claim["lease_token"], state="waiting_for_model",
        error="session_model_unavailable",
    )
    waiting = controller.act(
        service, completed["id"], invocation(message_id="card-1"), "setup.status"
    )
    assert waiting["setup_continuation"]["state"] == "waiting_for_model"
    assert controller.view(waiting).summary == "Setup waiting for model"


@pytest.mark.parametrize(
    ("outcome", "summary"),
    [("failed", "Setup step failed"), ("blocked", "Terminal permission required")],
)
def test_failed_and_blocked_setup_states_recheck_to_fresh_review(
    tmp_path: Path, outcome: str, summary: str,
):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    parent = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(parent["id"], invocation(), "card-1")
    controller.act(service, parent["id"], invocation(message_id="card-1"), "confirm")
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None
    setup = controller.create_setup(
        service, claim, invocation(), phase="setup", index=0,
        command="printf attempt", explanation="Configure the skill.",
    )
    controller.bind_delivery(setup["id"], invocation(), "setup-card")
    service.setup_result = {"state": outcome, "operation_id": "setup-operation"}
    terminal = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "confirm"
    )
    assert terminal["state"] == "failed"
    assert controller.view(terminal).summary == summary

    successor = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "recheck"
    )
    assert successor["state"] == "pending"
    assert successor["id"] != setup["id"]
    assert successor["plan"]["parent_consent_id"] == parent["id"]
    assert successor["plan"]["slug"] == "managed"
    assert successor["plan"]["setup_explanation"] == "Configure the skill."
    assert successor["plan"]["receipt"] != setup["plan"]["receipt"]
    assert controller.load(setup["id"])["state"] == "stale"
    plan_count = service.setup_plans
    duplicate = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "recheck"
    )
    assert duplicate["id"] == successor["id"]
    assert service.setup_plans == plan_count

    deferred = controller.act(
        service, successor["id"], invocation(message_id="setup-card"), "defer"
    )
    assert deferred["state"] == "deferred"
    assert controller.latest_setup(parent)["id"] == successor["id"]

    resumed = controller.act(
        service, parent["id"], invocation(message_id="card-1"), "setup.status"
    )
    assert resumed["id"] == parent["id"]
    assert resumed["setup_continuation"]["state"] == "queued"
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None and claim["parent"]["id"] == parent["id"]
    fresh = controller.create_setup(
        service, claim, invocation(), phase="setup", index=0,
        command="printf later", explanation="Review the step again.",
    )
    assert fresh["plan"]["parent_consent_id"] == parent["id"]
    assert fresh["plan"]["receipt"] != successor["plan"]["receipt"]


def test_assessment_projection_toggle_is_read_only_and_owner_bound(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(record["id"], invocation(), "card-1")
    plans = service.plans

    shown = controller.act(
        service, record["id"], invocation(message_id="card-1"), "assessment.show"
    )
    assert shown["assessment_expanded"] is True
    assert service.plans == plans and service.applies == 0
    hidden = controller.act(
        service, record["id"], invocation(message_id="card-1"), "assessment.hide"
    )
    assert hidden["assessment_expanded"] is False
    assert service.plans == plans and service.applies == 0

    service.owner = {**OWNER, "account_id": "intruder"}
    with pytest.raises(PermissionError, match="another enrolment"):
        controller.act(
            service, record["id"], invocation(message_id="card-1"), "assessment.show"
        )


def test_unknown_setup_requires_explicit_review_and_clear_without_rerun(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    parent = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(parent["id"], invocation(), "card-1")
    controller.act(service, parent["id"], invocation(message_id="card-1"), "confirm")
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None
    setup = controller.create_setup(
        service, claim, invocation(), phase="setup", index=0,
        command="printf configured", explanation="Configure the skill.",
    )
    controller.bind_delivery(setup["id"], invocation(), "setup-card")
    service.setup_result = {"state": "unknown", "operation_id": "setup-op-1"}

    unknown = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "confirm"
    )
    assert unknown["state"] == "applying"
    assert controller.view(unknown).summary == "Setup outcome unknown"
    reviewed = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "setup.recover"
    )
    assert reviewed["result"]["setup"]["recovery_review"] is True
    cleared = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "setup.clear"
    )
    assert cleared["result"]["setup"]["state"] == "abandoned"
    assert cleared["state"] == "needs_review"
    assert service.setup_applies == 1


def test_stale_setup_progress_cannot_overwrite_a_newer_clear_from_another_connection(  # pylint: disable=too-many-locals
    tmp_path: Path,
):
    first = NativeReviewController(WisdomStore(tmp_path))
    second = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    parent = first.request(service, invocation(), "skill-1")
    first.bind_delivery(parent["id"], invocation(), "card-1")
    first.act(service, parent["id"], invocation(message_id="card-1"), "confirm")
    claim = first.claim_handoff(service, invocation())
    assert claim is not None
    setup = first.create_setup(
        service, claim, invocation(), phase="setup", index=0,
        command="printf configured", explanation="Configure the skill.",
    )
    first.bind_delivery(setup["id"], invocation(), "setup-card")
    service.setup_result = {"state": "unknown", "operation_id": "setup-op-1"}
    callback = invocation(message_id="setup-card")
    first.act(service, setup["id"], callback, "confirm")

    entered = threading.Event()
    release = threading.Event()
    recover_calls = 0

    def racing_recover(_skill_id, operation_id, *, acknowledged=False):
        nonlocal recover_calls
        recover_calls += 1
        if recover_calls == 1:
            entered.set()
            assert release.wait(timeout=2)
        return {
            "state": "abandoned" if acknowledged else "unknown",
            "operation_id": operation_id,
            **({} if acknowledged else {"recovery_review": True}),
        }

    service.installed_setup_recover = racing_recover
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_progress = pool.submit(first.act, service, setup["id"], callback, "inspect")
        assert entered.wait(timeout=2)
        reviewed = second.act(service, setup["id"], callback, "setup.recover")
        assert reviewed["result"]["setup"]["recovery_review"] is True
        cleared = second.act(service, setup["id"], callback, "setup.clear")
        assert cleared["state"] == "needs_review"
        release.set()
        with pytest.raises(PermissionError, match="state changed"):
            stale_progress.result(timeout=2)

    durable = NativeReviewController(WisdomStore(tmp_path)).load(setup["id"])
    assert durable["state"] == "needs_review"
    assert durable["result"]["setup"]["state"] == "abandoned"


def test_stale_complete_cannot_restore_terminal_setup_or_enqueue_handoff(tmp_path: Path):
    first = NativeReviewController(WisdomStore(tmp_path))
    second = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    record = first.request(service, invocation(), "skill-1")
    first.bind_delivery(record["id"], invocation(), "card-1")
    with first.store.transaction() as db:
        db.execute(
            "UPDATE wisdom_consent SET state='applying' WHERE id=?",
            (record["id"],),
        )
    stale = first.load(record["id"])
    second._store_setup_result(
        stale, {"state": "abandoned", "operation_id": "setup-op-1"},
        state="needs_review",
    )

    with pytest.raises(PermissionError, match="state changed"):
        first._complete(
            stale, {"state": "passed"},
            {"skill_id": "skill-1", "version": 2, "ready_to_use": False},
        )

    durable = first.load(record["id"])
    assert durable["state"] == "needs_review"
    with first.store.transaction() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM wisdom_setup_handoff WHERE consent_id=?",
            (record["id"],),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_unrenderable_setup_step_has_terminal_handoff_and_cannot_be_confirmed(
    tmp_path: Path, platform: str,
):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    service.setup_instruction = "Publisher guidance: " + ("<& long guidance > & " * 240)
    route = invocation(platform=platform, session_key=f"{platform}:owner")
    parent = controller.request(service, route, "skill-1")
    controller.bind_delivery(parent["id"], route, "card-1")
    controller.act(service, parent["id"], invocation(
        platform=platform, session_key=f"{platform}:owner", message_id="card-1"
    ), "confirm")
    claim = controller.claim_handoff(service, route)
    assert claim is not None
    command = "printf '%s' UNSEEN_COMMAND_TAIL"
    setup = controller.create_setup(
        service, claim, route, phase="setup", index=0, command=command,
        explanation="Configure only this declared step.",
    )
    controller.bind_delivery(setup["id"], route, "setup-card")

    view = controller.view(controller.load(setup["id"]))
    rendered = (
        "\n".join(
            block.get("text", {}).get("text", "")
            for block in blocks(view, setup["id"])
            if block.get("type") == "section"
        )
        if platform == "slack" else telegram_text(view)
    )
    assert command not in rendered
    assert "Run this step" not in [action.label for action in view.actions]
    assert "Native approval is disabled" in rendered
    assert "hermes wisdom installed-setup inspect skill-1 --version 2 --json" in rendered
    assert "hermes wisdom installed-setup plan" in rendered

    with pytest.raises(PermissionError, match="complete setup step cannot be reviewed"):
        controller.act(
            service, setup["id"], invocation(
                platform=platform, session_key=f"{platform}:owner", message_id="setup-card"
            ), "confirm",
        )
    assert service.setup_applies == 0
    assert controller.load(setup["id"])["state"] == "pending"


def test_generic_recover_cannot_replay_or_complete_a_running_setup(tmp_path: Path):
    controller = NativeReviewController(WisdomStore(tmp_path))
    service = Service()
    parent = controller.request(service, invocation(), "skill-1")
    controller.bind_delivery(parent["id"], invocation(), "card-1")
    controller.act(service, parent["id"], invocation(message_id="card-1"), "confirm")
    claim = controller.claim_handoff(service, invocation())
    assert claim is not None
    setup = controller.create_setup(
        service, claim, invocation(), phase="setup", index=0,
        command="printf configured", explanation="Configure the skill.",
    )
    controller.bind_delivery(setup["id"], invocation(), "setup-card")
    service.setup_result = {"state": "running", "operation_id": "setup-op-1"}
    running = controller.act(
        service, setup["id"], invocation(message_id="setup-card"), "confirm"
    )
    assert running["state"] == "applying"
    assert service.setup_applies == 1

    with pytest.raises(PermissionError, match="only available for updates"):
        controller.act(
            service, setup["id"], invocation(message_id="setup-card"), "recover"
        )
    assert service.setup_applies == 1
    assert controller.load(setup["id"])["state"] == "applying"


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_publication_confirmation_is_removed_when_exact_review_is_truncated(
    tmp_path: Path, platform: str,
):
    class PublicationService(Service):
        def publication_review(self, draft_id):
            return {
                "draft_id": draft_id, "editorial_name": "Large publication",
                "receipt": "publication-receipt", "description": "z" * 7000,
                "files": [{"path": "SKILL.md", "hash": "e" * 64}],
                "hashes": {
                    "content": "sha256:" + "f" * 64,
                    "author_description": "sha256:" + "a" * 64,
                    "package_manifest": "sha256:" + "b" * 64,
                },
                "security_check": {"status": "pass", "summary": "No known matches."},
                "professionalism_check": {"status": "pass"},
                "publication_mode": "moderated", "policy_version": 7,
                "notice": "Nothing has been uploaded.", "allowed": True,
                "owner": dict(self.owner),
            }

        def publication_submit(self, _receipt):
            raise AssertionError("truncated publication must not submit")

    controller = NativeReviewController(WisdomStore(tmp_path))
    service = PublicationService()
    request = invocation(platform=platform)
    record = controller.request_publication(service, request, "draft-large")
    controller.bind_delivery(record["id"], request, "publication-card")
    view = controller.view(record)
    assert view.summary == "Complete review required in terminal"
    assert [action.label for action in view.actions] == ["Not Now"]
    assert "publication review draft-large --json" in view.detail
    with pytest.raises(PermissionError, match="cannot be shown"):
        controller.act(
            service, record["id"],
            invocation(platform=platform, message_id="publication-card"), "confirm",
        )
