"""Owner-bound recommendation entry points, without live services."""
# pylint: disable=missing-function-docstring,protected-access,duplicate-code
from dataclasses import replace
from pathlib import Path

import pytest
from native_test_support import native_invocation, owner
from test_native_surfaces import Service

from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.store import WisdomStore


def shared_job():
    return {
        "state": "ready", "reference": {
            "kind": "skill", "skill_id": "skill-1", "version": 2,
        },
        "advice": {
            "title": "Shared workflow", "explanation": "Useful in recurring reviews.",
            "relevance": "recommend", "provenance": {"kind": "agent_assessed"},
        },
    }


def shared_facts():
    return {
        "skill": {"id": "skill-1", "state": "active", "slug": "managed"},
        "version": {"version": 2, "content_hash": "b" * 64,
                    "security_check": {"status": "pass"}},
    }


class RecommendationService(Service):
    """Record planning separately from irreversible application."""
    def __init__(self, store):
        self.store = store
        self.planned = []

    def install_plan(self, reference, *, update_mode=None):
        self.planned.append((reference, update_mode))
        return super().install_plan(reference, update_mode=update_mode)

    def prepare_review_deferral(self, plan):
        return {"owner": owner(), "reference": {
            "kind": "skill", "skill_id": plan["skill_id"], "version": plan["version"],
        }, "not_now_suppression_days": 7}

    def defer_review(self, _deferral, *, may_apply):
        assert may_apply()
        return {"deferred": True}


def test_recipient_offer_enters_exact_policy_review_without_installing(tmp_path: Path):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    record = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(route, message_id="card-1")
    view = controller.view(controller.load(record["id"]))
    assert "Hermes has detected a newly shared skill in your organization, " in view.to_text()
    assert view.notice.endswith("Would you like to install this skill?")
    assert [action.label for action in view.actions] == [
        "View Details", "Not Now", "Mute Skill Recommendations", "Install Skill",
    ]
    assert service.planned == []
    modes = controller.act(service, record["id"], click, "recommendation.install")
    assert modes["operation"] == "install_modes"
    assert service.planned == []
    review = controller.act(service, record["id"], click, "install.mode.manual")
    assert service.planned == [("skill-1@v2", "MANUAL")]
    assert review["operation"] == "install" and review["state"] == "pending"
    assert review["plan"]["receipt"] == "install-receipt"
    assert "confirm" in [a.action for a in controller.view(review).actions]
    with pytest.raises((PermissionError, ValueError)):
        controller.act(service, record["id"], click, "recommendation.install")


@pytest.mark.parametrize("status", ["blocked", "pending", "unavailable"])
def test_recommendation_preserves_warnings_and_rejects_approval(tmp_path, status):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    facts = shared_facts()
    facts["version"]["security_check"] = {"status": status, "summary": "Material finding"}
    record = controller.request_recommendation(service, route, shared_job(), facts)
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(route, message_id="card-1")
    view = controller.view(record)
    assert "Material finding" in view.to_text()
    assert "Install Skill" not in [a.label for a in view.actions]
    with pytest.raises(PermissionError):
        controller.act(service, record["id"], click, "recommendation.install")
    with pytest.raises(PermissionError):
        controller.act(service, record["id"], click, "confirm")
    assert controller.load(record["id"])["state"] == "pending"
    assert service.planned == []


def test_details_are_exact_read_only_and_return_to_offer(tmp_path):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    record = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(route, message_id="card-1")
    details = controller.act(service, record["id"], click, "recommendation.details")
    text = controller.view(details).to_text()
    assert "v2" in text and "b" * 64 in text
    back = controller.act(service, record["id"], click, "recommendation.back")
    assert controller.view(back).notice.endswith("Would you like to install this skill?")
    assert service.planned == []


@pytest.mark.parametrize("name", [None, "Example Org", "bad\u202ename"])
def test_org_introduction_uses_only_resolved_safe_name(tmp_path, name):
    store = WisdomStore(tmp_path)
    store.record_organization_display_name_check("org", name)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    record = controller.request_recommendation(
        service, native_invocation(), shared_job(), shared_facts(),
    )
    text = controller.view(record).to_text()
    if name == "Example Org":
        assert "your organization (Example Org), that could be useful to you!" in text
    else:
        assert "your organization, that could be useful to you!" in text
        assert "bad" not in text


def test_recipient_settings_open_on_original_route_without_muting(tmp_path):
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    from test_native_surfaces import (  # pylint: disable=import-outside-toplevel
        FakeLlm, SlackAdapter, SlackApp, _slack_action, callback_body, invocation,
    )
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime  # pylint: disable=import-outside-toplevel
    import asyncio  # pylint: disable=import-outside-toplevel

    async def scenario():
        store = WisdomStore(tmp_path)
        store.activate_installation_identity("install", "org")
        service = RecommendationService(store)
        service.client = SimpleNamespace(
            identity={"owner": "member"}, owner_user_id="member",
            display_org_id="org", expected_organization_id="org",
            recommendation_mute=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
        )
        service.notification_mode = lambda: "agent"
        service.notification_mode_writable = lambda: True
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app, adapter = SlackApp(), SlackAdapter()
        runtime.wire_slack(app, adapter)
        route = invocation(adapter)
        record = runtime.reviews.request_recommendation(service, route, shared_job(), shared_facts())
        await runtime._deliver_new(route, record)
        button = _slack_action(app.client.posts[-1]["blocks"], "Mute Skill Recommendations")
        async def ack():
            return None
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert not app.client.ephemeral
        edited = app.client.edits[-1]
        assert edited["ts"] == "card-1"
        assert "Notification settings" in edited["text"]
        assert "all Collective Wisdom recommendations for your account in this organization" in edited["text"]
        assert service.planned == []
        assert store.get_metadata("recommendation_mute") is None
        runtime.close()
    asyncio.run(scenario())
