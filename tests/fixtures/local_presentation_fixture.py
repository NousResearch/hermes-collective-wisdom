"""Shared isolated host discovery, Gateway feed, and model recording fixture."""
# pylint: disable=import-outside-toplevel,too-many-locals,too-many-statements
# pylint: disable=invalid-name,protected-access,missing-function-docstring
# pylint: disable=missing-class-docstring,too-few-public-methods,unused-import
# Host-only imports run in the configured subprocess; locals are exported to probes.
# Recording transport response shapes intentionally match independent unit fixtures.
# pylint: disable=import-error,possibly-unused-variable,duplicate-code
from contextlib import contextmanager


@contextmanager
def discovered_wisdom_fixture():
    """Load the real plugin with disposable state and no network clients."""

    import asyncio
    import importlib
    import json
    import os
    import time
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    from hermes_cli import plugins
    from hermes_cli import plugins_presentation as presentation
    from agent.auxiliary_client import reset_runtime_main, set_runtime_main

    PLUGIN_ID = "hermes-collective-wisdom"
    AGENT_ROOT = Path(os.environ["WISDOM_PRESENTATION_AGENT_DIR"]).resolve()
    HOME = Path(os.environ["HERMES_HOME"]).resolve()
    SURFACE_ID = os.environ["WISDOM_PRESENTATION_SURFACE_ID"]

    assert Path(plugins.__file__).resolve().is_relative_to(AGENT_ROOT)
    assert Path(presentation.__file__).resolve().is_relative_to(AGENT_ROOT)

    manager = plugins.PluginManager()
    service_opens = []
    model_calls = []
    fixture_calls = []
    rendered = []
    try:
        manager.discover_and_load()
        loaded = manager._plugins["hermes-collective-wisdom"]
        assert loaded.enabled is True
        runtime = manager._plugin_commands["wisdom"]["handler"].__self__
        package = runtime.__class__.__module__.rsplit(".", 1)[0]
        store_module = importlib.import_module(f"{package}.store")
        bridge_module = importlib.import_module(f"{package}.mediation")
        mediation_module = importlib.import_module(f"{package}.mediation_store")
        delivery_module = importlib.import_module(f"{package}.client_delivery")

        owner = {
            "profile_name": "default",
            "profile_home": str(HOME),
            "account_id": "member",
            "organization_id": "org",
            "base_url": "https://fixture-gateway.invalid",
            "installation_id": "install",
        }
        store = store_module.WisdomStore(HOME / "wisdom-state")
        store.activate_installation_identity("install", "org")
        store.bind_qualification_owner(owner)

        client_module = importlib.import_module(f"{package}.client")
        consumption_module = importlib.import_module(f"{package}.consumption")

        class FixtureGatewayClient:
            """Synthetic Gateway responses; no network or live credentials are used."""

            identity = {"owner": "member"}
            owner_user_id = "member"
            expected_organization_id = "org"
            display_org_id = "org"
            base_url = "https://fixture-gateway.invalid"

            @staticmethod
            def agent_led_policy():
                fixture_calls.append("policy")
                return client_module.AgentLedPolicyResponse(
                    org_id="org", usage_evidence_window_days=7,
                    min_aggregate_invocations=3, consecutive_day_usage_counts=True,
                    repeated_edits_count=True, max_recommendations_per_user_per_week=0,
                    publication_mode="moderated", install_popularity_threshold=2,
                    notification_defaults=client_module.AgentLedNotificationDefaults(
                        skill_ready_to_share=True, teammate_published=True,
                        update_available=True,
                    ),
                    manager_review_email_cadence="immediate",
                    not_now_suppression_days=7, version=1,
                    updated_by_user_id="manager",
                )

            @staticmethod
            def recommendation_mute():
                return SimpleNamespace(org_id="org", muted=False)

            @staticmethod
            def recommendation_suppressions(_keys):
                return []

            @staticmethod
            def feed(cursor, *, installation_id):
                fixture_calls.append("feed")
                assert installation_id == "install"
                assert cursor is None
                return client_module.Feed(events=[client_module.FeedEvent(
                    event_id="event-1", kind="new", skill_id="skill-1", version=1,
                    takedown_generation=0, installation_id=None, update_mode=None,
                    occurred_at=datetime.now(timezone.utc),
                )], next_cursor="done", has_more=False)

            @staticmethod
            def version(skill_id, version):
                fixture_calls.append("version")
                assert (skill_id, version) == ("skill-1", 1)
                return SimpleNamespace(
                    skill={"id": skill_id, "slug": "shared", "state": "active"},
                    version={"version": version, "state": "published",
                             "published_by_user_id": "other-member"},
                )

            @staticmethod
            def claim_notification_delivery(request_id, reference):
                return delivery_module.ClientDeliveryResponse(
                    org_id="org",
                    recipient_user_id="member",
                    event_id="event-1",
                    request_id=request_id,
                    reference=reference,
                    state="claimed",
                    reason=None,
                    lease_until=datetime.fromtimestamp(
                        time.time() + 120, timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                )

            @staticmethod
            def settle_notification_delivery(
                event_id, request_id, reference, *, outcome, receipt,
            ):
                del event_id, receipt
                return delivery_module.ClientDeliveryResponse(
                    org_id="org",
                    recipient_user_id="member",
                    event_id="event-1",
                    request_id=request_id,
                    reference=reference,
                    state=outcome,
                    reason=None,
                    lease_until=None,
                )

        class FixtureService:
            client = FixtureGatewayClient()
            config = {}
            profile = ("default", HOME)


            def __init__(self):
                self.store = store
                service_opens.append(self)

            @staticmethod
            def _installation_owner():
                return dict(owner)

            native_owner = _installation_owner

            @staticmethod
            def check_owner():
                fixture_calls.append("owner")
                assert store.get_metadata("qualification_owner") == owner

            @staticmethod
            def notification_config():
                return {"notifications": {"delivery_mode": "agent", "new_skills": "immediate"}}

            def _consumption(self):
                fixture_calls.append("consumption")
                return consumption_module.WisdomConsumption(
                    self.store, self.client, config=self.notification_config()
                )

            @staticmethod
            def native_skill_inspect(skill_id, *, version):
                fixture_calls.append("inspect")
                return {
                    "kind": "skill",
                    "skill_id": skill_id,
                    "version": version,
                    "facts": {
                        "skill": {"id": skill_id, "slug": "shared", "state": "active"},
                        "version": {
                            "version": version,
                            "state": "published",
                            "content_hash": "sha256:" + "a" * 64,
                            "editorial_name": "Shared workflow",
                            "published_by_user_id": "other-member",
                        },
                    },
                }

            @staticmethod
            def close():
                return None

        runtime.native.service_factory = FixtureService
        runtime._service = FixtureService


        def complete_structured(**kwargs):
            model_calls.append(kwargs)
            import json
            evidence = json.loads(kwargs["input"][0]["text"])["untrusted_evidence"]
            return SimpleNamespace(
                provider="fixture-provider", model="fixture-model",
                parsed={"advice": [{
                    "assessment_id": item["assessment_id"],
                    "relevance": "recommend", "title": "Shared workflow",
                    "explanation": "Useful for recurring reviews.",
                } for item in evidence]},
            )

        runtime.native.llm = SimpleNamespace(complete_structured=complete_structured)
        pre_llm = manager._hooks["pre_llm_call"][0]
        closure = dict(zip(
            pre_llm.__code__.co_freevars,
            (cell.cell_contents for cell in pre_llm.__closure__),
        ))
        assessment = closure["assessment"]
        assessment.llm = runtime.native.llm
        assessment.service_factory = FixtureService
        assert not service_opens  # discovery alone is inert
        queue = mediation_module.MediationStore(store, owner=owner)
        assert queue.assessments("org") == []

        def follow_inspection_handoff(surface, session_id):
            """Follow exact advice through the real hook and scoped tool dispatcher."""
            from agent.turn_context import _collect_pre_llm_call_context
            from tools.registry import registry
            agent = SimpleNamespace(
                _persist_disabled=False, session_id=session_id, model="fixture-model",
                platform=surface, _parent_session_id="", _user_id="member",
            )
            token = set_runtime_main("fixture-provider", "fixture-model", session_id=session_id)
            installation = owner["installation_id"]
            try:
                _collect_pre_llm_call_context(
                    agent, effective_task_id=session_id, turn_id="inspection-turn",
                    original_user_message="inspect the suggested version",
                    messages=[], conversation_history=[],
                )
                args = {"kind": "skill", "identity": "skill-1", "version": 1}
                result = json.loads(registry.dispatch(
                    "wisdom_inspect", args, scope=manager.scope_key,
                    task_id=session_id, session_id=session_id,
                ))
                assert result["skill_id"] == "skill-1" and result["version"] == 1, result
                reads = fixture_calls.count("inspect")
                owner["installation_id"] = "replacement-installation"
                denied = json.loads(registry.dispatch(
                    "wisdom_inspect", args, scope=manager.scope_key,
                    task_id=session_id, session_id=session_id,
                ))
                assert "error" in denied, denied
                assert fixture_calls.count("inspect") == reads
            finally:
                owner["installation_id"] = installation
                reset_runtime_main(token)

        yield locals()
    finally:
        manager.unload()
