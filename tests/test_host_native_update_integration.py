"""Pinned-host admission and lifecycle proof for the native Wisdom boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from host_probe_support import (
    immutable_host_checkout,
    isolated_host_environment,
    run_host_python,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"
PLUGIN_ID = "hermes-collective-wisdom"

PROBE = r'''
import asyncio
import base64
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import agent.process_bootstrap as process_bootstrap
from hermes_cli import auth
from agent.auxiliary_client import (
    _read_main_model,
    _read_main_provider,
    reset_runtime_main,
    set_runtime_main,
)
from agent.turn_context import _collect_pre_llm_call_context
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import set_current_session_id
from hermes_cli.plugins import get_plugin_manager
from plugins.platforms.slack.adapter import SlackAdapter
from tools.registry import registry

PLUGIN_ID = "hermes-collective-wisdom"


class SlackClient:
    def __init__(self):
        self.posts = []
        self.updates = []

    async def chat_postMessage(self, **payload):
        self.posts.append(payload)
        return {"ts": f"card-{len(self.posts)}"}

    async def chat_update(self, **payload):
        self.updates.append(payload)
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


class Service:
    requests = []
    applies = 0

    def close(self):
        pass

    def native_owner(self):
        return {
            "profile_name": RUNTIME.profile[0], "profile_home": str(RUNTIME.profile[1]),
            "account_id": "member-1", "organization_id": "org-1",
            "base_url": "https://gateway.invalid", "installation_id": "install-1",
        }

    def update_plan(self, skill_id, **_choices):
        self.requests.append(skill_id)
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
            "owner": self.native_owner(),
        }

    def update_apply(self, receipt, *, may_apply):
        assert receipt == "receipt"
        assert may_apply() is True
        Service.applies += 1
        return {"updated": True, "version": 2}

    def installed_setup_inspect(self, skill_id, **_kwargs):
        assert skill_id == "skill-1"
        return {"ready_to_use": True, "skill_id": skill_id}

    def inbox(self):
        return {"activity": [{"kind": "update", "skill_id": "skill-1", "version": 2}]}

    def list_installations(self):
        return [{"skill_id": "skill-1", "version": 2, "state": "installed"}]


class WireModel:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **_kwargs):
        return self.value


def install_authenticated_fixture(runtime):
    claims = {
        "sub": "member-1", "org_id": "org-1", "exp": time.time() + 600,
        "nbf": 0, "wisdom_scopes": ["wisdom:read", "wisdom:install", "wisdom:publish"],
    }
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value).encode()
    ).decode().rstrip("=")
    token = encode({"alg": "none"}) + "." + encode(claims) + ".fixture"
    auth._save_auth_store({"providers": {"nous": {
        "access_token": token, "user_id": "member-1", "org_id": "org-1",
        "portal_base_url": "http://localhost:3000", "refresh_token": "never-use",
    }}})

    runtime_module = __import__(runtime.__class__.__module__, fromlist=["InvocationService"])
    client_module = __import__(
        runtime_module.InvocationService.__module__.rsplit(".", 1)[0] + ".client",
        fromlist=["WisdomClient"],
    )
    client_module.WisdomClient.capability = lambda _self: WireModel({"features": ["wisdom"]})
    client_module.WisdomClient.register_identity = lambda _self, identity: WireModel({
        "installation_id": identity, "state": "active",
    })
    client_module.WisdomClient.version = lambda _self, skill_id, version: WireModel({
        "skill": {"id": skill_id, "slug": "recording-skill", "state": "active"},
        "version": {"version": version, "state": "published", "content_hash": "a" * 64},
    })
    service = runtime._service()
    try:
        configured = service.setup(accept_disclosure=True)
        assert configured["configured"] is True
        assert service.check_owner().organization_id == "org-1"
    finally:
        service.close()


def source(user="U1", chat="D1", *, profile=None, message="request-1"):
    return SessionSource(
        platform=Platform.SLACK, chat_id=chat, chat_type="dm", user_id=user,
        user_name=user.lower(), scope_id="W1", message_id=message, profile=profile,
    )


def event(text, current_source):
    return MessageEvent(
        text=text, source=current_source, user_id=current_source.user_id,
        user_name=current_source.user_name, message_id=current_source.message_id,
    )


def agent_for(current_source, *, durable, model, sender=None, parent=""):
    return SimpleNamespace(
        _persist_disabled=False, session_id=durable, model=model, platform="slack",
        _parent_session_id=parent, _user_id=sender or current_source.user_id,
        base_url=f"https://{durable}.invalid/v1", api_key=f"key-{durable}",
        api_mode="chat_completions", auth_mode="api_key",
    )


async def admit(runner, incoming):
    admitted = await runner._hm_admit_event(incoming)
    return admitted[0] if admitted is not None else None


async def lifecycle(
    runner, current_source, current_agent, *, task_id, turn_id, dispatch_tools=False,
):
    route_key = runner._session_key_for_source(current_source)
    context = SessionContext(
        source=current_source, connected_platforms=[Platform.SLACK], home_channels={},
        session_key=route_key, session_id=current_agent.session_id,
    )
    tokens = runner._set_session_env(context)
    set_current_session_id(current_agent.session_id)
    runtime_token = set_runtime_main(
        current_agent.provider, current_agent.model,
        base_url=current_agent.base_url, api_key=current_agent.api_key,
        api_mode=current_agent.api_mode, auth_mode=current_agent.auth_mode,
        session_id=current_agent.session_id,
    )
    try:
        def collect():
            return _collect_pre_llm_call_context(
                current_agent,
                effective_task_id=task_id,
                turn_id=turn_id,
                original_user_message="ordinary human turn",
                messages=[{"role": "user", "content": "ordinary human turn"}],
                conversation_history=[],
            )
        await runner._run_in_executor_with_context(collect)
        if dispatch_tools:
            names = {"wisdom_inbox", "wisdom_inspect", "present_wisdom_consent"}
            assert {name for name in names if registry.get_entry(name)} == names
            TOOL_RESULTS.update({
                "inbox": registry.dispatch(
                    "wisdom_inbox", {}, task_id=task_id,
                    session_id=current_agent.session_id,
                ),
                "inspect": registry.dispatch(
                    "wisdom_inspect",
                    {"kind": "skill", "identity": "skill-1", "version": 2},
                    task_id=task_id, session_id=current_agent.session_id,
                ),
                "present": registry.dispatch(
                    "present_wisdom_consent",
                    {
                        "kind": "skill", "identity": "skill-1", "version": 2,
                        "title": "Managed update", "explanation": "Review exact package",
                    },
                    task_id=task_id, session_id=current_agent.session_id,
                ),
            })
    finally:
        reset_runtime_main(runtime_token)
        runner._clear_session_env(tokens)


async def main():
    global RUNTIME
    manager = get_plugin_manager()
    manager.discover_and_load()
    from hermes_cli.config import load_config_readonly
    assert load_config_readonly().get("fallback_providers"), load_config_readonly()
    assert PLUGIN_ID in manager._plugins
    assert set(manager._hooks).issuperset({"pre_gateway_dispatch", "pre_llm_call"})
    assert "wisdom" in manager._plugin_commands
    RUNTIME = manager._plugin_commands["wisdom"]["handler"].__self__
    assert Path(manager._plugins[PLUGIN_ID].module.__file__).resolve() == PROJECT_ROOT / "__init__.py"
    app = SlackApp()
    adapter = object.__new__(SlackAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._owner_profile = None
    adapter._message_handler = None
    adapter._authorization_check = lambda user_id, chat_type, _chat_id: (
        user_id in {"U1", "U2"} and chat_type == "dm"
    )
    factories = manager._platform_handler_factories["slack"]
    assert len(factories) == 1
    factories[0][0](app, adapter)
    RUNTIME.native.service_factory = Service

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {}
    runner._primary_profile_name = RUNTIME.profile[0]
    runner._startup_restore_in_progress = False
    runner._draining = False
    runner._scale_to_zero_note_real_inbound = lambda: None

    # Capture happens before the real host allowlist decision, but denial creates
    # neither state nor SDK delivery. A later admitted event replaces that capture.
    denied = event("/wisdom update skill-denied", source("U9", "D9"))
    assert await admit(runner, denied) is None
    assert Service.requests == []
    assert not (RUNTIME.root / "wisdom.db").exists()
    assert app.client.posts == []
    install_authenticated_fixture(RUNTIME)

    admitted_source = source(profile=RUNTIME.profile[0])
    admitted_event = event("/wisdom update skill-1", admitted_source)
    assert await admit(runner, admitted_event) is admitted_event
    handled, reply, command = await runner._hm_dispatch_quick_and_plugin_commands(
        admitted_event, admitted_source, admitted_event.get_command()
    )
    assert (handled, reply, command) == (True, None, "wisdom")
    assert Service.requests == ["skill-1"]
    assert len(app.client.posts) == 1
    record = RUNTIME.native.reviews.load(
        app.client.posts[0]["blocks"][-1]["elements"][-1]["value"].split(":")[-1]
    )
    assert record["owner_session"] == runner._session_key_for_source(admitted_source)
    assert record["actor_id"] == "U1"

    action = app.client.posts[0]["blocks"][-1]["elements"][-1]
    ack_count = 0

    async def callback_ack():
        nonlocal ack_count
        ack_count += 1

    def callback_body(**changes):
        body = {
            "user": {"id": "U1", "name": "u1"},
            "channel": {"id": "D1"}, "team": {"id": "W1"},
            "message": {"ts": "card-1"},
            "container": {"message_ts": "card-1"},
        }
        body.update(changes)
        return body

    # Exercise the plugin closure through the real host Slack authorization
    # helper. Every route field is checked against the owning review record.
    denied_callbacks = [
        callback_body(user={"id": "U9", "name": "intruder"}),
        callback_body(channel={"id": "D9"}),
        callback_body(team={"id": "W9"}),
        callback_body(message={"ts": "card-9"}, container={"message_ts": "card-9"}),
        callback_body(message={"ts": "card-1", "thread_ts": "thread-9"}),
    ]
    for body in denied_callbacks:
        await app.handler_for(action["action_id"])(callback_ack, body, action)
    adapter._owner_profile = "other-profile"
    await app.handler_for(action["action_id"])(callback_ack, callback_body(), action)
    adapter._owner_profile = None

    assert Service.applies == 0 and app.client.updates == []
    await app.handler_for(action["action_id"])(callback_ack, callback_body(), action)
    assert Service.applies == 1
    assert app.client.updates[-1]["channel"] == "D1"
    assert app.client.updates[-1]["ts"] == "card-1"
    assert ack_count == len(denied_callbacks) + 2

    # A quick-command alias cannot launder its pre-auth capture into /wisdom.
    alias_event = event("/alias update skill-alias", admitted_source)
    assert await admit(runner, alias_event) is alias_event
    assert RUNTIME.native.consume_request("update skill-alias") is None
    assert Service.requests == ["skill-1"]

    calls = []
    outcomes = []
    entered = asyncio.Event()
    release = asyncio.Event()

    def response_for(request):
        payload = json.loads(request.content)
        model = payload["model"]
        calls.append({
            "url": str(request.url), "wire_model": model,
            "context_provider": _read_main_provider(),
            "context_model": _read_main_model(),
        })
        if (
            model == "unavailable-model"
            or (model == "needs-fallback" and request.url.host != "fallback.invalid")
            or (request.url.host == "fallback.invalid" and _read_main_model() == "unavailable-model")
        ):
            return httpx.Response(429, json={"error": {"message": "recording transport rate limit"}})
        if model == "blocked-model":
            entered.set()
            raise AssertionError("blocked model must use async recording transport")
        return httpx.Response(
            200,
            json={
                "id": "recorded", "object": "chat.completion", "created": 1,
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant",
                    "content": '{"command":"true","explanation":"Run the declared check."}',
                }}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def async_response_for(request):
        payload = json.loads(request.content)
        model = payload["model"]
        if model == "blocked-model":
            calls.append({
                "url": str(request.url), "wire_model": model,
                "context_provider": _read_main_provider(),
                "context_model": _read_main_model(),
            })
            entered.set()
            await release.wait()
        return response_for(request)

    def recording_http_client(_base_url, *, async_mode=False, verify=True):
        del verify
        transport = httpx.MockTransport(async_response_for if async_mode else response_for)
        return httpx.AsyncClient(transport=transport) if async_mode else httpx.Client(transport=transport)

    # Keep the real PluginLlm -> async_call_llm -> routing/fallback path. Replace
    # only the host's network client construction with an in-memory HTTP transport.
    process_bootstrap.build_keepalive_http_client = recording_http_client

    async def probe_resume(invocation, *, expected_model):
        from gateway.session_context import get_session_env
        assert invocation.host_session_id
        assert invocation.host_session_id != invocation.session_key
        assert get_session_env("HERMES_SESSION_KEY") == invocation.session_key
        assert get_session_env("HERMES_SESSION_USER_ID") == invocation.actor_id
        info = {
            "prerequisites": [],
            "guidance": {"setup_instructions": ["Run the declared check"],
                         "verification_step": "Verify it"},
        }
        try:
            result = await RUNTIME.native._propose(
                info, phase="setup", index=0, instruction="Run the declared check",
                expected_model=expected_model,
            )
        except Exception:
            result = None
        outcomes.append((invocation.host_session_id, invocation.session_key, expected_model, result))

    RUNTIME.native.resume_setup = probe_resume

    async def wait_outcomes(expected):
        for _ in range(1000):
            if len(outcomes) == expected:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"expected {expected} outcomes, got {len(outcomes)}")

    async def run_route(user, chat, durable, provider, model):
        expected = len(outcomes) + 1
        current_source = source(user, chat, profile=RUNTIME.profile[0], message=f"msg-{chat}")
        incoming = event("ordinary human turn", current_source)
        assert await admit(runner, incoming) is incoming
        current_agent = agent_for(current_source, durable=durable, model=model)
        current_agent.provider = provider
        await lifecycle(
            runner, current_source, current_agent,
            task_id=f"task-{chat}", turn_id=f"turn-{chat}",
        )
        await wait_outcomes(expected)

    await run_route("U1", "D1", "durable-a", "custom", "model-a")
    await run_route("U2", "D2", "durable-b", "custom", "needs-fallback")
    await run_route("U1", "D1", "durable-c", "custom", "unavailable-model")
    actual_routes = [(row["url"].split("/")[2], row["wire_model"]) for row in calls]
    assert actual_routes == [
        ("durable-a.invalid", "model-a"),
        ("durable-b.invalid", "needs-fallback"),
        ("fallback.invalid", "fallback-model"),
        ("durable-c.invalid", "unavailable-model"),
        ("fallback.invalid", "fallback-model"),
    ], actual_routes
    assert [(row["context_provider"], row["context_model"]) for row in calls] == [
        ("custom", "model-a"),
        ("custom", "needs-fallback"), ("custom", "needs-fallback"),
        ("custom", "unavailable-model"),
        ("custom", "unavailable-model"),
    ]
    assert outcomes[0][3]["command"] == "true"
    # The configured fallback really runs, but _propose rejects its different
    # model rather than pretending it used the owning conversation model.
    assert outcomes[1][3] is None
    assert outcomes[2][3] is None
    assert outcomes[0][0] == "durable-a"
    assert outcomes[0][1] == runner._session_key_for_source(
        source("U1", "D1", profile=RUNTIME.profile[0])
    )
    assert outcomes[1][0] == "durable-b"
    assert outcomes[1][1] == runner._session_key_for_source(
        source("U2", "D2", profile=RUNTIME.profile[0])
    )

    # Dispatch the plugin tools through the pinned host registry with its real
    # task/session kwargs while the admitted route context is still bound.
    tool_source = source("U1", "D1", profile=RUNTIME.profile[0], message="tool-message")
    tool_event = event("ordinary human turn", tool_source)
    assert await admit(runner, tool_event) is tool_event
    tool_agent = agent_for(tool_source, durable="durable-tools", model="model-a")
    tool_agent.provider = "custom"
    await lifecycle(
        runner, tool_source, tool_agent, task_id="task-tools", turn_id="turn-tools",
        dispatch_tools=True,
    )
    await wait_outcomes(4)
    decoded_tools = {name: json.loads(value) for name, value in TOOL_RESULTS.items()}
    assert decoded_tools["inbox"] == {
        "mode": "agent", "assessments": [], "interactions": [], "installed_setup": [],
    }, decoded_tools
    assert decoded_tools["inspect"] == {
        "facts": {
            "skill": {"id": "skill-1", "slug": "recording-skill", "state": "active"},
            "version": {"version": 2, "state": "published", "content_hash": "a" * 64},
        },
        "kind": "skill", "skill_id": "skill-1", "version": 2,
    }
    assert decoded_tools["present"]["status"] == "queued"
    assert decoded_tools["present"]["delivery"] == {"state": "queued"}
    assert decoded_tools["present"]["executed"] is False
    assert "presented" not in decoded_tools["present"]

    # Payload identity and subagent ancestry fail before spawn/LLM.
    before = len(calls)
    for sender, parent in (("U9", ""), ("U1", "parent-session")):
        current_source = source("U1", "D1", profile=RUNTIME.profile[0], message="negative")
        incoming = event("ordinary human turn", current_source)
        assert await admit(runner, incoming) is incoming
        current_agent = agent_for(
            current_source, durable=f"negative-{sender}-{parent}", model="model-a",
            sender=sender, parent=parent,
        )
        current_agent.provider = "provider-a"
        await lifecycle(runner, current_source, current_agent, task_id="negative", turn_id="negative")
    await asyncio.sleep(0.01)
    assert len(calls) == before, calls

    # A blocked supervised SDK task is cancelled by real manager unload; old
    # platform closures remain closed and cannot later call the transport.
    blocked_source = source("U1", "D1", profile=RUNTIME.profile[0], message="blocked")
    blocked_event = event("ordinary human turn", blocked_source)
    assert await admit(runner, blocked_event) is blocked_event
    blocked_agent = agent_for(blocked_source, durable="durable-blocked", model="blocked-model")
    blocked_agent.provider = "custom"
    await lifecycle(runner, blocked_source, blocked_agent, task_id="blocked", turn_id="blocked")
    await asyncio.wait_for(entered.wait(), timeout=1)
    old_runtime = RUNTIME
    old_handler = app.handler_for(action["action_id"])
    assert manager.unload(PLUGIN_ID) is True
    assert old_runtime._closed is True
    release.set()
    await asyncio.sleep(0.01)
    assert not any(item[2] == "blocked-model" for item in outcomes)
    async def ack():
        return None

    before_unloaded = len(calls)
    await old_handler(ack, {}, {})
    assert len(calls) == before_unloaded

    manager.discover_and_load(force=True)
    replacement = manager._plugin_commands["wisdom"]["handler"].__self__
    assert replacement is not old_runtime and not replacement._closed
    manager.unload()
    runner._shutdown_executor()
    print("pinned-host native admission/lifecycle passed")


PROJECT_ROOT = Path(os.environ["WISDOM_PLUGIN_ROOT"]).resolve()
RUNTIME = None
TOOL_RESULTS = {}
asyncio.run(main())
'''


def test_pinned_host_native_admission_and_lifecycle(tmp_path: Path) -> None:
    """Use real discovery, gateway admission/dispatch, bounded hooks and ctx services."""
    with immutable_host_checkout(EXPECTED_HOST_REVISION) as agent_path:
        profile = tmp_path / ".hermes" / "profiles" / "default"
        plugins = profile / "plugins"
        plugins.mkdir(parents=True)
        (plugins / PLUGIN_ID).symlink_to(PROJECT_ROOT, target_is_directory=True)
        (profile / "config.yaml").write_text(
            f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n  hook_callback_timeout: 5\n"
            "model:\n  provider: custom:fallback\n  default: fallback-model\n"
            "fallback_providers:\n"
            "  - provider: custom:fallback\n"
            "    model: fallback-model\n"
            "    base_url: https://fallback.invalid/v1\n"
            "custom_providers:\n"
            "  - name: fallback\n"
            "    base_url: https://fallback.invalid/v1\n"
            "    api_key: fallback-key\n"
            "    model: fallback-model\n",
            encoding="utf-8",
        )
        environment = isolated_host_environment(
            home=tmp_path,
            hermes_home=profile,
            host_path=agent_path,
            python_paths=(PROJECT_ROOT, agent_path),
        )
        environment.update(
            {
                "WISDOM_PLUGIN_ROOT": str(PROJECT_ROOT),
                "HERMES_SYNC_BASE_URL": "https://gateway.invalid",
                "SLACK_ALLOWED_USERS": "U1,U2",
            }
        )
        result = run_host_python(
            ["-c", PROBE], cwd=tmp_path, environment=environment, timeout=30
        )
        assert result.stdout.strip() == "pinned-host native admission/lifecycle passed"


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_pinned_host_real_sdk_native_journey(tmp_path: Path, platform: str) -> None:
    """Dispatch the full update/setup journey through each real platform SDK."""
    with immutable_host_checkout(EXPECTED_HOST_REVISION) as agent_path:
        profile = tmp_path / platform / "profile"
        plugins = profile / "plugins"
        plugins.mkdir(parents=True)
        (plugins / PLUGIN_ID).symlink_to(PROJECT_ROOT, target_is_directory=True)
        (profile / "config.yaml").write_text(
            f"plugins:\n  enabled:\n    - {PLUGIN_ID}\n"
            "terminal:\n  backend: local\napprovals:\n  mode: manual\n",
            encoding="utf-8",
        )
        environment = isolated_host_environment(
            home=tmp_path,
            hermes_home=profile,
            host_path=agent_path,
            python_paths=(PROJECT_ROOT, agent_path),
        )
        environment.update(
            {
                "WISDOM_PLUGIN_ROOT": str(PROJECT_ROOT),
                "SLACK_ALLOWED_USERS": "U1",
                "TELEGRAM_ALLOWED_USERS": "1",
            }
        )
        probe = PROJECT_ROOT / "tests" / "native_sdk_journey_probe.py"
        result = run_host_python(
            [str(probe), platform, str(tmp_path / platform)],
            cwd=tmp_path,
            environment=environment,
            timeout=60,
        )
        assert result.stdout.strip() == f"real {platform} SDK journey passed"
