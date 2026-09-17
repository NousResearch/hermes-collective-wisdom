"""Native SDK adapter behavior without live transports."""

# Test doubles intentionally implement external SDK spellings and minimal protocols.
# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=too-few-public-methods,invalid-name,duplicate-code
# pylint: disable=protected-access,too-many-statements,use-implicit-booleaness-not-comparison
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from native_test_support import native_invocation, owner as owner_fixture, publication_review

from hermes_wisdom_plugin.client import (
    AgentLedNotificationDefaults,
    AgentLedPolicyResponse,
    WisdomMuteResponse,
)
from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.delivery import DeliveryReceipt
from hermes_wisdom_plugin.delivery_outbox import DeliveryOutbox
from hermes_wisdom_plugin import mediation
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.native_review import NativeReviewController, callback_value
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime, assessment_view
from hermes_wisdom_plugin.native_slack import blocks as slack_blocks
from hermes_wisdom_plugin.native_slack import callback_invocation as slack_invocation
from hermes_wisdom_plugin.native_telegram import callback_invocation as telegram_invocation
from hermes_wisdom_plugin.qualification import snapshot_tree
from hermes_wisdom_plugin.store import WisdomStore


OWNER = owner_fixture()


class Service:
    applies = 0
    installs = 0

    def close(self):
        pass

    def native_owner(self):
        return dict(OWNER)

    def update_plan(self, skill_id, **_choices):
        return {
            "operation": "update", "receipt": "receipt", "skill_id": skill_id,
            "slug": "managed", "from_version": 1, "version": 2,
            "previous_content_hash": "a" * 64, "content_hash": "b" * 64,
            "manifest_hash": "c" * 64, "update_mode": "MANUAL", "allowed": True,
            "modified": False, "sensitive_expansion": [],
            "compatibility": {"outcome": "compatible"},
            "local_scan": {"allowed": True, "status": "pass"},
            "server_checks": {"status": "pass"},
            "files": [{"path": "SKILL.md", "hash": "d" * 64}],
            "owner": dict(OWNER),
        }

    def install_plan(self, reference, *, update_mode=None):
        return {
            **self.update_plan(reference.split("@", 1)[0]),
            "operation": "install", "receipt": "install-receipt",
            "from_version": None, "update_mode": update_mode,
        }

    def install_apply(self, receipt, **kwargs):
        assert kwargs["may_apply"]() is True
        assert receipt == "install-receipt"
        Service.installs += 1
        return {"installed": True, "version": 2}

    def native_install_inspect(self, receipt):
        assert receipt == "install-receipt"
        return self.native_update_inspect(receipt)

    def native_update_inspect(self, _receipt):
        return [{"path": "SKILL.md", "content": "# exact", "hash": "d" * 64}]

    def update_apply(self, _receipt, *, may_apply):
        assert may_apply() is True
        Service.applies += 1
        return {"updated": True, "version": 2}

    def installed_setup_inspect(self, _skill_id, *, version=None):
        return {
            "skill_id": "skill-1", "slug": "managed", "version": version,
            "content_hash": "b" * 64,
            "guidance": {"setup_instructions": ["Configure"], "verification_step": "Verify"},
            "prerequisites": [], "setup_progress": [], "ready_to_use": None,
        }

    def installed_setup_plan(self, skill_id, **values):
        return {
            "receipt": "setup-receipt", "skill_id": skill_id, "slug": "managed",
            "version": values["version"], "content_hash": "b" * 64,
            "step": {"phase": values["phase"], "index": values["index"],
                     "command": values["command"]},
            "setup_instruction": "Configure", "allowed": True, "owner": dict(OWNER),
        }

    def installed_setup_apply(self, _receipt, *, may_apply):
        assert may_apply() is True
        return {"state": "passed"}


class FakeLlm:
    def __init__(self):
        self.calls = []

    async def acomplete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            provider="openai-codex", model="active-model",
            parsed={"command": "printf configured", "explanation": "Configure the skill."},
        )


class SlackClient:
    def __init__(self):
        self.posts = []
        self.edits = []
        self.ephemeral = []
        self.fail_edit = False

    async def chat_postMessage(self, **payload):
        self.posts.append(payload)
        return {"ts": "card-1"}

    async def chat_update(self, **payload):
        self.edits.append(payload)
        if self.fail_edit:
            raise RuntimeError("deleted")
        return {"ok": True}

    async def chat_postEphemeral(self, **payload):
        self.ephemeral.append(payload)
        return {"ok": True}


class SlackApp:
    def __init__(self):
        self.client = SlackClient()
        self.handlers = {}

    def action(self, action_id):
        def register(callback):
            self.handlers[action_id] = callback
            return callback
        return register

    def handler_for(self, action_id):
        for matcher, callback in self.handlers.items():
            if matcher == action_id or (
                hasattr(matcher, "fullmatch") and matcher.fullmatch(action_id)
            ):
                return callback
        raise KeyError(action_id)


def async_receipted_slack_post(client):
    async def post(**payload):
        client.posts.append(payload)
        return {"ok": True, "channel": payload["channel"], "ts": "assessment-1"}
    return post


class SlackAdapter:
    _owner_profile = "default"

    def _is_interactive_user_authorized(self, user_id, **_kwargs):
        return user_id == "U1"


class TelegramBot:
    def __init__(self):
        self.edits = []
        self.fail_edit = False

    async def do_api_request(self, endpoint, *, api_kwargs):
        self.edits.append((endpoint, api_kwargs))
        if self.fail_edit:
            raise TimeoutError("telegram edit acknowledgement timed out")
        return {"ok": True, "result": {"message_id": api_kwargs["message_id"]}}


class TelegramAdapter:
    _owner_profile = "default"

    def __init__(self, bot):
        self._bot = bot

    def _is_callback_user_authorized(self, user_id, **_kwargs):
        return user_id == "7"


class TelegramQuery:
    def __init__(self, bot, *, assessment_id: str, action: str):
        self.data = callback_value(assessment_id, action)
        self.from_user = SimpleNamespace(id=7, username="shannon", first_name="Shannon")
        self.message = SimpleNamespace(
            chat=SimpleNamespace(type="private"), chat_id=123,
            message_thread_id=None, message_id=77, get_bot=lambda: bot,
        )
        self.plain_edits = []

    async def edit_message_text(self, **payload):
        self.plain_edits.append(payload)


def invocation(adapter):
    return native_invocation(adapter, thread_id="")


def callback_body():
    return {
        "user": {"id": "U1", "name": "shannon"}, "channel": {"id": "D1"},
        "team": {"id": "W1"}, "message": {"ts": "card-1"},
        "container": {"message_ts": "card-1"},
    }


def _slack_action(blocks, label):
    for block in blocks:
        for element in block.get("elements", []):
            if element.get("type") == "button" and element["text"]["text"] == label:
                return element
    raise AssertionError(f"button not found: {label}")


def _delivered_candidate(tmp_path: Path, route):  # pylint: disable=too-many-locals
    store = WisdomStore(tmp_path / "state")
    store.activate_installation_identity("install", "org")
    store.bind_qualification_owner(OWNER)
    local = tmp_path / "candidate"
    local.mkdir()
    (local / "SKILL.md").write_text("# Candidate\n", encoding="utf-8")
    content_hash, tree = snapshot_tree(local)
    skill_id = store.register_skill(
        local, content_hash=content_hash, source_kind="local", tree=tree
    )
    event_id = store.emit_local_event(
        kind="wisdom.candidate", skill_id=skill_id, content_hash=content_hash,
        session_id="origin", task_id="task", qualification="high_usage",
        payload={"local_reasons": {}, "consent_required": True, "networked": False},
    )
    queue = MediationStore(store, owner=OWNER, clock=lambda: 1_000.0)
    queue.register_session(
        "org", session_key=route.session_key, session_id="session-1",
        platform=route.platform, actor_id=route.actor_id, private=True,
        available=True, user_activity=True,
        address={
            "chat_id": route.chat_id, "thread_id": route.thread_id,
            "scope_id": route.scope_id,
        },
    )
    assessment_id = queue.enqueue_owned(
        "org", f"candidate:{skill_id}:{content_hash}",
        {"kind": "candidate", "local_skill_id": skill_id,
         "content_hash": content_hash, "event_id": event_id},
    )
    job = queue.claim("org", route.session_key, assessment_only=True)[0]
    assert queue.save_advice("org", assessment_id, job["lease_token"], {
        "assessment_id": assessment_id, "relevance": "recommend",
        "title": "Candidate", "explanation": "You use this often.",
        "provenance": {"kind": "local_qualification"},
    })
    delivered = queue.claim("org", route.session_key, ready_only=True)[0]
    queue.bind_candidate_policy("org", assessment_id, delivered["lease_token"], 7)
    assert queue.begin_delivery("org", assessment_id, delivered["lease_token"])
    assert queue.complete_delivery(
        "org", assessment_id, delivered["lease_token"],
        receipt=DeliveryReceipt(
            platform=route.platform, destination=route.chat_id,
            message_id=route.message_id, scope_id=route.scope_id,
            acknowledgement="provider_accepted",
        ),
    )
    return store, queue, assessment_id, event_id


def test_slack_settings_card_uses_config_callbacks_and_original_message(tmp_path: Path):
    async def scenario():
        store = WisdomStore(tmp_path)
        store.activate_installation_identity("install", "org")

        class SettingsService(Service):
            def __init__(self):
                self.store = store
                self.client = SimpleNamespace(
                    identity={"owner": "member"}, owner_user_id="member",
                    display_org_id="org", expected_organization_id="org",
                    recommendation_mute=lambda: (_ for _ in ()).throw(
                        ConnectionError("offline")
                    ),
                )

            def require_setup(self):
                return None

            @staticmethod
            def notification_mode():
                return "agent" if config.get("notifications.delivery_mode") == "agent" else "fixed"

            @staticmethod
            def notification_mode_writable():
                return True

            @staticmethod
            def set_notification_mode(value):
                sets.append(("notifications.delivery_mode", value))
                config["notifications.delivery_mode"] = value

        config = {"notifications.delivery_mode": "agent"}
        sets = []
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", SettingsService, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)

        assert await runtime.begin_settings(invocation(adapter)) is None
        button = _slack_action(app.client.posts[-1]["blocks"], "Fixed notifications")

        async def ack():
            return None

        await app.handler_for(button["action_id"])(ack, callback_body(), button)
        assert sets == [("notifications.delivery_mode", "fixed")]
        assert app.client.edits[-1]["ts"] == "card-1"
        assert "Notification settings updated" in app.client.edits[-1]["text"]

    asyncio.run(scenario())


def test_fallback_assessment_projection_is_truthful_and_action_free():
    view = assessment_view({
        "state": "fallback", "advice": None,
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2},
    })

    assert view.summary == "Agent assessment unavailable"
    assert "could not assess" in view.items[0].detail
    assert "wisdom_inspect" in view.notice
    assert "identity=skill-1" in view.notice and "version=2" in view.notice
    assert "/wisdom show skill-1" in view.notice
    assert "/wisdom show skill-1@v2" not in view.notice
    assert view.actions == [] and view.items[0].actions == []


def test_candidate_assessment_handoff_uses_exact_event_inspection():
    view = assessment_view({
        "state": "ready",
        "advice": {
            "relevance": "recommend", "title": "Local workflow",
            "explanation": "You refined this workflow repeatedly.",
            "provenance": {"kind": "local_qualification"},
        },
        "reference": {
            "kind": "candidate", "local_skill_id": "local-row-id",
            "event_id": "candidate-event-id",
        },
    })

    assert "wisdom_inspect" not in view.notice
    assert "candidate-event-id" not in view.notice
    assert "local-row-id" not in view.notice
    assert "/wisdom suggest" not in view.notice
    assert [(action.label, action.action) for action in view.actions] == [
        ("Share", "candidate_share"),
        ("Review in Portal", "candidate_portal"),
        ("Maybe Later", "candidate_defer")
    ]
    assert view.items[0].actions == []


def test_shared_skill_assessment_remains_without_not_now():
    view = assessment_view({
        "state": "ready",
        "advice": {
            "relevance": "recommend", "title": "Shared workflow",
            "explanation": "A teammate published this workflow.",
            "provenance": {"kind": "agent_assessed"},
        },
        "reference": {"kind": "skill", "skill_id": "skill-1", "version": 1},
    })

    assert view.actions == [] and view.items[0].actions == []


@pytest.mark.parametrize("action", ["share", "portal"])
def test_candidate_action_reuses_exact_owner_route_and_separates_private_draft(
    tmp_path: Path, action: str,
):
    store = WisdomStore(tmp_path / "state")
    service = SimpleNamespace(
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(return_value="local-draft"),
        publication_review=Mock(side_effect=[
            publication_review("local-draft"),
            {**publication_review("local-draft"),
             "operation_id": "publication-operation"},
        ]),
        publication_submit_private=Mock(return_value={
            "operation_id": "publication-operation", "state": "pending",
            "phase": "policy_review_required", "publication_state": "ready",
            "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft?org_id=org",
        }),
    )
    controller = NativeReviewController(store)
    route = invocation(SlackAdapter())

    record = controller.request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=action == "portal", may_apply=lambda: True,
    )
    replay = controller.request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=action == "portal", may_apply=lambda: True,
    )

    assert replay["id"] == record["id"]
    assert record["operation"] == "publish" and record["state"] == "pending"
    assert record["plan"]["candidate_assessment_id"] == "assessment-1"
    assert service.native_candidate_prepare.call_count == 1
    if action == "portal":
        assert service.publication_submit_private.call_count == 1
        receipt = service.publication_submit_private.call_args.args[0]
        guard = service.publication_submit_private.call_args.kwargs["may_apply"]
        assert receipt == "publication-receipt"
        from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel
        with pytest.raises(WisdomConflict, match="lease was lost"):
            guard()
        assert record["result"]["publication_state"] == "ready"
        assert record["result"]["portal_url"].startswith("https://portal.example/")
        assert service.publication_review.call_count == 2
    else:
        service.publication_submit_private.assert_not_called()
        assert record["result"] is None
        assert service.publication_review.call_count == 1


def test_candidate_portal_action_recovers_lost_ack_without_duplicate_draft(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    uncertain = {
        "operation_id": "publication-operation", "state": "uncertain",
        "phase": "draft_submission_unknown", "remote_draft_id": None,
    }
    recovered = {
        "operation_id": "publication-operation", "state": "pending",
        "phase": "policy_review_required", "publication_state": "ready",
        "remote_draft_id": "remote-draft",
        "portal_url": "https://portal.example/wisdom/review/remote-draft?org_id=org",
    }
    service = SimpleNamespace(
        store=store,
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(return_value="local-draft"),
        publication_review=Mock(side_effect=[
            publication_review("local-draft"),
            {**publication_review("local-draft"),
             "operation_id": "publication-operation"},
        ]),
        publication_submit_private=Mock(return_value=uncertain),
        publication_reopen_private=Mock(return_value=recovered),
    )
    controller = NativeReviewController(store)
    route = invocation(SlackAdapter())

    record = controller.request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=True, may_apply=lambda: True,
    )
    replay = NativeReviewController(WisdomStore(tmp_path / "state")).request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=True, may_apply=lambda: True,
    )

    assert replay["id"] == record["id"]
    assert record["result"]["remote_draft_id"] == "remote-draft"
    service.publication_submit_private.assert_called_once()
    assert service.publication_submit_private.call_args.kwargs[
        "candidate_assessment_id"
    ] == "assessment-1"
    service.publication_reopen_private.assert_called_once_with(
        "publication-operation", may_apply=service.publication_reopen_private.call_args.kwargs[
            "may_apply"
        ]
    )
    service.native_candidate_prepare.assert_called_once_with("candidate-event")


def test_candidate_portal_action_claims_before_concurrent_prepare_or_upload(  # pylint: disable=too-many-locals
    tmp_path: Path,
):
    root = tmp_path / "state"
    store = WisdomStore(root)
    prepare_started = Event()
    release_prepare = Event()

    def prepare(_event_id):
        prepare_started.set()
        assert release_prepare.wait(timeout=5)
        return "local-draft"

    service = SimpleNamespace(
        store=store,
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(side_effect=prepare),
        publication_review=Mock(side_effect=[
            publication_review("local-draft"),
            {**publication_review("local-draft"),
             "operation_id": "publication-operation"},
        ]),
        publication_submit_private=Mock(return_value={
            "operation_id": "publication-operation", "state": "pending",
            "phase": "policy_review_required", "publication_state": "ready",
            "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft",
        }),
    )
    route = invocation(SlackAdapter())
    first = NativeReviewController(WisdomStore(root))
    second = NativeReviewController(WisdomStore(root))

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            first.request_candidate_action, service, route, "candidate-event",
            assessment_id="assessment-1", open_in_portal=True,
            may_apply=lambda: True,
        )
        assert prepare_started.wait(timeout=5)
        duplicate = pool.submit(
            second.request_candidate_action, service, route, "candidate-event",
            assessment_id="assessment-1", open_in_portal=True,
            may_apply=lambda: True,
        ).result(timeout=2)
        release_prepare.set()
        completed = pending.result(timeout=5)

    assert duplicate["id"] == completed["id"]
    assert duplicate["plan"]["candidate_action_state"] == "preparing"
    assert service.native_candidate_prepare.call_count == 1
    assert service.publication_submit_private.call_count == 1
    with store.transaction() as db:
        rows = db.execute(
            "SELECT id FROM wisdom_consent WHERE "
            "json_extract(plan_json,'$.candidate_assessment_id')='assessment-1'"
        ).fetchall()
    assert [row["id"] for row in rows] == [completed["id"]]


def test_candidate_portal_active_upload_returns_progress_projection(tmp_path: Path):
    root = tmp_path / "state"
    upload_started = Event()
    release_upload = Event()

    def upload(_receipt, **_kwargs):
        upload_started.set()
        assert release_upload.wait(timeout=5)
        return {
            "operation_id": "publication-operation", "state": "pending",
            "phase": "policy_review_required", "publication_state": "ready",
            "remote_draft_id": "remote-draft",
            "portal_url": "https://portal.example/wisdom/review/remote-draft",
        }

    service = SimpleNamespace(
        store=WisdomStore(root),
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(return_value="local-draft"),
        publication_review=Mock(side_effect=[
            publication_review("local-draft"),
            {**publication_review("local-draft"), "operation_id": "publication-operation"},
        ]),
        publication_submit_private=Mock(side_effect=upload),
    )
    route = invocation(SlackAdapter())

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            NativeReviewController(WisdomStore(root)).request_candidate_action,
            service, route, "candidate-event", assessment_id="assessment-1",
            open_in_portal=True, may_apply=lambda: True,
        )
        assert upload_started.wait(timeout=5)
        duplicate = pool.submit(
            NativeReviewController(WisdomStore(root)).request_candidate_action,
            service, route, "candidate-event", assessment_id="assessment-1",
            open_in_portal=True, may_apply=lambda: True,
        ).result(timeout=2)
        release_upload.set()
        pending.result(timeout=5)

    assert duplicate["plan"]["candidate_action_state"] == "preparing"
    service.native_candidate_prepare.assert_called_once()
    service.publication_submit_private.assert_called_once()


def test_candidate_action_retries_failed_preparation_after_claim(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    service = SimpleNamespace(
        store=store,
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(side_effect=[RuntimeError("worker stopped"), "local-draft"]),
        publication_review=Mock(return_value=publication_review("local-draft")),
        publication_submit_private=Mock(),
    )
    route = invocation(SlackAdapter())

    with pytest.raises(RuntimeError, match="worker stopped"):
        NativeReviewController(WisdomStore(tmp_path / "state")).request_candidate_action(
            service, route, "candidate-event", assessment_id="assessment-1",
            open_in_portal=False, may_apply=lambda: True,
        )
    recovered = NativeReviewController(WisdomStore(tmp_path / "state")).request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=False, may_apply=lambda: True,
    )

    assert recovered["operation"] == "publish"
    assert recovered["plan"].get("candidate_action_state") is None
    assert service.native_candidate_prepare.call_count == 2
    service.publication_submit_private.assert_not_called()


def test_candidate_action_stops_when_preparation_outlives_its_lease(tmp_path: Path):
    from hermes_wisdom_plugin.errors import WisdomConflict  # pylint: disable=import-outside-toplevel

    store = WisdomStore(tmp_path / "state")
    lock_key = "candidate-action:org:default:assessment-1"

    def expired_preparation(_event_id):
        with store.transaction() as db:
            db.execute(
                "UPDATE operation_lock SET expires_at='1970-01-01T00:00:00+00:00' "
                "WHERE entity_id=?", (lock_key,),
            )
        assert store.acquire_operation_lock(lock_key) is not None
        return "local-draft"

    service = SimpleNamespace(
        store=store, native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(side_effect=expired_preparation),
        publication_review=Mock(return_value=publication_review("local-draft")),
        publication_submit_private=Mock(),
    )
    with pytest.raises(WisdomConflict, match="lease was lost"):
        NativeReviewController(store).request_candidate_action(
            service, invocation(SlackAdapter()), "candidate-event",
            assessment_id="assessment-1", open_in_portal=False, may_apply=lambda: True,
        )
    service.publication_review.assert_not_called()
    service.publication_submit_private.assert_not_called()


def test_candidate_action_recovers_orphaned_preparing_claim_after_lease_expiry(tmp_path: Path):
    root = tmp_path / "state"
    store = WisdomStore(root)
    service = SimpleNamespace(
        store=store,
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(return_value="local-draft"),
        publication_review=Mock(return_value=publication_review("local-draft")),
        publication_submit_private=Mock(),
    )
    route = invocation(SlackAdapter())
    abandoned, claimed = NativeReviewController(store)._claim_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1", action="share"
    )
    assert claimed and abandoned["plan"]["candidate_action_state"] == "preparing"
    lock_key = "candidate-action:org:default:assessment-1"
    stale_owner = store.acquire_operation_lock(lock_key)
    assert stale_owner is not None
    with store.transaction() as db:
        db.execute(
            "UPDATE operation_lock SET expires_at='1970-01-01T00:00:00+00:00' "
            "WHERE entity_id=?",
            (lock_key,),
        )

    recovered = NativeReviewController(WisdomStore(root)).request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=False, may_apply=lambda: True,
    )

    assert recovered["id"] == abandoned["id"]
    assert recovered["operation"] == "publish"
    service.native_candidate_prepare.assert_called_once_with("candidate-event")


def test_candidate_portal_retries_plan_when_no_publication_operation_exists(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    completed = {
        "operation_id": "publication-operation", "state": "pending",
        "phase": "policy_review_required", "publication_state": "ready",
        "remote_draft_id": "remote-draft",
        "portal_url": "https://portal.example/wisdom/review/remote-draft",
    }
    service = SimpleNamespace(
        store=store,
        native_owner=Mock(return_value=dict(OWNER)),
        native_candidate_prepare=Mock(return_value="local-draft"),
        publication_review=Mock(side_effect=[
            publication_review("local-draft"),
            {**publication_review("local-draft"), "operation_id": "publication-operation"},
        ]),
        publication_submit_private=Mock(side_effect=[
            RuntimeError("stopped before operation creation"), completed,
        ]),
    )
    route = invocation(SlackAdapter())

    with pytest.raises(RuntimeError, match="before operation creation"):
        NativeReviewController(WisdomStore(tmp_path / "state")).request_candidate_action(
            service, route, "candidate-event", assessment_id="assessment-1",
            open_in_portal=True, may_apply=lambda: True,
        )
    recovered = NativeReviewController(WisdomStore(tmp_path / "state")).request_candidate_action(
        service, route, "candidate-event", assessment_id="assessment-1",
        open_in_portal=True, may_apply=lambda: True,
    )

    assert recovered["result"]["portal_url"] == completed["portal_url"]
    service.native_candidate_prepare.assert_called_once_with("candidate-event")
    assert service.publication_submit_private.call_count == 2


def test_candidate_share_callback_updates_original_card_to_exact_final_review(tmp_path: Path):
    async def scenario():  # pylint: disable=too-many-locals
        adapter = SlackAdapter()
        route = replace(invocation(adapter), message_id="card-1")
        store, queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
        service = SimpleNamespace(
            store=store, close=Mock(), native_owner=Mock(return_value=dict(OWNER)),
            native_candidate_prepare=Mock(return_value="local-draft"),
            publication_review=Mock(return_value=publication_review("local-draft")),
            publication_submit_private=Mock(),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        runtime.wire_slack(app, adapter)
        row = queue.assessments("org")[0]
        original_blocks = slack_blocks(assessment_view(row), assessment_id)
        button = _slack_action(original_blocks, "Share")
        body: dict[str, Any] = callback_body()
        body["message"]["blocks"] = original_blocks

        foreign: dict[str, Any] = callback_body()
        foreign["user"]["id"] = "U9"
        foreign["message"]["blocks"] = original_blocks
        with pytest.raises(PermissionError):
            await runtime._slack_callback(
                app, adapter, runtime._leases[("slack", id(adapter))], foreign, button
            )
        service.native_candidate_prepare.assert_not_called()

        await runtime._slack_callback(
            app, adapter, runtime._leases[("slack", id(adapter))], body, button
        )

        assert len(app.client.edits) == 1
        assert app.client.edits[0]["ts"] == "card-1"
        assert _slack_action(app.client.edits[0]["blocks"], "Share My Skill")
        service.publication_submit_private.assert_not_called()
        pending = runtime.reviews.pending(OWNER, invocation=route)
        assert len(pending) == 1
        record = runtime.reviews.load(pending[0]["id"])
        assert record["message_id"] == "card-1"
        assert record["plan"]["candidate_assessment_id"] == assessment_id

    asyncio.run(scenario())


def test_telegram_candidate_share_callback_replaces_original_card(tmp_path: Path):
    async def scenario():
        bot = TelegramBot()
        adapter = TelegramAdapter(bot)
        route = native_invocation(
            adapter, platform="telegram", chat_type="private", chat_id="123",
            actor_id="7", actor_name="shannon", thread_id="", scope_id="",
            message_id="77", session_key="telegram:123:7",
        )
        store, _queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
        service = SimpleNamespace(
            store=store, close=Mock(), native_owner=Mock(return_value=dict(OWNER)),
            native_candidate_prepare=Mock(return_value="local-draft"),
            publication_review=Mock(return_value=publication_review("local-draft")),
            publication_submit_private=Mock(),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        token = runtime._lease("telegram", object(), adapter)
        query = TelegramQuery(bot, assessment_id=assessment_id, action="candidate_share")

        await runtime._telegram_callback(object(), adapter, token, query)

        assert len(bot.edits) == 1 and query.plain_edits == []
        endpoint, payload = bot.edits[0]
        assert endpoint == "editMessageText"
        assert payload["chat_id"] == 123 and payload["message_id"] == 77
        assert "Share My Skill" in payload["rich_message"]["html"]
        service.native_candidate_prepare.assert_called_once()
        service.publication_submit_private.assert_not_called()

    asyncio.run(scenario())


def test_candidate_portal_callback_replaces_original_card_with_private_url(tmp_path: Path):
    async def scenario():
        adapter = SlackAdapter()
        route = replace(invocation(adapter), message_id="card-1")
        store, queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
        portal_url = "https://portal.example/wisdom/review/remote-draft"
        service = SimpleNamespace(
            store=store, close=Mock(), native_owner=Mock(return_value=dict(OWNER)),
            native_candidate_prepare=Mock(return_value="local-draft"),
            publication_review=Mock(side_effect=[
                publication_review("local-draft"),
                {**publication_review("local-draft"),
                 "operation_id": "publication-operation"},
            ]),
            publication_submit_private=Mock(return_value={
                "operation_id": "publication-operation", "state": "pending",
                "phase": "policy_review_required", "publication_state": "ready",
                "remote_draft_id": "remote-draft", "portal_url": portal_url,
            }),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        runtime.wire_slack(app, adapter)
        row = queue.assessments("org")[0]
        original_blocks = slack_blocks(assessment_view(row), assessment_id)
        button = _slack_action(original_blocks, "Review in Portal")
        body = callback_body()
        body["message"]["blocks"] = original_blocks

        await runtime._slack_callback(
            app, adapter, runtime._leases[("slack", id(adapter))], body, button
        )

        assert len(app.client.edits) == 1
        assert app.client.edits[0]["ts"] == "card-1"
        assert "Private Draft Ready" in app.client.edits[0]["text"]
        assert "sha256:" not in app.client.edits[0]["text"]
        assert _slack_action(app.client.edits[0]["blocks"], "Show checks")
        assert _slack_action(app.client.edits[0]["blocks"], "View More Details")
        portal = _slack_action(app.client.edits[0]["blocks"], "Review in Portal")
        assert portal["url"] == portal_url
        service.publication_submit_private.assert_called_once()

    asyncio.run(scenario())


def test_telegram_private_draft_details_toggle_edits_original_card_read_only(tmp_path: Path):
    async def scenario():
        bot = TelegramBot()
        adapter = TelegramAdapter(bot)
        route = native_invocation(
            adapter, platform="telegram", chat_type="private", chat_id="123",
            actor_id="7", actor_name="shannon", thread_id="", scope_id="",
            message_id="77", session_key="telegram:123:7",
        )
        store, _queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
        plan = publication_review(
            "local-draft",
            editorial_name="Incident Review",
            editorial_description="Turns incident notes into a repeatable review.",
            description="Exact description with private evidence.",
            files=[{
                "path": "SKILL.md", "hash": "sha256:" + "d" * 64,
                "content_utf8": "# Incident Review\n",
            }],
        )
        service = SimpleNamespace(
            store=store, close=Mock(), native_owner=Mock(return_value=dict(OWNER)),
            native_candidate_prepare=Mock(return_value="local-draft"),
            publication_review=Mock(side_effect=[
                plan, {**plan, "operation_id": "publication-operation"},
            ]),
            publication_submit_private=Mock(return_value={
                "operation_id": "publication-operation", "state": "pending",
                "phase": "policy_review_required", "publication_state": "ready",
                "remote_draft_id": "remote-draft",
                "portal_url": "https://portal.example/wisdom/review/remote-draft",
            }),
            publication_submit=Mock(),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        token = runtime._lease("telegram", object(), adapter)

        await runtime._telegram_callback(
            object(), adapter, token,
            TelegramQuery(bot, assessment_id=assessment_id, action="candidate_portal"),
        )
        record = runtime.reviews._candidate_action_record("org", "default", assessment_id)
        assert record is not None
        record_id = record["id"]
        assert "Private Draft Ready" in bot.edits[-1][1]["rich_message"]["html"]
        assert "sha256:" not in bot.edits[-1][1]["rich_message"]["html"]

        await runtime._telegram_callback(
            object(), adapter, token,
            TelegramQuery(bot, assessment_id=record_id, action="details.0"),
        )
        assert "Publication details" in bot.edits[-1][1]["rich_message"]["html"]
        assert "Share My Skill" not in bot.edits[-1][1]["rich_message"]["html"]

        await runtime._telegram_callback(
            object(), adapter, token,
            TelegramQuery(bot, assessment_id=record_id, action="back"),
        )
        assert "Private Draft Ready" in bot.edits[-1][1]["rich_message"]["html"]
        assert "Share My Skill" in bot.edits[-1][1]["rich_message"]["html"]
        service.publication_submit.assert_not_called()
        service.publication_submit_private.assert_called_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_candidate_portal_edit_failure_is_uncertain_and_old_callback_cannot_upload_again(
    tmp_path: Path, platform: str,
):
    async def scenario():  # pylint: disable=too-many-locals
        if platform == "slack":
            adapter = SlackAdapter()
            route = replace(invocation(adapter), message_id="card-1")
        else:
            bot = TelegramBot()
            adapter = TelegramAdapter(bot)
            route = native_invocation(
                adapter, platform="telegram", chat_type="private", chat_id="123",
                actor_id="7", actor_name="shannon", thread_id="", scope_id="",
                message_id="77", session_key="telegram:123:7",
            )
        store, queue, assessment_id, _event_id = _delivered_candidate(tmp_path, route)
        service = SimpleNamespace(
            store=store, close=Mock(), native_owner=Mock(return_value=dict(OWNER)),
            native_candidate_prepare=Mock(return_value="local-draft"),
            publication_review=Mock(side_effect=[
                publication_review("local-draft"),
                {**publication_review("local-draft"),
                 "operation_id": "publication-operation"},
            ]),
            publication_submit_private=Mock(return_value={
                "operation_id": "publication-operation", "state": "pending",
                "phase": "policy_review_required", "publication_state": "ready",
                "remote_draft_id": "remote-draft",
                "portal_url": "https://portal.example/wisdom/review/remote-draft",
            }),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        if platform == "slack":
            native = SlackApp()
            runtime.wire_slack(native, adapter)
            native.client.fail_edit = True
            row = queue.assessments("org")[0]
            blocks = slack_blocks(assessment_view(row), assessment_id)
            callback = _slack_action(blocks, "Review in Portal")
            body = callback_body()
            body["message"]["blocks"] = blocks

            async def invoke_callback():
                await runtime._slack_callback(
                    native, adapter, runtime._leases[("slack", id(adapter))],
                    body, callback,
                )
        else:
            native = object()
            token = runtime._lease("telegram", native, adapter)
            bot.fail_edit = True
            query = TelegramQuery(bot, assessment_id=assessment_id, action="candidate_portal")

            async def invoke_callback():
                await runtime._telegram_callback(native, adapter, token, query)

        with pytest.raises((RuntimeError, TimeoutError)):
            await invoke_callback()
        record = runtime.reviews._candidate_action_record("org", "default", assessment_id)
        assert record is not None and record["delivery_state"] == "delivery_uncertain"
        assert service.publication_submit_private.call_count == 1

        with pytest.raises(PermissionError, match="delivery state changed"):
            await invoke_callback()
        assert service.publication_submit_private.call_count == 1
        service.native_candidate_prepare.assert_called_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("edit_fails", "source_changed", "late_close"),
    [(False, False, ""), (True, False, ""), (False, True, ""),
     (False, False, "snapshot"), (False, False, "after_commit")],
)
def test_native_candidate_not_now_uses_snapshot_once_and_edits_original(
    tmp_path: Path, edit_fails: bool, source_changed: bool, late_close: str, monkeypatch,
):
    async def scenario():  # pylint: disable=too-many-locals
        store = WisdomStore(tmp_path / "state")
        store.activate_installation_identity("install", "org")
        store.bind_qualification_owner(OWNER)
        local = tmp_path / "candidate"
        local.mkdir()
        (local / "SKILL.md").write_text("# Candidate\n", encoding="utf-8")
        content_hash, tree = snapshot_tree(local)
        skill_id = store.register_skill(
            local, content_hash=content_hash, source_kind="local", tree=tree
        )
        event_id = store.emit_local_event(
            kind="wisdom.candidate", skill_id=skill_id, content_hash=content_hash,
            session_id="origin", task_id="task", qualification="high_usage",
            payload={
                "local_reasons": {"consecutive_business_days": 3},
                "consent_required": True, "networked": False,
            },
        )
        queue = MediationStore(store, owner=OWNER, clock=lambda: 1_000.0)
        adapter = SlackAdapter()
        route = invocation(adapter)
        queue.register_session(
            "org", session_key=route.session_key, session_id="session-1",
            platform="slack", actor_id=route.actor_id, private=True,
            available=True, user_activity=True,
            address={"chat_id": "D1", "thread_id": "", "scope_id": "W1"},
        )
        assessment_id = queue.enqueue_owned(
            "org", f"candidate:{skill_id}:{content_hash}",
            {"kind": "candidate", "local_skill_id": skill_id,
             "content_hash": content_hash, "event_id": event_id},
        )
        job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", assessment_id, job["lease_token"], {
            "assessment_id": assessment_id, "relevance": "recommend",
            "title": "Candidate", "explanation": "You use this often.",
            "provenance": {"kind": "local_qualification"},
        })
        delivered = queue.claim("org", route.session_key, ready_only=True)[0]
        queue.bind_candidate_policy("org", assessment_id, delivered["lease_token"], 7)
        assert queue.begin_delivery("org", assessment_id, delivered["lease_token"])
        assert queue.complete_delivery(
            "org", assessment_id, delivered["lease_token"],
            receipt=DeliveryReceipt(
                platform="slack", destination="D1", message_id="card-1",
                scope_id="W1", acknowledgement="provider_accepted",
            ),
        )

        client = Mock(
            identity={"owner": "member"}, owner_user_id="member",
            display_org_id="org", expected_organization_id="org",
        )
        native_owner = Mock(return_value=dict(OWNER))
        service = SimpleNamespace(
            store=store, client=client, native_owner=native_owner, close=Mock(),
        )
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        runtime.wire_slack(app, adapter)
        row = queue.assessments("org")[0]
        original_blocks = slack_blocks(assessment_view(row), assessment_id)
        button = _slack_action(original_blocks, "Maybe Later")

        if late_close == "snapshot":
            from hermes_wisdom_plugin import qualification  # pylint: disable=import-outside-toplevel
            original_snapshot = qualification.snapshot_tree

            def snapshot_and_close(path):
                result = original_snapshot(path)
                runtime.close()
                return result

            monkeypatch.setattr(qualification, "snapshot_tree", snapshot_and_close)
        elif late_close == "after_commit":
            original_defer = runtime._defer_candidate

            async def defer_and_close(*args, guard, **kwargs):
                result = await original_defer(*args, guard=guard, **kwargs)
                runtime.close()
                return result

            monkeypatch.setattr(runtime, "_defer_candidate", defer_and_close)
        if source_changed:
            (local / "SKILL.md").write_text("# Changed candidate\n", encoding="utf-8")
        app.client.fail_edit = edit_fails
        body = callback_body()
        body["message"]["blocks"] = original_blocks
        action = runtime._slack_callback(
            app, adapter, runtime._leases[("slack", id(adapter))],
            body, button,
        )
        if source_changed or late_close:
            with pytest.raises(PermissionError):
                await action
            with store.transaction() as database:
                assert database.execute(
                    "SELECT COUNT(*) FROM wisdom_preference_outbox"
                ).fetchone()[0] == (1 if late_close == "after_commit" else 0)
                assert database.execute(
                    "SELECT state FROM wisdom_shared_assessment WHERE id=?",
                    (assessment_id,),
                ).fetchone()[0] == ("deferred" if late_close == "after_commit" else "delivered")
            assert app.client.edits == []
            return
        if edit_fails:
            with pytest.raises(RuntimeError, match="deleted"):
                await action
        else:
            await action
        with store.transaction() as database:
            suppression = database.execute(
                "SELECT suppress_until,created_at FROM wisdom_preference_outbox"
            ).fetchone()
            assessment = dict(database.execute(
                "SELECT state,last_error,event_key FROM wisdom_shared_assessment WHERE id=?",
                (assessment_id,),
            ).fetchone())
            consent_count = database.execute("SELECT COUNT(*) FROM wisdom_consent").fetchone()[0]
        assert suppression["suppress_until"] - suppression["created_at"] == 7 * 86400
        first_deadline = suppression["suppress_until"]
        assert assessment["state"] == "deferred"
        assert assessment["last_error"] == ("RuntimeError" if edit_fails else None)
        assert assessment["event_key"].endswith(f":deferred:{assessment_id}")
        assert consent_count == 0
        assert len(app.client.edits) == 1
        assert app.client.edits[0]["ts"] == "card-1"
        if not edit_fails:
            assert app.client.edits[0]["blocks"] == [
                block for block in original_blocks if block["type"] != "actions"
            ]

        app.client.fail_edit = False
        await runtime._slack_callback(
            app, adapter, runtime._leases[("slack", id(adapter))],
            callback_body(), button,
        )
        with store.transaction() as database:
            duplicate = database.execute(
                "SELECT suppress_until FROM wisdom_preference_outbox"
            ).fetchone()[0]
            receipt_count = database.execute(
                "SELECT COUNT(*) FROM wisdom_shared_delivery_receipt WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()[0]
        assert duplicate == first_deadline
        assert len(app.client.edits) == 1
        assert receipt_count == 1
        assert not client.method_calls
        assert native_owner.call_count >= 4

        local_presentations = []

        async def notify(**kwargs):
            local_presentations.append(kwargs)
            return SimpleNamespace(outcome="rendered")

        await runtime.present_local_assessments(
            SimpleNamespace(
                profile_name="default", session_id="other-session", turn_id="other-turn",
                surface_id="tui:other-session", surface_generation=1, reason="reply",
                notify=notify,
            ),
            get_delivery_mode=lambda: "agent", clock=lambda: 1_000.0,
        )
        assert not local_presentations

    asyncio.run(scenario())


@pytest.mark.parametrize("late_failure", ["admission", "lease"])
def test_candidate_policy_read_rechecks_admission_before_binding(
    tmp_path, monkeypatch, late_failure,
):
    from hermes_wisdom_plugin.preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

    async def scenario():
        adapter = SlackAdapter()
        route = invocation(adapter)
        runtime = NativeSurfaceRuntime(tmp_path, "default", Mock(), FakeLlm(), Mock())
        runtime.wire_slack(SlackApp(), adapter)
        checker = Mock()
        checker.delivery_current.return_value = True
        queue = Mock()
        queue.clock.return_value = 1_000.0
        outbox = Mock()

        def policy_read(_self, _org):
            if late_failure == "admission":
                checker.delivery_current.return_value = False
            else:
                runtime.close()
            return 7

        monkeypatch.setattr(WisdomPreferences, "review_suppression_days", policy_read)
        await runtime._deliver_assessment(  # pylint: disable=protected-access
            route, {"id": "candidate", "state": "ready", "owner_session": route.session_key,
                    "lease_token": "queue-lease", "reference": {"kind": "candidate"},
                    "advice": {"title": "candidate"}}, checker, queue, outbox, "org",
        )
        queue.bind_candidate_policy.assert_not_called()
        outbox.reserve.assert_not_called()
        runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("late_failure", ["admission", "lease"])
@pytest.mark.parametrize("relevance", ["recommend", "digest"])
def test_ready_assessment_uses_real_slack_sender_receipt_and_durable_outbox(
    tmp_path: Path, late_failure: str, relevance: str,
):
    async def scenario():  # pylint: disable=too-many-locals
        now = 1_000.0
        store = WisdomStore(tmp_path / "state")
        store.activate_installation_identity("install", "org")
        store.bind_qualification_owner(OWNER)
        client = Mock(
            identity={"owner": "member"}, owner_user_id="member",
            expected_organization_id="org", display_org_id="org",
        )
        client.agent_led_policy.return_value = AgentLedPolicyResponse(
            org_id="org", usage_evidence_window_days=7,
            min_aggregate_invocations=3, consecutive_day_usage_counts=True,
            repeated_edits_count=True, max_recommendations_per_user_per_week=3,
            publication_mode="moderated", install_popularity_threshold=2,
            notification_defaults=AgentLedNotificationDefaults(
                skill_ready_to_share=True, teammate_published=True, update_available=True,
            ),
            manager_review_email_cadence="immediate", not_now_suppression_days=7,
            version=1, updated_by_user_id="manager",
        )
        client.recommendation_mute.return_value = WisdomMuteResponse(
            org_id="org", muted=False, duration=None, muted_until=None,
            forever=False, revision=1,
        )
        client.recommendation_suppressions.return_value = []

        def delivery_response(request_id, reference, *, state="claimed"):
            return ClientDeliveryResponse(
                org_id="org", recipient_user_id="member", event_id="event-1",
                request_id=request_id, reference=reference, state=state, reason=None,
                lease_until=(datetime.fromtimestamp(now + 120, timezone.utc)
                             .isoformat().replace("+00:00", "Z")) if state == "claimed" else None,
            )

        client.claim_notification_delivery.side_effect = delivery_response
        client.settle_notification_delivery.side_effect = (
            lambda event_id, request_id, reference, *, outcome, receipt: delivery_response(
                request_id, reference, state=outcome
            )
        )
        service = SimpleNamespace(store=store, client=client, config={})
        service._installation_owner = Mock(return_value=dict(OWNER))
        service.native_owner = Mock(return_value=dict(OWNER))
        def inspect(skill_id, *, version):
            return {
            "kind": "skill", "skill_id": skill_id, "version": version,
            "facts": {
                "skill": {"id": skill_id, "slug": "managed", "state": "active"},
                "version": {"version": 1, "state": "published",
                            "content_hash": "sha256:" + "a" * 64,
                            "editorial_name": "Managed",
                            "published_by_user_id": "someone-else"},
            },
        }
        service.native_skill_inspect = Mock(side_effect=inspect)
        service.close = Mock()
        queue = MediationStore(store, owner=OWNER, clock=lambda: now)
        route = invocation(SlackAdapter())
        queue.register_session(
            "org", session_key=route.session_key, session_id="session-1",
            platform="slack", actor_id=route.actor_id, private=True,
            available=True, user_activity=True,
            address={"chat_id": route.chat_id, "thread_id": "", "scope_id": route.scope_id},
        )
        assessment_id = queue.enqueue_owned(
            "org", "feed:event-1",
            {"kind": "skill", "event_id": "event-1", "skill_id": "skill-1", "version": 1},
        )
        job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", assessment_id, job["lease_token"], {
            "assessment_id": assessment_id, "relevance": relevance,
            "title": "Managed workflow",
            "explanation": "Useful in recurring reviews." if relevance == "recommend" else
            "Redundant with the installed workflow; no supported practical benefit.",
            "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
        })

        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        app.client.chat_postMessage = async_receipted_slack_post(app.client)
        runtime.wire_slack(app, route.adapter)

        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True, clock=lambda: now,
        )

        row = queue.assessments("org")[0]
        assert row["state"] == "delivered"
        assert len(app.client.posts) == 1
        assert "Agent-assessed advice" in app.client.posts[0]["text"]
        if relevance == "digest":
            posted = app.client.posts[0]
            assert "no supported practical benefit" in posted["text"]
            assert "could be useful to you" not in posted["text"]
            assert "Would you like to install" not in posted["text"]
            assert not any(block["type"] == "actions" for block in posted["blocks"])
            with store.transaction() as database:
                assert database.execute("SELECT COUNT(*) FROM wisdom_consent").fetchone()[0] == 0
        else:
            assert "Would you like to install this skill?" in app.client.posts[0]["text"]
            details = _slack_action(app.client.posts[0]["blocks"], "View Details")
            body = callback_body()
            body["message"]["ts"] = body["container"]["message_ts"] = "assessment-1"
            async def ack():
                return None
            await app.handler_for(details["action_id"])(ack, body, details)
            assert not app.client.ephemeral
            assert "Exact version: v1" in app.client.edits[-1]["text"]
            assert app.client.edits[-1]["ts"] == "assessment-1"
        with store.transaction() as database:
            receipt = database.execute(
                "SELECT receipt_json FROM wisdom_shared_delivery_receipt WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()
            remote = database.execute(
                "SELECT state,outcome,receipt_json FROM wisdom_remote_delivery"
            ).fetchone()
        assert receipt is not None and '\"message_id\":\"assessment-1\"' in receipt[0]
        assert tuple(remote[:2]) == ("outcome", "acknowledged")
        assert remote[2] == receipt[0]

        uncertain_id = queue.enqueue_owned(
            "org", "feed:event-2",
            {"kind": "skill", "event_id": "event-2", "skill_id": "skill-2", "version": 1},
        )
        uncertain_job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", uncertain_id, uncertain_job["lease_token"], {
            "assessment_id": uncertain_id, "relevance": "digest",
            "title": "Another workflow", "explanation": "Review it manually.",
            "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
        })

        async def malformed(**payload):
            app.client.posts.append(payload)
            return {"ok": True, "channel": payload["channel"]}

        app.client.chat_postMessage = malformed
        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True, clock=lambda: now,
        )
        uncertain = next(
            item for item in queue.assessments("org") if item["id"] == uncertain_id
        )
        assert uncertain["state"] == "delivery_uncertain"
        sends = len(app.client.posts)
        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True, clock=lambda: now,
        )
        assert len(app.client.posts) == sends

        denied_id = queue.enqueue_owned(
            "org", "feed:event-3",
            {"kind": "skill", "event_id": "event-3", "skill_id": "skill-3", "version": 1},
        )
        denied_job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", denied_id, denied_job["lease_token"], {
            "assessment_id": denied_id, "relevance": "recommend",
            "title": "Denied workflow", "explanation": "Must not be sent.",
            "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
        })
        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "fixed",
            admission_current=lambda: False, clock=lambda: now,
        )
        denied = next(item for item in queue.assessments("org") if item["id"] == denied_id)
        assert denied["state"] == "ready" and denied["delivered_at"] is None
        assert len(app.client.posts) == sends
        with store.transaction() as database:
            assert database.execute(
                "SELECT COUNT(*) FROM wisdom_consent WHERE operation != 'recommendation'"
            ).fetchone()[0] == 0
        app.client.chat_postMessage = async_receipted_slack_post(app.client)
        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True, clock=lambda: now,
        )
        assert next(
            item for item in queue.assessments("org") if item["id"] == denied_id
        )["state"] == "delivered"
        assert len(app.client.posts) == sends + 1

        late_id = queue.enqueue_owned(
            "org", "feed:event-4",
            {"kind": "skill", "event_id": "event-4", "skill_id": "skill-4", "version": 1},
        )
        late_job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", late_id, late_job["lease_token"], {
            "assessment_id": late_id, "relevance": "recommend",
            "title": "Late-fenced workflow", "explanation": "Must not be sent.",
            "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
        })
        admitted = [True]
        original_delivery_ready = MediationStore.delivery_ready

        ready_read = [False]
        original_inspect = service.native_skill_inspect

        def inspect_after_ready(*args, **kwargs):
            facts = original_inspect(*args, **kwargs)
            if late_failure == "lease" and ready_read[0]:
                with store.transaction() as database:
                    database.execute(
                        "UPDATE wisdom_shared_assessment SET lease_until=0 WHERE id=?",
                        (late_id,),
                    )
            return facts

        def revoke_after_delivery_ready(self, *args, **kwargs):
            result = original_delivery_ready(self, *args, **kwargs)
            ready_read[0] = True
            admitted[0] = late_failure != "admission"
            return result

        service.native_skill_inspect = inspect_after_ready
        MediationStore.delivery_ready = revoke_after_delivery_ready
        posts_before_late_fence = len(app.client.posts)
        try:
            await runtime.deliver_assessments(
                route, OWNER, get_delivery_mode=lambda: "agent",
                admission_current=lambda: admitted[0], clock=lambda: now,
            )
        finally:
            MediationStore.delivery_ready = original_delivery_ready
            service.native_skill_inspect = original_inspect
        assert len(app.client.posts) == posts_before_late_fence
        assert next(
            item for item in queue.assessments("org") if item["id"] == late_id
        )["state"] == "ready"

        fallback_id = queue.enqueue_owned(
            "org", "feed:event-5",
            {"kind": "skill", "event_id": "event-5", "skill_id": "skill-5", "version": 1},
        )
        with store.transaction() as database:
            database.execute(
                """UPDATE wisdom_shared_assessment SET state='fallback',attempts=3,
                advice_json=NULL WHERE id=?""",
                (fallback_id,),
            )
        admitted[0] = True
        await runtime.deliver_assessments(
            route, OWNER, get_delivery_mode=lambda: "agent",
            admission_current=lambda: admitted[0], clock=lambda: now,
        )
        fallback = next(
            item for item in queue.assessments("org") if item["id"] == fallback_id
        )
        assert fallback["state"] == "delivered"
        assert "Agent assessment unavailable" in app.client.posts[-1]["text"]
        assert "could not assess" in app.client.posts[-1]["text"]

        revoked_id = queue.enqueue_owned(
            "org", "feed:event-6",
            {"kind": "skill", "event_id": "event-6", "skill_id": "skill-6", "version": 1},
        )
        revoked_job = queue.claim("org", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org", revoked_id, revoked_job["lease_token"], {
            "assessment_id": revoked_id, "relevance": "recommend",
            "title": "Revoked workflow", "explanation": "Must remain local.",
            "provenance": {"kind": "agent_assessed", "provider": "p", "model": "m"},
        })
        replacement_owner = {**OWNER, "account_id": "replacement"}
        original_reserve = DeliveryOutbox.reserve

        def revoke_after_reservation(self, *args, **kwargs):
            request_id = original_reserve(self, *args, **kwargs)
            store.bind_qualification_owner(replacement_owner)
            service._installation_owner.return_value = dict(replacement_owner)
            return request_id

        DeliveryOutbox.reserve = revoke_after_reservation
        posts_before_reservation_revocation = len(app.client.posts)
        try:
            await runtime.deliver_assessments(
                route, OWNER, get_delivery_mode=lambda: "agent",
                admission_current=lambda: True, clock=lambda: now,
            )
        finally:
            DeliveryOutbox.reserve = original_reserve
        assert len(app.client.posts) == posts_before_reservation_revocation
        with store.transaction() as database:
            revoked = dict(database.execute(
                """SELECT state,lease_token FROM wisdom_shared_assessment
                WHERE id=?""", (revoked_id,),
            ).fetchone())
            remote = database.execute(
                """SELECT state,outcome FROM wisdom_remote_delivery
                WHERE assessment_id=?""", (revoked_id,),
            ).fetchone()
        assert revoked["state"] == "ready" and revoked["lease_token"] is None
        assert tuple(remote) == ("outcome", "not_sent")

    asyncio.run(scenario())


@pytest.mark.parametrize("owner_profile, expected", [(None, "default"), ("named", "named")])
def test_callback_profile_comes_from_adapter_binding(owner_profile, expected):
    slack_adapter = SimpleNamespace(
        _owner_profile=owner_profile,
        _is_interactive_user_authorized=lambda *_args, **_kwargs: True,
    )
    slack = slack_invocation(slack_adapter, callback_body(), {"profile": "forged"})
    assert slack.profile == expected

    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"), chat_id=1001, message_thread_id=None,
        message_id=1, get_bot=lambda: None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="u1", first_name="U1"), message=message,
    )
    telegram_adapter = SimpleNamespace(
        _owner_profile=owner_profile, _bot=None,
        _is_callback_user_authorized=lambda *_args, **_kwargs: True,
    )
    telegram = telegram_invocation(telegram_adapter, query, {"profile": "forged"})
    assert telegram.profile == expected


@pytest.mark.parametrize("factory", ["slack", "telegram"])
def test_callback_without_adapter_profile_binding_is_rejected(factory):
    adapter = SimpleNamespace(
        _is_interactive_user_authorized=lambda *_args, **_kwargs: True,
        _is_callback_user_authorized=lambda *_args, **_kwargs: True,
        _bot=None,
    )
    if factory == "slack":
        with pytest.raises(PermissionError, match="profile binding"):
            slack_invocation(adapter, callback_body(), {"profile": "forged"})
        return
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"), chat_id=1001, message_thread_id=None,
        message_id=1, get_bot=lambda: None,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="u1", first_name="U1"), message=message,
    )
    with pytest.raises(PermissionError, match="profile binding"):
        telegram_invocation(adapter, query, {"profile": "forged"})


def test_slack_dispatch_ack_update_edit_recovery_and_lifecycle(tmp_path: Path):
    async def scenario():
        Service.applies = 0
        llm = FakeLlm()
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, llm,
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin(invocation(adapter), "skill-1") is None
        action = app.client.posts[0]["blocks"][-1]["elements"][-1]
        events = []

        async def ack():
            events.append("ack")

        app.client.fail_edit = True
        await app.handler_for(action["action_id"])(ack, callback_body(), action)
        assert events == ["ack"]
        assert Service.applies == 1
        record_id = action["value"].split(":")[-1]
        assert runtime.reviews.load(record_id)["delivery_state"] == "edit_failed"

        app.client.fail_edit = False
        await app.handler_for(action["action_id"])(ack, callback_body(), action)
        assert Service.applies == 1
        assert "\n\nSetup queued\n" in app.client.edits[-1]["text"]

        check = app.client.edits[-1]["blocks"][-1]["elements"][0]
        await app.handler_for(check["action_id"])(ack, callback_body(), check)
        assert not llm.calls  # callback only inspects/queues; it does not inject a turn
        await runtime.resume_setup(invocation(adapter), expected_model="active-model")
        assert len(llm.calls) == 1
        assert "provider" not in llm.calls[0] and "model" not in llm.calls[0]
        assert len(app.client.posts) == 2
        assert app.client.posts[-1]["text"].startswith(
            "Hermes Collective Wisdom\n\nReview setup step"
        )
        setup_button = app.client.posts[-1]["blocks"][-1]["elements"][-1]
        assert setup_button["text"]["text"] == "Run this step"

        old_handler = app.handler_for(action["action_id"])
        replacement = SlackApp()
        runtime.wire_slack(replacement, adapter)
        await old_handler(ack, callback_body(), action)
        assert app.client.ephemeral[-1]["text"] == (
            "Collective Wisdom could not complete that action. Reopen the current review."
        )
        runtime.close()
        await replacement.handler_for(action["action_id"])(ack, callback_body(), action)
        assert replacement.client.ephemeral[-1]["text"] == (
            "Collective Wisdom could not complete that action. Reopen the current review."
        )

    asyncio.run(scenario())


def test_native_install_review_is_hash_bound_and_setup_remains_separate(tmp_path: Path):
    async def scenario():
        Service.installs = 0
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin_install(
            invocation(adapter), "skill-1@v2", update_mode="MANUAL"
        ) is None
        review = _slack_action(app.client.posts[-1]["blocks"], "Review first")

        async def act(button):
            await runtime._slack_callback(  # pylint: disable=protected-access
                app, adapter, runtime._leases[("slack", id(adapter))],  # pylint: disable=protected-access
                callback_body(), button,
            )

        await act(review)
        back = _slack_action(app.client.edits[-1]["blocks"], "Looks good.")
        await act(back)
        assert Service.installs == 0
        approve = _slack_action(app.client.edits[-1]["blocks"], "Install")
        await act(approve)
        assert Service.installs == 1
        assert "Setup queued" in app.client.edits[-1]["text"]
        assert "Run this step" not in app.client.edits[-1]["text"]
        assert _slack_action(app.client.edits[-1]["blocks"], "Check setup")

    asyncio.run(scenario())


def test_native_publication_uses_one_confirmation_and_original_card(tmp_path: Path):
    class PublicationService(Service):
        submits = 0

        def publication_review(self, draft_id):
            return publication_review(
                draft_id,
                description="Share exact reviewed behavior.",
                files=[{"path": "SKILL.md", "hash": "a" * 64}],
                hashes={
                    "content": "sha256:" + "b" * 64,
                    "author_description": "sha256:" + "c" * 64,
                    "package_manifest": "sha256:" + "d" * 64,
                },
                security_check={"status": "pass", "summary": "No known matches detected."},
                notice="Nothing has been uploaded.",
            )

        def publication_submit(self, receipt, *, may_apply):
            assert may_apply() is True
            assert receipt == "publication-receipt"
            PublicationService.submits += 1
            return {
                "operation_id": "publication-operation", "state": "done",
                "publication_state": "pending_moderation", "remote_draft_id": "draft-remote",
            }

        def publication_reopen(self, operation_id, *, may_apply):
            assert may_apply() is True
            assert operation_id == "publication-operation"
            return {
                "operation_id": operation_id, "state": "done",
                "publication_state": "published", "remote_draft_id": "draft-remote",
            }

        def publication_decline(self, operation_id, *, may_apply):
            assert may_apply() is True
            assert operation_id == "publication-operation"
            return {
                "operation_id": operation_id, "state": "done",
                "publication_state": "declined", "remote_draft_id": "draft-remote",
            }

    async def scenario():
        PublicationService.submits = 0
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", PublicationService, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin_publication(invocation(adapter), "local-draft") is None
        confirm = _slack_action(app.client.posts[-1]["blocks"], "Share My Skill")

        async def act(button):
            await runtime._slack_callback(  # pylint: disable=protected-access
                app, adapter, runtime._leases[("slack", id(adapter))],  # pylint: disable=protected-access
                callback_body(), button,
            )

        await act(confirm)
        assert PublicationService.submits == 1
        assert app.client.edits[-1]["ts"] == "card-1"
        assert "Pending moderation" in app.client.edits[-1]["text"]
        refresh = _slack_action(app.client.edits[-1]["blocks"], "Check status")
        await act(refresh)
        assert PublicationService.submits == 1
        assert app.client.edits[-1]["ts"] == "card-1"
        assert "Shared!" in app.client.edits[-1]["text"]

    asyncio.run(scenario())


def test_concurrent_slack_callbacks_leave_newest_durable_view_on_card(tmp_path: Path):
    """A delayed older edit must not overwrite a newer deferred projection."""

    class ConcurrentService(Service):
        def prepare_review_deferral(self, plan):
            return {
                "owner": self.native_owner(),
                "reference": {
                    "kind": "skill", "skill_id": plan["skill_id"],
                    "version": plan["version"],
                },
                "not_now_suppression_days": 7,
            }

        def defer_review(self, deferral, *, may_apply):
            assert may_apply() is True
            return {
                "deferred": True,
                "skill_id": deferral["reference"]["skill_id"],
                "version": deferral["reference"]["version"],
                "suppression_days": deferral["not_now_suppression_days"],
            }

    async def scenario():
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", ConcurrentService, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin(invocation(adapter), "skill-1") is None
        review = _slack_action(app.client.posts[0]["blocks"], "Review first")
        defer = _slack_action(app.client.posts[0]["blocks"], "Not Now")
        first_edit_entered = asyncio.Event()
        release_first_edit = asyncio.Event()
        original_update = app.client.chat_update
        calls = 0

        async def delayed_update(**payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_edit_entered.set()
                await release_first_edit.wait()
            return await original_update(**payload)

        app.client.chat_update = delayed_update

        async def ack():
            return None

        body = callback_body()
        body["message"].update({
            "blocks": app.client.posts[0]["blocks"], "text": app.client.posts[0]["text"],
        })
        first = asyncio.create_task(
            app.handler_for(review["action_id"])(ack, body, review)
        )
        await asyncio.wait_for(first_edit_entered.wait(), timeout=1)
        second = asyncio.create_task(
            app.handler_for(defer["action_id"])(ack, body, defer)
        )
        await asyncio.sleep(0.05)
        assert not second.done(), "newer callback must wait for the active card edit"
        release_first_edit.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        record_id = review["value"].split(":")[-1]
        assert runtime.reviews.load(record_id)["state"] == "deferred"
        assert app.client.edits[-1]["text"] == app.client.posts[0]["text"]
        assert not any(block.get("type") == "actions" for block in app.client.edits[-1]["blocks"])

    asyncio.run(scenario())


def test_successor_record_callbacks_share_the_physical_message_delivery_lock(tmp_path: Path):
    """A successor review must not race its predecessor's edit of the same card."""

    class AcknowledgementService(Service):
        def update_plan(self, skill_id, **choices):
            plan = super().update_plan(skill_id, **choices)
            accepted = bool(choices.get("preserve_modified"))
            return {
                **plan,
                "allowed": accepted,
                "modified": True,
                "preservation": {
                    "action": "preserve_unmanaged_copy" if accepted else "block",
                    "destination": (
                        "/tmp/profile/skills/managed-local-reviewed" if accepted else None
                    ),
                },
                "policy": dict(choices),
            }

    async def scenario():  # pylint: disable=too-many-locals
        Service.applies = 0
        service = AcknowledgementService()
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", lambda: service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        assert await runtime.begin(invocation(adapter), "skill-1") is None
        acknowledge = _slack_action(
            app.client.posts[0]["blocks"],
            "Preserve my modified copy as an unmanaged fork first.",
        )
        first_edit_entered = asyncio.Event()
        release_first_edit = asyncio.Event()
        successor_payload = {}
        original_update = app.client.chat_update

        async def delayed_update(**payload):
            if not successor_payload:
                successor_payload.update(payload)
                first_edit_entered.set()
                await release_first_edit.wait()
            return await original_update(**payload)

        app.client.chat_update = delayed_update

        async def ack():
            return None

        first = asyncio.create_task(
            app.handler_for(acknowledge["action_id"])(ack, callback_body(), acknowledge)
        )
        await asyncio.wait_for(first_edit_entered.wait(), timeout=1)
        inspect = _slack_action(successor_payload["blocks"], "Review first")
        second = asyncio.create_task(
            app.handler_for(inspect["action_id"])(ack, callback_body(), inspect)
        )
        await asyncio.sleep(0.05)
        assert not second.done(), "successor callback must wait for the same physical card"
        release_first_edit.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        original_id = acknowledge["value"].split(":")[-1]
        successor_id = inspect["value"].split(":")[-1]
        assert original_id != successor_id
        assert runtime.reviews.load(original_id)["state"] == "stale"
        assert runtime.reviews.load(successor_id)["state"] == "pending"
        assert "Review proposed package" in app.client.edits[-1]["text"]
        assert Service.applies == 0

    asyncio.run(scenario())


def test_lifecycle_from_host_worker_schedules_on_native_loop(tmp_path: Path):
    async def scenario():
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        called = asyncio.Event()

        async def resume(captured, *, expected_model):
            assert captured.session_key == "slack:W1:D1:U1"
            assert expected_model == "active-model"
            called.set()

        runtime.resume_setup = resume
        source = SimpleNamespace(
            platform=SimpleNamespace(value="slack"), profile="default", chat_type="dm",
            chat_id="D1", user_id="U1", user_name="Shannon", thread_id="", scope_id="W1",
        )
        event = SimpleNamespace(
            source=source, message_id="request-2", internal=False,
            get_command=lambda: "", get_command_args=lambda: "",
        )
        gateway = SimpleNamespace(
            _session_key_for_source=lambda _source: "slack:W1:D1:U1",
            _adapter_for_source=lambda _source: adapter,
            _is_user_authorized_for_source=lambda _source: True,
        )
        runtime.hook(event=event, gateway=gateway)
        await asyncio.to_thread(
            runtime.lifecycle_hook,
            model="active-model",
            session_id="durable-session-1",
            task_id="host-task-1",
            turn_id="turn-1",
            platform="slack",
            sender_id="U1",
            parent_session_id="",
        )
        await asyncio.wait_for(called.wait(), timeout=1)

    asyncio.run(scenario())


def test_cached_slack_worker_recovers_route_when_capture_context_is_missing(tmp_path: Path):
    async def scenario():
        route = {
            "platform": "slack", "profile": "default", "chat_id": "D1",
            "user_id": "U1", "thread_id": "", "scope_id": "W1",
            "message_id": "request-2", "session_key": "slack:W1:D1:U1",
            "session_id": "durable-session-1",
        }
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
            session_context=lambda: dict(route),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        called = asyncio.Event()

        async def resume(invocation, *, expected_model):  # pylint: disable=redefined-outer-name
            assert invocation.message_id == "request-2"
            assert expected_model == "active-model"
            called.set()

        runtime.resume_setup = resume
        source = SimpleNamespace(
            platform=SimpleNamespace(value="slack"), profile="default", chat_type="dm",
            chat_id="D1", user_id="U1", user_name="Shannon", thread_id="", scope_id="W1",
        )
        event = SimpleNamespace(
            source=source, message_id="request-2", internal=False,
            get_command=lambda: "", get_command_args=lambda: "",
        )
        gateway = SimpleNamespace(
            _session_key_for_source=lambda _source: "slack:W1:D1:U1",
            _adapter_for_source=lambda _source: adapter,
            _is_user_authorized_for_source=lambda _source: True,
        )
        runtime.hook(event=event, gateway=gateway)
        runtime.capture._value.set(None)

        await asyncio.to_thread(
            runtime.lifecycle_hook, model="active-model",
            session_id="durable-session-1", task_id="host-task-1", turn_id="turn-1",
            platform="slack", sender_id="U1", parent_session_id="",
        )
        await asyncio.wait_for(called.wait(), timeout=1)

    asyncio.run(scenario())


def test_lifecycle_rejects_non_owning_or_subagent_payload(tmp_path: Path):
    async def scenario():
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        called = asyncio.Event()

        async def resume(invocation, *, expected_model):  # pylint: disable=redefined-outer-name
            del invocation, expected_model
            called.set()

        runtime.resume_setup = resume
        source = SimpleNamespace(
            platform=SimpleNamespace(value="slack"), profile="default", chat_type="dm",
            chat_id="D1", user_id="U1", user_name="Shannon", thread_id="", scope_id="W1",
        )
        gateway = SimpleNamespace(
            _session_key_for_source=lambda _source: "slack:W1:D1:U1",
            _adapter_for_source=lambda _source: adapter,
            _is_user_authorized_for_source=lambda _source: True,
        )
        base = {
            "model": "active-model", "session_id": "durable-session-1",
            "task_id": "host-task-1", "turn_id": "turn-1", "platform": "slack",
            "sender_id": "U1", "parent_session_id": "",
        }
        for changed in (
            {"sender_id": "U2"}, {"platform": "telegram"},
            {"parent_session_id": "parent-session"}, {"session_id": ""},
            {"turn_id": ""}, {"task_id": ""},
        ):
            event = SimpleNamespace(
                source=source, message_id="request-2", internal=False,
                get_command=lambda: "", get_command_args=lambda: "",
            )
            runtime.hook(event=event, gateway=gateway)
            await asyncio.to_thread(runtime.lifecycle_hook, **{**base, **changed})
        await asyncio.sleep(0)
        assert not called.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (PermissionError("account=private-user token=private-token"), "capture_rejected"),
        (RuntimeError("account=private-user token=private-token"), "context_unavailable"),
    ],
)
def test_lifecycle_logs_bounded_private_admission_failure(
    tmp_path: Path, caplog, failure, reason
):
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
        session_context=lambda: (_ for _ in ()).throw(failure),
    )
    if isinstance(failure, PermissionError):
        runtime.capture.consume_lifecycle = Mock(side_effect=failure)
    else:
        runtime.capture.consume_lifecycle = Mock(return_value=invocation(SlackAdapter()))

    with caplog.at_level(logging.WARNING, logger="hermes_wisdom_plugin.native_runtime"):
        runtime.lifecycle_hook(
            model="private-model", session_id="private-session", task_id="private-task",
            turn_id="private-turn", platform="slack", sender_id="private-user",
            parent_session_id="",
        )

    assert caplog.messages == [
        f"Collective Wisdom lifecycle admission rejected (reason={reason})"
    ]
    assert "private" not in caplog.text
    assert type(failure).__name__ not in caplog.text


def test_lifecycle_context_mismatch_logs_field_names_only(tmp_path: Path, caplog):
    captured = invocation(SlackAdapter())
    captured = replace(
        captured, message_id="expected-message", host_session_id="expected-session",
        task_id="expected-task", turn_id="expected-turn",
    )
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
        session_context=lambda: {
            "platform": captured.platform, "profile": captured.profile,
            "chat_id": captured.chat_id, "user_id": "private-user",
            "thread_id": captured.thread_id, "scope_id": captured.scope_id,
            "message_id": captured.message_id, "session_key": captured.session_key,
            "session_id": "private-session",
            "credential": "private-token",
        },
    )
    runtime.capture.consume_lifecycle = Mock(return_value=captured)

    with caplog.at_level(logging.WARNING, logger="hermes_wisdom_plugin.native_runtime"):
        runtime.lifecycle_hook(model="private-model")

    assert caplog.messages == [
        "Collective Wisdom lifecycle admission rejected "
        "(reason=context_mismatch; fields=user_id,session_id)"
    ]
    assert "private" not in caplog.text
    assert "credential" not in caplog.text


def test_lifecycle_expected_no_capture_is_silent(tmp_path: Path, caplog):
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
    )
    runtime.capture.consume_lifecycle = Mock(return_value=None)

    with caplog.at_level(logging.DEBUG, logger="hermes_wisdom_plugin.native_runtime"):
        runtime.lifecycle_hook(model="private-model", parent_session_id="parent-session")

    assert caplog.messages == []


def test_lifecycle_rejects_host_session_context_route_mismatch(tmp_path: Path):
    async def scenario():
        route = {
            "platform": "slack", "profile": "default", "chat_id": "D1",
            "user_id": "U2", "thread_id": "", "scope_id": "W1",
            "message_id": "request-2", "session_key": "slack:W1:D1:U1",
            "session_id": "durable-session-1",
        }
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(),
            lambda coro, **_kwargs: asyncio.create_task(coro),
            session_context=lambda: dict(route),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        called = asyncio.Event()

        async def resume(invocation, *, expected_model):  # pylint: disable=redefined-outer-name
            del invocation, expected_model
            called.set()

        runtime.resume_setup = resume
        source = SimpleNamespace(
            platform=SimpleNamespace(value="slack"), profile="default", chat_type="dm",
            chat_id="D1", user_id="U1", user_name="Shannon", thread_id="", scope_id="W1",
        )
        event = SimpleNamespace(
            source=source, message_id="request-2", internal=False,
            get_command=lambda: "", get_command_args=lambda: "",
        )
        gateway = SimpleNamespace(
            _session_key_for_source=lambda _source: "slack:W1:D1:U1",
            _adapter_for_source=lambda _source: adapter,
            _is_user_authorized_for_source=lambda _source: True,
        )
        runtime.hook(event=event, gateway=gateway)
        await asyncio.to_thread(
            runtime.lifecycle_hook, model="active-model", session_id="durable-session-1",
            task_id="host-task-1", turn_id="turn-1", platform="slack",
            sender_id="U1", parent_session_id="",
        )
        await asyncio.sleep(0)
        assert not called.is_set()

    asyncio.run(scenario())


def test_cached_gateway_lifecycle_uses_matching_active_runtime_session(tmp_path: Path, monkeypatch):
    async def scenario():
        route = {
            "platform": "slack", "profile": "default", "chat_id": "D1",
            "user_id": "U1", "thread_id": "", "scope_id": "W1",
            "message_id": "request-2", "session_key": "slack:W1:D1:U1",
            "session_id": "",
        }
        active = {
            "session_id": "durable-session-1", "provider": "fixture-provider",
            "model": "active-model",
        }
        monkeypatch.setattr(mediation, "_active_main_runtime", lambda: dict(active))
        runtime = NativeSurfaceRuntime(
            tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
            session_context=lambda: dict(route),
        )
        adapter = SlackAdapter()
        source = SimpleNamespace(
            platform=SimpleNamespace(value="slack"), profile="default", chat_type="dm",
            chat_id="D1", user_id="U1", user_name="Shannon", thread_id="", scope_id="W1",
        )
        event = SimpleNamespace(
            source=source, message_id="request-2", internal=False,
            get_command=lambda: "", get_command_args=lambda: "",
        )
        gateway = SimpleNamespace(
            _session_key_for_source=lambda _source: "slack:W1:D1:U1",
            _adapter_for_source=lambda _source: adapter,
            _is_user_authorized_for_source=lambda _source: True,
        )
        runtime.hook(event=event, gateway=gateway)
        await asyncio.to_thread(
            runtime.lifecycle_hook, model="active-model", session_id="durable-session-1",
            task_id="host-task-1", turn_id="turn-1", platform="slack",
            sender_id="U1", parent_session_id="",
        )

        admitted = await asyncio.to_thread(
            runtime.tool_invocation,
            task_id="host-task-1", session_id="durable-session-1",
        )
        assert admitted.host_session_id == "durable-session-1"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        ("conflicting-session", "durable-session-1", "host-task-1", "durable-session-1"),
        ("", "", "host-task-1", "durable-session-1"),
        ("", "other-session", "host-task-1", "durable-session-1"),
        ("", "durable-session-1", "other-task", "durable-session-1"),
        ("", "durable-session-1", "host-task-1", "other-session"),
    ],
)
def test_cached_gateway_binding_rejects_conflicts_and_missing_proof(
    tmp_path: Path, monkeypatch, case
):
    context_session, runtime_session, tool_task, tool_session = case
    route = {
        "platform": "slack", "profile": "default", "chat_id": "D1",
        "user_id": "U1", "thread_id": "", "scope_id": "W1",
        "message_id": "request-2", "session_key": "slack:W1:D1:U1",
        "session_id": context_session,
    }
    active = {
        "session_id": runtime_session, "provider": "fixture-provider",
        "model": "active-model",
    } if runtime_session else None
    monkeypatch.setattr(
        mediation, "_active_main_runtime", lambda: dict(active) if active else None,
    )
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
        session_context=lambda: dict(route),
    )
    captured = replace(
        invocation(SlackAdapter()), message_id="request-2",
        host_session_id="durable-session-1", task_id="host-task-1", turn_id="turn-1",
    )
    runtime._tool_invocations[("host-task-1", "durable-session-1")] = (
        float("inf"), captured,
    )

    with pytest.raises(PermissionError, match="active admitted private human session"):
        runtime.tool_invocation(task_id=tool_task, session_id=tool_session)


def test_cached_gateway_tool_rejects_owner_change_after_admission(tmp_path: Path, monkeypatch):
    route = {
        "platform": "slack", "profile": "default", "chat_id": "D1",
        "user_id": "U1", "thread_id": "", "scope_id": "W1",
        "message_id": "request-2", "session_key": "slack:W1:D1:U1", "session_id": "",
    }
    monkeypatch.setattr(mediation, "_active_main_runtime", lambda: {
        "session_id": "durable-session-1", "provider": "fixture-provider",
        "model": "active-model",
    })
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
        session_context=lambda: dict(route),
    )
    captured = replace(
        invocation(SlackAdapter()), message_id="request-2",
        host_session_id="durable-session-1", task_id="host-task-1", turn_id="turn-1",
    )
    runtime._tool_invocations[("host-task-1", "durable-session-1")] = (
        float("inf"), captured,
    )
    route["user_id"] = "U9"

    with pytest.raises(PermissionError, match="active admitted private human session"):
        runtime.tool_invocation(task_id="host-task-1", session_id="durable-session-1")


def test_model_tool_revalidates_live_host_context_and_retires_stale_authority(tmp_path: Path):
    route = {
        "platform": "slack", "profile": "default", "chat_id": "D1",
        "user_id": "U1", "thread_id": "", "scope_id": "W1",
        "message_id": "request-2", "session_key": "slack:W1:D1:U1",
        "session_id": "durable-session-1",
    }
    runtime = NativeSurfaceRuntime(
        tmp_path, "default", Service, FakeLlm(), lambda *_args, **_kwargs: None,
        session_context=lambda: dict(route),
    )
    original = invocation(SlackAdapter())
    captured = replace(
        original, message_id="request-2", host_session_id="durable-session-1",
        task_id="host-task-1", turn_id="turn-1",
    )
    runtime._tool_invocations[("host-task-1", "durable-session-1")] = (  # pylint: disable=protected-access
        float("inf"), captured,
    )
    assert runtime.tool_invocation(
        task_id="host-task-1", session_id="durable-session-1"
    ) == captured

    route["user_id"] = "U9"
    with pytest.raises(PermissionError, match="active admitted private human session"):
        runtime.tool_invocation(task_id="host-task-1", session_id="durable-session-1")
    assert not runtime._tool_invocations  # pylint: disable=protected-access


def test_model_consent_routes_private_candidate_and_exact_skill_version(
    tmp_path: Path, monkeypatch
):
    async def scenario():
        tasks = []
        route = invocation(SlackAdapter())

        def spawn(coroutine, **_kwargs):
            tasks.append(asyncio.create_task(coroutine))

        runtime = NativeSurfaceRuntime(tmp_path, "default", Service, FakeLlm(), spawn)
        runtime._loops[("slack", id(route.adapter))] = asyncio.get_running_loop()  # pylint: disable=protected-access
        calls = []

        async def candidate(active_invocation, event_id):
            del active_invocation
            calls.append(("candidate", event_id))

        async def install_modes(active_invocation, reference):
            del active_invocation
            calls.append(("skill", reference))

        monkeypatch.setattr(runtime, "begin_candidate", candidate)
        monkeypatch.setattr(runtime, "begin_install_modes", install_modes)
        assert runtime.queue_model_consent(
            route, {"kind": "candidate", "identity": "event-1"}
        ) == {"state": "queued"}
        assert runtime.queue_model_consent(
            route, {"kind": "skill", "identity": "skill-1", "version": 7}
        ) == {"state": "queued"}
        await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        assert calls == [("candidate", "event-1"), ("skill", "skill-1@v7")]

    asyncio.run(scenario())
