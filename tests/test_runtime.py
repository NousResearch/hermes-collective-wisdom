"""Registration ownership, profile fencing and cancellable slash command budgets."""

# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

import asyncio
import os
import threading
import time
from unittest.mock import Mock

import pytest

from test_native_review import invocation as _inbox_invocation

from hermes_wisdom_plugin import runtime
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomError
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.store import WisdomStore


INBOX_OWNER = {
    "profile_name": "default", "profile_home": "/profiles/default",
    "account_id": "account-1", "organization_id": "org-1",
    "base_url": "https://gateway.invalid", "installation_id": "installation-1",
}


class _InboxReviewService:
    @staticmethod
    def native_owner():
        return dict(INBOX_OWNER)


def test_service_refuses_profile_drift_before_authentication(tmp_path, monkeypatch):
    """A cached registration cannot follow a new profile's mutable host data root."""
    service = runtime.InvocationService(tmp_path / "state", ("original", tmp_path))
    monkeypatch.setattr(runtime, "current_profile", lambda: ("other", tmp_path / "other"))
    with pytest.raises(WisdomAuthError, match="profile changed"):
        service.check_owner()
    assert not (tmp_path / "state").exists()


def test_status_without_setup_does_not_create_storage(tmp_path, monkeypatch):
    """Status is not an enrolment shortcut."""
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    monkeypatch.setattr(service, "check_owner", lambda: None)
    assert service.status() == {
        "configured": False, "state": "not_configured", "reason": "not_configured"
    }
    assert not service.root.exists()
    with pytest.raises(WisdomError) as exc_info:
        service.require_setup()
    assert exc_info.value.code == "setup_required"
    assert not service.root.exists()


def test_worker_deadline_kills_stalled_request_and_reaps_process(tmp_path, monkeypatch):
    """The overall command deadline applies even when transport timeouts cannot help."""
    monkeypatch.setattr(runtime, "SLASH_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(
        tmp_path, ("default", tmp_path), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    start = time.monotonic()
    result = owner._run_worker("browse", {}, threading.Event())  # pylint: disable=protected-access
    assert "timed out" in result
    assert time.monotonic() - start < 2
    assert not owner._children  # pylint: disable=protected-access


def test_worker_inherits_captured_profile_without_mutating_host_env(tmp_path, monkeypatch):
    """A host ContextVar profile override must survive the worker boundary."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "ambient"))
    monkeypatch.setattr(runtime, "_BOOTSTRAP", (
        "import json,os,sys;sys.stdin.read();print(json.dumps(os.environ['HERMES_HOME']))"
    ))
    owner = runtime.CommandRuntime(
        tmp_path / "state", ("custom", tmp_path / "captured"), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    result = owner._run_worker("status", {}, threading.Event())  # pylint: disable=protected-access
    assert result == str(tmp_path / "captured")
    assert os.environ["HERMES_HOME"] == str(tmp_path / "ambient")


def test_unload_terminates_owned_worker_and_prevents_reuse(tmp_path, monkeypatch):
    """Unload has no unbounded network or thread tail."""
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(
        tmp_path, ("default", tmp_path), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    thread = threading.Thread(target=owner._run_worker, args=(  # pylint: disable=protected-access
        "browse", {}, threading.Event()
    ))
    thread.start()
    limit = time.monotonic() + 2
    while not owner._children and time.monotonic() < limit:  # pylint: disable=protected-access
        time.sleep(0.01)
    assert owner._children  # pylint: disable=protected-access
    owner.close()
    thread.join(2)
    assert not thread.is_alive()
    assert not owner._children  # pylint: disable=protected-access
    with pytest.raises(WisdomError, match="unloaded"):
        owner._service()  # pylint: disable=protected-access


def test_slash_cancellation_reaps_worker_before_return(tmp_path, monkeypatch):
    """Cancelling an async slash invocation cannot leave its worker behind."""
    monkeypatch.setattr(runtime, "_BOOTSTRAP", "import sys,time;sys.stdin.read();time.sleep(60)")
    owner = runtime.CommandRuntime(
        tmp_path, ("default", tmp_path), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(owner, "_service", lambda: Mock(
        binding=lambda: {}, check_owner=lambda: None, close=lambda: None
    ))

    async def exercise():
        task = asyncio.create_task(owner.slash("browse"))
        for _ in range(100):
            if owner._children:  # pylint: disable=protected-access
                break
            await asyncio.sleep(0.01)
        assert owner._children  # pylint: disable=protected-access
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner._children  # pylint: disable=protected-access

    asyncio.run(exercise())


def test_slash_routes_only_explicit_settings_to_native_entrypoint(tmp_path, monkeypatch):
    owner = runtime.CommandRuntime(
        tmp_path, ("default", tmp_path), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    route = object()
    calls = []
    monkeypatch.setattr(
        owner.native, "consume_request",
        lambda raw: route if raw == "settings" else None,
    )

    async def begin_settings(invocation):
        calls.append(invocation)
        return "settings-card"

    monkeypatch.setattr(owner.native, "begin_settings", begin_settings)
    monkeypatch.setattr(owner, "_slash", lambda raw: asyncio.sleep(0, result=f"safe:{raw}"))

    assert asyncio.run(owner.slash("settings")) == "settings-card"
    assert asyncio.run(owner.slash("")) == "safe:"
    assert calls == [route]


def test_native_update_begin_rejects_empty_subject_without_settings_fallback(
    tmp_path, monkeypatch
):
    owner = runtime.CommandRuntime(
        tmp_path, ("default", tmp_path), llm=object(),
        spawn_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(owner.native, "_assert_active_adapter", lambda _route: None)
    with pytest.raises(ValueError, match="skill id"):
        asyncio.run(owner.native.begin(object(), ""))


def test_notification_mode_setter_absence_or_managed_failure_never_changes_snapshot(
    tmp_path, monkeypatch
):
    live = {"notifications.delivery_mode": "agent"}
    owner = {"organization_id": "org"}
    service = runtime.InvocationService(
        tmp_path, ("default", tmp_path),
        get_config=lambda key, default=None: live.get(key, default),
    )
    monkeypatch.setattr(service, "native_owner", lambda: dict(owner))
    assert service.notification_mode_writable() is False
    with pytest.raises(PermissionError, match="read-only"):
        service.set_notification_mode("fixed")
    assert service.notification_mode() == "agent"

    managed = runtime.InvocationService(
        tmp_path, ("default", tmp_path),
        get_config=lambda key, default=None: live.get(key, default),
        set_config=lambda _key, _value: (_ for _ in ()).throw(
            PermissionError("managed setting")
        ),
    )
    monkeypatch.setattr(managed, "native_owner", lambda: dict(owner))
    assert managed.notification_mode_writable() is True
    with pytest.raises(PermissionError, match="managed setting"):
        managed.set_notification_mode("fixed")
    assert live == {"notifications.delivery_mode": "agent"}


def test_invocation_service_reads_live_notification_config_for_inbox(
    tmp_path, monkeypatch
):
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    store.bind_qualification_owner(INBOX_OWNER)
    queue = MediationStore(store, owner=INBOX_OWNER)
    queue.enqueue_owned(
        "org-1", "feed:proactive",
        {"kind": "skill", "skill_id": "proactive", "version": 1},
    )
    requested_id = queue.enqueue_owned(
        "org-1", "candidate:requested",
        {"kind": "candidate", "event_id": "requested", "user_requested": True},
    )
    live = {"notifications.delivery_mode": "fixed"}
    service = runtime.InvocationService(
        tmp_path / "wisdom", ("default", tmp_path),
        config={"notifications": {"delivery_mode": "agent"}},
        get_config=lambda key, default=None: live.get(key, default),
    )
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(INBOX_OWNER))

    result = service.inbox()

    assert result["mode"] == "fixed"
    assert [row["id"] for row in result["assessments"]] == [requested_id]


def test_installed_setup_methods_share_one_owner_bound_controller(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))

    class Controller:
        def inspect(self, skill_id, *, version=None):
            return ("inspect", skill_id, version)

        def plan(self, skill_id, **values):
            return ("plan", skill_id, values)

        def apply(self, receipt, *, may_apply=None):
            assert may_apply is None or may_apply() is True
            return ("apply", receipt)

        def status(self, skill_id, *, version=None):
            return ("status", skill_id, version)

        def recovery_status(self, skill_id, operation_id):
            return ("recovery-status", skill_id, operation_id)

        def recover(self, skill_id, operation_id, **values):
            return ("recover", skill_id, operation_id, values)

    controller = Controller()
    monkeypatch.setattr(service, "_setup_services", lambda: controller)
    monkeypatch.setattr(service, "check_owner", lambda: None)
    assert service.installed_setup_inspect("skill", version=1) == ("inspect", "skill", 1)
    assert service.installed_setup_plan(
        "skill", version=1, phase="setup", index=0, command="printf x"
    )[0] == "plan"
    assert service.installed_setup_apply("receipt") == ("apply", "receipt")
    assert service.installed_setup_status("skill") == ("status", "skill", None)
    snapshot = service.installed_setup_recovery_inspect("skill", "operation")
    assert snapshot == ("recovery-status", "skill", "operation")
    recovered = service.installed_setup_recover(
        "skill", "operation", acknowledged=True,
        expected_recovery=snapshot, may_apply=lambda: True,
    )
    assert recovered[:-1] == ("recover", "skill", "operation")
    assert recovered[-1]["acknowledged_effects_checked_and_stopped"] is True
    assert recovered[-1]["expected_recovery"] == snapshot
    assert recovered[-1]["may_apply"]() is True


def test_install_apply_propagates_optional_invocation_authority(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    captured = []

    class Executor:
        def apply(self, receipt, accept_partial=False, *, may_apply=None):
            captured.append((receipt, accept_partial, may_apply))
            if may_apply is not None and may_apply() is not True:
                raise WisdomAuthError("Collective Wisdom review is no longer current")
            return {"installed": True}

    monkeypatch.setattr(service, "_installation_services", lambda: (object(), Executor()))
    def current():
        return False

    with pytest.raises(WisdomAuthError, match="no longer current"):
        service.install_apply("receipt", accept_partial=True, may_apply=current)

    assert captured == [("receipt", True, current)]


def test_uninstall_propagates_optional_invocation_authority(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    captured = []

    class Executor:
        def uninstall(self, skill_id, *, may_apply=None):
            captured.append((skill_id, may_apply))
            if may_apply is None or may_apply() is not True:
                raise WisdomAuthError("Collective Wisdom review is no longer current")
            return {"uninstalled": True}

    monkeypatch.setattr(service, "_installation_services", lambda: (object(), Executor()))

    def revoked():
        return False

    with pytest.raises(WisdomAuthError, match="no longer current"):
        service.uninstall("skill", may_apply=revoked)

    assert captured == [("skill", revoked)]


def test_setup_and_suggest_reject_revoked_desktop_authority(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    effects = []

    class Onboarding:
        def setup(self, *, accept_disclosure, may_apply=None):
            effects.append(("setup", accept_disclosure))
            del may_apply
            return {"configured": True}

    class Reviewer:
        def prepare(self, skill_name, *, allow_model_review, may_apply=None):
            effects.append(("suggest", skill_name, allow_model_review, may_apply))
            return {"draft_id": "local:1"}

    class Store:
        @staticmethod
        def bind_qualification_owner(owner):
            del owner

    def open_onboarding():
        return Onboarding()

    def installation_owner():
        return {"account_id": "a"}

    def contribution_reviewer():
        return Reviewer()

    monkeypatch.setattr(service, "_install_credentials", lambda: None)
    monkeypatch.setattr(service, "_open", open_onboarding)
    monkeypatch.setattr(service, "_installation_owner", installation_owner)
    monkeypatch.setattr(service, "_contribution_reviewer", contribution_reviewer)
    setattr(service, "_onboarding", type("Bound", (), {"store": Store()})())
    monkeypatch.setattr(service, "check_owner", lambda: None)

    def revoked():
        return False

    with pytest.raises(WisdomAuthError, match="no longer current"):
        service.setup(accept_disclosure=True, may_apply=revoked)
    suggest = getattr(service, "suggest")
    with pytest.raises(WisdomAuthError, match="no longer current"):
        suggest(  # pylint: disable=unexpected-keyword-arg
            "skill", allow_model_review=False, may_apply=revoked
        )

    assert not effects


def test_setup_propagates_current_authority_to_onboarding(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    captured = []

    class Onboarding:
        def setup(self, *, accept_disclosure, may_apply=None):
            captured.append((accept_disclosure, may_apply))
            return {"configured": True}

    def open_onboarding():
        return Onboarding()

    def current():
        return True

    monkeypatch.setattr(service, "_install_credentials", lambda: None)
    monkeypatch.setattr(service, "_open", open_onboarding)
    monkeypatch.setattr(service, "check_owner", lambda: None)

    assert service.setup(accept_disclosure=True, may_apply=current) == {
        "configured": True
    }
    assert captured == [(True, current)]


def test_update_methods_share_one_owner_bound_reviewer_and_executor(tmp_path, monkeypatch):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))

    class Reviewer:
        def check(self, skill_id):
            return ("check", skill_id)

        def plan(self, skill_id, **values):
            return ("plan", skill_id, values)

    class Executor:
        def apply(self, receipt, *, may_apply=None):
            assert may_apply is None or may_apply() is True
            return ("apply", receipt)

    monkeypatch.setattr(service, "_update_services", lambda: (Reviewer(), Executor()))
    monkeypatch.setattr(service, "check_owner", lambda: None)

    assert service.check_update("skill") == ("check", "skill")
    assert service.update_plan(
        "skill", preserve_modified=True, accept_sensitive=True, accept_partial=False
    ) == ("plan", "skill", {
        "preserve_modified": True,
        "accept_sensitive": True,
        "accept_partial": False,
        "accept_warnings": False,
    })
    assert service.update_plan("skill", accept_warnings=True) == ("plan", "skill", {
        "preserve_modified": False,
        "accept_sensitive": False,
        "accept_partial": False,
        "accept_warnings": True,
    })
    assert service.update_apply("receipt") == ("apply", "receipt")


def test_native_update_inspection_chunks_losslessly_before_surface_limits(
    tmp_path, monkeypatch
):
    service = runtime.InvocationService(tmp_path / "state", ("default", tmp_path))
    exact = ("<&>" * 1200) + "\nfinal byte"

    class Verified:
        files = [("SKILL.md", "file", exact.encode("utf-8"))]

    class Receipts:
        @staticmethod
        def load(receipt):
            assert receipt == "receipt"
            return {"files": [{"path": "SKILL.md", "hash": "d" * 64}]}

    class Reviewer:
        receipts = Receipts()

        @staticmethod
        def verify(plan):
            assert plan["files"][0]["hash"] == "d" * 64
            return Verified()

    monkeypatch.setattr(service, "_update_services", lambda: (Reviewer(), object()))
    monkeypatch.setattr(service, "check_owner", lambda: None)

    pages = service.native_update_inspect("receipt")
    assert len(pages) > 1
    assert "".join(page["content"] for page in pages) == exact
    assert all(len(page["content"]) <= 500 for page in pages)
    assert [(page["content_start"], page["content_end"]) for page in pages] == [
        (index * 500, min(len(exact), (index + 1) * 500))
        for index in range(len(pages))
    ]


def test_native_candidate_inspection_never_crosses_same_org_reenrollment(
    tmp_path, monkeypatch
):
    """Candidate reads require the exact owner and a current, non-tombstoned source."""
    profile = tmp_path / "profile"
    root = profile / "wisdom"
    store = WisdomStore(root)
    store.activate_installation_identity("installation-1", "org-1")
    owner_a = {
        "profile_name": "default", "profile_home": str(profile.resolve()),
        "account_id": "owner-a", "organization_id": "org-1",
        "base_url": "https://gateway.invalid", "installation_id": "installation-1",
    }
    owner_b = {**owner_a, "account_id": "owner-b", "installation_id": "installation-2"}
    store.bind_qualification_owner(owner_a)
    skill_path = profile / "skills" / "private-workflow"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("prior owner private bytes", encoding="utf-8")
    old_skill_id = store.register_skill(
        skill_path, content_hash="sha256:owner-a", source_kind="local"
    )
    old_event_id = store.emit_local_event(
        kind="wisdom.candidate", skill_id=old_skill_id,
        content_hash="sha256:owner-a",
        payload={"skill_name": "prior-owner-private-workflow"},
        session_id="session-a", task_id="task-a", qualification="high_usage",
    )
    assert old_event_id is not None

    service = runtime.InvocationService(root, ("default", profile.resolve()))
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", lambda: None)
    current_owner = {"value": owner_a}
    monkeypatch.setattr(service, "_installation_owner", lambda: current_owner["value"])

    assert service.native_candidate_inspect(old_event_id)["facts"]["name"] == (
        "prior-owner-private-workflow"
    )
    current_owner["value"] = owner_b
    with pytest.raises(WisdomError, match="not found"):
        service.native_candidate_inspect(old_event_id)
    assert store.get_metadata("qualification_owner") == owner_a

    store.activate_installation_identity("installation-2", "org-1")
    store.bind_qualification_owner(owner_b)
    with pytest.raises(WisdomError, match="not found"):
        service.native_candidate_inspect(old_event_id)
    old_skill = store.local_skill(old_skill_id)
    assert old_skill is not None and old_skill["deleted_at"] is not None

    new_skill_id = store.register_skill(
        skill_path, content_hash="sha256:owner-b", source_kind="local"
    )
    new_event_id = store.emit_local_event(
        kind="wisdom.candidate", skill_id=new_skill_id,
        content_hash="sha256:owner-b",
        payload={"skill_name": "new-owner-private-workflow"},
        session_id="session-b", task_id="task-b", qualification="high_usage",
    )
    assert new_event_id is not None
    assert service.native_candidate_inspect(new_event_id)["facts"]["name"] == (
        "new-owner-private-workflow"
    )
    current_owner["value"] = owner_a
    with pytest.raises(WisdomError, match="not found"):
        service.native_candidate_inspect(new_event_id)


def test_closed_service_cannot_publish_late_results(tmp_path):
    """Post-response ownership check rejects work after plugin unload."""
    service = runtime.InvocationService(tmp_path, ("default", tmp_path))
    service.close()
    with pytest.raises(WisdomError, match="unloaded"):
        service.check_owner()


def test_inbox_projects_owner_activity_reviews_and_installed_setup_without_effects(
    tmp_path, monkeypatch
):
    """One admitted private route sees only bounded safe facts from durable local state."""
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    store.bind_qualification_owner(INBOX_OWNER)
    queue = MediationStore(store, owner=INBOX_OWNER)
    assessment_id = queue.enqueue_owned(
        "org-1", "feed:event-1",
        {"kind": "skill", "skill_id": "skill-1", "version": 2,
         "event_id": "event-1", "private_evidence": "must-not-leak"},
    )
    assessment = queue.assessments("org-1")[0]
    route = _inbox_invocation()
    review = NativeReviewController(store).request_install_modes(
        _InboxReviewService(), route, "skill-1@v2"
    )
    store.record_install({
        "skill_id": "skill-1", "org_id": "org-1", "slug": "shared-skill",
        "version": 2, "content_hash": "sha256:" + "a" * 64,
        "baseline": {"SKILL.md": "sha256:" + "b" * 64},
        "target_path": "/profiles/default/skills/shared-skill", "update_mode": "MANUAL",
        "owner": INBOX_OWNER,
    })
    before = (tmp_path / "wisdom" / "wisdom.db").read_bytes()
    service = runtime.InvocationService(
        tmp_path / "wisdom", ("default", tmp_path),
        config={"notifications": {"delivery_mode": "agent"}},
    )
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(INBOX_OWNER))

    result = service.inbox(invocation=route)

    assert result == {
        "mode": "agent",
        "assessments": [{
            "id": assessment_id, "event_key": assessment["event_key"],
            "state": "pending", "advice": None, "delivered_at": None,
            "owner_session": None,
            "reference": {"kind": "skill", "skill_id": "skill-1", "version": 2,
                          "event_id": "event-1"},
        }],
        "interactions": [{
            "id": review["id"], "operation": "install_modes", "state": "pending",
            "delivery_state": "unsent", "expires_at": review["expires_at"],
        }],
        "installed_setup": [{
            "kind": "installed", "identity": "skill-1", "version": 2,
            "content_hash": "sha256:" + "a" * 64,
            "instruction": (
                "Inspect installed setup; file installation does not establish readiness."
            ),
        }],
    }
    assert service.client.method_calls == []
    assert (tmp_path / "wisdom" / "wisdom.db").read_bytes() == before


@pytest.mark.parametrize(
    "qualification_owner", [None, INBOX_OWNER, {**INBOX_OWNER, "account_id": "old"}]
)
def test_first_inbox_read_without_mediation_tables_is_read_only(
    tmp_path, monkeypatch, qualification_owner
):
    """Qualification state cannot make a first inbox read create mediation schema."""
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    route = _inbox_invocation()
    review = NativeReviewController(store).request_install_modes(
        _InboxReviewService(), route, "skill-1"
    )
    if qualification_owner is not None:
        store.bind_qualification_owner(qualification_owner)
    with store.transaction() as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='wisdom_shared_assessment'"
        ).fetchone() is None
    database = tmp_path / "wisdom" / "wisdom.db"
    before = database.read_bytes()
    service = runtime.InvocationService(tmp_path / "wisdom", ("default", tmp_path))
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(INBOX_OWNER))

    result = service.inbox(invocation=route)

    assert result["assessments"] == []
    assert result["interactions"] == [{
        "id": review["id"], "operation": "install_modes", "state": "pending",
        "delivery_state": "unsent", "expires_at": review["expires_at"],
    }]
    assert database.read_bytes() == before


def test_inbox_fixed_mode_keeps_requested_work_and_fences_native_route(
    tmp_path, monkeypatch
):
    """Malformed mode fails closed while an admitted route retains its manual review."""
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    store.bind_qualification_owner(INBOX_OWNER)
    queue = MediationStore(store, owner=INBOX_OWNER)
    queue.enqueue_owned(
        "org-1", "feed:proactive",
        {"kind": "skill", "skill_id": "proactive", "version": 1},
    )
    requested_id = queue.enqueue_owned(
        "org-1", "candidate:requested",
        {"kind": "candidate", "event_id": "requested", "user_requested": True},
    )
    controller = NativeReviewController(store)
    route = _inbox_invocation()
    owned = controller.request_install_modes(_InboxReviewService(), route, "skill-1")
    controller.request_install_modes(
        _InboxReviewService(),
        _inbox_invocation(
            session_key="slack:W1:D2:U2", chat_id="D2", actor_id="U2",
            message_id="foreign-request",
        ),
        "skill-2",
    )
    with store.transaction() as db:
        db.execute("UPDATE wisdom_consent SET expires_at=0 WHERE id=?", (owned["id"],))
    service = runtime.InvocationService(
        tmp_path / "wisdom", ("default", tmp_path),
        config={"notifications": {"delivery_mode": "unexpected"}},
    )
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(INBOX_OWNER))

    result = service.inbox(invocation=route)

    assert result["mode"] == "fixed"
    assert [row["id"] for row in result["assessments"]] == [requested_id]
    assert result["interactions"] == [{
        "id": owned["id"], "operation": "install_modes", "state": "expired",
        "delivery_state": "unsent", "expires_at": 0.0,
    }]
    assert controller.load(owned["id"])["state"] == "pending"
    assert service.client.method_calls == []


def test_terminal_inbox_does_not_inherit_same_org_prior_owner_state(tmp_path, monkeypatch):
    """Terminal-wide review inspection remains fenced to the exact enrolment owner."""
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    store.bind_qualification_owner(INBOX_OWNER)
    prior_assessment = MediationStore(store, owner=INBOX_OWNER).enqueue_owned(
        "org-1", "feed:prior-owner",
        {"kind": "skill", "skill_id": "prior-owner", "version": 1},
    )
    controller = NativeReviewController(store)
    prior = controller.request_install_modes(
        _InboxReviewService(), _inbox_invocation(), "prior-skill"
    )
    next_owner = {
        **INBOX_OWNER, "account_id": "account-2", "installation_id": "installation-2",
    }

    class NextOwnerService:
        @staticmethod
        def native_owner():
            return dict(next_owner)

    store.activate_installation_identity("installation-2", "org-1")
    store.bind_qualification_owner(next_owner)
    current_assessment = MediationStore(store, owner=next_owner).enqueue_owned(
        "org-1", "feed:current-owner",
        {"kind": "skill", "skill_id": "current-owner", "version": 1},
    )
    current = controller.request_install_modes(
        NextOwnerService(),
        _inbox_invocation(
            session_key="slack:W1:D2:U2", chat_id="D2", actor_id="U2",
            message_id="current-request",
        ),
        "current-skill",
    )
    service = runtime.InvocationService(tmp_path / "wisdom", ("default", tmp_path))
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(next_owner))

    result = service.inbox()

    assert [row["id"] for row in result["assessments"]] == [current_assessment]
    assert prior_assessment not in str(result)
    assert [row["id"] for row in result["interactions"]] == [current["id"]]
    assert prior["id"] not in str(result)


def test_inbox_rejects_owner_change_after_read(tmp_path, monkeypatch):
    store = WisdomStore(tmp_path / "wisdom")
    store.activate_installation_identity("installation-1", "org-1")
    store.bind_qualification_owner(INBOX_OWNER)
    service = runtime.InvocationService(tmp_path / "wisdom", ("default", tmp_path))
    service._onboarding = Mock(store=store)  # pylint: disable=protected-access
    service.client = Mock()
    monkeypatch.setattr(service, "require_setup", lambda: None)
    monkeypatch.setattr(service, "check_owner", Mock())
    changed = {**INBOX_OWNER, "account_id": "account-2"}
    monkeypatch.setattr(
        service, "_installation_owner", Mock(side_effect=[dict(INBOX_OWNER), changed])
    )

    with pytest.raises(WisdomAuthError, match="inbox owner changed"):
        service.inbox()
