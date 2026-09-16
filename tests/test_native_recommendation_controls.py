"""Owner-bound recommendation entry points, without live services."""
# pylint: disable=missing-function-docstring,protected-access,duplicate-code
# SDK service doubles are configured explicitly per scenario.
# pylint: disable=attribute-defined-outside-init
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


def test_digest_cannot_request_recipient_controls(tmp_path):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    job = shared_job()
    job["advice"]["relevance"] = "digest"
    with pytest.raises(ValueError):
        NativeReviewController(store).request_recommendation(
            service, native_invocation(), job, shared_facts(),
        )
    with store.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM wisdom_consent").fetchone()[0] == 0


def test_multiple_delivered_recommendations_keep_independent_controls(tmp_path):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    first = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(first["id"], route, "card-1")
    second = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(second["id"], route, "card-2")
    assert controller.load(first["id"])["state"] == "pending"
    result = controller.act(service, first["id"], replace(route, message_id="card-1"),
                            "recommendation.details")
    assert "Exact version" in controller.view(result).to_text()


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
    assert not service.planned
    modes = controller.act(service, record["id"], click, "recommendation.install")
    assert modes["operation"] == "install_modes"
    assert not service.planned
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
    assert not service.planned


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
    assert not service.planned


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
        record = runtime.reviews.request_recommendation(
            service, route, shared_job(), shared_facts(),
        )
        await runtime._deliver_new(route, record)
        button = _slack_action(app.client.posts[-1]["blocks"], "Mute Skill Recommendations")
        async def ack():
            return None
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert not app.client.ephemeral
        edited = app.client.edits[-1]
        assert edited["ts"] == "card-1"
        assert "Notification settings" in edited["text"]
        assert (
            "all Collective Wisdom recommendations for your account in this organization"
            in edited["text"]
        )
        assert not service.planned
        assert store.get_metadata("recommendation_mute") is None
        runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    {"actor_id": "other"}, {"profile": "other"}, {"session_key": "other"},
    {"chat_id": "other"}, {"thread_id": "other"}, {"scope_id": "other"},
    {"message_id": "other"},
])
def test_recipient_controls_reject_other_routes(tmp_path, change):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    record = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(replace(route, message_id="card-1"), **change)
    for action in (
        "recommendation.install", "recommendation.details", "recommendation.settings", "defer",
    ):
        with pytest.raises(PermissionError):
            controller.act(service, record["id"], click, action)
    assert not service.planned


def test_recipient_not_now_dismisses_and_replay_cannot_install(tmp_path):
    store = WisdomStore(tmp_path)
    service = RecommendationService(store)
    controller = NativeReviewController(store)
    route = native_invocation()
    record = controller.request_recommendation(service, route, shared_job(), shared_facts())
    controller.bind_delivery(record["id"], route, "card-1")
    click = replace(route, message_id="card-1")
    result = controller.act(service, record["id"], click, "defer")
    assert controller.view(result).dismissed
    restarted = NativeReviewController(WisdomStore(tmp_path))
    with pytest.raises(PermissionError):
        restarted.act(service, record["id"], click, "recommendation.install")
    assert not service.planned


def test_sharing_snooze_opens_settings_without_publication(tmp_path):
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    from native_test_support import publication_review  # pylint: disable=import-outside-toplevel
    store = WisdomStore(tmp_path)
    store.activate_installation_identity("install", "org")
    service = RecommendationService(store)
    service.client = SimpleNamespace(
        identity={"owner": "member"}, owner_user_id="member", display_org_id="org",
        recommendation_mute=lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    service.notification_mode = lambda: "agent"
    service.notification_mode_writable = lambda: False
    service.publication_review = publication_review
    controller = NativeReviewController(store)
    route = native_invocation()
    record = controller.request_publication(service, route, "draft-1")
    controller.bind_delivery(record["id"], route, "card-1")
    button = next(
        a for a in controller.view(record).actions if a.label == "Snooze Collective Wisdom"
    )
    settings = controller.act(
        service, record["id"], replace(route, message_id="card-1"), button.action,
    )
    assert settings["operation"] == "settings"
    assert settings["message_id"] == "card-1"
    assert controller.load(record["id"])["state"] == "superseded"


def test_actual_preparation_failure_has_typed_safe_retry_card(tmp_path, monkeypatch):
    import asyncio  # pylint: disable=import-outside-toplevel
    from test_native_surfaces import FakeLlm, SlackApp, SlackAdapter, invocation  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.runtime import InvocationService  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.package import PackagePolicyError  # pylint: disable=import-outside-toplevel

    service = InvocationService(tmp_path, ("default", tmp_path))
    monkeypatch.setattr(
        service, "native_candidate_inspect", lambda _event: {"facts": {"name": "example"}},
    )
    monkeypatch.setattr(service, "native_owner", owner)
    monkeypatch.setattr(service, "suggest", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        PackagePolicyError("secret-test-value /private/path")
    ))
    async def scenario():
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app, adapter = SlackApp(), SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin_candidate(invocation(adapter), "event-1") is None
        text = app.client.posts[-1]["text"]
        assert "Share Packaging failed" in text
        assert "Nothing was uploaded or shared" in text
        assert "secret-test-value" not in text and "/private/path" not in text
        actions = [
            e["text"]["text"] for b in app.client.posts[-1]["blocks"]
            for e in b.get("elements", []) if e.get("type") == "button"
        ]
        assert actions == ["Retry Packaging"]
        assert "Snooze" not in text
        from test_native_surfaces import _slack_action, callback_body  # pylint: disable=import-outside-toplevel
        from native_test_support import publication_review  # pylint: disable=import-outside-toplevel
        monkeypatch.setattr(service, "suggest", lambda *_args, **_kwargs: {"draft_id": "draft-1"})
        monkeypatch.setattr(service, "publication_review", publication_review)
        monkeypatch.setattr(service, "close", lambda: None)
        button = _slack_action(app.client.posts[-1]["blocks"], "Retry Packaging")
        async def ack():
            return None
        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert not app.client.ephemeral
        assert "Ready For Review" in app.client.edits[-1]["text"]
        assert _slack_action(app.client.edits[-1]["blocks"], "Snooze Collective Wisdom")
        assert _slack_action(app.client.edits[-1]["blocks"], "Share My Skill")
        runtime.close()
    asyncio.run(scenario())
