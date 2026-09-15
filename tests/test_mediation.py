"""Queued assessment generation over the durable owner-bound mediation store."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-many-locals
# pylint: disable=protected-access,too-few-public-methods,too-many-arguments
# pylint: disable=use-implicit-booleaness-not-comparison,import-outside-toplevel
# pylint: disable=unused-variable,unused-argument,invalid-name

from __future__ import annotations

import json
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_sdk_fixtures import agent_policy as _policy
from hermes_wisdom_plugin.client import WisdomMuteResponse
from hermes_wisdom_plugin.client_delivery import ClientDeliveryResponse
from hermes_wisdom_plugin.mediation import (
    Advice,
    AssessmentBridge,
    QueuedAssessment,
    conversation_context,
)
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.native_context import NativeInvocation
from hermes_wisdom_plugin.native_runtime import NativeSurfaceRuntime
from hermes_wisdom_plugin.qualification import snapshot_tree
from hermes_wisdom_plugin.store import WisdomStore


NOW = 1_000.0


def _owner(root: Path, account: str = "account-1") -> dict[str, str]:
    return {
        "profile_name": "default", "profile_home": str(root / "profile"),
        "account_id": account, "organization_id": "org-1",
        "base_url": "https://gateway.example", "installation_id": "hwi_test",
    }




class RecordingLlm:
    def __init__(self, *, fail: bool = False, mutate=None):
        self.calls = []
        self.fail = fail
        self.mutate = mutate

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.mutate:
            self.mutate()
        if self.fail:
            raise RuntimeError("provider unavailable")
        evidence = json.loads(kwargs["input"][0]["text"])["untrusted_evidence"]
        return SimpleNamespace(
            provider="active-provider", model="active-model",
            parsed={"advice": [{
                "assessment_id": item["assessment_id"],
                "relevance": "recommend", "title": "Useful workflow",
                "explanation": "Adds a distinct step to a recurring workflow.",
            } for item in evidence]},
        )


def _service(tmp_path, *, publisher="account-2"):
    store = WisdomStore(tmp_path / "state")
    store.activate_installation_identity("hwi_test", "org-1")
    owner = _owner(tmp_path)
    store.bind_qualification_owner(owner)
    client = Mock(
        expected_organization_id="org-1", display_org_id="org-1",
        owner_user_id="account-1", identity={"owner": "account-1"},
    )
    client.agent_led_policy.return_value = _policy()
    client.recommendation_mute.return_value = WisdomMuteResponse(
        org_id="org-1", muted=False, duration=None, muted_until=None,
        forever=False, revision=1,
    )
    client.recommendation_suppressions.return_value = []
    service = SimpleNamespace(store=store, client=client, config={})
    service.publisher = publisher
    service.version_override = None
    service._installation_owner = Mock(return_value=owner)
    service.check_owner = Mock(return_value={})
    def inspect(skill_id, *, version):
        actual_version = service.version_override or version
        return {
            "kind": "skill", "skill_id": skill_id, "version": actual_version,
            "facts": {
                "skill": {"id": skill_id, "slug": skill_id, "state": "active"},
                "version": {
                    "version": actual_version, "state": "published",
                    "content_hash": "sha256:" + "a" * 64,
                    "author_description": "A shared recurring workflow.",
                    "editorial_name": "Shared workflow",
                    "editorial_description": "Adds checks.",
                    "published_by_user_id": service.publisher,
                    "security_check": {"state": "pass"},
                    "professionalism_check": {"state": "pass"},
                },
            },
        }
    service.native_skill_inspect = Mock(side_effect=inspect)
    service.close = Mock()
    return service, owner


def _queue(service, owner, count=1):
    queue = MediationStore(service.store, owner=owner, clock=lambda: NOW)
    identities = [
        queue.enqueue_owned(
            "org-1", f"feed:event-{index}",
            {"kind": "skill", "event_id": f"event-{index}",
             "skill_id": f"skill-{index + 1}", "version": 1},
        )
        for index in range(count)
    ]
    return queue, identities


def _run(
    service, owner, llm, *, mode="agent", history=None, model="active-model",
    route_session="session-1",
):
    return QueuedAssessment(
        service, owner, llm, get_delivery_mode=lambda: mode,
        clock=lambda: NOW,
        runtime_main=lambda: {
            "session_id": route_session, "provider": "active-provider", "model": model,
        },
    ).run(
        session_key="local:session-1", session_id="session-1", platform="local",
        actor_id="account-1", model=model, history=history or [],
        address={"chat_id": "terminal"},
    )


def test_shared_pending_becomes_ready_with_exact_schema_route_and_bounded_context(tmp_path):
    service, owner = _service(tmp_path)
    queue, identities = _queue(service, owner)
    llm = RecordingLlm()
    history = [
        {"role": "system", "content": "secret system"},
        {"role": "tool", "content": "secret tool"},
        {"role": "user", "content": "old"},
        *({"role": "assistant" if index % 2 else "user", "content": "x" * 2000}
          for index in range(8)),
        {"role": "user", "content": "attachment", "display_kind": "attachment"},
    ]

    result = _run(service, owner, llm, history=history)

    assert result == {"claimed": 1, "ready": 1, "failed": 0, "waiting": 0, "more": False}
    row = next(item for item in queue.assessments("org-1") if item["id"] == identities[0])
    assert row["state"] == "ready"
    assert row["advice"]["provenance"] == {
        "kind": "agent_assessed", "provider": "active-provider", "model": "active-model",
    }
    call = llm.calls[0]
    assert call["task"] is None
    assert call["schema_name"] == "collective_wisdom.recommendations"
    assert call["temperature"] == 0.0 and call["max_tokens"] == 2400
    assert set(call) == {
        "instructions", "input", "json_schema", "schema_name", "purpose",
        "task", "temperature", "max_tokens", "timeout",
    }
    payload = json.loads(call["input"][0]["text"])
    assert payload["conversation_excerpts"] == conversation_context(history)
    assert len(payload["conversation_excerpts"]) == 6
    assert all(len(item["content"]) <= 1500 for item in payload["conversation_excerpts"])
    assert "secret" not in json.dumps(payload)
    assert service.native_skill_inspect.call_count >= 2


def test_ready_only_claim_preserves_fresh_session_lease_and_reclaims_expired_work(tmp_path):
    service, owner = _service(tmp_path)
    now = [NOW]
    queue = MediationStore(service.store, owner=owner, clock=lambda: now[0])
    queue.register_session(
        "org-1", session_key="native", session_id="session-1", platform="slack",
        actor_id="account-1", private=True, available=True, user_activity=True,
        address={"chat_id": "D1", "scope_id": "W1"},
    )
    first = queue.enqueue_owned(
        "org-1", "feed:first",
        {"kind": "skill", "event_id": "first", "skill_id": "skill-1", "version": 1},
    )
    job = queue.claim("org-1", "native", assessment_only=True)[0]
    assert queue.save_advice("org-1", first, job["lease_token"], {"title": "Ready"})

    fresh = queue.claim("org-1", "native", ready_only=True)
    assert [row["id"] for row in fresh] == [first]
    assert fresh[0]["lease_token"] == job["lease_token"]

    now[0] += 181
    queue.register_session(
        "org-1", session_key="native", session_id="session-1", platform="slack",
        actor_id="account-1", private=True, available=True, user_activity=True,
        address={"chat_id": "D1", "scope_id": "W1"},
    )
    reclaimed = queue.claim("org-1", "native", ready_only=True)
    assert [row["id"] for row in reclaimed] == [first]
    assert reclaimed[0]["lease_token"] != job["lease_token"]


def test_candidate_advice_is_local_and_distinct_from_consumer_recommendation(tmp_path):
    service, owner = _service(tmp_path)
    local = tmp_path / "profile" / "skills" / "incident-review"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text(
        "---\nname: incident-review\ndescription: Review incidents.\n---\n# Review\n",
        encoding="utf-8",
    )
    content_hash, tree = snapshot_tree(local)
    skill_id = service.store.register_skill(
        local, source_kind="local", content_hash=content_hash, tree=tree,
    )
    skill = service.store.local_skill(skill_id)
    assert skill is not None
    event_id = service.store.emit_local_event(
        kind="wisdom.candidate", skill_id=skill["id"], content_hash=content_hash,
        session_id="session-1", task_id="task-1", qualification="high_usage",
        payload={
            "skill_name": "incident-review", "editorial_name": "Incident Review",
            "editorial_description": "Review incidents.",
            "local_reasons": {"consecutive_business_days": 3},
            "consent_required": True, "networked": False,
        },
    )
    assert event_id is not None
    queue = MediationStore(service.store, owner=owner, clock=lambda: NOW)
    identity = queue.enqueue_owned(
        "org-1", f"candidate:{skill['id']}:{content_hash}",
        {"kind": "candidate", "local_skill_id": skill["id"],
         "content_hash": content_hash, "event_id": event_id},
    )
    llm = RecordingLlm()

    result = _run(service, owner, llm)

    row = next(item for item in queue.assessments("org-1") if item["id"] == identity)
    assert result["ready"] == 1 and llm.calls == []
    assert row["advice"] == {
        "assessment_kind": "qualification", "relevance": "recommend",
        "title": "Incident Review",
        "explanation": "You used this skill consistently across many days.",
        "provenance": {
            "kind": "local_qualification", "event_id": event_id,
            "qualification": "high_usage", "source_snapshot": "verified",
            "security_review": "manual_review_required",
            "professionalism_review": "unavailable",
        },
    }
    with service.store.transaction() as database:
        database.execute(
            "UPDATE wisdom_shared_assessment SET state='fallback',advice_json=NULL WHERE id=?",
            (identity,),
        )
    fallback = next(item for item in queue.assessments("org-1") if item["id"] == identity)
    checker = QueuedAssessment(
        service, owner, None, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
    )
    assert checker.delivery_facts(queue, "org-1", fallback) is not None
    assert llm.calls == []


def test_candidate_without_exact_event_skill_file_or_current_bytes_never_becomes_ready(tmp_path):
    service, owner = _service(tmp_path)
    local = tmp_path / "profile" / "skills" / "missing-skill"
    local.mkdir(parents=True)
    fake_hash = "sha256:" + "b" * 64
    skill_id = service.store.register_skill(
        local, source_kind="local", content_hash=fake_hash,
    )
    queue = MediationStore(service.store, owner=owner, clock=lambda: NOW)
    queue.enqueue_owned(
        "org-1", "candidate:missing",
        {"kind": "candidate", "local_skill_id": skill_id, "content_hash": fake_hash},
    )

    result = _run(service, owner, RecordingLlm())

    assert result["ready"] == 0 and result["failed"] == 1
    assert queue.assessments("org-1")[0]["state"] == "pending"


@pytest.mark.parametrize("mutation", ["removed", "changed"])
def test_candidate_rechecks_current_source_bytes_before_ready(tmp_path, mutation):
    service, owner = _service(tmp_path)
    local = tmp_path / "profile" / "skills" / "current-source"
    local.mkdir(parents=True)
    skill_file = local / "SKILL.md"
    skill_file.write_text("---\nname: current-source\n---\n# Current\n", encoding="utf-8")
    content_hash, tree = snapshot_tree(local)
    skill_id = service.store.register_skill(
        local, source_kind="local", content_hash=content_hash, tree=tree,
    )
    event_id = service.store.emit_local_event(
        kind="wisdom.candidate", skill_id=skill_id, content_hash=content_hash,
        session_id="session-1", task_id="task-1", qualification="refinement",
        payload={
            "skill_name": "current-source", "local_reasons": {"refinements": 2},
            "consent_required": True, "networked": False,
        },
    )
    queue = MediationStore(service.store, owner=owner, clock=lambda: NOW)
    queue.enqueue_owned(
        "org-1", f"candidate:{mutation}",
        {"kind": "candidate", "event_id": event_id, "local_skill_id": skill_id,
         "content_hash": content_hash},
    )
    if mutation == "removed":
        skill_file.unlink()
    else:
        skill_file.write_text("---\nname: current-source\n---\n# Changed\n", encoding="utf-8")

    result = _run(service, owner, RecordingLlm())

    assert result["ready"] == 0 and result["failed"] == 1
    assert queue.assessments("org-1")[0]["state"] == "pending"


@pytest.mark.parametrize("denial", ["fixed", "muted", "suppressed", "policy", "self"])
def test_current_preferences_and_self_publisher_block_model_without_spending_attempt(
    tmp_path, denial
):
    service, owner = _service(tmp_path, publisher="account-1" if denial == "self" else "account-2")
    queue, _identities = _queue(service, owner)
    if denial == "muted":
        service.client.recommendation_mute.return_value = WisdomMuteResponse(
            org_id="org-1", muted=True, duration="forever", muted_until=None,
            forever=True, revision=1,
        )
    elif denial == "suppressed":
        from hermes_wisdom_plugin.client import WisdomSuppression
        from hermes_wisdom_plugin.preferences import suppression_key
        service.client.recommendation_suppressions.return_value = [WisdomSuppression(
            key=suppression_key({"kind": "skill", "skill_id": "skill-1", "version": 1}),
            suppress_until="2099-01-01T00:00:00Z",
        )]
    elif denial == "policy":
        service.client.agent_led_policy.return_value = _policy(shared=False)
    llm = RecordingLlm()

    _run(service, owner, llm, mode="fixed" if denial == "fixed" else "agent")

    row = queue.assessments("org-1")[0]
    assert llm.calls == []
    assert row["attempts"] == 0
    assert row["state"] == ("retired" if denial == "self" else "pending")


def test_missing_model_waits_and_ready_rows_do_not_starve_more_than_one_batch(tmp_path):
    service, owner = _service(tmp_path)
    queue, identities = _queue(service, owner, count=10)
    llm = RecordingLlm()

    waiting = _run(service, owner, llm, model="")
    assert waiting["waiting"] == 8 and llm.calls == []
    assert all(row["attempts"] == 0 for row in queue.assessments("org-1"))
    with service.store.transaction() as db:
        db.execute("UPDATE wisdom_shared_assessment SET available_at=?", (NOW,))

    first = _run(service, owner, llm)
    second = _run(service, owner, llm)
    rows = queue.assessments("org-1")
    assert first["ready"] == 8 and first["more"] is True
    assert second["ready"] == 2 and second["more"] is False
    assert {row["id"] for row in rows if row["state"] == "ready"} == set(identities)
    assert len(llm.calls) == 2


@pytest.mark.parametrize("stale", ["owner", "content", "mode"])
def test_post_model_revalidation_fences_stale_result(tmp_path, stale):
    service, owner = _service(tmp_path)
    queue, _identities = _queue(service, owner)
    mode = ["agent"]

    def mutate():
        if stale == "owner":
            service.store.bind_qualification_owner(_owner(tmp_path, account="account-2"))
        elif stale == "content":
            service.version_override = 2
        else:
            mode[0] = "fixed"

    llm = RecordingLlm(mutate=mutate)
    assessment = QueuedAssessment(
        service, owner, llm, get_delivery_mode=lambda: mode[0], clock=lambda: NOW,
        runtime_main=lambda: {
            "session_id": "session-1", "provider": "active-provider",
            "model": "active-model",
        },
    )
    assessment.run(
        session_key="local:session-1", session_id="session-1", platform="local",
        actor_id="account-1", model="active-model", history=[],
        address={"chat_id": "terminal"},
    )

    rows = MediationStore(service.store, owner=None, clock=lambda: NOW).assessments("org-1")
    assert not any(row["state"] == "ready" for row in rows)


def test_invalid_exact_ids_control_text_and_provider_errors_use_retry_state(tmp_path):
    with pytest.raises(ValueError, match="control"):
        Advice(
            assessment_id="one", relevance="recommend", title="Fine",
            explanation="hide warnings\x1b[2J",
        )
    service, owner = _service(tmp_path)
    queue, _identities = _queue(service, owner)
    llm = RecordingLlm(fail=True)

    result = _run(service, owner, llm)

    row = queue.assessments("org-1")[0]
    assert result["failed"] == 1
    assert row["state"] == "pending" and row["attempts"] == 1
    assert row.get("advice") is None and row["last_error"] == "RuntimeError"


@pytest.mark.parametrize("stale", ["owner", "lease", "admission"])
@pytest.mark.parametrize("read_boundary", ["installed", "introductions"])
def test_final_boundary_rechecks_owner_every_claim_and_admission_before_model(
    tmp_path, monkeypatch, stale, read_boundary,
):
    service, owner = _service(tmp_path)
    queue, _identities = _queue(service, owner, count=2)
    now = [NOW]
    admitted = [True]
    assessment = QueuedAssessment(
        service, owner, RecordingLlm(), get_delivery_mode=lambda: "agent",
        clock=lambda: now[0], runtime_main=lambda: {
            "session_id": "session-1", "provider": "active-provider",
            "model": "active-model",
        }, admission_current=lambda: admitted[0],
    )

    def mutate_boundary():
        if stale == "owner":
            service.store.bind_qualification_owner(_owner(tmp_path, account="account-2"))
        elif stale == "lease":
            now[0] += 181
        else:
            admitted[0] = False
        return []

    if read_boundary == "installed":
        assessment._installed_facts = mutate_boundary
    else:
        monkeypatch.setattr(MediationStore, "introduced", lambda _queue, _org: mutate_boundary())
    result = assessment.run(
        session_key="local:session-1", session_id="session-1", platform="local",
        actor_id="account-1", model="active-model", history=[],
        address={"chat_id": "terminal"},
    )

    assert assessment.llm.calls == []
    assert result["ready"] == 0


@pytest.mark.parametrize(
    "route_session,route_model",
    [(None, None), ("other", "active-model"), ("session-1", "other")],
)
def test_missing_or_different_active_main_runtime_waits_without_transport(
    tmp_path, route_session, route_model
):
    service, owner = _service(tmp_path)
    queue, _identities = _queue(service, owner)
    llm = RecordingLlm()
    runtime = None if route_session is None else {
        "session_id": route_session, "provider": "active-provider", "model": route_model,
    }
    result = QueuedAssessment(
        service, owner, llm, get_delivery_mode=lambda: "agent", clock=lambda: NOW,
        runtime_main=lambda: runtime,
    ).run(
        session_key="local:session-1", session_id="session-1", platform="local",
        actor_id="account-1", model="active-model", history=[],
        address={"chat_id": "terminal"},
    )

    assert llm.calls == [] and result["waiting"] == 1
    row = queue.assessments("org-1")[0]
    assert row["state"] == "pending" and row["attempts"] == 0


def test_retry_exhaustion_never_makes_a_fourth_model_call(tmp_path):
    service, owner = _service(tmp_path)
    queue, _identities = _queue(service, owner)
    llm = RecordingLlm(fail=True)

    for _index in range(3):
        _run(service, owner, llm)
        with service.store.transaction() as db:
            db.execute("UPDATE wisdom_shared_assessment SET available_at=?", (NOW,))
    fourth = _run(service, owner, llm)

    assert len(llm.calls) == 3 and fourth["ready"] == 0
    assert queue.assessments("org-1")[0]["state"] == "fallback"


@pytest.mark.parametrize("corrupt", ["missing", "foreign", "duplicate", "control"])
def test_structured_result_requires_exact_unique_ids_and_safe_bounded_text(tmp_path, corrupt):
    service, owner = _service(tmp_path)
    queue, identities = _queue(service, owner)

    class CorruptLlm(RecordingLlm):
        def complete_structured(self, **kwargs):
            result = super().complete_structured(**kwargs)
            advice = result.parsed["advice"]
            if corrupt == "missing":
                advice.clear()
            elif corrupt == "foreign":
                advice[0]["assessment_id"] = "foreign"
            elif corrupt == "duplicate":
                advice.append(dict(advice[0]))
            else:
                advice[0]["title"] = "bad\u0007title"
            return result

    result = _run(service, owner, CorruptLlm())

    row = next(item for item in queue.assessments("org-1") if item["id"] == identities[0])
    assert result["failed"] == 1
    assert row["state"] == "pending" and row.get("advice") is None


def test_conversation_context_is_role_filtered_tail_bounded_and_deterministic():
    history = (
        [{"role": "system", "content": "never"}, {"role": "tool", "content": "never"}]
        + [{"role": "user", "content": f"u-{index}-" + "x" * 1000} for index in range(100)]
        + [{"role": "assistant", "content": "a-" + "y" * 10_000}]
    )

    first = conversation_context(history)

    assert first == conversation_context(history)
    assert len(first) <= 24
    assert sum(len(item["content"]) for item in first) <= 24_000
    assert {item["role"] for item in first} <= {"user", "assistant"}
    assert first[-1]["content"] == "a-" + "y" * 1498


def test_bridge_rejects_child_shared_and_forged_native_context_before_service_or_model(tmp_path):
    llm = RecordingLlm()
    factory = Mock(side_effect=AssertionError("denied context opened service"))
    bridge = AssessmentBridge(
        factory, llm, get_delivery_mode=lambda: "agent", profile_name="default",
    )
    native = Mock()
    native.tool_invocation.side_effect = PermissionError("forged")

    for facts in (
        {"platform": "cli", "parent_session_id": "parent", "session_id": "s",
         "task_id": "t", "turn_id": "u", "model": "m"},
        {"platform": "discord", "parent_session_id": "", "session_id": "s",
         "task_id": "t", "turn_id": "u", "model": "m"},
        {"platform": "slack", "parent_session_id": "", "session_id": "s",
         "task_id": "t", "turn_id": "u", "model": "m"},
    ):
        bridge.run(native_runtime=native, **facts)
    bridge.close()
    bridge.run(
        platform="cli", parent_session_id="", session_id="s",
        task_id="t", turn_id="u", model="m",
    )

    assert factory.call_count == 0 and llm.calls == []


def test_bridge_schedules_native_drain_even_when_generation_is_unavailable(tmp_path, monkeypatch):
    service, owner = _service(tmp_path)
    _queue(service, owner)
    invocation = SimpleNamespace(
        profile="default", session_key="slack:W1:D1:U1", actor_id="U1",
        platform="slack", chat_id="D1", thread_id="", scope_id="W1",
    )
    native = Mock()
    native.tool_invocation.return_value = invocation
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(
        _RUNTIME_MAIN_CONTEXT=SimpleNamespace(get=lambda: {
            "session_id": "session-1", "provider": "active-provider",
            "model": "active-model",
        })
    ))
    bridge = AssessmentBridge(
        lambda: service, RecordingLlm(), get_delivery_mode=lambda: "agent",
        profile_name="default",
    )

    bridge.run(
        native_runtime=native, platform="slack", parent_session_id="",
        session_id="session-1", task_id="task-1", turn_id="turn-1",
        model="", conversation_history=[],
    )

    assert native.queue_assessment_delivery.call_count == 1
    call = native.queue_assessment_delivery.call_args
    assert call.args[:2] == (invocation, owner)
    assert call.kwargs["get_delivery_mode"]() == "agent"
    assert call.kwargs["admission_current"]() is True


def test_native_human_turn_produces_assesses_and_delivers_due_feed_once(
    tmp_path, monkeypatch
):
    service, owner = _service(tmp_path)
    service.profile = ("default", tmp_path / "profile")
    service.notification_config = Mock(return_value={
        "notifications": {"delivery_mode": "agent"}
    })
    service.client.recommendation_suppressions.return_value = []

    class DueConsumption:
        def __init__(self):
            self.ready = False
            self.polls = 0

        def poll_feed(self):
            self.polls += 1
            return {"inserted": 1, "pages": 1, "cursor": "cursor-1"}

        def refresh_metadata(self):
            self.ready = True
            return {"enriched": 1, "remaining": 0}

        def notifications(self, **_kwargs):
            assert self.ready
            return {"events": [{
                "event_id": "feed-1", "category": "new_skill", "kind": "new",
                "skill_id": "skill-1", "skill_name": "Shared workflow", "version": 1,
                "occurred_at": "2026-09-15T00:00:00Z",
            }], "more": False}

    consumption = DueConsumption()
    service._consumption = Mock(return_value=consumption)

    def delivery_response(request_id, reference, *, state="claimed"):
        return ClientDeliveryResponse(
            org_id="org-1", recipient_user_id="account-1", event_id="feed-1",
            request_id=request_id, reference=reference, state=state, reason=None,
            lease_until=(datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + 120, timezone.utc
            ).isoformat().replace("+00:00", "Z")) if state == "claimed" else None,
        )

    service.client.claim_notification_delivery.side_effect = delivery_response
    service.client.settle_notification_delivery.side_effect = (
        lambda event_id, request_id, reference, *, outcome, receipt: delivery_response(
            request_id, reference, state=outcome
        )
    )
    llm = RecordingLlm()
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(
        _RUNTIME_MAIN_CONTEXT=SimpleNamespace(get=lambda: {
            "session_id": "session-1", "provider": "active-provider",
            "model": "active-model",
        })
    ))

    class SlackClient:
        def __init__(self):
            self.posts = []

        async def chat_postMessage(self, **payload):
            self.posts.append(payload)
            return {"ok": True, "channel": payload["channel"], "ts": "advice-1"}

    class SlackApp:
        def __init__(self):
            self.client = SlackClient()

        def action(self, _pattern):
            return lambda callback: callback

    class SlackAdapter:
        _owner_profile = "default"

    async def scenario():
        tasks = []
        runtime = NativeSurfaceRuntime(
            tmp_path / "state", "default", lambda: service, llm,
            lambda coro, **_kwargs: tasks.append(asyncio.create_task(coro)),
        )
        app = SlackApp()
        adapter = SlackAdapter()
        runtime.wire_slack(app, adapter)
        invocation = NativeInvocation(
            platform="slack", profile="default", chat_type="dm", chat_id="D1",
            actor_id="account-1", actor_name="Shannon", thread_id="", scope_id="W1",
            message_id="request-1", session_key="slack:W1:D1:account-1",
            adapter=adapter, host_session_id="session-1", task_id="task-1",
            turn_id="turn-1",
        )
        runtime.tool_invocation = lambda **_kwargs: invocation
        bridge = AssessmentBridge(
            lambda: service, llm, get_delivery_mode=lambda: "agent",
            profile_name="default",
        )

        bridge.run(
            native_runtime=runtime, platform="slack", parent_session_id="",
            session_id="session-1", task_id="task-1", turn_id="turn-1",
            sender_id="account-1", model="active-model", conversation_history=[],
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        restarted = AssessmentBridge(
            lambda: service, llm, get_delivery_mode=lambda: "agent",
            profile_name="default",
        )
        restarted.run(
            native_runtime=runtime, platform="slack", parent_session_id="",
            session_id="session-1", task_id="task-2", turn_id="turn-2",
            sender_id="account-1", model="active-model", conversation_history=[],
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        return app

    app = asyncio.run(scenario())

    rows = MediationStore(service.store, owner=owner).assessments("org-1")
    assert consumption.polls == 1
    assert len(llm.calls) == 1
    assert len(rows) == 1 and rows[0]["state"] == "delivered", (
        rows, app.client.posts, service.client.method_calls
    )
    assert len(app.client.posts) == 1


def test_local_pre_llm_facts_do_not_produce_without_human_origin_proof(tmp_path):
    service, _owner_value = _service(tmp_path)
    service._consumption = Mock(
        side_effect=AssertionError("unproven local origin started cadence")
    )
    bridge = AssessmentBridge(
        lambda: service, RecordingLlm(), get_delivery_mode=lambda: "agent",
        profile_name="default",
    )

    bridge.run(
        platform="cli", parent_session_id="", session_id="session-1",
        task_id="task-1", turn_id="turn-1", model="active-model",
        conversation_history=[],
    )

    service._consumption.assert_not_called()
    service.client.agent_led_policy.assert_not_called()


@pytest.mark.parametrize("boundary", ["consumption", "owner_read"])
def test_native_cadence_unload_during_last_read_denies_network(tmp_path, boundary):
    service, owner = _service(tmp_path)
    bridge = AssessmentBridge(
        lambda: service, RecordingLlm(), get_delivery_mode=lambda: "agent",
        profile_name="default",
    )
    consumption = Mock()
    if boundary == "consumption":
        def consumption_reader():
            bridge.close()
            return consumption
        service._consumption = consumption_reader
    else:
        def owner_reader():
            bridge.close()
            return owner
        service._installation_owner = owner_reader
        service._consumption = Mock(return_value=consumption)
    queue = MediationStore(service.store, owner=owner)
    bridge._produce_due(
        service, owner, queue, lambda: True, native_admitted=True,
    )
    consumption.poll_feed.assert_not_called()
    if boundary == "owner_read":
        service.client.agent_led_policy.assert_not_called()


def test_failed_native_refresh_uses_durable_cooldown_across_bridge_restart(tmp_path):
    service, _owner_value = _service(tmp_path)
    service.profile = ("default", tmp_path / "profile")
    service.notification_config = Mock(return_value={
        "notifications": {"delivery_mode": "agent"}
    })
    consumption = Mock()
    consumption.poll_feed.side_effect = ConnectionError("fixture gateway unavailable")
    service._consumption = Mock(return_value=consumption)
    invocation = SimpleNamespace(
        profile="default", session_key="slack:W1:D1:account-1",
        actor_id="account-1", platform="slack", chat_id="D1",
        thread_id="", scope_id="W1",
    )
    native = Mock()
    native.tool_invocation.return_value = invocation

    for task_id in ("task-1", "task-2"):
        AssessmentBridge(
            lambda: service, RecordingLlm(), get_delivery_mode=lambda: "agent",
            profile_name="default",
        ).run(
            native_runtime=native, platform="slack", parent_session_id="",
            session_id="session-1", task_id=task_id, turn_id=task_id,
            sender_id="account-1", model="active-model", conversation_history=[],
        )

    assert consumption.poll_feed.call_count == 1
    assert service.client.agent_led_policy.call_count == 1
