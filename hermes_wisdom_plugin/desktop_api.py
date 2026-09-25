"""Thin Desktop adapter over the existing owner-bound workflow service.

The renderer receives opaque, short-lived view IDs. They are not Hermes session
identities and carry no authority by themselves: every use is checked against
the currently scoped profile and freshly read account/enrolment owner.
"""

from __future__ import annotations

import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    WisdomAuthError,
    WisdomConflict,
    WisdomError,
    WisdomNotFound,
    WisdomUnavailable,
    WisdomValidationError,
)
from .direct_services import WISDOM_DISCLOSURE


_PRE_ENROLLMENT_CODES = frozenset({
    "setup_required",
    "not_configured",
    "registration_incomplete",
    "owner_changed",
})


class ViewBody(BaseModel):
    """Strict request schema; identity is never accepted from the renderer."""

    model_config = ConfigDict(extra="forbid", strict=True)
    view_id: str = Field(min_length=8, max_length=128)


class InstallReviewBody(ViewBody):
    """Select a published version without approving installation."""

    reference: str = Field(min_length=1, max_length=512)
    update_mode: str | None = Field(default=None, max_length=64)
    accept_warnings: bool = False


class ReceiptBody(ViewBody):
    """Confirm an exact server-issued receipt."""

    receipt: str = Field(min_length=8, max_length=256)
    accept_partial: bool = False


class SkillBody(ViewBody):
    """Select an owner-managed skill."""

    skill_id: str = Field(min_length=1, max_length=512)


class UpdateReviewBody(SkillBody):
    """Declare explicit update review choices."""

    preserve_modified: bool = False
    accept_sensitive: bool = False
    accept_partial: bool = False
    accept_warnings: bool = False


class ReviewBody(ViewBody):
    """Confirm a server-held uninstall snapshot."""

    review_id: str = Field(min_length=8, max_length=128)


class ModeBody(ViewBody):
    """Compare-and-set the notification mode."""

    mode: str = Field(pattern=r"^(agent|fixed)$")
    expected_mode: str = Field(pattern=r"^(agent|fixed)$")


class MuteBody(ViewBody):
    """Apply one revision-bound organization mute choice."""

    control_id: str = Field(min_length=1, max_length=64)
    duration: str | None = Field(
        default=None, pattern=r"^(1_day|1_week|30_days|forever)$"
    )


class EnrollmentBody(ViewBody):
    """Accept the displayed disclosure version."""

    accepted: bool
    disclosure_version: str


class DraftBody(ViewBody):
    """Prepare one local skill without upload authority."""

    skill_name: str = Field(min_length=1, max_length=256)


class DraftReviewBody(ViewBody):
    """Review a private prepared draft."""

    draft_id: str = Field(min_length=1, max_length=512)
    accept_warnings: bool = False


class DraftSaveBody(ViewBody):
    """Replace one complete prepared package using its displayed hash snapshot."""

    author_description: str
    files: list[dict[str, Any]]
    expected_hashes: dict[str, str]


class PreparedReviewBody(ViewBody):
    """Re-run local checks for one exact saved prepared package."""

    reviewed_hashes: dict[str, str]


@dataclass(slots=True)
class _View:
    profile: tuple[str, str]
    owner: dict[str, str]
    expires_at: float


@dataclass(slots=True)
class _Review:
    view_id: str
    kind: str
    subject: str
    snapshot: dict[str, Any]
    expires_at: float


class DesktopWorkflowApi:  # pylint: disable=too-many-public-methods
    """Map UI gestures to the shared workflow without duplicating its kernels."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        view_ttl_seconds: float = 15 * 60,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._clock = clock
        self._enabled = enabled
        self._view_ttl = float(view_ttl_seconds)
        self._lock = threading.RLock()
        self._views: dict[str, _View] = {}
        self._reviews: dict[str, _Review] = {}

    @staticmethod
    def _profile(service: Any) -> tuple[str, str]:
        name, home = service.profile
        return str(name), str(Path(home).resolve(strict=False))

    @staticmethod
    def _owner(service: Any) -> dict[str, str]:
        """Prefer the exact enrolment owner; pre-enrolment falls back to account binding."""
        try:
            owner = service.native_owner()
        except (WisdomConflict, WisdomValidationError) as exc:
            if exc.code not in _PRE_ENROLLMENT_CODES:
                raise
            owner = service.binding()
        if not isinstance(owner, dict) or not owner:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        return {str(key): str(value) for key, value in owner.items()}

    @staticmethod
    def _binding(service: Any) -> dict[str, str]:
        owner = service.binding()
        if not isinstance(owner, dict) or not owner:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        return {str(key): str(value) for key, value in owner.items()}

    def open_view(self, service: Any) -> dict[str, Any]:
        """Bind a short-lived page to fresh account and profile state."""
        now = self._clock()
        view_id = "wiv_" + secrets.token_hex(20)
        view = _View(
            profile=self._profile(service),
            owner=self._owner(service),
            expires_at=now + self._view_ttl,
        )
        with self._lock:
            self._purge(now)
            self._views[view_id] = view
        return {
            "view_id": view_id,
            "profile": view.profile[0],
            "expires_at_monotonic": view.expires_at,
        }

    def close_view(self, service: Any, view_id: str) -> dict[str, bool]:
        """Revoke this owner’s page and pending removal reviews."""
        # Do not let another profile/account use a guessed ID to revoke a live view.
        self._require_view(service, view_id)
        with self._lock:
            self._views.pop(view_id, None)
            self._reviews = {
                key: value for key, value in self._reviews.items()
                if value.view_id != view_id
            }
        return {"closed": True}

    def current(
        self,
        service: Any,
        view_id: str,
        *,
        enrolling: bool = False,
        review_id: str | None = None,
        kind: str | None = None,
    ) -> bool:
        """Provide the shared executor’s late authority fence."""
        try:
            if review_id is None and kind is None:
                self._require_view(service, view_id, enrolling=enrolling)
            elif review_id is not None and kind is not None and not enrolling:
                self._require_review(service, view_id, review_id, kind)
            else:
                raise WisdomAuthError("Collective Wisdom review is no longer current")
        except WisdomAuthError:
            return False
        return True

    def _require_lifecycle(
        self, service: Any, view_id: str, view: _View
    ) -> None:
        """Fence one bounded owner read against view and host lifecycle changes."""
        with self._lock:
            if (
                self._views.get(str(view_id)) is not view
                or view.expires_at <= self._clock()
                or not self._enabled()
                or view.profile != self._profile(service)
            ):
                raise WisdomAuthError("Collective Wisdom review is no longer current")

    def _require_view(self, service: Any, view_id: str, *, enrolling: bool = False) -> _View:
        now = self._clock()
        with self._lock:
            self._purge(now)
            view = self._views.get(str(view_id))
        if view is None:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        self._require_lifecycle(service, view_id, view)
        owner_reader = (
            self._binding
            if enrolling and "installation_id" not in view.owner
            else self._owner
        )
        first_owner = owner_reader(service)
        self._require_lifecycle(service, view_id, view)
        if view.owner != first_owner:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        # A host-owned credential read can return the old owner while rotating the
        # backing account immediately afterwards. Two matching snapshots are the
        # bounded consistency fence; kernels still recheck before effects.
        second_owner = owner_reader(service)
        self._require_lifecycle(service, view_id, view)
        if first_owner != second_owner or view.owner != second_owner:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        return view

    def _record_review(
        self,
        service: Any,
        view_id: str,
        kind: str,
        subject: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind an exact native receipt to the live view and intended action."""
        view = self._require_view(service, view_id)
        receipt = result.get("receipt")
        if not isinstance(receipt, str) or not receipt:
            raise WisdomValidationError("workflow review did not return an exact receipt")
        with self._lock:
            if (
                self._views.get(str(view_id)) is not view
                or view.expires_at <= self._clock()
                or not self._enabled()
            ):
                raise WisdomAuthError("Collective Wisdom review is no longer current")
            self._reviews[receipt] = _Review(
                view_id=view_id,
                kind=kind,
                subject=subject,
                snapshot=deepcopy(result),
                expires_at=view.expires_at,
            )
        return result

    def _require_review(
        self, service: Any, view_id: str, review_id: str, kind: str
    ) -> _Review:
        """Require both the live originating view and its matching action review."""
        self._require_view(service, view_id)
        with self._lock:
            review = self._reviews.get(str(review_id))
            if (
                review is None
                or review.view_id != view_id
                or review.kind != kind
                or review.expires_at <= self._clock()
            ):
                raise WisdomAuthError("Collective Wisdom review is no longer current")
        return review

    def _purge(self, now: float) -> None:
        expired_views = {
            key for key, value in self._views.items() if value.expires_at <= now
        }
        for key in expired_views:
            self._views.pop(key, None)
        self._reviews = {
            key: value for key, value in self._reviews.items()
            if value.expires_at > now and value.view_id not in expired_views
        }

    @staticmethod
    def _named_outcome(result: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        """Carry the reviewed name through a thin executor result, never an ID guess."""
        skill_id = snapshot.get("skill_id")
        name = snapshot.get("slug") or snapshot.get("skill_name")
        if skill_id and result.get("skill_id") not in (None, skill_id):
            return {**result, "installed": False, "updated": False,
                    "uninstalled": False, "state": "unknown",
                    "skill_name": name if isinstance(name, str) else "",
                    "detail": "The result did not match the reviewed skill. "
                    "Check installed skills before trying again."}
        if not isinstance(name, str) or not name:
            return result
        return {**result, "skill_name": name}

    def install_review(
        self,
        service: Any,
        view_id: str,
        reference: str,
        *,
        update_mode: str | None = None,
        accept_warnings: bool = False,
    ) -> dict[str, Any]:
        """Prepare the existing hash-bound installation review."""
        self._require_view(service, view_id)
        result = service.install_plan(
            reference,
            update_mode=update_mode,
            accept_warnings=accept_warnings,
        )
        return self._record_review(service, view_id, "install", reference, result)

    def install_confirm(
        self, service: Any, view_id: str, receipt: str, *, accept_partial: bool = False
    ) -> dict[str, Any]:
        """Confirm through the shared installer with late revocation checks."""
        review = self._require_review(service, view_id, receipt, "install")
        result = service.install_apply(
            receipt,
            accept_partial=accept_partial,
            may_apply=lambda: self.current(
                service, view_id, review_id=receipt, kind="install"
            ),
        )
        return self._named_outcome(result, review.snapshot)

    def update_review(self, service: Any, view_id: str, skill_id: str, **options: Any):
        """Obtain an exact update plan, checking scope after I/O."""
        self._require_view(service, view_id)
        result = service.update_plan(skill_id, **options)
        return self._record_review(service, view_id, "update", skill_id, result)

    def check_update(self, service: Any, view_id: str, skill_id: str):
        """Read update availability without creating a review or applying files."""
        self._require_view(service, view_id)
        result = service.check_update(skill_id)
        self._require_view(service, view_id)
        return result

    def update_confirm(self, service: Any, view_id: str, receipt: str):
        """Apply the reviewed update through the existing receipt kernel."""
        review = self._require_review(service, view_id, receipt, "update")
        result = service.update_apply(
            receipt,
            may_apply=lambda: self.current(
                service, view_id, review_id=receipt, kind="update"
            ),
        )
        return self._named_outcome(result, review.snapshot)

    def set_notification_mode(
        self,
        service: Any,
        view_id: str,
        mode: str,
        *,
        expected_mode: str,
    ):
        """Apply a deliberate notification choice, not a stale default."""
        self._require_view(service, view_id)
        return service.set_notification_mode(
            mode,
            expected_mode=expected_mode,
            may_apply=lambda: self.current(service, view_id),
        )

    def notification_settings(self, service: Any, view_id: str):
        """Read current mode and a revision-bound organization mute control."""
        self._require_view(service, view_id)
        return service.notification_settings(
            include_mute_control=True,
            may_apply=lambda: self.current(service, view_id),
        )

    def choose_notification_mute(
        self,
        service: Any,
        view_id: str,
        control_id: str,
        duration: str | None,
    ):
        """Apply only the organization mute snapshot displayed in this view."""
        self._require_view(service, view_id)
        return service.choose_notification_mute(
            control_id,
            duration,
            may_apply=lambda: self.current(service, view_id),
        )

    def enroll(self, service: Any, view_id: str, *, accepted: bool):
        """Enroll only with explicit disclosure and a current account binding."""
        self._require_view(service, view_id)
        if accepted is not True:
            raise WisdomValidationError("Collective Wisdom disclosure was not accepted")
        return service.setup(
            accept_disclosure=True,
            # First enrollment creates an identity. Only this action keeps its
            # pre-enrollment account binding; old install controls stay stale.
            may_apply=lambda: self.current(service, view_id, enrolling=True),
        )

    def prepare_draft(self, service: Any, view_id: str, skill_name: str):
        """Prepare locally without model-review or publication permission."""
        self._require_view(service, view_id)
        return service.suggest(
            skill_name,
            allow_model_review=False,
            may_apply=lambda: self.current(service, view_id),
        )

    def candidates(self, service: Any, view_id: str) -> dict[str, Any]:
        """Read current local candidates without granting publication authority."""
        self._require_view(service, view_id)
        rows = service.notice_candidates()
        references = [{
            "kind": "candidate", "content_hash": row["content_hash"],
            "notice_key": row["event_id"],
        } for row in rows]
        preferences = service.notice_preferences(references) if references else {}
        suppressed = preferences.get("suppressed") or {}
        self._require_view(service, view_id)
        return {
            "candidates": [
                {
                    **row,
                    "local_reasons": (
                        row.get("payload", {}).get("local_reasons", {})
                        if isinstance(row.get("payload"), dict)
                        else {}
                    ),
                }
                for row in rows if not suppressed.get(row["event_id"])
            ]
        }

    def prepare_candidate(
        self, service: Any, view_id: str, event_id: str
    ) -> dict[str, Any]:
        """Prepare the candidate's current local skill as a private draft."""
        self._require_view(service, view_id)
        inspected = service.native_candidate_inspect(event_id)
        self._require_view(service, view_id)
        skill_name = str((inspected.get("facts") or {}).get("name") or "")
        if not skill_name:
            raise WisdomValidationError("Collective Wisdom candidate is incomplete")
        return service.suggest(
            skill_name,
            allow_model_review=False,
            may_apply=lambda: self.current(service, view_id),
        )

    def defer_candidate(
        self, service: Any, view_id: str, event_id: str
    ) -> dict[str, Any]:
        """Persist Not now through the existing exact candidate suppression."""
        self._require_view(service, view_id)
        return service.defer_candidate(
            event_id, may_apply=lambda: self.current(service, view_id)
        )

    def prepared_drafts(self, service: Any, view_id: str) -> dict[str, Any]:
        """List resumable private drafts without granting publication authority."""
        self._require_view(service, view_id)
        rows = service.prepared_drafts()
        self._require_view(service, view_id)
        return {"drafts": rows}

    def prepared_inspect(self, service: Any, view_id: str, draft_id: str):
        """Read complete inert draft bytes only for the originating live view."""
        self._require_view(service, view_id)
        result = service.prepared_inspect(draft_id)
        self._require_view(service, view_id)
        return result

    def prepared_save(  # pylint: disable=too-many-arguments
        self,
        service: Any,
        view_id: str,
        draft_id: str,
        *,
        author_description: str,
        files: list[dict[str, Any]],
        expected_hashes: dict[str, str],
    ):
        """Save a complete owner-private package through the existing CAS workflow."""
        self._require_view(service, view_id)
        result = service.prepared_save(
            draft_id,
            author_description=author_description,
            files=files,
            expected_hashes=expected_hashes,
            may_apply=lambda: self.current(service, view_id),
        )
        self._require_view(service, view_id)
        return result

    def prepared_review(
        self,
        service: Any,
        view_id: str,
        draft_id: str,
        *,
        reviewed_hashes: dict[str, str],
    ):
        """Re-run exact local checks without model disclosure or upload."""
        self._require_view(service, view_id)
        result = service.prepared_review(
            draft_id,
            allow_model_review=False,
            reviewed_hashes=reviewed_hashes,
            may_apply=lambda: self.current(service, view_id),
        )
        self._require_view(service, view_id)
        return result

    def publication_review(
        self,
        service: Any,
        view_id: str,
        draft_id: str,
        *,
        accept_warnings: bool = False,
    ):
        """Freeze the existing publication service’s exact review."""
        self._require_view(service, view_id)
        result = service.publication_review(
            draft_id,
            accept_warnings=accept_warnings,
            may_apply=lambda: self.current(service, view_id),
        )
        return self._record_review(service, view_id, "publication", draft_id, result)

    def publication_confirm(self, service: Any, view_id: str, receipt: str):
        """Submit only the previously reviewed private draft."""
        review = self._require_review(service, view_id, receipt, "publication")
        result = service.publication_submit(
            receipt,
            may_apply=lambda: self.current(
                service, view_id, review_id=receipt, kind="publication"
            ),
        )
        return self._named_outcome(result, review.snapshot)

    def uninstall_review(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Snapshot current managed installation facts before removal."""
        view = self._require_view(service, view_id)
        installation = next(
            (item for item in service.list_installations() if item.get("skill_id") == skill_id),
            None,
        )
        self._require_view(service, view_id)
        if installation is None:
            raise WisdomNotFound("managed installation not found")
        review_id = "wir_" + secrets.token_hex(20)
        snapshot = deepcopy(installation)
        with self._lock:
            self._reviews[review_id] = _Review(
                view_id=view_id,
                kind="uninstall",
                subject=skill_id,
                snapshot=snapshot,
                expires_at=view.expires_at,
            )
        return {"review_id": review_id, "installation": snapshot}

    def uninstall_confirm(self, service: Any, view_id: str, review_id: str):
        """Remove only after confirming that the reviewed snapshot is unchanged."""
        self._require_view(service, view_id)
        with self._lock:
            review = self._reviews.get(str(review_id))
        if review is None or review.view_id != view_id or review.kind != "uninstall":
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        current = next(
            (
                item for item in service.list_installations()
                if item.get("skill_id") == review.subject
            ),
            None,
        )
        self._require_view(service, view_id)
        if current != review.snapshot:
            raise WisdomConflict("managed installation changed; review it again")
        result = service.uninstall(
            review.subject, may_apply=lambda: self.current(service, view_id)
        )
        with self._lock:
            self._reviews.pop(review_id, None)
        return self._named_outcome(result, review.snapshot)


def _add_candidate_routes(router, api, invoke):
    from fastapi import Query  # pylint: disable=import-outside-toplevel

    @router.get("/candidates")
    def candidates(view_id: str = Query(min_length=8, max_length=128)):
        return invoke(lambda service: api.candidates(service, view_id))

    @router.post("/candidates/{event_id}/prepare")
    def candidate_prepare(event_id: str, body: ViewBody):
        return invoke(lambda service: api.prepare_candidate(
            service, body.view_id, event_id
        ))

    @router.post("/candidates/{event_id}/not-now")
    def candidate_not_now(event_id: str, body: ViewBody):
        return invoke(lambda service: api.defer_candidate(
            service, body.view_id, event_id
        ))


def build_router(  # pylint: disable=too-many-locals
    service_factory: Callable[[], Any],
    *,
    workflow: DesktopWorkflowApi | None = None,
    route_class: type | None = None,
):
    """Build the supported FastAPI router without owning host authentication.

    The host mounts this router below its authenticated plugin namespace. The
    supplied factory must already be inside the request's validated profile
    scope; no owner/profile value is accepted in a JSON body.
    """
    from fastapi import (  # pylint: disable=import-outside-toplevel
        APIRouter,
        HTTPException,
        Query,
    )
    from fastapi.routing import APIRoute  # pylint: disable=import-outside-toplevel

    api = workflow or DesktopWorkflowApi()
    router = APIRouter(route_class=route_class or APIRoute)

    def invoke(operation: Callable[[Any], Any]):
        service = None
        try:
            service = service_factory()
            return operation(service)
        except WisdomAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except WisdomUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except WisdomNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (WisdomConflict, WisdomValidationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WisdomError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                close()

    router.add_api_route(
        "/views", lambda: invoke(api.open_view), methods=["POST"], name="open_view"
    )

    @router.post("/notices/end-turn")
    def end_turn_notices():
        def check(service):
            notices: list[dict[str, Any]] = []
            result = service.end_turn_notices(notices.extend)
            return {**result, "notices": notices}

        return invoke(check)

    router.add_api_route(
        "/views/{view_id}",
        lambda view_id: invoke(lambda service: api.close_view(service, view_id)),
        methods=["DELETE"],
        name="close_view",
    )

    @router.get("/bootstrap")
    def bootstrap():
        def read(service):
            status = service.status(include_operations=True)
            configured = status.get("configured") is True
            return {
                "status": status,
                "settings": service.notification_config(),
                "activity": (
                    service.inbox() if configured else {
                        "mode": "agent", "assessments": [], "interactions": [],
                        "installed_setup": [],
                    }
                ),
                "installations": service.list_installations() if configured else [],
            }
        return invoke(read)

    @router.get("/catalog")
    def catalog(q: str | None = Query(default=None, max_length=256)):
        return invoke(lambda service: {"skills": service.search_skills(q)})

    router.add_api_route(
        "/catalog/{skill_id}",
        lambda skill_id: invoke(lambda service: service.show(skill_id)),
        methods=["GET"],
        name="catalog_detail",
    )
    router.add_api_route(
        "/catalog/{skill_id}/versions",
        lambda skill_id: invoke(
            lambda service: {"versions": service.versions(skill_id)}
        ),
        methods=["GET"],
        name="catalog_versions",
    )
    router.add_api_route(
        "/installed",
        lambda: invoke(
            lambda service: {"installations": service.list_installations()}
        ),
        methods=["GET"],
        name="installed",
    )

    @router.get("/updates/{skill_id}")
    def update_check(skill_id: str, view_id: str = Query(min_length=8, max_length=128)):
        return invoke(lambda service: api.check_update(service, view_id, skill_id))

    @router.post("/install/review")
    def install_review(body: InstallReviewBody):
        return invoke(lambda service: api.install_review(
            service,
            body.view_id,
            body.reference,
            update_mode=body.update_mode,
            accept_warnings=body.accept_warnings,
        ))

    @router.post("/install/confirm")
    def install_confirm(body: ReceiptBody):
        return invoke(lambda service: api.install_confirm(
            service,
            body.view_id,
            body.receipt,
            accept_partial=body.accept_partial,
        ))

    @router.post("/update/review")
    def update_review(body: UpdateReviewBody):
        return invoke(lambda service: api.update_review(
            service,
            body.view_id,
            body.skill_id,
            preserve_modified=body.preserve_modified,
            accept_sensitive=body.accept_sensitive,
            accept_partial=body.accept_partial,
            accept_warnings=body.accept_warnings,
        ))

    @router.post("/update/confirm")
    def update_confirm(body: ReceiptBody):
        return invoke(lambda service: api.update_confirm(
            service, body.view_id, body.receipt
        ))

    @router.post("/uninstall/review")
    def uninstall_review(body: SkillBody):
        return invoke(lambda service: api.uninstall_review(
            service, body.view_id, body.skill_id
        ))

    @router.post("/uninstall/confirm")
    def uninstall_confirm(body: ReviewBody):
        return invoke(lambda service: api.uninstall_confirm(
            service, body.view_id, body.review_id
        ))

    @router.patch("/settings/mode")
    def settings_mode(body: ModeBody):
        return invoke(lambda service: api.set_notification_mode(
            service,
            body.view_id,
            body.mode,
            expected_mode=body.expected_mode,
        ))

    @router.get("/settings")
    def settings(view_id: str = Query(min_length=8, max_length=128)):
        return invoke(lambda service: api.notification_settings(service, view_id))

    @router.patch("/settings/mute")
    def settings_mute(body: MuteBody):
        return invoke(lambda service: api.choose_notification_mute(
            service,
            body.view_id,
            body.control_id,
            body.duration,
        ))

    router.add_api_route(
        "/enrollment/disclosure",
        lambda: {
            "version": "wisdom-desktop-v1",
            "text": (
                WISDOM_DISCLOSURE + " Enrolling does not upload local skill contents. "
                "Install, setup, and publication remain separate approvals."
            ),
        },
        methods=["GET"],
        name="enrollment_disclosure",
    )

    @router.post("/enrollment/confirm")
    def enrollment_confirm(body: EnrollmentBody):
        if body.disclosure_version != "wisdom-desktop-v1":
            raise HTTPException(status_code=409, detail="Disclosure changed; review it again")
        return invoke(lambda service: api.enroll(
            service, body.view_id, accepted=body.accepted
        ))

    @router.post("/drafts/prepare")
    def draft_prepare(body: DraftBody):
        return invoke(lambda service: api.prepare_draft(
            service, body.view_id, body.skill_name
        ))

    _add_candidate_routes(router, api, invoke)

    @router.get("/drafts")
    def draft_list(view_id: str = Query(min_length=8, max_length=128)):
        return invoke(lambda service: api.prepared_drafts(service, view_id))

    @router.get("/drafts/{draft_id}")
    def draft_inspect(
        draft_id: str,
        view_id: str = Query(min_length=8, max_length=128),
    ):
        return invoke(lambda service: api.prepared_inspect(
            service, view_id, draft_id
        ))

    @router.put("/drafts/{draft_id}")
    def draft_save(draft_id: str, body: DraftSaveBody):
        return invoke(lambda service: api.prepared_save(
            service,
            body.view_id,
            draft_id,
            author_description=body.author_description,
            files=body.files,
            expected_hashes=body.expected_hashes,
        ))

    @router.post("/drafts/{draft_id}/review")
    def draft_review(draft_id: str, body: PreparedReviewBody):
        return invoke(lambda service: api.prepared_review(
            service,
            body.view_id,
            draft_id,
            reviewed_hashes=body.reviewed_hashes,
        ))

    @router.post("/publication/review")
    def publication_review(body: DraftReviewBody):
        return invoke(lambda service: api.publication_review(
            service,
            body.view_id,
            body.draft_id,
            accept_warnings=body.accept_warnings,
        ))

    @router.post("/publication/confirm")
    def publication_confirm(body: ReceiptBody):
        return invoke(lambda service: api.publication_confirm(
            service, body.view_id, body.receipt
        ))

    return router
