"""Owned terminal command lifetime on the existing Hermes plugin APIs."""
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .account import HermesAccountAdapter
from .client import AgentLedPolicyResponse, WisdomClient
from .commands import PRIVATE_RANKING_COMMANDS, read_unavailable
from .errors import (
    WisdomAuthError, WisdomConflict, WisdomError, WisdomUnavailable, WisdomValidationError,
    describe_exception,
)
from .install_review import confirmed_file_change
from .package import PackagePolicyError
from .release_scope import active as release_scope


def _require_current(may_apply: Callable[[], bool] | None) -> None:
    """Fail closed when an optional interactive invocation has been revoked."""
    if may_apply is not None and may_apply() is not True:
        raise WisdomAuthError("Collective Wisdom review is no longer current")

SLASH_BUDGET_SECONDS = 25.0
NATIVE_INSPECTION_CHARS = 500
_UNAVAILABLE = "Collective Wisdom is unavailable. Complete setup in your terminal."
_REVIEW_UNAVAILABLE = "Couldn't open Collective Wisdom right now. Please try again."
_INSTALL_UNCERTAIN = (
    "Couldn't confirm the installation. Check installed skills before trying again."
)
logger = logging.getLogger(__name__)

def _native_review_reply(result: Any) -> Any:
    """The original native card is the reply; never echo its internal review ID."""
    if isinstance(result, dict):
        if result.get("state") == "queued":
            return ""
        if result.get("state") == "unavailable":
            return _REVIEW_UNAVAILABLE
    return result

_BOOTSTRAP = """
import importlib.util, json, sys
from pathlib import Path
request = json.loads(sys.stdin.read())
sys.path[:] = request['sys_path']
package = Path(request['package'])
spec = importlib.util.spec_from_file_location(
    '_wisdom_command_worker', package / '__init__.py',
    submodule_search_locations=[str(package)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from _wisdom_command_worker.runtime import worker_main
print(json.dumps(worker_main(request)))
"""


def current_profile() -> tuple[str, Path]:
    """Read the active profile using existing host APIs, without reading credentials."""
    name = import_module("hermes_cli.profiles").get_active_profile_name()
    home = import_module("hermes_constants").get_hermes_home()
    return name, Path(home).resolve()


def current_session_context() -> dict[str, str]:
    """Read the host-bound gateway context copied into the lifecycle callback."""
    get_session_env = import_module("gateway.session_context").get_session_env
    return {
        name: str(get_session_env(env_name, "") or "")
        for name, env_name in {
            "platform": "HERMES_SESSION_PLATFORM",
            "profile": "HERMES_SESSION_PROFILE",
            "chat_id": "HERMES_SESSION_CHAT_ID",
            "user_id": "HERMES_SESSION_USER_ID",
            "thread_id": "HERMES_SESSION_THREAD_ID",
            "scope_id": "HERMES_SESSION_SCOPE_ID",
            "message_id": "HERMES_SESSION_MESSAGE_ID",
            "session_key": "HERMES_SESSION_KEY",
            "session_id": "HERMES_SESSION_ID",
        }.items()
    }


class InvocationService:  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """Lazily compose the workflow for one captured profile and account."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        root: Path,
        profile: tuple[str, Path],
        *,
        state: Any,
        llm: Any | None = None,
        config: dict[str, Any] | None = None,
        get_config: Callable[[str, Any], Any] | None = None,
        set_config: Callable[[str, Any], Any] | None = None,
    ):
        if state is None:
            raise TypeError("host PluginState is required")
        self.root = root
        self.profile = profile
        self.llm = llm
        self.config = config or {}
        self._get_config = get_config
        self._set_config = set_config
        self._state = state
        self._installed = None
        self.account = None
        self.client = None
        self._enrollment = None
        self._direct_state = None
        self._qualification = None
        self._closed = False

    def notification_config(self) -> dict[str, Any]:
        """Read current profile notification settings through the public host API."""

        defaults = {
            "delivery_mode": "agent",
            "new_skills": "immediate",
            "updates": "immediate",
            "team_published": "immediate",
        }
        snapshot = self.config.get("notifications") or {}
        return {"notifications": {
            key: (
                self._get_config(f"notifications.{key}", snapshot.get(key, default))
                if self._get_config is not None else snapshot.get(key, default)
            )
            for key, default in defaults.items()
        }}

    def notification_mode(self) -> str:
        """Return the fail-closed live recommendation delivery mode."""

        configured = self.notification_config()["notifications"]["delivery_mode"]
        return "agent" if configured == "agent" else "fixed"

    def notification_mode_writable(self) -> bool:
        """Require both public config mutation authority and fresh readback."""

        return self._set_config is not None and self._get_config is not None

    def set_notification_mode(
        self,
        mode: str,
        *,
        expected_mode: str | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, str]:
        """Persist one owner- and invocation-fenced mode through the public host API."""

        if mode not in {"agent", "fixed"}:
            raise ValueError("Unsupported notification mode")
        owner = self.native_owner()
        if self._set_config is None or self._get_config is None:
            raise PermissionError("Collective Wisdom notification settings are read-only.")
        current = self.notification_mode()
        if expected_mode is not None and current != expected_mode:
            raise WisdomConflict("Notification mode changed; open settings again")
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        _require_current(may_apply)
        self._set_config("notifications.delivery_mode", mode)
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        if self.notification_mode() != mode:
            raise PermissionError("Notification mode change was not confirmed.")
        return {"delivery_mode": mode}

    def check_owner(self):
        """Fence profile/account changes before and after every operation."""
        if self._closed:
            raise WisdomError("Collective Wisdom plugin is unloaded")
        if current_profile() != self.profile:
            raise WisdomAuthError("Hermes profile changed; reload the plugin")
        if self.account is None:
            self.account = HermesAccountAdapter.current()
        return self.account.credentials()

    def binding(self) -> dict[str, str]:
        """Return a non-secret binding for the short-lived slash worker."""
        self.check_owner()
        return {
            "profile_name": self.account.profile_name,
            "profile_home": self.account.profile_home,
            "account_id": self.account.account_id,
            "organization_id": self.account.organization_id,
            "base_url": self.account.base_url,
        }

    def _open(self):
        self.check_owner()
        if self.client is None:
            self.client = WisdomClient(
                self.check_owner, self.account.organization_id, timeout=10,
                install_credential_provider=self._install_credentials,
                publish_credential_provider=self._publish_credentials,
                owner_user_id=self.account.account_id,
                route_base_url=self.account.base_url,
            )
        if self._enrollment is None:
            from .direct_services import (  # pylint: disable=import-outside-toplevel
                DirectState,
                EnrollmentService,
            )

            self._direct_state = self._direct_state or DirectState(self._state, self.root)
            self._enrollment = EnrollmentService(self._direct_state, self.account, self.client)
        return self._enrollment

    def _local_identity(self):
        """Read direct enrollment state without constructing credentials or a client."""
        if self._direct_state is None:
            from .direct_services import DirectState  # pylint: disable=import-outside-toplevel

            self._direct_state = DirectState(self._state, self.root)
        return self._direct_state.identity()

    @property
    def local_state(self):
        """Return the qualification domain over the injected host state."""
        if self._qualification is None:
            from .local_state import QualificationState  # pylint: disable=import-outside-toplevel

            self._qualification = QualificationState(self._state, self.root)
        return self._qualification

    def notice_feed(self, cursor: str | None):
        """Read one cursor page without claiming or settling delivery."""
        owner = self._installation_owner()
        page = self.client.feed(cursor, installation_id=owner.get("installation_id"))
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom notice owner changed")
        categories = {
            "new": "new_skill",
            "updated": "update_available",
            "installation_updated": "update_available",
        }
        events = []
        for item in page.events:
            row = item.model_dump(mode="json")
            category = categories.get(str(row.get("kind")))
            events.append({**row, "category": category})
        return type("NoticePage", (), {
            "events": events,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        })()

    def notice_policy(self) -> dict[str, Any]:
        """Project the existing organization notification defaults."""
        owner = self._installation_owner()
        policy = self.client.agent_led_policy()
        if policy.org_id != owner["organization_id"] or owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom notice policy owner changed")
        defaults = policy.notification_defaults
        settings = self.notification_config()["notifications"]
        enabled = settings["delivery_mode"] == "agent"
        return {
            "recommendation_mode": getattr(policy, "recommendation_mode", None),
            "new_skill": enabled and bool(defaults.teammate_published)
            and settings["new_skills"] == settings["team_published"] == "immediate",
            "update_available": enabled and bool(defaults.update_available)
            and settings["updates"] == "immediate",
            "skill_ready_to_share": enabled and bool(defaults.skill_ready_to_share),
        }

    def notice_version(self, skill_id: str, version: int | None) -> dict[str, Any]:
        """Read org-scoped exact-version metadata without catalog enrichment."""
        if version is None:
            raise ValueError("Publication feed requires an exact version")
        owner = self._installation_owner()
        try:
            revision = self._discovery().version(skill_id, version)
        except Exception:
            if owner != self._installation_owner():
                raise WisdomAuthError("Collective Wisdom notice owner changed") from None
            raise
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom notice owner changed")
        return revision

    def notice_publishers(
        self, references: list[dict[str, Any]],
    ) -> dict[tuple[str, int], dict[str, str | None]]:
        """Resolve at most one bounded Portal batch for the current owner."""
        from .publisher_names import publisher_names  # pylint: disable=import-outside-toplevel
        owner = self._installation_owner()
        assert self.client is not None
        result = publisher_names(self.account, references, session=self.client.session)
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom publisher owner changed")
        return result

    def notice_candidates(self) -> list[dict[str, Any]]:
        """Return qualified local candidates without model-generated presentation."""
        owner = self._installation_owner()
        if self.local_state.owner() != owner:
            return []
        return [
            {
                **row,
                "category": "skill_ready_to_share",
                "skill_name": (row.get("payload") or {}).get("skill_name")
                or row.get("skill_id"),
            }
            for row in self.local_state.candidates()
            if row.get("state") == "unread"
            and (skill := self.local_state.local_skill(str(row.get("skill_id") or "")))
            and skill.get("deleted_at") is None
            and skill.get("current_hash") == row.get("content_hash")
        ]

    def notice_preferences(self, references: list[dict[str, Any]]) -> dict[str, Any]:
        """Read remote mute and exact suppressions; never stage local delivery work."""
        from .notices import suppression_key  # pylint: disable=import-outside-toplevel

        owner = self._installation_owner()
        mute = self.client.recommendation_mute()
        keyed = [
            (
                str(reference["notice_key"]),
                suppression_key(reference),
            )
            for reference in references
        ]
        remote_keys = list(dict.fromkeys(remote for _local, remote in keyed))
        rows = self.client.recommendation_suppressions(remote_keys) if remote_keys else []
        if mute.org_id != owner["organization_id"] or owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom notice preference owner changed")
        by_remote = {row.key: row.suppress_until for row in rows}
        suppressed = {local: by_remote[remote] for local, remote in keyed if remote in by_remote}
        return {"muted": bool(mute.muted), "suppressed": suppressed}

    def end_turn_notices(self, renderer: Callable[[list[dict[str, Any]]], Any] | None,
                         *, candidate_event_id: str | None = None):
        """Run one failure-isolated best-effort check for a supported renderer."""
        from .notices import EndTurnNotices  # pylint: disable=import-outside-toplevel

        if self._direct_state is None:
            from .direct_services import DirectState  # pylint: disable=import-outside-toplevel

            self._direct_state = DirectState(self._state, self.root)
        return EndTurnNotices(self._direct_state, lambda: self).run(
            renderer=renderer, candidate_event_id=candidate_event_id,
        )

    def _install_credentials(self):
        self.check_owner()
        return self.account.credentials(scope="wisdom:install")

    def _publish_credentials(self):
        self.check_owner()
        return self.account.credentials(scope="wisdom:publish")

    def setup(
        self,
        *,
        accept_disclosure: bool,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Create state only after explicit terminal acceptance and scope checks."""
        if not accept_disclosure:
            raise WisdomValidationError("Collective Wisdom requires disclosure acceptance")
        _require_current(may_apply)
        self._install_credentials()
        _require_current(may_apply)
        result = self._open().setup(
            accept_disclosure=True, may_apply=may_apply
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def status(self, *, include_operations=False):
        """Inspect local setup without enrolling or contacting the Gateway."""
        self.check_owner()
        if self._local_identity() is None:
            return {"configured": False, "state": "not_configured", "reason": "not_configured"}
        result = self._open().status()
        if include_operations and result.get("configured"):
            owner = {**self.binding(), "installation_id": result.get("installation_id")}
            managed = [
                {"operation_id": operation["id"], "kind": operation["kind"],
                 "stage": operation["stage"], "skill_id": operation["skill_id"]}
                for operation in self.installed.pending_operations()
                if operation.get("payload", {}).get("owner") == owner
            ]
            assert self._direct_state is not None
            local = self._direct_state.pending_direct_operations(owner)
            result["pending_operations"] = managed + local
        self.check_owner()
        return result

    def _installation_owner(self):
        self.require_setup()
        self._install_credentials()
        return self._open().owner()

    def _installation_services(self):
        from .install_execution import InstallExecutor  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .install_receipts import InstallReceipts  # pylint: disable=import-outside-toplevel
        from .install_review import InstallReviewer  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        store = self.installed
        host = InstallationHost(self.profile[1])
        receipts = InstallReceipts(store, self._installation_owner)
        reviewer = InstallReviewer(self.client, store, receipts, host, self._installation_owner)
        executor = InstallExecutor(self.client, store, receipts, reviewer,
                                   host, self._installation_owner)
        return reviewer, executor

    def _setup_services(self):
        from .direct_services import SetupService  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .setup_host import SetupHost  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        assert self._direct_state is not None
        return SetupService(
            self._direct_state,
            self.installed,
            InstallationHost(self.profile[1]),
            SetupHost(),
            self._installation_owner,
        )

    def _update_services(self):
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .update_execution import UpdateExecutor  # pylint: disable=import-outside-toplevel
        from .update_receipts import UpdateReceipts  # pylint: disable=import-outside-toplevel
        from .update_review import UpdateReviewer  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        store = self.installed
        host = InstallationHost(self.profile[1])
        receipts = UpdateReceipts(store, self._installation_owner)
        reviewer = UpdateReviewer(
            self.client, store, receipts, host, self._installation_owner
        )
        executor = UpdateExecutor(
            self.client, store, receipts, reviewer, host, self._installation_owner
        )
        return reviewer, executor

    def _contribution_reviewer(self):
        """Build the single local contribution workflow over the profile store."""

        from .contribution_review import ContributionReviewer  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        assert self._direct_state is not None
        return ContributionReviewer(
            self._direct_state,
            InstallationHost(self.profile[1]),
            self._installation_owner,
            llm=self.llm,
            source_store=self.local_state,
        )

    def _publication_service(self):
        """Build the direct PluginState-backed publication service."""

        from .direct_services import PublicationService  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .publisher_enrollment import PublisherEnrollment  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        if self.client is None or self._direct_state is None:
            raise WisdomError("Collective Wisdom publication service is unavailable")
        return PublicationService(
            self._direct_state,
            self._contribution_reviewer(),
            self.client,
            self._installation_owner,
            PublisherEnrollment(
                self._direct_state, self.installed, self.client,
                InstallationHost(self.profile[1]), self._installation_owner,
            ),
        )

    def suggest(
        self,
        skill_name,
        *,
        allow_model_review=None,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Prepare one exact owner-private local draft without submission authority."""

        _require_current(may_apply)
        owner = self._installation_owner()
        _require_current(may_apply)
        self.local_state.bind_owner(owner)
        # An explicitly configured refusal is different from the historical
        # missing/default-false setting. Never override the former implicitly.
        try:
            configured = (self._get_config("qualification.allow_model_review", None)
                          if self._get_config is not None else None)
        except Exception:  # pylint: disable=broad-exception-caught
            configured = False
        if configured is False or not release_scope().model_editorial:
            allow_model_review = False
        result = self._contribution_reviewer().prepare(
            skill_name,
            allow_model_review=allow_model_review,
            may_apply=may_apply,
        )
        self.check_owner()
        _require_current(may_apply)
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom contribution owner changed")
        return result

    @property
    def qualification_state(self):
        """Return the owner-bound qualification aggregate."""

        self.require_setup()
        return self.local_state

    @property
    def installed(self):
        """Resolve the injected, profile-fenced installed-skill aggregate."""

        if self._installed is None:
            from .installed_state import InstalledState  # pylint: disable=import-outside-toplevel

            self._installed = InstalledState(self._state, self.root)
        return self._installed

    def notifications(self, *, mark_seen=False):
        """Return manually discoverable local candidates; notices are not receipts."""
        del mark_seen
        owner = self._installation_owner()
        if self.local_state.owner() != owner:
            return {"events": [], "more": False}
        return {"events": self.notice_candidates(), "more": False}

    def inbox(self, *, invocation=None):
        """Catch up one page, then read owner-bound facts and reviews."""
        from .native_review import NativeReviewController  # pylint: disable=import-outside-toplevel
        from .review_state import ReviewState  # pylint: disable=import-outside-toplevel
        owner = self._installation_owner()
        catch_up = self.end_turn_notices(None)
        assert self._direct_state is not None
        known = self._direct_state.known_publications()
        mode = self.notification_mode()
        assessments = self.notice_candidates()[-100:]
        interactions = NativeReviewController(ReviewState(self._state, self.root)).pending(
            owner, invocation=invocation, limit=100
        )
        installed_setup = [{
            "kind": "installed", "identity": row["skill_id"],
            "version": row["version"], "content_hash": row["content_hash"],
            "instruction": (
                "Inspect installed setup; file installation does not establish readiness."
            ),
        } for row in self.installed.installations(owner) if row["state"] == "active"][:100]
        self.check_owner()
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom inbox owner changed")
        return {
            "mode": mode, "assessments": assessments, "interactions": interactions,
            "installed_setup": installed_setup, "known_publications": known,
            "catch_up": catch_up,
        }

    def prepare_candidate_offer(self, event_id):
        """Prepare once before the first actionable owner-private offer."""
        from .setup_lock import managed_operation_guard  # pylint: disable=import-outside-toplevel
        owner = self._installation_owner()
        event = self.qualification_state.local_event(event_id)
        if (self.qualification_state.owner() != owner or not event
                or event.get("organization_id") != owner["organization_id"]
                or event.get("kind") != "wisdom.candidate"):
            raise WisdomError("Collective Wisdom candidate was not found")
        expected = event["content_hash"]
        lock_key = "candidate-preparation:" + json.dumps(
            [owner, event_id, expected], sort_keys=True
        )
        with managed_operation_guard(self.local_state.root, lock_key):
            return self._prepare_candidate_offer_locked(event_id, owner, expected)

    def _reopen_candidate_preparation(self, event_id, owner, expected, source, previous):
        """Validate and, if needed, rebind the saved exact editorial draft."""
        from .package import source_fingerprint  # pylint: disable=import-outside-toplevel
        draft = (self._direct_state.draft(previous.get("draft_id"))
                 if self._direct_state else None)
        if (not draft or draft.get("owner") != owner
                or draft.get("source_hash") != source_fingerprint(source)):
            raise WisdomConflict("candidate preparation changed; prepare again")
        hashes = {"content": draft.get("content_hash"),
                  "author_description": draft.get("description_hash"),
                  "package_manifest": draft.get("manifest_hash")}
        if hashes != previous.get("hashes"):
            # A saved, complete editorial overlay can outlive an interrupted
            # binding update. Reopen verifies its exact files before rebinding.
            if draft.get("editorial_complete_hash") != hashes["content"]:
                raise WisdomConflict("candidate preparation changed; prepare again")
            self.prepared_review(draft["id"], allow_model_review=False)
            self.qualification_state.save_candidate_preparation(
                event_id, expected, {"draft_id": draft["id"], "hashes": hashes},
                previous=previous,
            )
            previous = {"draft_id": draft["id"], "hashes": hashes}
        inspected = self.native_candidate_inspect(event_id)
        return previous, inspected, draft

    def _candidate_offer_source(self, event, expected):
        """Resolve the still-current local source of the qualified event."""
        from .qualification import snapshot_tree  # pylint: disable=import-outside-toplevel
        payload = event.get("payload") or {}
        name = str(payload.get("skill_name") or event.get("skill_id") or "")
        source = self._contribution_reviewer().source(name)
        local = self.qualification_state.local_skill(str(event.get("skill_id")))
        if (not local or local.get("deleted_at") is not None
                or local.get("current_hash") != expected
                or str(source) != local.get("canonical_path")):
            raise WisdomConflict("candidate source changed")
        if snapshot_tree(source)[0] != expected:
            raise WisdomConflict("candidate source changed")
        return name, source

    def _prepare_candidate_offer_locked(self, event_id, owner, expected):  # pylint: disable=too-many-locals
        """Bind one saved draft to the candidate while holding its operation guard."""
        event = self.qualification_state.local_event(event_id)
        if (self._installation_owner() != owner or self.qualification_state.owner() != owner
                or not event or event.get("content_hash") != expected):
            raise WisdomConflict("candidate changed during preparation")
        payload = event.get("payload") or {}
        from .qualification import snapshot_tree  # pylint: disable=import-outside-toplevel
        name, source = self._candidate_offer_source(event, expected)
        previous = payload.get("preparation")
        if previous:
            previous, inspected, draft = self._reopen_candidate_preparation(
                event_id, owner, expected, source, previous,
            )
            if (inspected["facts"]["editorial_status"] == "complete" or self.llm is None
                    or draft.get("author_edited")):
                return inspected
        else:
            inspected = self.native_candidate_inspect(event_id)
        try:
            prepared = self.suggest(name)
        except (PackagePolicyError, WisdomValidationError, OSError):
            inspected["facts"]["editorial_failure"] = "package_preparation_failed"
            return inspected
        if snapshot_tree(source)[0] != expected:
            raise WisdomConflict("candidate source changed during preparation")
        if self._installation_owner() != self.qualification_state.owner():
            raise WisdomAuthError("candidate owner changed during preparation")
        binding = {"draft_id": prepared["draft_id"], "hashes": prepared["hashes"]}
        if previous is None:
            self.qualification_state.save_candidate_preparation(event_id, expected, binding)
        else:
            self.qualification_state.save_candidate_preparation(
                event_id, expected, binding, previous=previous,
            )
        return self.native_candidate_inspect(event_id)

    def native_candidate_inspect(self, event_id):  # pylint: disable=too-many-locals
        """Inspect one exact owner-private candidate without catalog disclosure."""
        owner = self._installation_owner()
        store = self.qualification_state
        if store.owner() != owner:
            raise WisdomError("Collective Wisdom candidate was not found")
        event = store.local_event(event_id)
        if (
            not event or event.get("kind") != "wisdom.candidate"
            or event.get("organization_id") != owner["organization_id"]
        ):
            raise WisdomError("Collective Wisdom candidate was not found")
        local = store.local_skill(str(event.get("skill_id") or ""))
        if not local or local.get("deleted_at") is not None:
            raise WisdomError("Collective Wisdom candidate was not found")
        if local.get("current_hash") != event.get("content_hash"):
            raise WisdomError("Collective Wisdom candidate changed")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        from .qualification import snapshot_tree  # pylint: disable=import-outside-toplevel
        if snapshot_tree(Path(local["canonical_path"]))[0] != event["content_hash"]:
            raise WisdomConflict("candidate source changed")
        editorial = {key: payload.get(key) for key in (
            "editorial_name", "editorial_description", "coworker_benefit"
        ) if payload.get(key)}
        preparation = payload.get("preparation") or {}
        checked = None
        if preparation:
            prepared_id = preparation.get("draft_id")
            draft = (self._direct_state.draft(prepared_id)
                     if self._direct_state and isinstance(prepared_id, str) else None)
            if (not draft or draft.get("owner") != owner
                or {"content": draft.get("content_hash"),
                    "author_description": draft.get("description_hash"),
                    "package_manifest": draft.get("manifest_hash")}
                != preparation.get("hashes")):
                raise WisdomConflict("candidate preparation changed; prepare again")
            checked = self._contribution_reviewer().reopen(draft["id"])
            editorial = {key: checked[key] for key in (
                "editorial_name", "editorial_description", "coworker_benefit"
            ) if checked.get(key)}
        result = {
            "kind": "candidate", "event_id": str(event["event_id"]),
            "content_hash": str(event["content_hash"]),
            "facts": {
                "name": str(payload.get("skill_name") or event.get("skill_id") or "Local skill"),
                "qualification": event.get("qualification"),
                "editorial": editorial,
                "editorial_status": (
                    checked["editorial_status"] if checked is not None else "incomplete"
                ),
                "editorial_failure": checked.get("editorial_failure") if checked else None,
            },
            "hashes": preparation.get("hashes", {}),
            "security_check": checked["security_check"] if checked is not None else {},
            "professionalism_check": (
                checked["professionalism_check"] if checked is not None else {}
            ),
            "networked": False,
        }
        self.check_owner()
        if owner != self._installation_owner():
            raise WisdomAuthError("candidate owner changed during inspection")
        if snapshot_tree(Path(local["canonical_path"]))[0] != event["content_hash"]:
            raise WisdomConflict("candidate source changed")
        return result

    def native_skill_inspect(self, skill_id, *, version):
        """Inspect one exact immutable public skill version."""
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("select an exact version before inspecting a shared skill")
        result = self._discovery().version(skill_id, version)
        self.check_owner()
        return {"kind": "skill", "skill_id": skill_id, "version": version, "facts": result}

    def native_candidate_prepare(self, event_id):
        """Prepare a local candidate for native publication review without upload."""
        inspected = self.native_candidate_inspect(event_id)
        skill_name = str((inspected.get("facts") or {}).get("name") or "")
        from .errors import SharePackagingError  # pylint: disable=import-outside-toplevel
        try:
            prepared = self.suggest(skill_name, allow_model_review=False)
        except (OSError, PackagePolicyError, WisdomValidationError):
            raise SharePackagingError() from None
        draft_id = str(prepared.get("draft_id") or prepared.get("local_draft_id") or "")
        if not draft_id:
            raise SharePackagingError()
        return draft_id

    def defer_candidate(
        self, event_id: str, *, may_apply: Callable[[], bool]
    ) -> dict[str, Any]:
        """Persist Not now for one exact candidate revision."""
        _require_current(may_apply)
        owner = self.native_owner()
        inspected = self.native_candidate_inspect(event_id)
        content_hash = inspected.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            raise WisdomValidationError("Collective Wisdom candidate is incomplete")
        if self.client is None:
            raise WisdomError("Collective Wisdom candidate deferral is unavailable")
        policy = AgentLedPolicyResponse.model_validate(self.client.agent_led_policy())
        if policy.org_id != owner["organization_id"] or self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom candidate owner changed")
        _require_current(may_apply)
        from .notices import suppression_key  # pylint: disable=import-outside-toplevel
        result = self.client.suppress_recommendation(suppression_key({
            "kind": "candidate", "content_hash": content_hash,
        }))
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom candidate owner changed")
        return {
            "deferred": True,
            "event_id": event_id,
            "not_now_suppression_days": policy.not_now_suppression_days,
            "preference_sync": "confirmed",
            "suppress_until": result.suppress_until,
        }

    def notification_settings(
        self, *, may_apply: Callable[[], bool], include_mute_control: bool = False
    ) -> dict[str, Any]:
        """Prepare one current mode/mute snapshot for an invocation-scoped review."""

        _require_current(may_apply)
        owner = self.native_owner()
        _require_current(may_apply)
        mode = self.notification_mode()
        try:
            mute = self.client.recommendation_mute()
            gateway_status = "available"
            gateway_mute = mute.model_dump(mode="json")
            mute_control = gateway_mute if include_mute_control else None
        except WisdomAuthError:
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            # Read-only/missing Gateway authority removes only those controls.
            mute_control = None
            gateway_status = "unavailable"
            gateway_mute = None
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        _require_current(may_apply)
        return {
            "owner": owner,
            "delivery_mode": mode,
            "mode_writable": self.notification_mode_writable(),
            "mode_meanings": {
                "agent": "Eligible recommendations may be assessed and delivered.",
                "fixed": "Assessment and proactive delivery are off; manual review remains.",
            },
            "gateway_status": gateway_status,
            "gateway_mute": gateway_mute,
            "mute_sync": None,
            "mute_control": mute_control,
        }

    def notification_mute_status(self) -> dict[str, Any]:
        """Inspect persisted mute state without preparing a choice or flushing work."""

        return self.mute("status")

    def choose_notification_mute(
        self,
        control_id: str,
        duration: str | None,
        *,
        may_apply: Callable[[], bool],
    ) -> dict[str, Any]:
        """Apply one exact revision-bound mute choice and attempt its idempotent sync."""

        owner = self.native_owner()
        client = self.client
        if client is None:
            raise WisdomValidationError("Collective Wisdom service is unavailable")
        current = client.recommendation_mute()
        if current.revision is None or control_id != str(current.revision):
            raise WisdomConflict("Collective Wisdom mute setting changed; review it again")
        _require_current(may_apply)
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        result = client.set_recommendation_mute(
            duration,
            expected_revision=current.revision,
            mutation_id=str(uuid4()),
            requested_at=datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
        )
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        confirmed = client.recommendation_mute()
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        if (confirmed.revision != result.revision or confirmed.duration != duration
                or confirmed.muted != (duration is not None)
                or confirmed.org_id != owner["organization_id"]):
            raise WisdomConflict(
                "Notification preference could not be confirmed; open settings again"
            )
        return confirmed.model_dump(mode="json")

    def mute(self, choice="status"):
        """Inspect or mutate the shared organization notification preference."""

        self._installation_owner()
        if choice == "status":
            result = self.client.recommendation_mute()
        else:
            duration = {"off": None, "1d": "1_day", "1w": "1_week",
                        "30d": "30_days"}.get(choice, choice)
            result = self.client.set_recommendation_mute(duration)
        self.check_owner()
        return result.model_dump(mode="json")

    def maintenance(self, *, refresh_policy=False, apply_automatic=False):
        """Explicit bounded producer; automatic mutation requires its own flag."""

        from .automatic_updates import (  # pylint: disable=import-outside-toplevel
            AutomaticUpdateMaintenance,
        )
        owner = self._installation_owner()
        self.local_state.bind_owner(owner)
        policy = self.client.agent_led_policy()
        updates = None
        if apply_automatic:
            reviewer, executor = self._update_services()
            updates = AutomaticUpdateMaintenance(
                self.installed,
                reviewer,
                executor,
                self._installation_owner,
                limit=8,
            ).run()
        self.check_owner()
        return {
            "policy": policy.model_dump(mode="json") if refresh_policy else None,
            "automatic_updates": updates,
        }

    def prepared_drafts(self):
        """List current owner's saved local drafts without exposing package bytes."""
        self._installation_owner()
        result = self._contribution_reviewer().prepared_drafts()
        self.check_owner()
        return result

    def prepared_inspect(self, draft_id):
        """Export one complete exact owner-private editable draft payload."""

        self._installation_owner()
        result = self._contribution_reviewer().inspect_prepared(draft_id)
        self.check_owner()
        return result

    def _named_local_state(self):
        """Read one owner-fenced snapshot for terminal name resolution only."""
        owner = self._installation_owner()
        if self._direct_state is None:
            raise WisdomError("Collective Wisdom local state is unavailable")
        snapshot = self._direct_state._load()  # pylint: disable=protected-access
        self.check_owner()
        return owner, snapshot

    def resolve_local_draft(self, reference):
        """Resolve one owner-local prepared draft, refusing same-name ambiguity."""
        owner, snapshot = self._named_local_state()
        drafts = snapshot.get("drafts", {})
        if not isinstance(drafts, dict):
            raise WisdomValidationError("Local draft state is invalid")
        if reference in drafts and isinstance(drafts[reference], dict):
            if drafts[reference].get("owner") == owner:
                return reference
            raise WisdomAuthError("Local draft belongs to another owner")
        matches = [key for key, row in drafts.items() if isinstance(row, dict)
                   and row.get("owner") == owner and row.get("slug") == reference]
        had_multiple = len(matches) > 1
        if had_multiple:
            # Prior published drafts remain in history. Select only the one
            # bound to the current, profile-local source; never guess by time.
            if any(not isinstance(drafts[key].get("source_path"), str)
                   or not isinstance(drafts[key].get("source_hash"), str)
                   for key in matches):
                raise WisdomConflict("Local draft name is ambiguous")
            from .package import source_fingerprint  # pylint: disable=import-outside-toplevel
            source = self._contribution_reviewer().source(reference)
            current_hash = source_fingerprint(source)
            self.check_owner()
            matches = [key for key in matches
                       if drafts[key]["source_path"] == str(source)
                       and drafts[key]["source_hash"] == current_hash]
        if not matches and not had_multiple:
            from .errors import WisdomNotFound  # pylint: disable=import-outside-toplevel
            raise WisdomNotFound("Local draft not found")
        if len(matches) != 1:
            raise WisdomConflict("Local draft name is ambiguous")
        return matches[0]

    def resolve_publication_operation(self, reference):
        """Bind a name to one retained operation; never select a newest operation."""
        owner, snapshot = self._named_local_state()
        operations = snapshot.get("publications", {})
        drafts = snapshot.get("drafts", {})
        if not isinstance(operations, dict) or not isinstance(drafts, dict):
            raise WisdomValidationError("Local publication state is invalid")
        if reference in operations and isinstance(operations[reference], dict):
            if operations[reference].get("owner") == owner:
                return reference
            raise WisdomAuthError("Publication belongs to another owner")
        matching_drafts = [key for key, row in drafts.items() if isinstance(row, dict)
                           and row.get("owner") == owner and row.get("slug") == reference]
        matches = [key for key, row in operations.items() if isinstance(row, dict)
                   and row.get("owner") == owner
                   and row.get("local_draft_id") in matching_drafts]
        if len(matches) > 1:
            raise WisdomConflict("Publication name is ambiguous")
        if not matches:
            from .errors import WisdomNotFound  # pylint: disable=import-outside-toplevel
            raise WisdomNotFound("Publication not found")
        return matches[0]

    def resolve_publication_receipt(self, reference):
        """Find one still-unconsumed, owner-bound review; submit rechecks expiry/bytes."""
        from .errors import WisdomNotFound  # pylint: disable=import-outside-toplevel

        owner, snapshot = self._named_local_state()
        reviews = snapshot.get("publication_reviews", {})
        drafts = snapshot.get("drafts", {})
        if not isinstance(reviews, dict) or not isinstance(drafts, dict):
            raise WisdomValidationError("Publication review state is invalid")
        if reference in reviews and isinstance(reviews[reference], dict):
            if reviews[reference].get("owner") != owner:
                raise WisdomAuthError("Publication review belongs to another owner")
            return reference
        matching_drafts = [key for key, row in drafts.items() if isinstance(row, dict)
                           and row.get("owner") == owner and row.get("slug") == reference]
        matches = [key for key, row in reviews.items() if isinstance(row, dict)
                   and row.get("owner") == owner and row.get("consumed_at") is None
                   and row.get("draft_id") in matching_drafts]
        if len(matches) > 1:
            raise WisdomConflict("Publication reviews are ambiguous; review the skill again")
        if not matches:
            raise WisdomNotFound("Publication review not found; review the skill again")
        return matches[0]

    def prepared_save(
        self, draft_id, *, author_description, files, expected_hashes,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Save one complete local edit without upload or execution."""

        _require_current(may_apply)
        self._installation_owner()
        result = self._contribution_reviewer().save_prepared(
            draft_id,
            author_description=author_description,
            files=files,
            expected_hashes=expected_hashes,
            may_apply=may_apply,
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def prepared_review(
        self, draft_id, *, allow_model_review=False,
        reviewed_hashes: dict[str, str] | None = None,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Re-run exact local checks and optionally the explicitly authorized model review."""

        _require_current(may_apply)
        self._installation_owner()
        result = self._contribution_reviewer().reopen(
            draft_id,
            allow_model_review=bool(allow_model_review) and release_scope().model_editorial,
            reviewed_hashes=reviewed_hashes, may_apply=may_apply,
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def share_status(self, skill_name):
        """Read retained incomplete sharing intent without replaying any effect."""
        return self._publication_service().share_status(skill_name)

    def publication_review(
        self, draft_id, *, accept_warnings=False,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Review exact contribution bytes and current server policy without upload."""

        _require_current(may_apply)
        result = self._publication_service().review(
            draft_id, accept_warnings=bool(accept_warnings), may_apply=may_apply
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def publication_submit(
        self, receipt, *, may_apply: Callable[[], bool] | None = None,
    ):
        """Consume one explicit final owner authorization and submit exact bytes."""

        _require_current(may_apply)
        result = self._publication_service().submit(receipt, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def publication_preflight(self, receipt, *, may_apply: Callable[[], bool] | None = None):
        """Read-only current-content check; never consumes a review receipt."""
        _require_current(may_apply)
        self._publication_service().preflight(receipt, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)

    def publication_for_candidate(self, assessment_id: str):
        """Read the direct publication identity for one private candidate action."""

        self._installation_owner()
        if self._direct_state is None:
            raise WisdomError("Collective Wisdom publication service is unavailable")
        return self._direct_state.publication_for_candidate(assessment_id)

    def local_publication_install_action(
        self, *, creator_user_id: str, content_hash: str,
    ) -> str:
        """Project exact profile-owned publication provenance from direct state."""

        owner = self._installation_owner()
        assert self._direct_state is not None
        return self._direct_state.local_publication_install_action(
            owner, creator_user_id=creator_user_id, content_hash=content_hash
        )

    def publication_submit_private(
        self, receipt, *, may_apply: Callable[[], bool] | None = None,
        candidate_assessment_id: str | None = None,
    ):
        """Submit exact bytes as an owner-private draft without approving publication."""

        from .portal_urls import portal_review_url  # pylint: disable=import-outside-toplevel

        _require_current(may_apply)
        result = self._publication_service().submit_private_draft(
            receipt, may_apply=may_apply,
            candidate_assessment_id=candidate_assessment_id,
        )
        self.check_owner()
        _require_current(may_apply)
        remote_id = str(result.get("remote_draft_id") or "")
        if not remote_id:
            return result
        owner = self._installation_owner()
        account = self.account
        if account is None:
            raise WisdomAuthError("Collective Wisdom account is unavailable")
        return {
            **result,
            "portal_url": portal_review_url(
                account.portal_base_url, owner["organization_id"], remote_id
            ),
        }

    def publication_reopen_private(
        self, operation_id, *, may_apply: Callable[[], bool] | None = None,
    ):
        """Reconcile one private draft and attach its owner Portal URL."""

        from .portal_urls import portal_review_url  # pylint: disable=import-outside-toplevel

        result = self.publication_reopen(operation_id, may_apply=may_apply)
        remote_id = str(result.get("remote_draft_id") or "")
        if not remote_id:
            return result
        owner = self._installation_owner()
        account = self.account
        if account is None:
            raise WisdomAuthError("Collective Wisdom account is unavailable")
        return {
            **result,
            "portal_url": portal_review_url(
                account.portal_base_url, owner["organization_id"], remote_id
            ),
        }

    def publication_status(self, operation_id):
        """Inspect durable publication state without a network retry."""

        return self._publication_service().status(operation_id)

    def publication_reopen(
        self, operation_id, *, may_apply: Callable[[], bool] | None = None,
    ):
        """Reconcile an uncertain publication through authoritative server state."""

        _require_current(may_apply)
        result = self._publication_service().reopen(operation_id, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def publication_enroll(
        self, skill_name, *, may_apply: Callable[[], bool] | None = None,
    ):
        """Explicitly link an exact already-published author source."""

        _require_current(may_apply)
        result = self._publication_service().enroll(skill_name, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def publication_decline(
        self, operation_id, *, may_apply: Callable[[], bool] | None = None,
    ):
        """Decline or withdraw a known exact remote draft when server state allows."""

        _require_current(may_apply)
        result = self._publication_service().decline(operation_id, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def installed_setup_inspect(self, skill_id, *, version=None):
        """Inspect installed setup guidance and durable progress."""

        result = self._setup_services().inspect(skill_id, version=version)
        self.check_owner()
        return result

    def installed_setup_plan(self, skill_id, *, version, phase, index, command):
        """Create a receipt for one exact setup action."""

        result = self._setup_services().plan(
            skill_id, version=version, phase=phase, index=index, command=command
        )
        self.check_owner()
        return result

    def installed_setup_apply(
        self, receipt, *, may_apply: Callable[[], bool] | None = None
    ):
        """Apply an explicit setup receipt."""

        result = self._setup_services().apply(receipt, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def installed_setup_status(self, skill_id, *, version=None):
        """Poll an existing setup handle without starting a command."""

        result = self._setup_services().status(skill_id, version=version)
        self.check_owner()
        return result

    def installed_setup_recover(
        self,
        skill_id,
        operation_id,
        *,
        acknowledged=False,
        expected_recovery: dict[str, Any] | None = None,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Review or explicitly abandon an uncertain setup operation."""

        result = self._setup_services().recover(
            skill_id,
            operation_id,
            acknowledged_effects_checked_and_stopped=acknowledged,
            expected_recovery=expected_recovery,
            may_apply=may_apply,
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def installed_setup_recovery_inspect(self, skill_id: str, operation_id: str):
        """Inspect one exact setup journal without polling or mutation."""

        result = self._setup_services().recovery_status(skill_id, operation_id)
        self.check_owner()
        return result

    def install_plan(self, reference, *, update_mode=None, accept_warnings=False):
        """Prepare an owner-bound terminal review without installing any files."""
        return self._installation_services()[0].plan(
            reference, update_mode=update_mode,
            accept_warnings=bool(accept_warnings),
        )

    def check_update(self, skill_id):
        """Read authoritative named-skill update state without starting work."""

        resolved_id, slug = self._resolve_update_reference(skill_id)
        result = self._update_services()[0].check(resolved_id, slug=slug)
        self.check_owner()
        return {**result, "slug": slug}

    def _resolve_update_reference(self, reference):
        detail = self.resolve_skill(reference)
        skill = detail.get("skill") if isinstance(detail, dict) else None
        skill_id = skill.get("id") if isinstance(skill, dict) else None
        slug = skill.get("slug") if isinstance(skill, dict) else None
        if not isinstance(skill_id, str) or not skill_id or not isinstance(slug, str) or not slug:
            raise WisdomValidationError("Wisdom update reference is invalid")
        return skill_id, slug

    def update_plan(
        self,
        skill_id,
        *,
        preserve_modified=False,
        accept_sensitive=False,
        accept_partial=False,
        accept_warnings=False,
    ):
        """Create an exact update receipt with all exceptional actions bound."""

        resolved_id, slug = self._resolve_update_reference(skill_id)
        result = self._update_services()[0].plan(
            resolved_id,
            slug=slug,
            preserve_modified=preserve_modified,
            accept_sensitive=accept_sensitive,
            accept_partial=accept_partial,
            accept_warnings=accept_warnings,
        )
        self.check_owner()
        return result

    def update_apply(
        self, receipt, *, may_apply: Callable[[], bool] | None = None
    ):
        """Apply or retry only the exact owner- and invocation-bound update receipt."""

        result = self._update_services()[1].apply(receipt, may_apply=may_apply)
        self.check_owner()
        _require_current(may_apply)
        return result

    def update_recovery_inspect(self, skill_id: str, operation_id: str):
        """Inspect exactly one update journal; never pick from broad pending state."""

        result = self._update_services()[1].recovery_status(skill_id, operation_id)
        self.check_owner()
        return result

    def update_recovery_resume(
        self,
        skill_id: str,
        operation_id: str,
        receipt: str,
        *,
        may_apply: Callable[[], bool],
    ):
        """Resume only the exact receipt named by the freshly inspected journal."""

        executor = self._update_services()[1]
        status = executor.recovery_status(skill_id, operation_id)
        if (
            status.get("operation_id") != operation_id
            or status.get("skill_id") != skill_id
            or status.get("receipt") != receipt
            or status.get("can_resume") is not True
        ):
            raise WisdomValidationError("Collective Wisdom update recovery identity is invalid")
        if may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        result = executor.apply(receipt, may_apply=may_apply, expected_recovery=status)
        self.check_owner()
        _require_current(may_apply)
        return result

    def prepare_update_deferral(
        self, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Preserve the existing update-only interface over shared review deferral."""

        if plan.get("operation") != "update":
            raise WisdomValidationError("Collective Wisdom update deferral is invalid")
        return self.prepare_review_deferral(plan)

    def prepare_review_deferral(  # pylint: disable=too-many-boolean-expressions
        self, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Bind an exact install/update reference to freshly verified cooldown policy."""

        owner = self.native_owner()
        skill_id = plan.get("skill_id")
        version = plan.get("version")
        if (
            plan.get("operation") not in {"install", "update"}
            or plan.get("owner") != owner
            or not isinstance(skill_id, str)
            or not skill_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise WisdomValidationError("Collective Wisdom review deferral is invalid")
        policy = AgentLedPolicyResponse.model_validate(self.client.agent_led_policy())
        if (
            policy.org_id != owner["organization_id"]
            or self.native_owner() != owner
        ):
            raise WisdomAuthError("Collective Wisdom review deferral owner changed")
        return {
            "owner": owner,
            "reference": {
                "kind": "skill", "skill_id": skill_id, "version": version,
            },
            "not_now_suppression_days": policy.not_now_suppression_days,
        }

    def defer_update(
        self, deferral: dict[str, Any], *, may_apply: Callable[[], bool]
    ) -> dict[str, Any]:
        """Preserve the existing update interface over shared exact deferral."""

        return self.defer_review(deferral, may_apply=may_apply)

    def defer_review(  # pylint: disable=too-many-boolean-expressions
        self, deferral: dict[str, Any], *, may_apply: Callable[[], bool]
    ) -> dict[str, Any]:
        """Persist one exact-version deferral without granting operation consent."""


        if may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        owner = self.native_owner()
        reference = deferral.get("reference")
        days = deferral.get("not_now_suppression_days")
        if (
            deferral.get("owner") != owner
            or not isinstance(reference, dict)
            or reference.get("kind") != "skill"
            or not isinstance(reference.get("skill_id"), str)
            or not reference["skill_id"]
            or not isinstance(reference.get("version"), int)
            or isinstance(reference.get("version"), bool)
            or reference["version"] < 1
            or not isinstance(days, int)
            or isinstance(days, bool)
            or not 1 <= days <= 365
        ):
            raise WisdomValidationError("Collective Wisdom review deferral is invalid")
        if self.client is None or may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        policy = AgentLedPolicyResponse.model_validate(self.client.agent_led_policy())
        if policy.org_id != owner["organization_id"]:
            raise WisdomAuthError("Collective Wisdom review deferral owner changed")
        if policy.not_now_suppression_days != days:
            raise WisdomConflict("Suppression policy changed; open the review again")
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom review deferral owner changed")
        from .notices import suppression_key  # pylint: disable=import-outside-toplevel
        if may_apply() is not True:
            raise WisdomAuthError("Collective Wisdom review is no longer current")
        key = suppression_key(reference)
        result = self.client.suppress_recommendation(key)
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom review deferral owner changed")
        return {
            "deferred": True,
            "skill_id": reference["skill_id"],
            "version": reference["version"],
            "not_now_suppression_days": days,
            "preference_sync": "confirmed",
            "suppress_until": result.suppress_until,
        }

    def native_owner(self):
        """Return the exact non-secret enrolment binding for native controls."""

        return self._installation_owner()

    def native_team_name(self):
        """Display-only Portal name; owner IDs continue to authorize effects."""
        self.check_owner()
        assert self.account is not None
        return self.account.team_name()

    def native_update_inspect(self, receipt):
        """Return complete exact reviewed update text files without granting consent."""
        reviewer, _executor = self._update_services()
        return self._native_package_inspect(reviewer, receipt, "Update")

    def native_install_inspect(self, receipt):
        """Return complete exact reviewed install text files without granting consent."""
        reviewer, _executor = self._installation_services()
        return self._native_package_inspect(reviewer, receipt, "Install")

    def _native_package_inspect(self, reviewer: Any, receipt: str, label: str):
        plan = reviewer.receipts.load(receipt)
        verified = reviewer.verify(plan)
        pages = []
        for path, mode, body in verified.files:
            if mode not in {"file", "exec"}:
                raise WisdomValidationError(f"{label} review contains an unsupported entry")
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                content = "[Base64-encoded binary]\n" + base64.b64encode(body).decode("ascii")
            digest = next(
                (
                    item.get("hash")
                    for item in plan.get("files", [])
                    if isinstance(item, dict) and item.get("path") == path
                ),
                None,
            )
            ranges = range(0, max(1, len(content)), NATIVE_INSPECTION_CHARS)
            pages.extend({
                "path": path,
                "content": content[start:start + NATIVE_INSPECTION_CHARS],
                "content_start": start,
                "content_end": min(len(content), start + NATIVE_INSPECTION_CHARS),
                "hash": digest,
            } for start in ranges)
        if not pages:
            raise WisdomValidationError(f"{label} review has no inspectable files")
        self.check_owner()
        return pages

    def install_apply(
        self, receipt, *, accept_partial=False,
        may_apply: Callable[[], bool] | None = None,
    ):
        """Install reviewed files without treating file consent as command consent."""

        _reviewer, executor = self._installation_services()
        result = executor.apply(
            receipt, accept_partial=accept_partial, may_apply=may_apply,
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def install_setup_apply(self, receipt, *, may_apply: Callable[[], bool] | None = None):
        """Execute declared setup only after distinct permission for installed files."""
        return self._file_setup_apply(receipt, "installed", may_apply=may_apply)

    def update_setup_apply(self, receipt, *, may_apply: Callable[[], bool] | None = None):
        """Execute new-version setup only after distinct permission for updated files."""
        return self._file_setup_apply(receipt, "updated", may_apply=may_apply)

    def _file_setup_apply(self, receipt, action, *, may_apply=None):
        reviewer = (self._installation_services()[0] if action == "installed"
                    else self._update_services()[0])
        plan = reviewer.receipts.load(receipt, allow_started=True)
        changed = reviewer.receipts.outcome(receipt)
        self.check_owner()
        _require_current(may_apply)
        commands = plan.get("setup_commands")
        if not confirmed_file_change(changed, action=action):
            raise PackagePolicyError(f"files are not confirmed {action} for this review")
        if not isinstance(commands, list) or not commands:
            raise PackagePolicyError("this version has no declared setup commands")
        try:
            return self._apply_installed_setup(receipt, plan, changed, may_apply=may_apply)
        except (PackagePolicyError, WisdomError) as exc:
            return {**changed, "setup": {"state": "blocked", "detail": str(exc)}}

    def _apply_installed_setup(self, receipt, plan, result, *, may_apply=None):
        """Run only the exact commands declared by the installed, reviewed bytes."""
        commands = plan["setup_commands"]
        setup = self._setup_services()
        info = setup.inspect(plan["skill_id"], version=plan["version"])
        guidance = info.get("guidance")
        if (
            info.get("content_hash") != plan.get("content_hash")
            or not isinstance(guidance, dict)
            or commands != guidance.get("setup_commands")
        ):
            raise PackagePolicyError("installed setup commands changed since approval")
        for index, command in enumerate(commands):
            _require_current(may_apply)
            step = setup.plan_for_install(
                plan["skill_id"], version=plan["version"], index=index,
                command=command, install_receipt=receipt,
            )
            outcome = setup.apply(step["receipt"], may_apply=may_apply)
            if outcome.get("state") != "passed":
                return {**result, "setup_execution_authorized": True,
                        "setup": {"state": outcome.get("state"), "step": index + 1}}
        return {**result, "setup_execution_authorized": True,
                "setup": {"state": "passed", "steps": len(commands)}}

    def list_installations(self):
        """Inspect private installation state only through the terminal entry."""
        return self._installation_services()[1].installed()

    def top_skills(self):
        """Rank this profile's own local skills from Hermes usage; no account needed."""
        from .top_skills import (  # pylint: disable=import-outside-toplevel
            collect_top_skills, published_source_paths,
        )

        try:
            installations = self.installed.installations()
        except (OSError, RuntimeError, TypeError, ValueError):
            installations = []
        try:
            published = published_source_paths(self._state.get("wisdom-core"))
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
            published = []
        return collect_top_skills(
            self.profile[1], installations=installations, published_paths=published,
        )

    def resolve_managed_skill(self, reference):
        """Resolve an exact active managed name or ID for terminal setup/removal."""
        from .errors import WisdomNotFound  # pylint: disable=import-outside-toplevel

        rows = self.list_installations()
        self.check_owner()
        matches = [row for row in rows if isinstance(row, dict)
                   and row.get("state") == "active"
                   and row.get("slug") == reference]
        if len(matches) > 1:
            raise WisdomConflict("Managed skill name is ambiguous; inspect installed --json")
        if matches:
            skill_id = matches[0].get("skill_id")
            if isinstance(skill_id, str) and skill_id:
                return skill_id
            raise WisdomValidationError("Managed skill identity is unavailable")
        if any(isinstance(row, dict) and row.get("state") == "active"
               and row.get("skill_id") == reference for row in rows):
            return reference
        raise WisdomNotFound("Managed skill not found")

    def uninstall(
        self, skill_id, *, may_apply: Callable[[], bool] | None = None
    ):
        """Perform one removal while its calling review remains current."""
        result = self._installation_services()[1].uninstall(
            skill_id, may_apply=may_apply
        )
        self.check_owner()
        _require_current(may_apply)
        return result

    def require_setup(self):
        """Deny discovery before creating state or issuing a request."""
        self.check_owner()
        if self._local_identity() is None:
            raise WisdomValidationError(
                "Run hermes wisdom setup in your terminal first",
                code="setup_required",
            )
        self._open().require_setup()

    def _discovery(self):
        from .catalog_status import (  # pylint: disable=import-outside-toplevel
            CatalogInstallationStatus,
        )
        from .discovery import Discovery  # pylint: disable=import-outside-toplevel

        self.require_setup()
        owner = self._installation_owner()
        local_status = CatalogInstallationStatus(self.installed, owner, self.profile[1])
        return Discovery(
            self.client, self.check_owner,
            portal_base_url=self.account.portal_base_url,
            organization_id=self.account.organization_id,
            installation_status=local_status.for_skill,
            org_slug=getattr(self.account, "organization_slug", None),
        )

    def list_skills(self):
        """Read one raw discovery page."""
        return self._discovery().list_skills()

    def search_skills(self, query=None):
        """Read published skills matching the supplied query."""
        return self._discovery().search_skills(query)

    def show(self, skill_id):
        """Inspect published metadata only."""
        return self.resolve_skill(skill_id)

    def resolve_skill(self, reference):
        """Resolve an ID or unique slug for readable slash navigation."""
        return self._discovery().resolve_skill(reference)

    def versions(self, skill_id):
        """Read published version history."""
        detail = self.resolve_skill(skill_id)
        skill = detail.get("skill") if isinstance(detail, dict) else None
        resolved = skill.get("id") if isinstance(skill, dict) else None
        if not isinstance(resolved, str) or not resolved:
            raise WisdomValidationError("Wisdom skill identity is unavailable")
        return self._discovery().versions(resolved)

    def close(self):
        """Close this invocation's client."""
        self._closed = True
        if self.client is not None:
            self.client.close()


class CommandRuntime:  # pylint: disable=too-many-instance-attributes
    """Own registrations and cancellable, read-only slash requests."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        root: Path,
        profile: tuple[str, Path],
        *,
        state: Any,
        llm: Any,
        spawn_task: Callable[..., Any],
        config: dict[str, Any] | None = None,
        get_config: Callable[[str, Any], Any] | None = None,
        set_config: Callable[[str, Any], Any] | None = None,
        inject_message: Callable[..., Any] | None = None,
        plugin_id: str = "collective-wisdom",
    ):
        from .native_runtime import NativeSurfaceRuntime  # pylint: disable=import-outside-toplevel

        if state is None:
            raise TypeError("host PluginState is required")
        self.root = Path(root).resolve()
        self.profile = profile
        self.llm = llm
        self.config = config or {}
        self.get_config = get_config
        self.set_config = set_config
        self.state = state
        self.inject_message = inject_message
        self.plugin_id = plugin_id

        self._lock = threading.RLock()
        self._closed = False
        self._children = set()
        self._services = set()

        self.registrations = []
        self.native = NativeSurfaceRuntime(
            self.root, profile[0], self._service, llm, spawn_task,
            installed_state=state,
            session_context=current_session_context,
        )

    def _service(self):
        with self._lock:
            if self._closed:
                raise WisdomError("Collective Wisdom plugin is unloaded")
            service = InvocationService(
                self.root, self.profile, llm=self.llm, config=self.config,
                get_config=self.get_config, set_config=self.set_config,
                state=self.state,
            )
            self._services.add(service)
            return service

    def _close_service(self, service):
        service.close()
        with self._lock:
            self._services.discard(service)

    def cli(self, args) -> int:
        """Synchronous native CLI entry, including interactive terminal setup."""
        from .commands import run_cli  # pylint: disable=import-outside-toplevel

        service = self._service()
        try:
            return run_cli(args, service)
        finally:
            self._close_service(service)

    def _slash_native_install(self, invocation, args):
        """Apply an admitted install without turning the command into another review."""
        if len(args) != 1:
            return "Usage: /wisdom install <skill-name>"
        context = contextvars.copy_context()

        async def apply_install():
            try:
                task = context.run(
                    asyncio.create_task,
                    asyncio.to_thread(self.native.install_direct, invocation, args[0]),
                )
                view = await task
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Collective Wisdom install failed (%s)", describe_exception(exc))
                return _INSTALL_UNCERTAIN
            if isinstance(view, dict):
                return _native_review_reply(view)
            try:
                await self.native.send_direct_result(invocation, view)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Slack may have accepted the card; never send a second receipt.
                logger.warning(
                    "Collective Wisdom install card uncertain (%s)", type(exc).__name__
                )
                return _INSTALL_UNCERTAIN
            return ""

        return apply_install()

    def slash(self, raw_args: str):  # pylint: disable=too-many-return-statements
        """Route admitted native commands and read-only requests."""
        gateway_command = self.native.command_pending()
        invocation = self.native.consume_request(raw_args)
        from .commands import parse_slash  # pylint: disable=import-outside-toplevel
        parsed = parse_slash(raw_args)
        if parsed is None:
            return "Invalid Wisdom command. Use /wisdom help."
        command, args = parsed
        if not release_scope().allows_slash(command):
            from .commands import run_slash  # pylint: disable=import-outside-toplevel
            # Dormant reviews (settings, suggestions, recommendations) never open.
            return run_slash(parsed, object())
        if command in PRIVATE_RANKING_COMMANDS:
            return self._slash_private_ranking(command, args, invocation, gateway_command)
        if invocation is not None:
            if command == "install":
                return self._slash_native_install(invocation, args)
            try:
                reply = self._slash_native_review(invocation, command, args)
                if reply is not None:
                    return reply
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Collective Wisdom review failed (%s)", describe_exception(exc))
                return _REVIEW_UNAVAILABLE
        # Capture the calling profile before the host moves an awaitable to a thread.
        context = contextvars.copy_context()

        async def resolve():
            task = context.run(asyncio.create_task, self._slash(parsed))
            return await task

        return resolve()

    def _slash_private_ranking(self, command, args, invocation, gateway_command):
        """Answer owner-private usage rankings only in a private or local session."""
        from .share_suggest import slash_share_suggest  # pylint: disable=import-outside-toplevel
        from .top_command import private_route, refusal, slash_top  # pylint: disable=import-outside-toplevel
        from .top_skills import session_surface  # pylint: disable=import-outside-toplevel

        route = private_route(invocation, gateway_command, session_surface())
        if route is None:
            return refusal(command)
        service = self._service()
        try:
            if command == "top":
                return slash_top(args, service)
            return slash_share_suggest(
                args, service, route, inject=self.inject_message, plugin_id=self.plugin_id,
                profile=self.profile[0],
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom top skills failed (%s)", describe_exception(exc))
            return _REVIEW_UNAVAILABLE
        finally:
            self._close_service(service)

    def _slash_native_review(self, invocation, command: str, args: list[str]):
        target = " ".join(args)
        if command in {"suggest", "share"} and len(args) != 1:
            return f"Usage: /wisdom {command} <local-skill>"
        effect_review = {
            "update": self.native.begin_update,
            "publish": self.native.begin_publication,
            "suggest": self.native.begin_suggest,
            # Share is the operator-facing name for the same owner-private
            # preparation/review flow. The final Share My Skill button remains
            # the only publication authority.
            "share": self.native.begin_suggest,
        }.get(command)
        if effect_review is not None:
            return _native_review_reply(effect_review(invocation, target))
        if command in {"browse", "show", "versions"}:
            return _native_review_reply(self.native.begin_catalog(invocation, command, target))
        if command == "settings":
            return _native_review_reply(self.native.begin_settings(invocation))
        return None

    async def _slash(self, parsed: tuple[str, list[str]]) -> str:
        """Run network work off-loop; cancelled TUI workers retain their own cleanup."""
        from .commands import CONTRIBUTION_COMMANDS, run_slash  # pylint: disable=import-outside-toplevel
        service = task = None
        cancel = threading.Event()
        try:
            command, _args = parsed
            if command in CONTRIBUTION_COMMANDS:
                return run_slash(parsed, object())
            service = self._service()
            if command in {"help", "setup", "install", "installed", "uninstall"}:
                return run_slash(parsed, service)
            binding = service.binding()
            task = asyncio.create_task(
                asyncio.to_thread(self._run_worker, parsed, binding, cancel)
            )
            result = await asyncio.shield(task)
            service.check_owner()
            return result
        except WisdomUnavailable:
            return read_unavailable(parsed) or str(WisdomUnavailable())
        except Exception:  # pylint: disable=broad-exception-caught
            return _UNAVAILABLE
        finally:
            cancel.set()
            if task is not None and not task.done():
                await asyncio.shield(task)
            if service is not None:
                self._close_service(service)


    def _run_worker(self, parsed: tuple[str, list[str]], binding, cancel):
        # A process boundary provides a real deadline even for stalled DNS or a
        # slow-drip HTTP response. No bearer is serialized or passed on argv.
        deadline = time.monotonic() + SLASH_BUDGET_SECONDS
        defaults = {
            "delivery_mode": "agent", "new_skills": "immediate",
            "updates": "immediate", "team_published": "immediate",
        }
        snapshot = self.config.get("notifications") or {}
        live_config = {"notifications": {
            key: (
                self.get_config(f"notifications.{key}", snapshot.get(key, default))
                if self.get_config is not None else snapshot.get(key, default)
            )
            for key, default in defaults.items()
        }}
        request = {"package": str(Path(__file__).resolve().parent), "sys_path": sys.path,
                   "root": str(self.root), "profile": [self.profile[0], str(self.profile[1])],
                   "binding": binding, "command": parsed, "config": live_config}
        payload = json.dumps(request)
        environment = os.environ.copy()
        # Host multi-profile callbacks may bind a ContextVar rather than an env
        # var. Project that captured scope into this child only, never the host.
        environment["HERMES_HOME"] = str(self.profile[1])
        with self._lock:
            if self._closed or cancel.is_set():
                return _UNAVAILABLE
            child = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-c", _BOOTSTRAP],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env=environment,
            )
            self._children.add(child)
        try:
            while not cancel.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return read_unavailable(parsed) or str(WisdomUnavailable())
                try:
                    output, _ = child.communicate(payload, timeout=min(0.1, remaining))
                    result = json.loads(output) if child.returncode == 0 else None
                    return result if isinstance(result, str) else _UNAVAILABLE
                except subprocess.TimeoutExpired:
                    payload = None
            return _UNAVAILABLE
        except (OSError, ValueError):
            return _UNAVAILABLE
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()
            with self._lock:
                self._children.discard(child)

    def close(self):
        """Unload only owned registrations, clients and command workers."""
        with self._lock:
            self._closed = True
            self.native.close()
            for child in self._children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=1)
            for service in self._services:
                service.close()
            for registration in reversed(self.registrations):
                registration.dispose()
            self.registrations.clear()


def worker_main(request) -> str:
    """Evaluate one read-only slash command against the captured owner."""
    from .commands import run_slash  # pylint: disable=import-outside-toplevel

    plugin_state = import_module("hermes_cli.plugins_state").PluginState

    service = InvocationService(
        Path(request["root"]), (request["profile"][0], Path(request["profile"][1])),
        state=plugin_state("collective-wisdom"),
        config=request.get("config"),
    )
    try:
        if service.binding() != request["binding"]:
            return _UNAVAILABLE
        result = run_slash((request["command"][0], request["command"][1]), service)
        service.check_owner()
        return result
    except WisdomUnavailable:
        return read_unavailable((request["command"][0], request["command"][1])) or str(
            WisdomUnavailable()
        )
    except WisdomError:
        return _UNAVAILABLE
    finally:
        service.close()
