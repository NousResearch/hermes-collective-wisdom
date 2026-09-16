"""Run disposable native journeys through real Slack Bolt and PTB dispatch.

Invoked by ``test_host_native_update_integration.py`` with the pinned host Python.
Network APIs are replaced at their request transports; workflow controllers,
receipts, SQLite, package mutation, setup execution, and SDK dispatch stay real.
"""

# This subprocess runs only inside the pinned host interpreter. Optional SDK/host
# imports and private host admission hooks are the protocol under test, not package APIs.
# pylint: disable=import-error,wrong-import-order,protected-access
# Behavior-named probe helpers and SDK-minimal doubles do not need API docstrings.
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# One executable journey intentionally keeps both SDKs on the same assertion path.
# pylint: disable=too-many-lines
# SDK and pinned-host objects are deliberately assembled without their live connectors.
# pylint: disable=attribute-defined-outside-init

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

from pathlib import Path
from types import SimpleNamespace

import httpx
from slack_bolt.async_app import AsyncApp
from slack_bolt.authorization import AuthorizeResult
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.web.async_client import AsyncWebClient
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from telegram.request import BaseRequest

from agent import process_bootstrap
from agent.auxiliary_client import reset_runtime_main, set_runtime_main
from agent.turn_context import _collect_pre_llm_call_context
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import set_current_session_id
from hermes_cli.plugins import get_plugin_manager
from hermes_wisdom_plugin.contract import canonical_json_bytes
from hermes_wisdom_plugin.client import WisdomMuteResponse
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.install_review import InstallReviewer
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.mediation_producer import MaintenanceMediationProducer
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.package import verify_content_files
from hermes_wisdom_plugin.qualification import snapshot_tree
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.setup_document import PREFIX, SETUP_PATH, SUFFIX
from hermes_wisdom_plugin.setup_execution import SetupController
from hermes_wisdom_plugin.setup_host import SetupHost
from hermes_wisdom_plugin.setup_receipts import SetupReceipts
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import UpdateReviewer
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools.terminal_tool import set_approval_callback

from native_sdk_fixtures import CatalogClient, Service






class RecordingSlackClient(AsyncWebClient):
    def __init__(self):
        super().__init__(token="xoxb-recording")
        self.calls = []
        self.next_message = 100

    async def _request(self, *, http_verb: str, api_url, req_args):
        del http_verb
        method = api_url.rsplit("/", 1)[-1]
        payload = dict(req_args.get("json") or req_args.get("params") or req_args.get("data") or {})
        self.calls.append((method, payload))
        result = {"ok": True}
        if method == "chat.postMessage":
            self.next_message += 1
            result["ts"] = str(self.next_message)
            result["channel"] = payload["channel"]
        return {"data": result, "headers": {}, "status_code": 200}


class RecordingTelegramRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self.next_message = 200

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **_kwargs):
        del method
        endpoint = url.rsplit("/", 1)[-1]
        payload = dict(request_data.parameters if request_data else {})
        self.calls.append((endpoint, payload))
        if endpoint == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Hermes", "username": "hermes_test"}
        elif endpoint in {"sendMessage", "sendRichMessage", "editMessageText"}:
            if endpoint in {"sendMessage", "sendRichMessage"}:
                self.next_message += 1
                message_id = self.next_message
            else:
                message_id = int(payload["message_id"])
            result = {
                "message_id": message_id, "date": 1,
                "chat": {"id": int(payload["chat_id"]), "type": "private"},
                "text": payload.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


def telegram_rich_card(payload):
    """Project recorded InputRichMessage HTML into callback-test card fields."""

    rich = payload.get("rich_message") or {}
    if isinstance(rich, str):
        rich = json.loads(rich)
    source = str(rich.get("html") or "")
    rows = []
    for row_source in re.findall(
        r"<tg-button-row\b[^>]*>(.*?)</tg-button-row>", source, re.DOTALL,
    ):
        row = []
        for attributes, label in re.findall(
            r"<tg-button\b([^>]*)>(.*?)</tg-button>", row_source, re.DOTALL,
        ):
            values = {
                key: html.unescape(value)
                for key, value in re.findall(r'(\w+)="([^"]*)"', attributes)
            }
            button = {"text": html.unescape(re.sub(r"<[^>]+>", "", label))}
            if values.get("type") == "url":
                button["url"] = values.get("url", "")
            else:
                button["callback_data"] = values.get("data", "")
            row.append(button)
        if row:
            rows.append(row)
    text_source = re.sub(
        r"<tg-button-row\b[^>]*>.*?</tg-button-row>", "", source,
        flags=re.DOTALL,
    )
    plain = re.sub(r"<br\s*/?>", "\n", text_source)
    plain = re.sub(r"</(?:h3|p)>", "\n\n", plain)
    plain = html.unescape(re.sub(r"<[^>]+>", "", plain)).strip()
    return {"text": plain, "reply_markup": {"inline_keyboard": rows} if rows else None}


class RecordingSettingsService(InvocationService):
    """Use production config methods with disposable owner/Gateway fixtures."""

    def __init__(self, journey):
        super().__init__(
            journey.state_root, ("default", journey.profile),
            config=journey.plugin_runtime.config,
            get_config=journey.plugin_runtime.get_config,
            set_config=journey.plugin_runtime.set_config,
        )
        self._recording_owner = dict(journey.owner)
        self._onboarding = SimpleNamespace(store=journey.store)
        self.client = journey.client

    def native_owner(self):
        return dict(self._recording_owner)

    def require_setup(self):
        return None

    def close(self):
        self._closed = True


def package_fixture(version):
    spec = {
        "hermes": {"minimum_version": "0.0.0"}, "platforms": [], "architectures": [],
        "model": {}, "tools": [], "plugins": [], "credentials": [], "connections": [],
        "filesystem": {}, "network": {}, "runtime": {}, "hardware": [], "known_limitations": [],
    }
    manifest = canonical_json_bytes({"schema_version": 1, "name": "managed", "requirements": spec})
    guidance = canonical_json_bytes({
        "schema_version": 1, "execution_requires_user_approval": True, "requirements": [],
        "setup_instructions": (
            ["Run the harmless setup check."] if version == 2 else []
        ), "credential_handoff": [],
        "compatibility_limits": [], "verification_step": (
            "Verify with the harmless true command." if version == 2 else ""
        ),
        "removed_or_generalized": [], "related_skills": [],
    })
    setup = PREFIX.encode() + guidance + SUFFIX.encode()
    body = f"# Managed v{version}\n"
    if version == 2:
        body += (
            "  leading whitespace <&> — λ\n"
            + ("hostile <&> exact content\n" * 90)
            + "\tfinal exact line with trailing spaces  \n\n"
        )
    files = [
        ("SKILL.md", "file", body.encode()),
        ("skill.manifest.json", "file", manifest),
        (SETUP_PATH, "file", setup),
    ]
    records, content_hash = verify_content_files(files)
    return files, content_hash, {row.path: row.hash for row in records}, spec


def button_value(markup, label):
    rows = (
        markup.inline_keyboard
        if hasattr(markup, "inline_keyboard") else markup["inline_keyboard"]
    )
    for row in rows:
        for button in row:
            text = button.text if hasattr(button, "text") else button["text"]
            if text == label:
                return (
                    button.callback_data
                    if hasattr(button, "callback_data") else button["callback_data"]
                )
    raise AssertionError(f"button not found: {label}")


def slack_button(blocks, label):
    labels = []
    for block in blocks:
        for element in block.get("elements", []):
            text = element.get("text")
            if element.get("type") == "button" and isinstance(text, dict):
                labels.append(text.get("text"))
                if text.get("text") == label:
                    return element
    raise AssertionError(f"button not found: {label}; available={labels}")


def action_url(payload, platform, label):
    if platform == "slack":
        return slack_button(payload["blocks"], label).get("url")
    markup = payload["reply_markup"]
    rows = (
        markup.inline_keyboard
        if hasattr(markup, "inline_keyboard") else markup["inline_keyboard"]
    )
    for row in rows:
        for button in row:
            text = button.text if hasattr(button, "text") else button["text"]
            if text == label:
                return button.url if hasattr(button, "url") else button.get("url")
    raise AssertionError(f"button not found: {label}")


class Journey:  # pylint: disable=too-many-instance-attributes
    def __init__(self, platform, root, manager, plugin_runtime):
        self.platform = platform
        self.manager = manager
        self.plugin_runtime = plugin_runtime
        self.root = root
        self.profile = root / "profile"
        self.profile.mkdir(parents=True, exist_ok=True)
        (self.profile / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - hermes-collective-wisdom\n"
            "terminal:\n  backend: local\napprovals:\n  mode: manual\n",
            encoding="utf-8",
        )
        self.state_root = root / "state"
        self.store = WisdomStore(self.state_root)
        self.store.activate_installation_identity("native-sdk", "org-1")
        self.owner = {
            "profile_name": "default", "profile_home": str(self.profile),
            "account_id": "member-1", "organization_id": "org-1",
            "base_url": "https://gateway.invalid", "installation_id": "native-sdk",
        }
        self.packages = {version: package_fixture(version) for version in (1, 2)}
        self.client = CatalogClient(self.packages)
        self.mute_sets = []

        def set_recommendation_mute(
            duration, *, expected_revision, mutation_id, requested_at,
        ):
            self.mute_sets.append((duration, expected_revision, mutation_id, requested_at))
            requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            days = {"1_day": 1, "1_week": 7, "30_days": 30}.get(duration)
            muted_until = (
                (requested + timedelta(days=days)).isoformat(timespec="milliseconds")
                .replace("+00:00", "Z") if days else None
            )
            return WisdomMuteResponse(
                org_id="org-1", muted=duration is not None,
                duration=duration, muted_until=muted_until,
                forever=duration == "forever", revision=expected_revision + 1,
                mutation_id=mutation_id,
            )

        self.client.set_recommendation_mute = set_recommendation_mute
        package = plugin_runtime.native.__class__.__module__.rsplit(".", 1)[0]
        self.client.response_type = __import__(
            package + ".client_delivery", fromlist=["ClientDeliveryResponse"]
        ).ClientDeliveryResponse
        self.host = InstallationHost(self.profile)
        def owner():
            return dict(self.owner)
        install_receipts = InstallReceipts(self.store, owner)
        self.install_reviewer = InstallReviewer(
            self.client, self.store, install_receipts, self.host, owner
        )
        self.installer = InstallExecutor(
            self.client, self.store, install_receipts, self.install_reviewer, self.host, owner
        )
        update_receipts = UpdateReceipts(self.store, owner)
        self.update_reviewer = UpdateReviewer(
            self.client, self.store, update_receipts, self.host, owner
        )
        self.updater = UpdateExecutor(
            self.client, self.store, update_receipts, self.update_reviewer, self.host, owner
        )
        self.setup = SetupController(
            self.store, SetupReceipts(self.store, owner), self.host, SetupHost(), owner
        )
        self.runtime = plugin_runtime.native
        self.runtime.root = self.state_root
        self.runtime.profile_name = "default"
        self.runtime.service_factory = self.service
        self.runtime._reviews = None
        self.model_calls = []

        def response_for(request):
            payload = json.loads(request.content)
            self.model_calls.append({"url": str(request.url), "model": payload["model"]})
            command = "false" if len(self.model_calls) == 1 else "true"
            return httpx.Response(200, json={
                "id": "recorded", "object": "chat.completion", "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "command": command, "explanation": "Run the declared step."
                    }),
                }}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        def recording_http_client(_base_url, *, async_mode=False, verify=True):
            del verify
            transport = httpx.MockTransport(response_for)
            return httpx.AsyncClient(transport=transport) if async_mode else httpx.Client(
                transport=transport
            )

        process_bootstrap.build_keepalive_http_client = recording_http_client
        self.card_id = None
        self.callback_counter = 0
        self.catch_all = 0

    def service(self):
        service = Service(
            self.state_root, self.profile, self.owner,
            self.installer, self.install_reviewer,
            self.updater, self.update_reviewer, self.setup, self.store,
        )
        service.require_setup = lambda: None
        return service

    def settings_service(self):
        return RecordingSettingsService(self)

    def invocation(self, adapter, *, actor="U1", message_id="request-1"):
        if self.platform == "slack":
            return NativeInvocation(
                platform="slack", profile="default", chat_type="dm", chat_id="D1",
                actor_id=actor, actor_name=actor.lower(), thread_id="", scope_id="W1",
                message_id=message_id, session_key="slack:W1:D1:U1", adapter=adapter,
            )
        return NativeInvocation(
            platform="telegram", profile="default", chat_type="private", chat_id="1001",
            actor_id="1" if actor == "U1" else "9", actor_name=actor.lower(),
            thread_id="", scope_id="", message_id=message_id,
            session_key="telegram:1001:1", adapter=adapter,
        )

    async def setup_platform(self):
        if self.platform == "slack":
            self.transport = RecordingSlackClient()

            async def authorize(**_kwargs):
                return AuthorizeResult(
                    enterprise_id=None, team_id="W1", bot_user_id="UBOT", bot_id="B1",
                    bot_token="xoxb-recording",
                )

            self.native = AsyncApp(
                client=self.transport, authorize=authorize, signing_secret="recording",
                request_verification_enabled=False, process_before_response=True,
            )
            self.adapter = object.__new__(SlackAdapter)
            self.adapter.config = PlatformConfig(enabled=True, extra={})
            self.adapter._owner_profile = "default"
            self.adapter._message_handler = None
            self.adapter._authorization_check = (
                lambda user_id, _chat_type, _chat_id: user_id == "U1"
            )
            factories = self.manager._platform_handler_factories["slack"]
            assert len(factories) == 1
            factories[0][0](self.native, self.adapter)
        else:
            self.transport = RecordingTelegramRequest()
            self.native = (
                Application.builder().token("123456:recording")
                .request(self.transport).build()
            )
            self.adapter = object.__new__(TelegramAdapter)
            self.adapter.config = PlatformConfig(enabled=True, extra={})
            self.adapter._owner_profile = "default"
            self.adapter._message_handler = None
            self.adapter._authorization_check = (
                lambda user_id, _chat_type, _chat_id, **_kwargs: user_id == "1"
            )
            self.adapter._bot = self.native.bot
            factories = self.manager._platform_handler_factories["telegram"]
            assert len(factories) == 1
            factories[0][0](self.native, self.adapter)

            async def catch_all(_update, _context):
                self.catch_all += 1

            self.native.add_handler(CallbackQueryHandler(catch_all), group=0)
            await self.native.initialize()

        platform = Platform.SLACK if self.platform == "slack" else Platform.TELEGRAM
        self.runner = object.__new__(GatewayRunner)
        self.runner.config = GatewayConfig()
        self.runner.adapters = {platform: self.adapter}
        self.runner._profile_adapters = {}
        self.runner._primary_profile_name = "default"
        self.runner._startup_restore_in_progress = False
        self.runner._draining = False
        self.runner._scale_to_zero_note_real_inbound = lambda: None

    def source(self, *, message_id):
        return SessionSource(
            platform=Platform.SLACK if self.platform == "slack" else Platform.TELEGRAM,
            chat_id="D1" if self.platform == "slack" else "1001",
            chat_type="dm" if self.platform == "slack" else "private",
            user_id="U1" if self.platform == "slack" else "1",
            user_name="u1", thread_id="", scope_id="W1" if self.platform == "slack" else "",
            message_id=message_id, profile="default",
        )

    async def command(self, *, message_id, text="/wisdom update skill-1"):
        source = self.source(message_id=message_id)
        event = MessageEvent(
            text=text, source=source,
            user_id=source.user_id, user_name=source.user_name, message_id=message_id,
        )
        admitted = await self.runner._hm_admit_event(event)
        assert admitted is not None and admitted[0] is event
        handled, reply, command = await self.runner._hm_dispatch_quick_and_plugin_commands(
            event, source, event.get_command()
        )
        assert (handled, reply, command) == (True, None, "wisdom")

    async def human_turn(self, *, message_id, expected_llm_calls):
        source = self.source(message_id=message_id)
        event = MessageEvent(
            text="ordinary human turn", source=source,
            user_id=source.user_id, user_name=source.user_name, message_id=message_id,
        )
        admitted = await self.runner._hm_admit_event(event)
        assert admitted is not None and admitted[0] is event
        agent = SimpleNamespace(
            _persist_disabled=False, session_id=f"durable-{message_id}",
            model="journey-model", platform=self.platform, _parent_session_id="",
            _user_id=source.user_id, provider="custom",
            base_url="https://recording.invalid/v1", api_key="recording-key",
            api_mode="chat_completions", auth_mode="api_key",
        )
        context = SessionContext(
            source=source, connected_platforms=[source.platform], home_channels={},
            session_key=self.runner._session_key_for_source(source), session_id=agent.session_id,
        )
        tokens = self.runner._set_session_env(context)
        set_current_session_id(agent.session_id)
        runtime_token = set_runtime_main(
            agent.provider, agent.model, base_url=agent.base_url, api_key=agent.api_key,
            api_mode=agent.api_mode, auth_mode=agent.auth_mode, session_id=agent.session_id,
        )
        try:
            def collect():
                return _collect_pre_llm_call_context(
                    agent, effective_task_id=f"task-{message_id}",
                    turn_id=f"turn-{message_id}", original_user_message=event.text,
                    messages=[{"role": "user", "content": event.text}],
                    conversation_history=[],
                )

            await self.runner._run_in_executor_with_context(collect)
            for _ in range(1000):
                if len(self.model_calls) == expected_llm_calls:
                    with self.store.transaction() as database:
                        claimed = database.execute(
                            "SELECT COUNT(*) FROM wisdom_setup_handoff WHERE state='claimed'"
                        ).fetchone()[0]
                        unsent = database.execute(
                            "SELECT COUNT(*) FROM wisdom_consent "
                            "WHERE operation='setup' AND delivery_state='unsent'"
                        ).fetchone()[0]
                    if claimed == 0 and unsent == 0:
                        return
                await asyncio.sleep(0.001)
        finally:
            reset_runtime_main(runtime_token)
            self.runner._clear_session_env(tokens)
        with self.store.transaction() as database:
            handoffs = [dict(row) for row in database.execute(
                "SELECT h.state,h.last_error,c.plan_json FROM wisdom_setup_handoff h "
                "JOIN wisdom_consent c ON c.id=h.consent_id ORDER BY h.created_at"
            ).fetchall()]
        raise AssertionError(
            f"expected {expected_llm_calls} lifecycle model calls, got {len(self.model_calls)}; "
            f"handoffs={handoffs}"
        )

    def latest(self, endpoint):
        return [payload for method, payload in self.transport.calls if method == endpoint][-1]

    @property
    def edit_endpoint(self):
        return "chat.update" if self.platform == "slack" else "editMessageText"

    def control_value(self, payload, label):
        if self.platform == "slack":
            return slack_button(payload["blocks"], label)["value"]
        return button_value(payload["reply_markup"], label)

    def count_calls(self, methods):
        return sum(method in methods for method, _payload in self.transport.calls)

    def count_denials(self):
        return sum(
            method == "chat.postEphemeral"
            or (method == "answerCallbackQuery" and payload.get("show_alert") is True)
            for method, payload in self.transport.calls
        )

    def current_card_payload(self):
        endpoints = (
            {"chat.postMessage", "chat.update"}
            if self.platform == "slack"
            else {"sendMessage", "sendRichMessage", "editMessageText"}
        )
        payload = dict(next(
            payload for method, payload in reversed(self.transport.calls)
            if method in endpoints
        ))
        if self.platform == "telegram":
            if payload.get("rich_message"):
                payload.update(telegram_rich_card(payload))
            for method, update in reversed(self.transport.calls):
                if method in endpoints:
                    break
                if method == "editMessageReplyMarkup":
                    payload["reply_markup"] = update.get("reply_markup")
                    break
        return payload

    async def click(self, label, *, actor="U1", card_id=None, payload=None):
        target_card = str(card_id or self.card_id)
        payload = payload or self.current_card_payload()
        if self.platform == "slack":
            button = slack_button(payload["blocks"], label)
            self.callback_counter += 1
            body = {
                "type": "block_actions", "api_app_id": "A1", "token": "recording",
                "trigger_id": f"trigger-{self.callback_counter}",
                "response_url": "https://hooks.slack.invalid/recording",
                "user": {"id": actor, "name": actor.lower()}, "team": {"id": "W1"},
                "channel": {"id": "D1"},
                "container": {"type": "message", "channel_id": "D1", "message_ts": target_card},
                "message": {
                    "ts": target_card, "blocks": payload["blocks"], "text": payload["text"],
                },
                "actions": [{**button, "action_ts": str(self.callback_counter)}],
            }
            response = await self.native.async_dispatch(
                AsyncBoltRequest(body=body, mode="socket_mode")
            )
            assert response.status == 200
        else:
            value = button_value(payload["reply_markup"], label)
            await self.telegram_callback(value, actor=actor, card_id=target_card)

    def has_button(self, label):
        payload = self.current_card_payload()
        try:
            if self.platform == "slack":
                slack_button(payload["blocks"], label)
            else:
                if not payload.get("reply_markup"):
                    return False
                button_value(payload["reply_markup"], label)
        except AssertionError:
            return False
        return True

    async def telegram_callback(self, value, *, actor="U1", card_id=None):
        self.callback_counter += 1
        raw = {
            "update_id": self.callback_counter,
            "callback_query": {
                "id": f"query-{self.callback_counter}", "chat_instance": "recording",
                "from": {"id": 1 if actor == "U1" else 9, "is_bot": False, "first_name": actor,
                         "username": actor},
                "data": value,
                "message": {
                    "message_id": int(card_id or self.card_id), "date": 1,
                    "chat": {"id": 1001, "type": "private"}, "text": "card",
                },
            },
        }
        await self.native.process_update(Update.de_json(raw, self.native.bot))

    async def set_card_from_last_send(self):
        if self.platform == "slack":
            self.card_id = str(
                self.latest("chat.postMessage").get("ts") or self.transport.next_message
            )
        else:
            self.card_id = str(self.transport.next_message)

    async def deliver_ready_assessment(self):
        """Drive durable advice through the connected real SDK before command journeys."""

        self.store.activate_installation_identity("native-sdk", "org-1")
        self.store.bind_qualification_owner(self.owner)
        queue = MediationStore(self.store, owner=self.owner)
        route = self.invocation(self.adapter, message_id="assessment-turn")
        queue.register_session(
            "org-1", session_key=route.session_key, session_id="assessment-session",
            platform=self.platform, actor_id=route.actor_id, private=True,
            available=True, user_activity=True,
            address={
                "chat_id": route.chat_id, "thread_id": route.thread_id,
                "scope_id": route.scope_id,
            },
        )
        assessment_id = queue.enqueue_owned(
            "org-1", "feed:native-sdk-assessment",
            {"kind": "skill", "event_id": "native-sdk-assessment",
             "skill_id": "skill-1", "version": 1},
        )
        job = queue.claim("org-1", route.session_key, assessment_only=True)[0]
        assert queue.save_advice("org-1", assessment_id, job["lease_token"], {
            "assessment_id": assessment_id, "relevance": "recommend",
            "title": "SDK delivery advice",
            "explanation": "Exercise the connected native transport.",
            "provenance": {
                "kind": "agent_assessed", "provider": "recording",
                "model": "journey-model",
            },
        })
        before = self.count_calls({"chat.postMessage", "sendMessage", "sendRichMessage"})
        await self.runtime.deliver_assessments(
            route, self.owner, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True,
        )
        sent = self.count_calls({"chat.postMessage", "sendMessage", "sendRichMessage"})
        with self.store.transaction() as database:
            diagnostic_remote = [dict(row) for row in database.execute(
                "SELECT state,outcome,last_error FROM wisdom_remote_delivery"
            ).fetchall()]
        assert sent == before + 1, {
            "assessment": queue.assessments("org-1"),
            "remote": diagnostic_remote,
            "transport_calls": self.transport.calls,
        }
        payload = self.current_card_payload()
        assert "Agent-assessed advice" in payload["text"]
        assert self.has_button("View Details")
        assert self.has_button("Not Now")
        assert self.has_button("Mute Skill Recommendations")
        recommendation = self.runtime.reviews.load(next(
            record["id"] for record in self.runtime.reviews.pending(self.owner)
            if record["operation"] == "recommendation"
        ))
        assert recommendation["plan"]["skill_id"] == "skill-1"
        assert recommendation["plan"]["version"] == 1
        # This fixture omits remote check facts; absent checks must not expose install.
        assert recommendation["plan"]["security_check"] is None
        assert not self.has_button("Install Skill")
        assert "/wisdom show skill-1@v1" not in payload["text"]
        delivered = queue.assessments("org-1")[0]
        assert delivered["state"] == "delivered"
        with self.store.transaction() as database:
            receipt = database.execute(
                "SELECT receipt_json FROM wisdom_shared_delivery_receipt WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()
        assert receipt is not None
        # Replayed/restarted workers see durable success and never send a second card.
        await self.runtime.deliver_assessments(
            route, self.owner, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True,
        )
        assert self.count_calls({
            "chat.postMessage", "sendMessage", "sendRichMessage",
        }) == before + 1

    async def deliver_candidate_and_defer(  # pylint: disable=too-many-locals,too-many-statements
        self,
    ):
        """Exercise candidate Maybe Later through the discovered plugin and real SDK callback."""

        local = self.profile / "skills" / "sdk-candidate"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text("# SDK candidate\n", encoding="utf-8")
        content_hash, tree = snapshot_tree(local)
        skill_id = self.store.register_skill(
            local, source_kind="local", content_hash=content_hash, tree=tree
        )
        event_id = self.store.emit_local_event(
            kind="wisdom.candidate", skill_id=skill_id, content_hash=content_hash,
            session_id="candidate-origin", task_id="candidate-task",
            qualification="high_usage", payload={
                "local_reasons": {"consecutive_business_days": 3},
                "consent_required": True, "networked": False,
            },
        )
        today = datetime.now(timezone.utc).date()
        for offset in (-2, -1, 0):
            self.store.record_usage_day(
                skill_id, (today + timedelta(days=offset)).isoformat(),
                timezone_name="UTC", retain_after="2026-01-01",
            )
        queue = MediationStore(self.store, owner=self.owner)
        route = self.invocation(self.adapter, message_id="candidate-turn")
        queue.register_session(
            "org-1", session_key=route.session_key, session_id="candidate-session",
            platform=self.platform, actor_id=route.actor_id, private=True,
            available=True, user_activity=True,
            address={"chat_id": route.chat_id, "thread_id": route.thread_id,
                     "scope_id": route.scope_id},
        )
        assessment_id = queue.enqueue_owned(
            "org-1", f"candidate:{skill_id}:{content_hash}",
            {"kind": "candidate", "event_id": event_id,
             "local_skill_id": skill_id, "content_hash": content_hash},
        )
        job = queue.claim("org-1", route.session_key, assessment_only=True)[0]
        package = self.runtime.__class__.__module__.rsplit(".", 1)[0]
        assessment_type = __import__(
            package + ".mediation", fromlist=["QueuedAssessment"]
        ).QueuedAssessment
        service = self.runtime.service_factory()
        try:
            checker = assessment_type(service, self.owner, None, get_delivery_mode=lambda: "agent")
            advice = checker._candidate_advice(job)  # pylint: disable=protected-access
        finally:
            service.close()
        assert queue.save_advice("org-1", assessment_id, job["lease_token"], advice)
        consent_before = len(self.runtime.reviews.pending(self.owner))
        sends_before = self.count_calls({"chat.postMessage", "sendMessage", "sendRichMessage"})
        policy_requests = 0
        agent_led_policy = self.client.agent_led_policy

        package = self.runtime.__class__.__module__.rsplit(".", 1)[0]
        policy_type = __import__(
            package + ".client", fromlist=["AgentLedPolicyResponse"]
        ).AgentLedPolicyResponse

        def counted_policy():
            nonlocal policy_requests
            policy_requests += 1
            return policy_type.model_validate(agent_led_policy().model_dump(mode="json"))

        self.client.agent_led_policy = counted_policy  # type: ignore[assignment]
        await self.runtime.deliver_assessments(
            route, self.owner, get_delivery_mode=lambda: "agent",
            admission_current=lambda: True,
        )
        policy_requests_after_delivery = policy_requests
        assert policy_requests_after_delivery >= 1
        assert self.count_calls({
            "chat.postMessage", "sendMessage", "sendRichMessage",
        }) == sends_before + 1, (
            self.platform, queue.assessments("org-1")
        )
        await self.set_card_from_last_send()
        card_id = self.card_id
        payload = self.current_card_payload()
        assert "Hermes thinks the following skill would be useful" in payload["text"]
        assert "wisdom_inspect" not in payload["text"]
        assert event_id not in payload["text"]
        assert self.has_button("Share")
        assert self.has_button("Review in Portal")
        assert self.has_button("Maybe Later")
        delivered = next(
            row for row in queue.assessments("org-1") if row["id"] == assessment_id
        )
        assert delivered["reference"]["not_now_suppression_days"] == 7

        edit_methods = {"chat.update", "editMessageText", "editMessageReplyMarkup"}
        edits_before = self.count_calls(edit_methods)
        await self.click("Maybe Later", actor="U9", card_id=card_id, payload=payload)
        assert self.count_calls(edit_methods) == edits_before
        await self.click("Maybe Later", card_id=card_id, payload=payload)
        assert self.count_calls(edit_methods) == edits_before + 1
        deferred = self.current_card_payload()
        assert deferred["text"] == payload["text"]
        assert not self.has_button("Maybe Later")
        with self.store.transaction() as database:
            suppression = database.execute(
                "SELECT suppress_until,created_at FROM wisdom_preference_outbox"
            ).fetchone()
            state = database.execute(
                "SELECT state,last_error FROM wisdom_shared_assessment WHERE id=?",
                (assessment_id,),
            ).fetchone()
        assert suppression["suppress_until"] - suppression["created_at"] == 7 * 86400
        assert tuple(state) == ("deferred", None)
        assert len(self.runtime.reviews.pending(self.owner)) == consent_before
        assert policy_requests == policy_requests_after_delivery, policy_requests

        await self.click("Maybe Later", card_id=card_id, payload=payload)
        assert self.count_calls(edit_methods) == edits_before + 1

        service = self.service()
        try:
            producer = MaintenanceMediationProducer(
                service,
                SimpleNamespace(notifications=lambda **_kwargs: {"events": [], "more": False}),
                self.owner,
                skills_root=self.profile / "skills",
                at=datetime.now(timezone.utc),
                timezone_info=(timezone.utc, "UTC"),
            )
            with self.store.transaction() as database:
                database.execute(
                    "UPDATE wisdom_preference_outbox SET attempts=3 WHERE organization_id='org-1'"
                )
            assert producer.produce(agent_led_policy())["weekly"] == 0
            with self.store.transaction() as database:
                database.execute(
                    """UPDATE wisdom_preference_outbox
                    SET suppress_until=0,state='expired' WHERE organization_id='org-1'"""
                )
            assert producer.produce(agent_led_policy())["weekly"] == 1
        finally:
            service.close()
        candidate_rows = [
            row for row in queue.assessments("org-1")
            if row["reference"].get("kind") == "candidate"
        ]
        assert len(candidate_rows) == 2
        assert candidate_rows[0]["id"] == assessment_id
        assert candidate_rows[0]["state"] == "deferred"
        assert candidate_rows[1]["id"] != assessment_id
        with self.store.transaction() as database:
            assert database.execute(
                "SELECT COUNT(*) FROM wisdom_shared_delivery_receipt WHERE assessment_id=?",
                (assessment_id,),
            ).fetchone()[0] == 1

    async def run(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self
    ):
        await self.setup_platform()

        self.runtime.service_factory = self.settings_service
        await self.command(message_id="settings-request", text="/wisdom settings")
        await self.set_card_from_last_send()
        settings_card = self.card_id
        settings_payload = self.current_card_payload()
        assert "Notification settings" in settings_payload["text"]
        assert self.has_button("Mute 1 week")
        denials = self.count_denials()
        await self.click("Fixed notifications", actor="U9")
        assert self.count_denials() == denials + 1
        assert "delivery_mode" not in Path(os.environ["HERMES_HOME"]).joinpath(
            "config.yaml"
        ).read_text(encoding="utf-8")

        await self.click(
            "Fixed notifications", card_id=settings_card, payload=settings_payload
        )
        assert "Notification settings updated" in self.current_card_payload()["text"]
        await self.click(
            "Fixed notifications", card_id=settings_card, payload=settings_payload
        )
        config_text = Path(os.environ["HERMES_HOME"]).joinpath("config.yaml").read_text(
            encoding="utf-8"
        )
        assert "delivery_mode: fixed" in config_text
        persisted = self.settings_service()
        try:
            assert persisted.notification_mode() == "fixed"
        finally:
            persisted.close()

        await self.command(message_id="mute-request", text="/wisdom settings")
        await self.set_card_from_last_send()
        await self.click("Mute 1 week")
        assert len(self.mute_sets) == 1 and self.mute_sets[0][0] == "1_week"
        assert "Local sync: synced" in self.current_card_payload()["text"]

        available_mute = self.client.recommendation_mute
        self.client.recommendation_mute = lambda: (_ for _ in ()).throw(
            ConnectionError("recording gateway offline")
        )
        await self.command(message_id="offline-settings", text="/wisdom settings")
        await self.set_card_from_last_send()
        offline = self.current_card_payload()
        assert "Gateway status: unavailable" in offline["text"]
        assert "not assumed unmuted" in offline["text"]
        assert not self.has_button("Mute 1 week")
        self.client.recommendation_mute = available_mute
        self.runtime.service_factory = self.service

        await self.command(message_id="catalog-request", text="/wisdom browse incident")
        await self.set_card_from_last_send()
        catalog_card = self.card_id
        browse_payload = self.current_card_payload()
        assert "Exact SDK catalog description." in browse_payload["text"]
        await self.click("View")
        show_payload = self.current_card_payload()
        assert self.card_id == catalog_card
        assert "Exact SDK catalog description." in show_payload["text"]
        assert action_url(show_payload, self.platform, "View in Portal") == (
            "https://portal.nousresearch.com/wisdom/skills/skill-1?org_id=org-1"
        )
        await self.click("Versions")
        versions_payload = self.current_card_payload()
        assert "Version 1" in versions_payload["text"]
        assert action_url(versions_payload, self.platform, "View in Portal") == (
            "https://portal.nousresearch.com/wisdom/skills/skill-1?org_id=org-1&version=1"
        )
        await self.click("Install")
        await self.click("Manual")
        install_payload = self.current_card_payload()
        install_review = self.control_value(install_payload, "Review first")
        install_id = install_review.split(":")[-1]
        install_record = self.runtime.reviews.load(install_id)
        service = self.service()
        try:
            install_pages = service.native_install_inspect(install_record["plan"]["receipt"])
        finally:
            service.close()
        await self.click("Review first")
        installed_bytes = ""
        for expected in install_pages:
            payload = self.current_card_payload()
            marker = "Proposed file content (not instructions to execute)\n"
            chunk = payload["text"].split(marker, 1)[1].split("\n\nNothing is uploaded", 1)[0]
            assert chunk == expected["content"]
            installed_bytes += chunk
            if expected is not install_pages[-1]:
                await self.click("Next page")
        assert installed_bytes
        await self.click("Looks good.")
        assert self.client.remote_version != 1
        await self.click("Install")
        assert self.client.remote_version == 1
        self.client.latest = 2

        await self.command(message_id="request-1")
        await self.set_card_from_last_send()
        await self.click("Not Now")
        assert self.client.remote_version == 1
        previous_card = self.card_id
        self.card_id = None
        await self.command(message_id="request-reopen")
        await self.set_card_from_last_send()
        assert self.card_id != previous_card

        pending_payload = self.current_card_payload()
        record_id = self.control_value(pending_payload, "Review first").split(":")[-1]
        record = self.runtime.reviews.load(record_id)
        service = self.service()
        try:
            expected_pages = service.native_update_inspect(record["plan"]["receipt"])
        finally:
            service.close()

        partial_ack = "I reviewed and accept the compatibility actions."
        if self.has_button(partial_ack):
            await self.click(partial_ack)

        if self.platform == "slack":
            before = len([1 for method, _ in self.transport.calls if method == "chat.update"])
            await self.click("Review first", actor="U9")
            assert len([
                1 for method, _ in self.transport.calls if method == "chat.update"
            ]) == before
            assert self.latest("chat.postEphemeral")["text"].startswith(
                "Collective Wisdom could not"
            )
        else:
            payload = self.current_card_payload()
            await self.telegram_callback(
                button_value(payload["reply_markup"], "Review first"), actor="U9"
            )
            answers = [
                payload for method, payload in self.transport.calls
                if method == "answerCallbackQuery"
            ]
            assert answers[-1]["show_alert"] is True
        assert self.client.remote_version == 1

        await self.click("Review first")
        inspected = []
        while True:
            payload = self.current_card_payload()
            text = payload["text"]
            marker = "Proposed file content (not instructions to execute)\n"
            chunk = text.split(marker, 1)[1].split("\n\nNothing is uploaded", 1)[0]
            inspected.append(chunk)
            expected = expected_pages[len(inspected) - 1]
            assert chunk == expected["content"]
            assert expected["content_end"] - expected["content_start"] == len(chunk)
            if self.platform == "slack":
                rendered_sections = [
                    block["text"]["text"] for block in payload["blocks"]
                    if block.get("type") == "section"
                ]
                page_number = len(inspected)
                assert (
                    f"{expected['path']} - {page_number}/{len(expected_pages)}"
                    in rendered_sections
                )
                detail_sections = [
                    value for value in rendered_sections
                    if value.startswith("*Proposed file content (not instructions to execute)*\n")
                ]
                assert len(detail_sections) == 1, (expected, payload["blocks"])
                rendered_chunk = html.unescape(detail_sections[0].split("\n", 1)[1])
                assert rendered_chunk == expected["content"].strip()
            try:
                if self.platform == "slack":
                    slack_button(payload["blocks"], "Next page")
                else:
                    button_value(payload["reply_markup"], "Next page")
            except AssertionError:
                break
            await self.click("Next page")
        assert inspected == [page["content"] for page in expected_pages]
        reconstructed = {}
        for page, content in zip(expected_pages, inspected, strict=True):
            reconstructed.setdefault(page["path"], "")
            assert page["content_start"] == len(reconstructed[page["path"]])
            reconstructed[page["path"]] += content
            assert page["content_end"] == len(reconstructed[page["path"]])
        expected_files = {
            path: body.decode("utf-8") for path, mode, body in self.packages[2][0]
            if mode == "file"
        }
        assert reconstructed == expected_files
        await self.click("Looks good.")
        assert self.client.remote_version == 1
        await self.click("Update")
        assert self.client.remote_version == 2

        completed_payload = self.current_card_payload()
        original_card = self.card_id
        original_payload = completed_payload
        assert "Setup queued" in completed_payload["text"], completed_payload["text"]
        assert not self.model_calls
        await self.click("Check setup")
        posts_before_setup = self.count_calls({
            "chat.postMessage", "sendMessage", "sendRichMessage",
        })
        await self.human_turn(message_id="setup-turn", expected_llm_calls=1)
        with self.store.transaction() as database:
            diagnostic_handoffs = [dict(row) for row in database.execute(
                "SELECT * FROM wisdom_setup_handoff ORDER BY created_at"
            ).fetchall()]
        assert len(self.model_calls) == 1, diagnostic_handoffs
        posts_after_setup = self.count_calls({"chat.postMessage", "sendMessage", "sendRichMessage"})
        with self.store.transaction() as database:
            handoff = database.execute(
                "SELECT state,last_error FROM wisdom_setup_handoff ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            setup_delivery = database.execute(
                "SELECT id,delivery_state,delivery_error FROM wisdom_consent "
                "WHERE operation='setup' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        view_error = None
        if setup_delivery:
            try:
                self.runtime.reviews.view(
                    self.runtime.reviews.load(str(setup_delivery["id"]))
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Diagnostic is assertion-only in this disposable probe.
                view_error = repr(exc)
        assert posts_after_setup == posts_before_setup + 1, {
            "handoff": dict(handoff) if handoff else None,
            "delivery": dict(setup_delivery) if setup_delivery else None,
            "view_error": view_error,
        }
        await self.set_card_from_last_send()
        setup_card = self.card_id
        setup_payload = self.current_card_payload()
        setup_control = self.control_value(setup_payload, "Run this step")
        setup_id = setup_control.split(":")[-1]
        setup_receipt = self.runtime.reviews.load(setup_id)["plan"]["receipt"]

        await self.click(
            "Check setup", card_id=original_card, payload=original_payload
        )
        original_payload = self.current_card_payload()
        assert "Setup needs attention" in original_payload["text"]
        assert "Run this step" not in original_payload["text"]

        edits_before_wrong_card = self.count_calls({"chat.update", "editMessageText"})
        denials_before_wrong_card = self.count_denials()
        await self.click(
            "Run this step", card_id=original_card, payload=setup_payload
        )
        assert self.count_calls({"chat.update", "editMessageText"}) == edits_before_wrong_card
        assert self.count_denials() == denials_before_wrong_card + 1
        assert not self.setup.inspect("skill-1", version=2)["setup_progress"]

        set_approval_callback(lambda *_args, **_kwargs: "once")
        await self.click("Run this step", card_id=setup_card, payload=setup_payload)
        failed_payload = self.current_card_payload()
        assert "Setup step failed" in failed_payload["text"]
        await self.click("Recheck", card_id=setup_card, payload=failed_payload)
        rechecked_payload = self.current_card_payload()
        rechecked_control = self.control_value(rechecked_payload, "Run this step")
        rechecked_id = rechecked_control.split(":")[-1]
        rechecked_receipt = self.runtime.reviews.load(rechecked_id)["plan"]["receipt"]
        assert rechecked_id != setup_id and rechecked_receipt != setup_receipt
        await self.click("Not Now", card_id=setup_card, payload=rechecked_payload)

        posts_before_deferred_turn = self.count_calls({
            "chat.postMessage", "sendMessage", "sendRichMessage",
        })
        await self.human_turn(message_id="deferred-turn", expected_llm_calls=1)
        assert self.count_calls({"chat.postMessage", "sendMessage", "sendRichMessage"}) == (
            posts_before_deferred_turn
        )

        await self.click(
            "Check setup", card_id=original_card, payload=original_payload
        )
        original_status = self.current_card_payload()
        assert "Setup queued" in original_status["text"]
        await self.human_turn(message_id="setup-retry-turn", expected_llm_calls=2)
        await self.set_card_from_last_send()
        fresh_setup_payload = self.current_card_payload()
        fresh_control = self.control_value(fresh_setup_payload, "Run this step")
        fresh_id = fresh_control.split(":")[-1]
        fresh_receipt = self.runtime.reviews.load(fresh_id)["plan"]["receipt"]
        assert fresh_id != rechecked_id and fresh_receipt != rechecked_receipt
        await self.click("Run this step")
        await self.click("Check setup")

        await self.human_turn(message_id="verify-turn", expected_llm_calls=3)
        await self.set_card_from_last_send()
        await self.click("Run this step")
        await self.click("Check setup")
        assert "Ready" in self.current_card_payload()["text"]
        assert self.setup.inspect("skill-1", version=2)["ready_to_use"] is True
        assert self.model_calls == [
            {"url": "https://recording.invalid/v1/chat/completions", "model": "journey-model"},
            {"url": "https://recording.invalid/v1/chat/completions", "model": "journey-model"},
            {"url": "https://recording.invalid/v1/chat/completions", "model": "journey-model"},
        ]

        await self.command(
            message_id="publication-request", text="/wisdom publish local-draft"
        )
        await self.set_card_from_last_send()
        publication_card = self.card_id
        publication_review = self.current_card_payload()
        assert "Exact SDK publication review." in publication_review["text"]
        assert "sha256:" + "f" * 64 in publication_review["text"]
        await self.click("Share My Skill")
        pending_publication = self.current_card_payload()
        assert self.card_id == publication_card
        assert "Pending moderation" in pending_publication["text"]
        assert action_url(pending_publication, self.platform, "View details") == (
            "https://portal.nousresearch.com/wisdom/review/remote-draft?org_id=org-1"
        )
        await self.click("Check status")
        published = self.current_card_payload()
        assert self.card_id == publication_card
        assert "Shared!" in published["text"]
        assert action_url(published, self.platform, "View details") == (
            "https://portal.nousresearch.com/wisdom/skills/skill-1?org_id=org-1"
        )

        await self.deliver_ready_assessment()
        await self.deliver_candidate_and_defer()

        reopened = NativeReviewController(WisdomStore(self.state_root))
        with self.store.transaction() as database:
            durable_rows = [dict(row) for row in database.execute(
                "SELECT id,operation FROM wisdom_consent "
                "WHERE state='completed' ORDER BY created_at"
            ).fetchall()]
        durable_views = {
            row["operation"]: reopened.view(reopened.load(row["id"])).to_text()
            for row in durable_rows
        }
        assert "Setup queued" in durable_views["update"]
        assert "Ready" in durable_views["setup"]

        # PTB ownership handler wins before catch-all; unrelated callbacks remain available.
        if self.platform == "telegram":
            own_count = self.catch_all
            assert own_count == 0
            await self.telegram_callback("other:callback")
            assert self.catch_all == 1
            await self.native.shutdown()
        set_approval_callback(None)
        self.runtime.close()
        self.runner._shutdown_executor()
        print(f"real {self.platform} SDK journey passed")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=("slack", "telegram"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    manager = get_plugin_manager()
    manager.discover_and_load()
    plugin_runtime = manager._plugin_commands["wisdom"]["handler"].__self__
    assert Path(manager._plugins["hermes-collective-wisdom"].module.__file__).resolve() == (
        Path(os.environ["WISDOM_PLUGIN_ROOT"]) / "__init__.py"
    )
    await Journey(args.platform, args.root.resolve(), manager, plugin_runtime).run()
    manager.unload()


if __name__ == "__main__":
    asyncio.run(main())
