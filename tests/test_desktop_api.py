"""Desktop workflow adapter authority and exact-review tests."""
# Recording service methods deliberately match the production interface.
# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
# pylint: disable=too-few-public-methods

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from hermes_wisdom_plugin.desktop_api import DesktopWorkflowApi, build_router
from hermes_wisdom_plugin.errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomValidationError,
)
from hermes_wisdom_plugin.direct_services import WISDOM_DISCLOSURE


def _request(app, method, path, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.request(method, path, **kwargs)
    return asyncio.run(send())


class Service:  # pylint: disable=too-many-public-methods
    """Small recording service with the same authority seams as InvocationService."""

    def __init__(self, home: Path, *, account: str = "account-a") -> None:
        self.profile = (home.name, home)
        self.account = account
        self.installations = [{
            "skill_id": "skill-1",
            "slug": "one",
            "version": 1,
            "content_hash": "sha256:" + "1" * 64,
            "modified": False,
            "state": "active",
        }]
        self.calls: list[tuple] = []

    def binding(self):
        return {
            "profile_name": self.profile[0],
            "profile_home": str(self.profile[1]),
            "account_id": self.account,
            "organization_id": "org-1",
            "base_url": "https://gateway.invalid",
        }

    def native_owner(self):
        return {**self.binding(), "installation_id": "installation-1"}

    def install_plan(self, reference, *, update_mode=None):
        self.calls.append(("install_plan", reference, update_mode))
        return {
            "operation": "install",
            "receipt": "wip_" + "a" * 32,
            "skill_id": "skill-1",
            "slug": "one",
            "version": 1,
            "content_hash": "sha256:" + "1" * 64,
            "files": [{"path": "SKILL.md", "hash": "sha256:" + "2" * 64}],
            "allowed": True,
        }

    def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
        self.calls.append(("install_apply", receipt, accept_partial))
        if may_apply is None or may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        return {"installed": True, "skill_id": "skill-1", "version": 1}

    def list_installations(self):
        return [dict(item) for item in self.installations]

    def uninstall(self, skill_id, *, may_apply=None):
        self.calls.append(("uninstall", skill_id))
        if may_apply is None or may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        return {"uninstalled": True, "skill_id": skill_id}

    def status(self, *, include_operations=False):
        return {"configured": True, "state": "active", "pending_operations": []}

    def search_skills(self, query=None):
        return [{"id": "skill-1", "slug": "one", "author_description": query or "all"}]

    def notification_config(self):
        return {"notifications": {"delivery_mode": "agent"}}

    def inbox(self):
        return {"mode": "agent", "assessments": [], "interactions": [], "installed_setup": []}

    def show(self, skill_id):
        return {"id": skill_id, "slug": "one", "latest_version_detail": {"version": {"version": 1}}}

    def versions(self, skill_id):
        return [{"skill_id": skill_id, "version": 1, "state": "published"}]

    def update_plan(self, skill_id, **options):
        return {
            "operation": "update", "skill_id": skill_id, "receipt": "wur_" + "b" * 32, **options,
        }

    def update_apply(self, receipt, *, may_apply=None):
        self.calls.append(("update_apply", receipt))
        assert may_apply is not None and may_apply() is True
        return {"updated": True, "receipt": receipt}

    def set_notification_mode(self, mode, *, expected_mode=None, may_apply=None):
        assert expected_mode == "agent"
        assert may_apply is not None and may_apply() is True
        return {"delivery_mode": mode}

    def suggest(self, skill_name, *, allow_model_review=False, may_apply=None):
        assert allow_model_review is False
        assert may_apply is not None and may_apply() is True
        return {"draft_id": "local:1", "skill_id": skill_name, "files": [], "hashes": {}}

    def prepared_inspect(self, draft_id):
        return {
            "draft_id": draft_id, "author_description": "Useful",
            "files": [], "expected_hashes": {},
        }

    def publication_review(self, draft_id, *, may_apply=None):
        assert may_apply is not None and may_apply() is True
        return {"draft_id": draft_id, "receipt": "wpr_" + "c" * 32, "policy": "moderated"}

    def publication_submit(self, receipt, *, may_apply=None):
        self.calls.append(("publication_submit", receipt))
        assert may_apply is not None and may_apply() is True
        return {"submitted": True, "receipt": receipt, "state": "pending_review"}

    def setup(self, *, accept_disclosure, may_apply=None):
        assert accept_disclosure is True
        assert may_apply is not None and may_apply() is True
        return {"configured": True, "state": "active"}

    def end_turn_notices(self, renderer):
        self.calls.append(("end_turn_notices",))
        renderer([{"event_id": "event-1", "category": "new_skill", "skill_id": "skill-1"}])
        return {"supported": True, "offered": 1, "polled": True, "more": False}


def test_install_confirmation_is_bound_to_server_issued_view_and_current_profile(tmp_path):
    now = [100.0]
    api = DesktopWorkflowApi(clock=lambda: now[0], view_ttl_seconds=60)
    service_a = Service(tmp_path / "a")
    service_b = Service(tmp_path / "b")

    view = api.open_view(service_a)
    plan = api.install_review(service_a, view["view_id"], "skill-1@v1")
    assert plan["receipt"].startswith("wip_")

    with pytest.raises(WisdomAuthError, match="no longer current"):
        api.install_confirm(service_b, view["view_id"], plan["receipt"])

    assert api.install_confirm(service_a, view["view_id"], plan["receipt"])["installed"] is True

    now[0] = 161.0
    with pytest.raises(WisdomAuthError, match="no longer current"):
        api.install_confirm(service_a, view["view_id"], plan["receipt"])


def test_uninstall_confirmation_rejects_changed_installation_after_review(tmp_path):
    api = DesktopWorkflowApi()
    service = Service(tmp_path / "a")
    view = api.open_view(service)
    review = api.uninstall_review(service, view["view_id"], "skill-1")

    service.installations[0]["modified"] = True
    with pytest.raises(WisdomConflict, match="changed; review it again"):
        api.uninstall_confirm(service, view["view_id"], review["review_id"])

    assert not any(call[0] == "uninstall" for call in service.calls)


def test_closing_view_revokes_outstanding_reviews(tmp_path):
    api = DesktopWorkflowApi()
    service = Service(tmp_path / "a")
    view = api.open_view(service)
    plan = api.install_review(service, view["view_id"], "skill-1@v1")

    api.close_view(service, view["view_id"])

    with pytest.raises(WisdomAuthError, match="no longer current"):
        api.install_confirm(service, view["view_id"], plan["receipt"])


def test_desktop_notice_route_returns_best_effort_human_surface_payload(tmp_path):
    service = Service(tmp_path / "a")
    app = FastAPI()
    app.include_router(build_router(lambda: service, workflow=DesktopWorkflowApi()))

    response = _request(app, "POST", "/notices/end-turn")

    assert response.status_code == 200
    assert response.json() == {
        "supported": True,
        "offered": 1,
        "polled": True,
        "more": False,
        "notices": [{"event_id": "event-1", "category": "new_skill", "skill_id": "skill-1"}],
    }


@pytest.mark.parametrize(
    ("review", "confirm", "apply_call"),
    [
        (
            lambda api, service, view_id: api.install_review(
                service, view_id, "skill-1@v1"
            ),
            lambda api, service, view_id, receipt: api.install_confirm(
                service, view_id, receipt
            ),
            "install_apply",
        ),
        (
            lambda api, service, view_id: api.update_review(
                service, view_id, "skill-1"
            ),
            lambda api, service, view_id, receipt: api.update_confirm(
                service, view_id, receipt
            ),
            "update_apply",
        ),
        (
            lambda api, service, view_id: api.publication_review(
                service, view_id, "local:1"
            ),
            lambda api, service, view_id, receipt: api.publication_confirm(
                service, view_id, receipt
            ),
            "publication_submit",
        ),
    ],
)
def test_receipt_cannot_cross_closed_same_owner_view(
    tmp_path, review, confirm, apply_call
):
    api = DesktopWorkflowApi()
    service = Service(tmp_path / "a")
    old_view = api.open_view(service)["view_id"]
    receipt = review(api, service, old_view)["receipt"]
    api.close_view(service, old_view)
    new_view = api.open_view(service)["view_id"]

    with pytest.raises(WisdomAuthError, match="no longer current"):
        confirm(api, service, new_view, receipt)

    assert not any(call[0] == apply_call for call in service.calls)


@pytest.mark.parametrize(
    ("review", "wrong_confirm", "wrong_apply_call"),
    [
        (
            lambda api, service, view_id: api.install_review(
                service, view_id, "skill-1@v1"
            ),
            lambda api, service, view_id, receipt: api.update_confirm(
                service, view_id, receipt
            ),
            "update_apply",
        ),
        (
            lambda api, service, view_id: api.update_review(
                service, view_id, "skill-1"
            ),
            lambda api, service, view_id, receipt: api.publication_confirm(
                service, view_id, receipt
            ),
            "publication_submit",
        ),
        (
            lambda api, service, view_id: api.publication_review(
                service, view_id, "local:1"
            ),
            lambda api, service, view_id, receipt: api.install_confirm(
                service, view_id, receipt
            ),
            "install_apply",
        ),
    ],
)
def test_receipt_cannot_authorize_a_different_action(
    tmp_path, review, wrong_confirm, wrong_apply_call
):
    api = DesktopWorkflowApi()
    service = Service(tmp_path / "a")
    view_id = api.open_view(service)["view_id"]
    receipt = review(api, service, view_id)["receipt"]

    with pytest.raises(WisdomAuthError, match="no longer current"):
        wrong_confirm(api, service, view_id, receipt)

    assert not any(call[0] == wrong_apply_call for call in service.calls)


@pytest.mark.parametrize(
    ("review", "confirm", "apply_call"),
    [
        (
            lambda api, service, view_id: api.install_review(
                service, view_id, "skill-1@v1"
            ),
            lambda api, service, view_id, receipt: api.install_confirm(
                service, view_id, receipt
            ),
            "install_apply",
        ),
        (
            lambda api, service, view_id: api.update_review(
                service, view_id, "skill-1"
            ),
            lambda api, service, view_id, receipt: api.update_confirm(
                service, view_id, receipt
            ),
            "update_apply",
        ),
        (
            lambda api, service, view_id: api.publication_review(
                service, view_id, "local:1"
            ),
            lambda api, service, view_id, receipt: api.publication_confirm(
                service, view_id, receipt
            ),
            "publication_submit",
        ),
    ],
)
def test_same_view_receipt_replay_preserves_exact_native_receipt(
    tmp_path, review, confirm, apply_call
):
    api = DesktopWorkflowApi()
    service = Service(tmp_path / "a")
    view_id = api.open_view(service)["view_id"]
    receipt = review(api, service, view_id)["receipt"]

    confirm(api, service, view_id, receipt)
    confirm(api, service, view_id, receipt)

    matching_calls = [call for call in service.calls if call[0] == apply_call]
    assert len(matching_calls) == 2
    assert all(call[1] == receipt for call in matching_calls)


def test_late_executor_callback_requires_matching_review(tmp_path):
    class DeferredService(Service):
        callback = None

        def install_apply(self, receipt, *, accept_partial=False, may_apply=None):
            self.callback = may_apply
            return {"pending": True, "receipt": receipt}

        def update_plan(self, skill_id, **options):
            return {
                "operation": "update",
                "skill_id": skill_id,
                "receipt": "wip_" + "a" * 32,
                **options,
            }

    api = DesktopWorkflowApi()
    service = DeferredService(tmp_path / "a")
    view_id = api.open_view(service)["view_id"]
    receipt = api.install_review(service, view_id, "skill-1@v1")["receipt"]
    api.install_confirm(service, view_id, receipt)
    assert service.callback() is True

    api.update_review(service, view_id, "skill-1")

    assert service.callback() is False


def test_fastapi_router_exposes_real_catalog_and_review_confirmation_flow(tmp_path):
    services = {"a": Service(tmp_path / "a"), "b": Service(tmp_path / "b")}
    selected = ["a"]
    workflow = DesktopWorkflowApi()
    app = FastAPI()
    app.include_router(
        build_router(lambda: services[selected[0]], workflow=workflow),
        prefix="/api/plugins/hermes-collective-wisdom",
    )
    async def request(method, path, **kwargs):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.request(method, path, **kwargs)

    opened = asyncio.run(request("POST", "/api/plugins/hermes-collective-wisdom/views")).json()
    view_id = opened["view_id"]
    catalog = asyncio.run(request(
        "GET",
        "/api/plugins/hermes-collective-wisdom/catalog", params={"q": "incident"}
    ))
    assert catalog.status_code == 200
    assert catalog.json()["skills"][0]["author_description"] == "incident"

    plan = asyncio.run(request(
        "POST",
        "/api/plugins/hermes-collective-wisdom/install/review",
        json={"view_id": view_id, "reference": "skill-1@v1"},
    ))
    assert plan.status_code == 200
    receipt = plan.json()["receipt"]
    applied = asyncio.run(request(
        "POST",
        "/api/plugins/hermes-collective-wisdom/install/confirm",
        json={"view_id": view_id, "receipt": receipt},
    ))
    assert applied.json() == {"installed": True, "skill_id": "skill-1", "version": 1}

    selected[0] = "b"
    stale = asyncio.run(request(
        "POST",
        "/api/plugins/hermes-collective-wisdom/install/confirm",
        json={"view_id": view_id, "receipt": receipt},
    ))
    assert stale.status_code == 401
    assert "no longer current" in stale.json()["detail"]


@pytest.mark.parametrize("payload", [
    {},
    {"view_id": "valid-view-id", "receipt": "valid-receipt", "owner": "forged"},
    {"view_id": "valid-view-id", "receipt": "valid-receipt", "accept_partial": "yes"},
])
def test_malformed_confirm_is_validation_error_not_internal_error(payload):
    app = FastAPI()
    app.include_router(build_router(lambda: None))
    result = _request(
        app, "POST", "/install/confirm", json=payload
    )
    assert result.status_code == 422


def test_service_factory_auth_failure_is_reported_as_auth_error():
    def unavailable():
        raise WisdomAuthError("Credentials unavailable")

    app = FastAPI()
    app.include_router(build_router(unavailable))
    result = _request(app, "POST", "/views")
    assert result.status_code == 401


@pytest.mark.parametrize(
    ("failure", "status_code"),
    [
        (PermissionError("operation is not permitted"), 403),
        (ValueError("documented validation failure"), 422),
    ],
)
def test_documented_builtin_workflow_failures_are_typed(failure, status_code):
    class FailingService:
        def search_skills(self, query=None):
            del query
            raise failure

    app = FastAPI()
    app.include_router(build_router(FailingService))
    result = _request(app, "GET", "/catalog")
    assert result.status_code == status_code
    assert result.json()["detail"] == str(failure)


def test_arbitrary_workflow_bug_is_not_swallowed_as_validation_error():
    class BrokenService:
        def search_skills(self, query=None):
            del query
            raise RuntimeError("bug")

    app = FastAPI()
    app.include_router(build_router(BrokenService))
    result = _request(app, "GET", "/catalog")
    assert result.status_code == 500


def test_enrollment_displays_existing_shared_disclosure():
    app = FastAPI()
    app.include_router(build_router(lambda: None))
    response = _request(app, "GET", "/enrollment/disclosure")
    assert WISDOM_DISCLOSURE in response.json()["text"]


@pytest.mark.parametrize(
    "setup_error",
    [
        WisdomValidationError("Setup required", code="setup_required"),
        WisdomConflict("Setup required", code="not_configured"),
        WisdomConflict("Setup required", code="registration_incomplete"),
    ],
)
def test_enrollment_identity_creation_does_not_turn_success_into_stale_error(
    tmp_path, setup_error
):
    class EnrollingService(Service):
        enrolled = False

        def native_owner(self):
            if not self.enrolled:
                raise setup_error
            return super().native_owner()

        def setup(self, *, accept_disclosure, may_apply=None):
            assert accept_disclosure is True and may_apply() is True
            self.enrolled = True
            assert may_apply() is True
            return {"configured": True}

    service = EnrollingService(tmp_path)
    workflow = DesktopWorkflowApi()
    view_id = workflow.open_view(service)["view_id"]
    assert workflow.enroll(service, view_id, accepted=True)["configured"] is True
    # Enrollment does not silently upgrade old controls to the new identity.
    assert workflow.current(service, view_id) is False


def test_open_view_does_not_fallback_on_native_auth_failure(tmp_path):
    class ExpiredService(Service):
        def native_owner(self):
            raise WisdomAuthError("install credentials expired")

    with pytest.raises(WisdomAuthError, match="credentials expired"):
        DesktopWorkflowApi().open_view(ExpiredService(tmp_path))


def test_enrolled_view_becomes_noncurrent_on_native_auth_failure(tmp_path):
    class ExpiringService(Service):
        expired = False

        def native_owner(self):
            if self.expired:
                raise WisdomAuthError("install credentials expired")
            return super().native_owner()

    service = ExpiringService(tmp_path)
    workflow = DesktopWorkflowApi()
    view_id = workflow.open_view(service)["view_id"]
    service.expired = True

    assert workflow.current(service, view_id) is False


@pytest.mark.parametrize(
    "failure",
    [
        WisdomValidationError("local state is corrupt", code="invalid_local_state"),
        WisdomValidationError("arbitrary validation failure"),
        WisdomConflict("owner changed", code="owner_changed"),
    ],
)
def test_open_view_fails_closed_for_non_setup_native_owner_failures(tmp_path, failure):
    class FailingService(Service):
        def native_owner(self):
            raise failure

    with pytest.raises(type(failure), match=str(failure)):
        DesktopWorkflowApi().open_view(FailingService(tmp_path))


def test_router_covers_management_settings_enrolment_and_publication(tmp_path):
    service = Service(tmp_path / "a")
    workflow = DesktopWorkflowApi()
    app = FastAPI()
    app.include_router(build_router(lambda: service, workflow=workflow), prefix="/wisdom")

    async def request(method, path, **kwargs):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.request(method, path, **kwargs)

    view_id = asyncio.run(request("POST", "/wisdom/views")).json()["view_id"]
    assert asyncio.run(request("GET", "/wisdom/catalog/skill-1")).json()["slug"] == "one"
    versions = asyncio.run(request("GET", "/wisdom/catalog/skill-1/versions")).json()
    assert versions["versions"][0]["version"] == 1
    installed = asyncio.run(request("GET", "/wisdom/installed")).json()
    assert installed["installations"][0]["skill_id"] == "skill-1"

    update = asyncio.run(request(
        "POST", "/wisdom/update/review",
        json={"view_id": view_id, "skill_id": "skill-1", "preserve_modified": True},
    )).json()
    assert asyncio.run(request(
        "POST", "/wisdom/update/confirm",
        json={"view_id": view_id, "receipt": update["receipt"]},
    )).json()["updated"] is True

    removal = asyncio.run(request(
        "POST", "/wisdom/uninstall/review",
        json={"view_id": view_id, "skill_id": "skill-1"},
    )).json()
    assert asyncio.run(request(
        "POST", "/wisdom/uninstall/confirm",
        json={"view_id": view_id, "review_id": removal["review_id"]},
    )).json()["uninstalled"] is True

    assert asyncio.run(request(
        "PATCH", "/wisdom/settings/mode",
        json={"view_id": view_id, "expected_mode": "agent", "mode": "fixed"},
    )).json() == {"delivery_mode": "fixed"}
    assert asyncio.run(request(
        "POST", "/wisdom/enrollment/confirm",
        json={"view_id": view_id, "accepted": True, "disclosure_version": "wisdom-desktop-v1"},
    )).json()["configured"] is True

    draft = asyncio.run(request(
        "POST", "/wisdom/drafts/prepare",
        json={"view_id": view_id, "skill_name": "my-skill"},
    )).json()
    inspected = asyncio.run(request("GET", f"/wisdom/drafts/{draft['draft_id']}")).json()
    assert inspected["author_description"] == "Useful"
    review = asyncio.run(request(
        "POST", "/wisdom/publication/review",
        json={"view_id": view_id, "draft_id": draft["draft_id"]},
    )).json()
    submitted = asyncio.run(request(
        "POST", "/wisdom/publication/confirm",
        json={"view_id": view_id, "receipt": review["receipt"]},
    )).json()
    assert submitted["state"] == "pending_review"
