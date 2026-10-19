"""Thin Desktop adapter over the existing owner-bound workflow service.

The renderer receives opaque, short-lived view IDs. They are not Hermes session
identities and carry no authority by themselves: every use is checked against
the currently scoped profile and freshly read account/enrolment owner.
"""
# pylint: disable=too-many-lines

from __future__ import annotations

import base64
import dataclasses
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

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
from .install_review import confirmed_file_change, confirmed_file_install
from .release_scope import active as release_scope


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


class CombineBody(ViewBody):
    """Resolve one Needs-a-look item for a skill you edited."""

    choice: str = Field(pattern=r"^(combine|keep_mine|use_new)$")


class AutoUpdateBody(ViewBody):
    """Turn automatic updates on or off for one installed team skill."""

    enabled: bool


class InstallNowBody(ViewBody):
    """One-click install; ``confirmed`` answers the short flagged confirm."""

    skill_id: str = Field(min_length=1, max_length=512)
    auto_update: bool = True
    confirmed: bool = False


class ShareStartBody(ViewBody):
    """Open the share dialog for one local skill (prepares a private draft)."""

    skill_name: str = Field(min_length=1, max_length=256)


class ShareConfirmBody(ViewBody):
    """Share one prepared draft into a folder, optionally under a new name."""

    draft_id: str = Field(min_length=1, max_length=512)
    skill_name: str = Field(min_length=1, max_length=256)
    folder: str | None = Field(default=None, max_length=128)
    rename: str | None = Field(default=None, max_length=128)
    accept_warnings: bool = False


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
    files_changed: bool = False
    setup_claimed: bool = False


class _WorkflowCore:  # pylint: disable=too-many-public-methods
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
        if confirmed_file_install(result) and review.snapshot.get("setup_commands"):
            with self._lock:
                if self._reviews.get(receipt) is review:
                    review.files_changed = True
        return self._named_outcome(result, review.snapshot)

    def install_setup_confirm(self, service: Any, view_id: str, receipt: str) -> dict[str, Any]:
        """Spend separate setup consent only for this view's completed file install."""
        return self._setup_confirm(service, view_id, receipt, kind="install")

    def update_setup_confirm(self, service: Any, view_id: str, receipt: str) -> dict[str, Any]:
        """Spend separate setup consent only for this view's completed file update."""
        return self._setup_confirm(service, view_id, receipt, kind="update")

    def _setup_confirm(self, service: Any, view_id: str, receipt: str, *, kind: str):
        review = self._require_review(service, view_id, receipt, kind)
        with self._lock:
            if (not review.files_changed or review.setup_claimed
                    or not review.snapshot.get("setup_commands")):
                raise WisdomConflict("Setup permission is not available for this file change")
            review.setup_claimed = True
        apply = service.install_setup_apply if kind == "install" else service.update_setup_apply
        result = apply(
            receipt,
            may_apply=lambda: self.current(
                service, view_id, review_id=receipt, kind=kind
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
        if (confirmed_file_change(result, action="updated")
                and review.snapshot.get("setup_commands")):
            with self._lock:
                if self._reviews.get(receipt) is review:
                    review.files_changed = True
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
        """Prepare an owner-private draft with the configured editorial route."""
        self._require_view(service, view_id)
        return service.suggest(
            skill_name,
            allow_model_review=None,
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
            allow_model_review=None,
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


# ---------------------------------------------------------------------------
# cw/v1 Desktop page adapter
#
# The Collective Wisdom page (Installed | Browse, detail, Needs a look, share
# dialog) reads one aggregated library and calls a few one-click actions. The
# product state model lives in the CORE modules ``team_status``, ``combine``,
# ``folders`` and ``consent``; this adapter only imports and calls them (lazily,
# so an older build still mounts the legacy routes) and reuses the existing
# review/receipt kernels for install, uninstall and publication.
# ---------------------------------------------------------------------------

_USED_BY_THRESHOLD = 10
_PREVIEW_LIMIT = 64 * 1024
_SLUG_OWNED_CODES = frozenset({"slug_owned", "slug_taken", "name_taken", "slug_conflict"})
_OWNER_NAME_FIELDS = (
    "owner_display_name", "owner_name", "owner_email", "created_by_display_name",
    "author_name", "author",
)


def _core(name: str):
    """Import one CORE v1 module (team_status, combine, folders, consent)."""
    return import_module(f"{__package__}.{name}")


def _call_optional(function: Callable[..., Any], *args: Any,
                   optional: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    """Pass keyword args CORE is adding only when this build's function accepts them."""
    import inspect  # pylint: disable=import-outside-toplevel

    extra: dict[str, Any] = {}
    if optional:
        try:
            parameters = inspect.signature(function).parameters
            accepts_any = any(p.kind is p.VAR_KEYWORD for p in parameters.values())
            extra = {key: value for key, value in optional.items()
                     if accepts_any or key in parameters}
        except (TypeError, ValueError):
            extra = {}
    return function(*args, **kwargs, **extra)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _publication_slug(value: str) -> str:
    """Mirror PublicationService._slug for the best-effort collision pre-check."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:64]


def _owner_label(item: dict[str, Any], account_id: str | None) -> tuple[str, bool]:
    owner_id = item.get("owner_user_id") or item.get("created_by_user_id")
    if account_id and owner_id == account_id:
        return "You", True
    for field in _OWNER_NAME_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200], False
    return "A teammate", False


def _used_by(count: object) -> dict[str, Any]:
    number = count if isinstance(count, int) and not isinstance(count, bool) else None
    if number is None:
        return {"count": None, "label": None}
    if number < _USED_BY_THRESHOLD:
        return {"count": None, "label": "Under 10"}
    return {"count": number, "label": f"Used by {number}"}


def _first_line(value: object, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _read_preview(directory: object) -> dict[str, Any] | None:
    """Read SKILL.md and a bounded file list from one local skill directory."""
    if not isinstance(directory, str) or not directory:
        return None
    root = Path(directory)
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        files = []
        for path in sorted(root.rglob("*")):
            if len(files) >= 200:
                break
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root).as_posix()
                if relative.startswith(".") or relative == "skill.manifest.json":
                    continue
                files.append({"path": relative, "size": path.stat().st_size})
        skill_md = root / "SKILL.md"
        text = None
        if skill_md.is_file() and not skill_md.is_symlink():
            with skill_md.open("rb") as handle:
                text = handle.read(_PREVIEW_LIMIT).decode("utf-8", errors="replace")
        return {"skill_md": text, "files": files}
    except (OSError, ValueError):
        return None


def _portal_url(service: Any, skill_id: str) -> str | None:
    try:
        account = service.account
        base = str(account.portal_base_url).rstrip("/")
        return (f"{base}/wisdom/skills/{quote(skill_id, safe='')}?"
                f"{urlencode({'org_id': account.organization_id})}")
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _team_name(service: Any) -> str | None:
    try:
        name = service.native_team_name()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    return name if isinstance(name, str) and name.strip() else None


def _status_rows(service: Any) -> tuple[list[dict[str, Any]], bool]:
    """Call team_status.team_statuses; unavailable only when CORE is not built yet."""
    try:
        module = _core("team_status")
    except ImportError:
        return [], False
    return [_jsonable(row) for row in module.team_statuses(service)], True


def _file_sizes(files: object) -> list[dict[str, Any]]:
    rows = []
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        if item["path"] == "skill.manifest.json":
            continue
        size = item.get("size")
        if not isinstance(size, int):
            encoded = item.get("content_base64")
            if isinstance(encoded, str):
                try:
                    size = len(base64.b64decode(encoded, validate=True))
                except (TypeError, ValueError):
                    size = None
            elif isinstance(item.get("content_utf8"), str):
                size = len(item["content_utf8"].encode("utf-8"))
        rows.append({"path": item["path"], "size": size})
    return rows


def _failed_checks(review: dict[str, Any]) -> dict[str, Any]:
    """Collapse local checks: mention them only when one fails."""
    security = _dict(review.get("security_check"))
    scan = _dict(review.get("local_scan"))
    guard = scan.get("guard") if isinstance(scan.get("guard"), dict) else scan
    findings = guard.get("findings") if isinstance(guard.get("findings"), list) else []
    messages = []
    for finding in findings[:6]:
        if isinstance(finding, dict):
            where = finding.get("path")
            text = finding.get("message") or finding.get("summary") or finding.get("detail")
            if text:
                messages.append(f"{where}: {text}" if where else str(text))
        elif finding:
            messages.append(str(finding))
    warning = security.get("local_status") == "warning_required" or guard.get("allowed") is False
    blocked = security.get("upload_allowed") is False and not warning
    return {
        "ok": not warning and not blocked,
        "needs_acknowledgement": warning and not blocked,
        "blocked": blocked,
        "messages": messages or (
            [str(security.get("summary"))] if (warning or blocked) and security.get("summary") else []
        ),
    }


def _rename_frontmatter(text: str, new_name: str) -> str:
    """Rewrite only the ``name:`` key inside the SKILL.md YAML frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise WisdomValidationError("SKILL.md has no frontmatter to rename")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            break
        if re.match(r"^name\s*:", lines[index]):
            lines[index] = f"name: {new_name}"
            return "\n".join(lines)
    raise WisdomValidationError("SKILL.md frontmatter has no name to rename")


def _local_unshared_skills(service: Any) -> list[dict[str, Any]]:
    """List this profile's own local skills not yet shared (the Share picker)."""
    from . import top_skills as top  # pylint: disable=import-outside-toplevel
    from .qualification import _eligible_path  # pylint: disable=import-outside-toplevel

    try:
        installations = service.installed.installations()
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        installations = []
    try:
        published = top.published_source_paths(service._state.get("wisdom-core"))  # pylint: disable=protected-access
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        published = []
    api = top.load_usage_api()
    override = top._home_override()  # pylint: disable=protected-access
    if api is None or override is None:
        # Fall back to the usage-ranked list the CLI uses.
        ranked = service.top_skills()
        return [
            {"name": item.name, "description": item.description}
            for item in ranked.items if not item.shared
        ]
    home = Path(service.profile[1]).expanduser().absolute()
    skills_root = home / "skills"
    team, shared = top._managed_paths(installations)  # pylint: disable=protected-access
    shared |= {top._resolved(path) for path in published if path}  # pylint: disable=protected-access
    rows: list[dict[str, Any]] = []
    with top._profile_home(home, override):  # pylint: disable=protected-access
        usage = api.load_usage() if callable(getattr(api, "load_usage", None)) else {}
        usage = usage if isinstance(usage, dict) else {}
        not_owned = top._ownership_filter(api)  # pylint: disable=protected-access
        hidden = (set(top._call(api, "list_archived_skill_names", ()))  # pylint: disable=protected-access
                  | set(top._call(api, "read_suppressed_names", ())))  # pylint: disable=protected-access
        resolver = top._skill_resolver(api, skills_root)  # pylint: disable=protected-access
        names = (sorted({name for name, _md in api._iter_skill_mds(  # pylint: disable=protected-access
            skills_root, local_only=True)}) if skills_root.is_dir() else [])
        for name in names:
            record = usage.get(name) if isinstance(usage.get(name), dict) else {}
            if name in hidden or not_owned(name, record):
                continue
            path = _eligible_path(name, skills_root=skills_root, skill_resolver=resolver)
            if path is None or top._team_installed(team, name, path) or str(path) in shared:  # pylint: disable=protected-access
                continue
            rows.append({
                "name": name,
                "description": top._description(path / "SKILL.md"),  # pylint: disable=protected-access
                "uses": top._count(record.get("use_count")),  # pylint: disable=protected-access
            })
    rows.sort(key=lambda row: (-row["uses"], row["name"]))
    return rows


def _shared_by_me(service: Any) -> list[dict[str, Any]]:
    """Owner publications with whether the local source changed since sharing."""
    from .package import source_fingerprint  # pylint: disable=import-outside-toplevel

    owner, snapshot = service._named_local_state()  # pylint: disable=protected-access
    drafts = _dict(snapshot.get("drafts"))
    publications = snapshot.get("publications")
    latest: dict[str, dict[str, Any]] = {}
    for row in (publications.values() if isinstance(publications, dict) else ()):
        if not (isinstance(row, dict) and row.get("owner") == owner
                and row.get("status") == "done"
                and row.get("publication_state") == "published"):
            continue
        draft = drafts.get(row.get("local_draft_id"))
        if not isinstance(draft, dict):
            continue
        source = draft.get("source_path")
        if not isinstance(source, str):
            continue
        result = _dict(row.get("publication_result"))
        request = _dict(row.get("request"))
        slug = str(request.get("slug") or draft.get("slug") or "")
        stamp = str(row.get("updated_at") or row.get("created_at") or "")
        prior = latest.get(source)
        if prior is not None and str(prior["_stamp"]) >= stamp:
            continue
        try:
            edited = source_fingerprint(Path(source)) != draft.get("source_hash")
            present = True
        except Exception:  # pylint: disable=broad-exception-caught
            edited, present = False, False
        latest[source] = {
            "_stamp": stamp, "slug": slug, "source_name": str(draft.get("slug") or slug),
            "source_path": source, "edited": edited, "source_present": present,
            "skill_id": result.get("skill_id") or result.get("skillId"),
            "shared_at": stamp or None,
        }
    return [{k: v for k, v in row.items() if k != "_stamp"} for row in latest.values()]


def _publisher_target(row: dict[str, Any]) -> bool:
    provenance = row.get("target_provenance")
    return isinstance(provenance, dict) and provenance.get("kind") == "publisher_source"


class _LibraryMixin:  # pylint: disable=too-few-public-methods
    """cw/v1 page reads and one-click actions; mixed into DesktopWorkflowApi."""

    # -- consent ----------------------------------------------------------
    def consent(self, service: Any) -> dict[str, Any]:
        """First-run notice state plus whether this profile is enrolled."""
        try:
            state = dict(_core("consent").consent_state(service))
        except ImportError:
            # Pre-v1 build: the local enrolment *is* the consent record.
            status = service.status()
            state = {"accepted": status.get("configured") is True,
                     "disclosure_text": WISDOM_DISCLOSURE}
        status = service.status()
        return {
            "accepted": state.get("accepted") is True,
            "disclosure_text": str(state.get("disclosure_text") or WISDOM_DISCLOSURE),
            "configured": status.get("configured") is True,
            "team_name": _team_name(service),
        }

    def accept_consent(self, service: Any, view_id: str) -> dict[str, Any]:
        """Record per-org consent once, then enrol this profile if needed."""
        self._require_view(service, view_id, enrolling=True)
        try:
            result = dict(_core("consent").accept(service) or {})
        except ImportError:
            result = {}
        if service.status().get("configured") is not True:
            self.enroll(service, view_id, accepted=True)
        return {**result, "accepted": True, "configured": True}

    # -- library ----------------------------------------------------------
    def library(self, service: Any) -> dict[str, Any]:  # pylint: disable=too-many-locals
        """Everything the page lists, already grouped and display-ready."""
        account_id = getattr(service.account, "account_id", None) if service.account else None
        installations = [
            row for row in service.list_installations()
            if isinstance(row, dict) and row.get("state") == "active"
        ]
        if account_id is None:
            account_id = getattr(service.account, "account_id", None)
        statuses, status_available = _status_rows(service)
        by_status = {row.get("skill_id"): row for row in statuses}
        catalog = service.search_skills(None)
        by_id = {item.get("id"): item for item in catalog if isinstance(item, dict)}
        shared_rows = _shared_by_me(service)
        shared_ids = {row["skill_id"] for row in shared_rows if row.get("skill_id")}
        shared_slugs = {row["slug"] for row in shared_rows}

        def base(item: dict[str, Any], skill_id: str) -> dict[str, Any]:
            owner, mine = _owner_label(item, account_id)
            return {
                "skill_id": skill_id,
                "slug": item.get("slug"),
                "name": item.get("editorial_name") or item.get("slug"),
                "description": _first_line(item.get("author_description")
                                           or item.get("explanation")),
                "owner": owner,
                "owner_is_me": mine,
                "folder": item.get("folder") or None,
                "used_by": _used_by(item.get("install_count")),
                "latest_version": item.get("latest_version"),
                "updated_at": item.get("latest_published_at") or item.get("updated_at"),
                "portal_url": _portal_url(service, skill_id),
                "local_name_collision": (
                    (item.get("installation_status") or {}).get("state") == "unmanaged_local"
                ),
            }

        installed = []
        installed_ids = set()
        for row in installations:
            skill_id = row.get("skill_id")
            if not isinstance(skill_id, str):
                continue
            installed_ids.add(skill_id)
            if _publisher_target(row) or skill_id in shared_ids:
                continue
            item = by_id.get(skill_id) or {"slug": row.get("slug")}
            status = by_status.get(skill_id) or {}
            # Fallback only (CORE team_status owns this): gone from the catalog.
            no_longer = skill_id not in by_id
            kind = status.get("kind") or ("no_longer_shared" if no_longer else "up_to_date")
            auto = status.get("auto_update")
            if auto is None:
                auto = row.get("effective_update_mode") in ("AUTO_WITH_NOTICE", "REQUIRED")
            installed.append({
                **base(item, skill_id),
                "installed_version": status.get("installed_version") or row.get("version"),
                "latest_version": (status.get("latest_version") or row.get("latest_version")
                                   or item.get("latest_version")),
                "applied_at": status.get("updated_at") or row.get("updated_at")
                or row.get("installed_at"),
                "status": {
                    "kind": kind,
                    "reason": status.get("reason"),
                    "new_capabilities": status.get("new_capabilities") or [],
                    "summary": status.get("summary"),
                    "seen": status.get("seen", True),
                },
                "auto_update": bool(auto),
                "modified": bool(row.get("modified")),
                "target_path": row.get("target_path"),
            })

        shared = []
        for row in shared_rows:
            item = by_id.get(row.get("skill_id")) or next(
                (value for value in catalog if value.get("slug") == row["slug"]), None)
            skill_id = (item or {}).get("id") or row.get("skill_id")
            if not skill_id:
                continue
            entry = base(item or {"slug": row["slug"]}, skill_id)
            shared.append({
                **entry,
                "owner": "You", "owner_is_me": True,
                "edited": row["edited"],
                "source_name": row["source_name"],
                "source_path": row["source_path"],
                "no_longer_shared": item is None,
            })

        browse = []
        for item in catalog:
            skill_id = item.get("id")
            if not isinstance(skill_id, str):
                continue
            entry = base(item, skill_id)
            entry["installed"] = skill_id in installed_ids
            entry["shared_by_me"] = skill_id in shared_ids or item.get("slug") in shared_slugs
            browse.append(entry)

        needs_look = sum(1 for row in installed if row["status"]["kind"] == "needs_look")
        return {
            "team_name": _team_name(service),
            "status_available": status_available,
            "needs_look": needs_look,
            "installed": installed,
            "shared": shared,
            "browse": browse,
        }

    def statuses(self, service: Any) -> dict[str, Any]:
        """Raw three-state status rows plus the session-start note."""
        rows, available = _status_rows(service)
        note = None
        if available:
            try:
                note = _core("team_status").session_note(service)
            except Exception:  # pylint: disable=broad-exception-caught
                note = None
        return {"available": available, "statuses": rows, "session_note": note}

    def mark_seen(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Clear the Updated badge once the detail pane has shown it."""
        self._require_view(service, view_id)
        _core("team_status").mark_seen(service, skill_id)
        return {"skill_id": skill_id, "seen": True}

    def preview(self, service: Any, skill_id: str) -> dict[str, Any]:
        """SKILL.md preview: local copy when present, else published metadata."""
        for row in service.list_installations():
            if (isinstance(row, dict) and row.get("skill_id") == skill_id
                    and row.get("state") == "active"):
                found = _read_preview(row.get("target_path"))
                if found:
                    return {"skill_id": skill_id, "source": "installed", **found}
        for row in _shared_by_me(service):
            if row.get("skill_id") == skill_id:
                found = _read_preview(row.get("source_path"))
                if found:
                    return {"skill_id": skill_id, "source": "local", **found}
        detail = service.show(skill_id)
        version = (detail.get("latest_version_detail") or {}).get("version") or {}
        return {
            "skill_id": skill_id, "source": "published", "skill_md": None, "files": [],
            "description": version.get("author_description")
            or (detail.get("skill") or {}).get("author_description"),
            "version": version.get("version"),
            "published_at": version.get("published_at") or version.get("created_at"),
            "verified_facts": version.get("verified_facts"),
        }

    # -- folders ----------------------------------------------------------
    @staticmethod
    def folders(service: Any) -> dict[str, Any]:
        """Org folders with counts, plus Hermes categories (count 0)."""
        return {"folders": _jsonable(_core("folders").folder_options(service))}

    @staticmethod
    def suggest_folder(service: Any, skill_name: str) -> dict[str, Any]:
        """One suggested folder with its one-line reason; never raises in CORE."""
        return dict(_core("folders").suggest_folder(service, skill_name))

    @staticmethod
    def local_skills(service: Any) -> dict[str, Any]:
        """Local skills not shared yet, for the Share a skill… picker."""
        return {"skills": _local_unshared_skills(service)}

    # -- install / uninstall ---------------------------------------------
    def install_now(  # pylint: disable=too-many-arguments
        self, service: Any, view_id: str, skill_id: str, *,
        auto_update: bool = True, confirmed: bool = False,
    ) -> dict[str, Any]:
        """One click install; returns ``confirm`` with one sentence when flagged."""
        self._require_view(service, view_id)
        plan = _call_optional(
            service.install_plan, skill_id,
            update_mode="AUTO_WITH_NOTICE" if auto_update else "MANUAL",
            accept_warnings=bool(confirmed),
            optional={"auto_update": auto_update},
        )
        self._record_review(service, view_id, "install", skill_id, plan)
        name = plan.get("editorial_name") or plan.get("slug") or skill_id
        policy = _dict(plan.get("policy"))
        compatibility = _dict(plan.get("compatibility"))
        reasons = []
        if policy.get("scan_requires_confirmation"):
            reasons.append("the safety check flagged something in its files")
        if policy.get("compatibility_requires_confirmation"):
            limits = compatibility.get("limitations") or compatibility.get("setup_actions") or []
            reasons.append(
                "it may need extra setup here" + (f" ({_first_line(limits[0], 120)})" if limits else "")
            )
        if plan.get("setup_commands"):
            reasons.append("it has setup commands that ask separately before running")
        if (not confirmed and (policy.get("scan_requires_confirmation")
                               or policy.get("compatibility_requires_confirmation"))):
            return {"state": "confirm", "skill_id": skill_id, "skill_name": name,
                    "sentence": f"Install {name}? Hermes noticed " + "; ".join(reasons) + "."}
        if "confirm_install" not in (plan.get("permitted_actions") or []):
            blocked = compatibility.get("blocked") or []
            return {"state": "blocked", "skill_id": skill_id, "skill_name": name,
                    "sentence": (f"{name} can't be installed here: "
                                 + (_first_line(blocked[0], 160) if blocked
                                    else "a check did not pass.")) }
        result = self.install_confirm(service, view_id, plan["receipt"],
                                      accept_partial=bool(confirmed))
        done = confirmed_file_install(result)
        return {
            "state": "installed" if done else "unknown",
            "skill_id": skill_id, "skill_name": name, "auto_update": auto_update,
            "setup_commands": (plan.get("setup_commands") or []) if done else [],
            "receipt": plan["receipt"] if done and plan.get("setup_commands") else None,
            "result": result,
        }

    def uninstall_now(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Remove at once (to the archive); the page offers Undo."""
        review = self.uninstall_review(service, view_id, skill_id)
        result = self.uninstall_confirm(service, view_id, review["review_id"])
        return {**result, "skill_id": skill_id, "undo_available": True}

    def restore_uninstalled(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Undo for the Removed toast; restores the archived copy."""
        self._require_view(service, view_id)
        return dict(service.restore_uninstalled(skill_id) or {})

    def set_auto_update(self, service: Any, view_id: str, skill_id: str,
                        enabled: bool) -> dict[str, Any]:
        """Per-installed-skill auto-update switch."""
        self._require_view(service, view_id)
        return dict(service.set_auto_update(skill_id, bool(enabled)) or {})

    # -- needs a look -----------------------------------------------------
    def combine_preview(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Show full skill: the prepared combined SKILL.md (None = can't combine)."""
        self._require_view(service, view_id)
        prepared = _core("combine").prepare(service, skill_id)
        if prepared is None:
            return {"available": False, "skill_id": skill_id}
        return {"available": True, **_jsonable(prepared)}

    def combine_apply(self, service: Any, view_id: str, skill_id: str,
                      choice: str) -> dict[str, Any]:
        """Combine / Keep mine / Use new version."""
        self._require_view(service, view_id)
        return dict(_core("combine").apply(service, skill_id, choice) or {})

    def flagged_update(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """The single Update for scan-flagged access expansion."""
        self._require_view(service, view_id)
        return dict(_core("combine").apply_flagged_update(service, skill_id) or {})

    def manual_update(self, service: Any, view_id: str, skill_id: str) -> dict[str, Any]:
        """Update with auto-update off and no local edits: acknowledgements in one go."""
        plan = self.update_review(
            service, view_id, skill_id, preserve_modified=True, accept_sensitive=True,
            accept_partial=True, accept_warnings=True,
        )
        if plan.get("modified"):
            raise WisdomConflict("You edited this skill; choose Combine, Keep mine or Use new version")
        return self.update_confirm(service, view_id, plan["receipt"])

    # -- share ------------------------------------------------------------
    def share_start(self, service: Any, view_id: str, skill_name: str) -> dict[str, Any]:
        """Prepare a private draft and everything the share dialog shows."""
        prepared = self.prepare_draft(service, view_id, skill_name)
        try:
            suggestion = self.suggest_folder(service, skill_name)
        except ImportError:
            suggestion = {"folder": "", "reason": ""}
        try:
            options = self.folders(service)["folders"]
        except ImportError:
            options = []
        slug = _publication_slug(prepared.get("editorial_name") or skill_name)
        account_id = getattr(service.account, "account_id", None)
        taken = any(
            item.get("slug") == slug
            and (item.get("owner_user_id") or item.get("created_by_user_id")) != account_id
            for item in service.search_skills(slug)
        )
        self._require_view(service, view_id)
        return {
            "draft_id": prepared.get("draft_id"),
            "skill_name": skill_name,
            "slug": slug,
            "description": _first_line(prepared.get("description")),
            "files": _file_sizes(prepared.get("files")),
            "checks": _failed_checks(prepared),
            "suggestion": suggestion,
            "folder_options": options,
            "team_name": _team_name(service),
            "name_taken": taken,
        }

    def _rename_draft(self, service: Any, view_id: str, draft_id: str, new_name: str) -> None:
        inspected = self.prepared_inspect(service, view_id, draft_id)
        files = []
        for item in inspected.get("files") or []:
            row = {"path": item["path"], **({"mode": "exec"} if item.get("mode") == "exec" else {})}
            if item["path"] == "SKILL.md" and isinstance(item.get("content_utf8"), str):
                row["content_utf8"] = _rename_frontmatter(item["content_utf8"], new_name)
            elif isinstance(item.get("content_utf8"), str):
                row["content_utf8"] = item["content_utf8"]
            else:
                row["content_base64"] = item.get("content_base64")
            files.append(row)
        self.prepared_save(
            service, view_id, draft_id,
            author_description=str(inspected.get("author_description") or ""),
            files=files, expected_hashes=inspected["expected_hashes"],
        )

    def share_confirm(  # pylint: disable=too-many-arguments
        self, service: Any, view_id: str, draft_id: str, *, skill_name: str,
        folder: str | None, rename: str | None, accept_warnings: bool,
    ) -> dict[str, Any]:
        """Share one prepared draft; name collisions return ``name_taken``."""
        self._require_view(service, view_id)
        name = skill_name
        if rename and rename.strip() and rename.strip() != skill_name:
            name = rename.strip()
            self._rename_draft(service, view_id, draft_id, name)
        folder = " ".join((folder or "").split())[:48] or None
        try:
            review = _call_optional(
                service.publication_review, draft_id,
                accept_warnings=bool(accept_warnings),
                may_apply=lambda: self.current(service, view_id),
                optional={"folder": folder},
            )
        except WisdomConflict as exc:
            if exc.code in _SLUG_OWNED_CODES:
                return {"state": "name_taken", "skill_name": name}
            raise
        self._record_review(service, view_id, "publication", draft_id, review)
        checks = _failed_checks(review)
        security = _dict(review.get("security_check"))
        if security.get("upload_allowed") is not True:
            return {"state": "checks_failed", "skill_name": name, "checks": checks}
        try:
            result = _call_optional(
                service.publication_submit, review["receipt"],
                may_apply=lambda: self.current(
                    service, view_id, review_id=review["receipt"], kind="publication"),
                optional={"folder": folder},
            )
        except WisdomConflict as exc:
            if exc.code in _SLUG_OWNED_CODES:
                return {"state": "name_taken", "skill_name": name}
            raise
        if result.get("state") == "rejected" and result.get("code") in _SLUG_OWNED_CODES:
            return {"state": "name_taken", "skill_name": name}
        publication = result.get("publication_state")
        return {
            **result,
            "state": ("shared" if result.get("state") == "done" and publication == "published"
                      else "submitted" if publication == "pending_moderation"
                      else result.get("state") or "unknown"),
            "skill_name": name, "folder": folder,
        }


class DesktopWorkflowApi(_LibraryMixin, _WorkflowCore):  # pylint: disable=too-many-public-methods
    """Map UI gestures to the shared workflow without duplicating its kernels."""


def _add_v1_routes(router, api, invoke):  # pylint: disable=too-many-locals
    """cw/v1 Collective Wisdom page routes (release-scope group 'core')."""
    from fastapi import Query  # pylint: disable=import-outside-toplevel

    @router.get("/consent")
    def consent_read():
        return invoke(api.consent)

    @router.post("/consent")
    def consent_accept(body: ViewBody):
        return invoke(lambda service: api.accept_consent(service, body.view_id))

    @router.get("/library")
    def library():
        return invoke(api.library)

    @router.get("/status")
    def status_rows():
        return invoke(api.statuses)

    @router.post("/status/{skill_id}/seen")
    def status_seen(skill_id: str, body: ViewBody):
        return invoke(lambda service: api.mark_seen(service, body.view_id, skill_id))

    @router.get("/skills/{skill_id}/preview")
    def skill_preview(skill_id: str):
        return invoke(lambda service: api.preview(service, skill_id))

    @router.get("/folders")
    def folders():
        return invoke(api.folders)

    @router.get("/folders/suggest")
    def folder_suggest(skill: str = Query(min_length=1, max_length=256)):
        return invoke(lambda service: api.suggest_folder(service, skill))

    @router.get("/local-skills")
    def local_skills():
        return invoke(api.local_skills)

    @router.post("/install/now")
    def install_now(body: InstallNowBody):
        return invoke(lambda service: api.install_now(
            service, body.view_id, body.skill_id,
            auto_update=body.auto_update, confirmed=body.confirmed,
        ))

    @router.post("/uninstall/{skill_id}")
    def uninstall_now(skill_id: str, body: ViewBody):
        return invoke(lambda service: api.uninstall_now(service, body.view_id, skill_id))

    @router.post("/uninstall/{skill_id}/undo")
    def uninstall_undo(skill_id: str, body: ViewBody):
        return invoke(lambda service: api.restore_uninstalled(service, body.view_id, skill_id))

    @router.patch("/installations/{skill_id}/auto-update")
    def auto_update(skill_id: str, body: AutoUpdateBody):
        return invoke(lambda service: api.set_auto_update(
            service, body.view_id, skill_id, body.enabled
        ))

    @router.get("/combine/{skill_id}")
    def combine_preview(skill_id: str, view_id: str = Query(min_length=8, max_length=128)):
        return invoke(lambda service: api.combine_preview(service, view_id, skill_id))

    @router.post("/combine/{skill_id}")
    def combine_apply(skill_id: str, body: CombineBody):
        return invoke(lambda service: api.combine_apply(
            service, body.view_id, skill_id, body.choice
        ))

    @router.post("/update/{skill_id}/flagged")
    def update_flagged(skill_id: str, body: ViewBody):
        return invoke(lambda service: api.flagged_update(service, body.view_id, skill_id))

    @router.post("/update/{skill_id}/apply")
    def update_manual(skill_id: str, body: ViewBody):
        return invoke(lambda service: api.manual_update(service, body.view_id, skill_id))

    @router.post("/share/start")
    def share_start(body: ShareStartBody):
        return invoke(lambda service: api.share_start(service, body.view_id, body.skill_name))

    @router.post("/share/confirm")
    def share_confirm(body: ShareConfirmBody):
        return invoke(lambda service: api.share_confirm(
            service, body.view_id, body.draft_id, skill_name=body.skill_name,
            folder=body.folder, rename=body.rename, accept_warnings=body.accept_warnings,
        ))


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



def _add_catalog_read_routes(router, api, invoke):
    from fastapi import Query  # pylint: disable=import-outside-toplevel

    @router.get("/bootstrap")
    def bootstrap():
        def read(service):
            scope = release_scope()
            status = service.status(include_operations=True)
            configured = status.get("configured") is True
            result = {"status": status, "surfaces": {
                "notification_settings": scope.notification_settings,
                "share_suggestions": "candidates" in scope.desktop_route_groups,
            }, "installations": service.list_installations() if configured else []}
            if scope.notification_settings:
                result["settings"] = service.notification_config()
                result["activity"] = service.inbox() if configured else {
                    "mode": "agent", "assessments": [], "interactions": [],
                    "installed_setup": [],
                }
            return result
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


def _add_package_routes(router, api, invoke):
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

    @router.post("/install/setup")
    def install_setup_confirm(body: ReceiptBody):
        return invoke(lambda service: api.install_setup_confirm(
            service, body.view_id, body.receipt,
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

    @router.post("/update/setup")
    def update_setup_confirm(body: ReceiptBody):
        return invoke(lambda service: api.update_setup_confirm(
            service, body.view_id, body.receipt,
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


def _add_catalog_and_install_routes(router, api, invoke):
    groups = release_scope().desktop_route_groups
    router.add_api_route(
        "/views", lambda: invoke(api.open_view), methods=["POST"], name="open_view"
    )

    if "notices" in groups:
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

    _add_catalog_read_routes(router, api, invoke)

    _add_package_routes(router, api, invoke)
    # Registered after the legacy package routes so /uninstall/review and
    # /uninstall/confirm keep matching before /uninstall/{skill_id}.
    if "core" in groups:
        _add_v1_routes(router, api, invoke)
    if "settings" in groups:
        _add_settings_routes(router, api, invoke)


def _add_settings_routes(router, api, invoke):
    from fastapi import Query  # pylint: disable=import-outside-toplevel

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

    _add_catalog_and_install_routes(router, api, invoke)

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

    if "candidates" in release_scope().desktop_route_groups:
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
