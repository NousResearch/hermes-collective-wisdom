"""Owned terminal command lifetime on the existing Hermes plugin APIs."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from .account import HermesAccountAdapter
from .client import WisdomClient
from .errors import WisdomAuthError, WisdomError, WisdomValidationError

SLASH_BUDGET_SECONDS = 25.0
NATIVE_INSPECTION_CHARS = 500
_UNAVAILABLE = "Collective Wisdom is unavailable. Complete setup in your terminal."
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
        llm: Any | None = None,
        config: dict[str, Any] | None = None,
        get_config: Callable[[str, Any], Any] | None = None,
        set_config: Callable[[str, Any], Any] | None = None,
    ):
        self.root = root
        self.profile = profile
        self.llm = llm
        self.config = config or {}
        self._get_config = get_config
        self._set_config = set_config
        self.account = None
        self.client = None
        self._onboarding = None
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
        """Report whether the public registration supplied config mutation authority."""

        return self._set_config is not None

    def set_notification_mode(self, mode: str) -> None:
        """Persist one owner-fenced mode through PluginContext.set_config."""

        if mode not in {"agent", "fixed"}:
            raise ValueError("Unsupported notification mode")
        owner = self.native_owner()
        if self._set_config is None:
            raise PermissionError("Collective Wisdom notification settings are read-only.")
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        self._set_config("notifications.delivery_mode", mode)
        if self.native_owner() != owner:
            raise WisdomAuthError("Collective Wisdom notification owner changed")
        if self.notification_mode() != mode:
            raise PermissionError("Notification mode change was not confirmed.")

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
        if self._onboarding is None:
            from .onboarding import Onboarding  # pylint: disable=import-outside-toplevel
            from .store import WisdomStore  # pylint: disable=import-outside-toplevel

            self._onboarding = Onboarding(self.account, WisdomStore(self.root), self.client)
        return self._onboarding

    def _install_credentials(self):
        self.check_owner()
        return self.account.credentials(scope="wisdom:install")

    def _publish_credentials(self):
        self.check_owner()
        return self.account.credentials(scope="wisdom:publish")

    def setup(self, *, accept_disclosure: bool):
        """Create state only after explicit terminal acceptance and scope checks."""
        if not accept_disclosure:
            raise WisdomValidationError("Collective Wisdom requires disclosure acceptance")
        self._install_credentials()
        result = self._open().setup(accept_disclosure=True)
        self.check_owner()
        return result

    def status(self, *, include_operations=False):
        """Inspect local setup without enrolling or contacting the Gateway."""
        self.check_owner()
        if not (self.root / "wisdom.db").is_file():
            return {"configured": False, "state": "not_configured", "reason": "not_configured"}
        result = self._open().status()
        if include_operations and result.get("configured"):
            owner = {**self.binding(), "installation_id": result.get("installation_id")}
            result["pending_operations"] = [
                {"operation_id": operation["id"], "kind": operation["kind"],
                 "phase": operation["phase"], "skill_id": operation["entity_id"]}
                for operation in self._onboarding.store.pending_operations()
                if json.loads(operation["payload_json"]).get("owner") == owner
            ]
        self.check_owner()
        return result

    def _installation_owner(self):
        self.require_setup()
        self._install_credentials()
        return {**self.binding(), "installation_id":
                self._onboarding.store.existing_installation_identity()}

    def _installation_services(self):
        from .install_execution import InstallExecutor  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .install_receipts import InstallReceipts  # pylint: disable=import-outside-toplevel
        from .install_review import InstallReviewer  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        store = self._onboarding.store
        host = InstallationHost(self.profile[1])
        receipts = InstallReceipts(store, self._installation_owner)
        reviewer = InstallReviewer(self.client, store, receipts, host, self._installation_owner)
        executor = InstallExecutor(self.client, store, receipts, reviewer,
                                   host, self._installation_owner)
        return reviewer, executor

    def _setup_services(self):
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .setup_execution import SetupController  # pylint: disable=import-outside-toplevel
        from .setup_host import SetupHost  # pylint: disable=import-outside-toplevel
        from .setup_receipts import SetupReceipts  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        if self._onboarding is None:
            raise WisdomError("Collective Wisdom setup service is unavailable")
        store = self._onboarding.store
        return SetupController(
            store,
            SetupReceipts(store, self._installation_owner),
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
        if self._onboarding is None:
            raise WisdomError("Collective Wisdom update service is unavailable")
        store = self._onboarding.store
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

        if self._onboarding is None:
            raise WisdomError("Collective Wisdom contribution service is unavailable")
        return ContributionReviewer(
            self._onboarding.store,
            InstallationHost(self.profile[1]),
            self._installation_owner,
            llm=self.llm,
        )

    def _publication_service(self):
        """Build the one shared publication state machine over existing services."""

        from .publication import PublicationController  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        if self._onboarding is None or self.client is None:
            raise WisdomError("Collective Wisdom publication service is unavailable")
        return PublicationController(
            self._onboarding.store,
            self._contribution_reviewer(),
            self.client,
            self._installation_owner,
        )

    def suggest(self, skill_name, *, allow_model_review=False):
        """Prepare one exact owner-private local draft without submission authority."""

        owner = self._installation_owner()
        self._onboarding.store.bind_qualification_owner(owner)
        result = self._contribution_reviewer().prepare(
            skill_name, allow_model_review=bool(allow_model_review)
        )
        self.check_owner()
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom contribution owner changed")
        return result

    def review_week(self, *, dry_run=False):
        """Use current owner and Gateway policy to queue local evidence; never call a model."""
        from .weekly_queue import (  # pylint: disable=import-outside-toplevel
            enqueue_weekly_review,
            weekly_policy_from_agent,
        )

        owner = self._installation_owner()
        policy = self.client.agent_led_policy()
        if policy.org_id != owner["organization_id"]:
            raise WisdomAuthError("Collective Wisdom weekly policy owner changed")
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom weekly review owner changed")
        store = self._onboarding.store
        store.bind_qualification_owner(owner)

        def authorized():
            try:
                return owner == self._installation_owner()
            except WisdomError:
                return False

        return enqueue_weekly_review(
            store=store,
            skills_root=self.profile[1] / "skills",
            policy=weekly_policy_from_agent(policy),
            authorized=authorized,
            dry_run=bool(dry_run),
        )

    @property
    def store(self):
        """Expose the active profile store to shared state kernels."""

        self.require_setup()
        return self._onboarding.store

    def _consumption(self):
        """Build the shared feed projection without starting background work."""

        from .consumption import WisdomConsumption  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        return WisdomConsumption(
            self._onboarding.store, self.client, config=self.notification_config()
        )

    def notifications(self, *, mark_seen=False):
        """Read durable local notices, optionally acknowledging selected rows."""

        result = self._consumption().notifications(mark_seen=bool(mark_seen))
        self.check_owner()
        return result

    def inbox(self, *, invocation=None):
        """Read owner-bound assessments and reviews without polling or mutation."""
        from .mediation_store import MediationStore  # pylint: disable=import-outside-toplevel
        from .native_review import NativeReviewController  # pylint: disable=import-outside-toplevel

        owner = self._installation_owner()
        store = self._onboarding.store
        mode = self.notification_mode()
        rows = (
            MediationStore(store, owner=owner, initialize_schema=False).assessments(
                owner["organization_id"]
            )
            if store.get_metadata("qualification_owner") == owner else []
        )
        if mode == "fixed":
            rows = [row for row in rows if row["reference"].get("user_requested") is True]
        assessments = [{
            **{key: row.get(key) for key in (
                "id", "event_key", "state", "advice", "delivered_at", "owner_session",
            )},
            "reference": {
                key: row["reference"].get(key)
                for key in (
                    "kind", "skill_id", "version", "event_id", "local_skill_id",
                    "content_hash",
                )
                if row["reference"].get(key) is not None
            },
        } for row in rows if row["reference"].get("kind") != "weekly_review"][-100:]
        interactions = NativeReviewController(store).pending(
            owner, invocation=invocation, limit=100
        )
        installed_setup = [{
            "kind": "installed", "identity": row["skill_id"],
            "version": row["version"], "content_hash": row["content_hash"],
            "instruction": (
                "Inspect installed setup; file installation does not establish readiness."
            ),
        } for row in store.installations(owner) if row["state"] == "active"][:100]
        self.check_owner()
        if owner != self._installation_owner():
            raise WisdomAuthError("Collective Wisdom inbox owner changed")
        return {
            "mode": mode, "assessments": assessments, "interactions": interactions,
            "installed_setup": installed_setup,
        }

    def native_candidate_inspect(self, event_id):
        """Inspect one exact owner-private candidate without catalog disclosure."""
        owner = self._installation_owner()
        store = self.store
        if store.get_metadata("qualification_owner") != owner:
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
        result = {
            "kind": "candidate", "event_id": str(event["id"]),
            "content_hash": str(event["content_hash"]),
            "facts": {
                "name": str(payload.get("skill_name") or event.get("skill_id") or "Local skill"),
                "qualification": event.get("qualification"),
                "editorial": {
                    key: payload.get(key) for key in (
                        "editorial_name", "editorial_description"
                    ) if payload.get(key)
                },
            },
            "networked": False,
        }
        self.check_owner()
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
        prepared = self.suggest(skill_name, allow_model_review=False)
        draft_id = str(prepared.get("draft_id") or prepared.get("local_draft_id") or "")
        if not draft_id:
            raise WisdomError("Collective Wisdom candidate could not be prepared")
        return draft_id

    def mute(self, choice="status"):
        """Inspect or mutate the shared organization notification preference."""

        from .preferences import WisdomPreferences  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        result = WisdomPreferences(self).native_mute_command(choice)
        self.check_owner()
        return result

    def sync_status(self):
        """Inspect durable remote delivery and operation-report recovery state."""

        from .sync_recovery import WisdomSyncRecovery  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        result = WisdomSyncRecovery(self).status()
        self.check_owner()
        return result

    def sync_retry(self):
        """Explicitly retry pending remote delivery and operation reports."""

        from .sync_recovery import WisdomSyncRecovery  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        result = WisdomSyncRecovery(self).retry()
        self.check_owner()
        return result

    def maintenance(self, *, refresh_policy=False, apply_automatic=False):
        """Explicit bounded producer; automatic mutation requires its own flag."""

        from .automatic_updates import (  # pylint: disable=import-outside-toplevel
            AutomaticUpdateMaintenance,
        )
        from .mediation_producer import (  # pylint: disable=import-outside-toplevel
            MaintenanceMediationProducer,
        )

        owner = self._installation_owner()
        self._onboarding.store.bind_qualification_owner(owner)
        policy = self.client.agent_led_policy()
        consumption = self._consumption()
        feed = consumption.poll_feed()
        metadata = consumption.refresh_metadata()
        mediation = MaintenanceMediationProducer(
            self,
            consumption,
            owner,
            skills_root=self.profile[1] / "skills",
            config=self.notification_config(),
        ).produce(policy)
        updates = None
        if apply_automatic:
            reviewer, executor = self._update_services()
            updates = AutomaticUpdateMaintenance(
                self._onboarding.store,
                reviewer,
                executor,
                self._installation_owner,
                limit=8,
            ).run()
        self.check_owner()
        return {
            "feed": feed,
            "metadata": metadata,
            "policy": policy.model_dump(mode="json") if refresh_policy else None,
            "mediation": mediation,
            "automatic_updates": updates,
        }

    def prepared_inspect(self, draft_id):
        """Export one complete exact owner-private editable draft payload."""

        self._installation_owner()
        result = self._contribution_reviewer().inspect_prepared(draft_id)
        self.check_owner()
        return result

    def prepared_save(self, draft_id, *, author_description, files, expected_hashes):
        """Save one complete local edit without upload or execution."""

        self._installation_owner()
        result = self._contribution_reviewer().save_prepared(
            draft_id,
            author_description=author_description,
            files=files,
            expected_hashes=expected_hashes,
        )
        self.check_owner()
        return result

    def prepared_review(self, draft_id, *, allow_model_review=False):
        """Re-run exact local checks and optionally the explicitly authorized model review."""

        self._installation_owner()
        result = self._contribution_reviewer().reopen(
            draft_id, allow_model_review=bool(allow_model_review)
        )
        self.check_owner()
        return result

    def publication_review(self, draft_id):
        """Review exact contribution bytes and current server policy without upload."""

        result = self._publication_service().review(draft_id)
        self.check_owner()
        return result

    def publication_submit(self, receipt):
        """Consume one explicit final owner authorization and submit exact bytes."""

        result = self._publication_service().submit(receipt)
        self.check_owner()
        return result

    def publication_status(self, operation_id):
        """Inspect durable publication state without a network retry."""

        return self._publication_service().status(operation_id)

    def publication_reopen(self, operation_id):
        """Reconcile an uncertain publication through authoritative server state."""

        result = self._publication_service().reopen(operation_id)
        self.check_owner()
        return result

    def publication_decline(self, operation_id):
        """Decline or withdraw a known exact remote draft when server state allows."""

        result = self._publication_service().decline(operation_id)
        self.check_owner()
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

    def installed_setup_apply(self, receipt):
        """Apply an explicit setup receipt."""

        result = self._setup_services().apply(receipt)
        self.check_owner()
        return result

    def installed_setup_status(self, skill_id, *, version=None):
        """Poll an existing setup handle without starting a command."""

        result = self._setup_services().status(skill_id, version=version)
        self.check_owner()
        return result

    def installed_setup_recover(self, skill_id, operation_id, *, acknowledged=False):
        """Review or explicitly abandon an uncertain setup operation."""

        result = self._setup_services().recover(
            skill_id,
            operation_id,
            acknowledged_effects_checked_and_stopped=acknowledged,
        )
        self.check_owner()
        return result

    def install_plan(self, reference, *, update_mode=None):
        """Prepare an owner-bound terminal review without installing any files."""
        return self._installation_services()[0].plan(reference, update_mode=update_mode)

    def check_update(self, skill_id):
        """Read authoritative named-skill update state without starting work."""

        result = self._update_services()[0].check(skill_id)
        self.check_owner()
        return result

    def update_plan(
        self,
        skill_id,
        *,
        preserve_modified=False,
        accept_sensitive=False,
        accept_partial=False,
    ):
        """Create an exact update receipt with all exceptional actions bound."""

        result = self._update_services()[0].plan(
            skill_id,
            preserve_modified=preserve_modified,
            accept_sensitive=accept_sensitive,
            accept_partial=accept_partial,
        )
        self.check_owner()
        return result

    def update_apply(self, receipt):
        """Apply or retry only the exact owner-bound update receipt."""

        result = self._update_services()[1].apply(receipt)
        self.check_owner()
        return result

    def native_owner(self):
        """Return the exact non-secret enrolment binding for native controls."""

        return self._installation_owner()

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
            if mode != "file":
                raise WisdomValidationError(f"{label} review contains an unsupported entry")
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                content = "[Binary file; exact SHA-256 is shown in the package review.]"
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

    def install_apply(self, receipt, *, accept_partial=False):
        """Apply or retry only the explicitly selected terminal receipt."""
        return self._installation_services()[1].apply(receipt, accept_partial=accept_partial)

    def list_installations(self):
        """Inspect private installation state only through the terminal entry."""
        return self._installation_services()[1].installed()

    def uninstall(self, skill_id):
        """Perform the removal already confirmed by the terminal controller."""
        return self._installation_services()[1].uninstall(skill_id)

    def require_setup(self):
        """Deny discovery before creating state or issuing a request."""
        self.check_owner()
        if not (self.root / "wisdom.db").is_file():
            raise WisdomValidationError("Run hermes wisdom setup in your terminal first")
        self._open().require_setup()

    def _discovery(self):
        from .discovery import Discovery  # pylint: disable=import-outside-toplevel

        self.require_setup()
        return Discovery(
            self.client, self.check_owner,
            portal_base_url=self.account.portal_base_url,
            organization_id=self.account.organization_id,
        )

    def list_skills(self):
        """Read one raw discovery page."""
        return self._discovery().list_skills()

    def search_skills(self, query=None):
        """Read published skills matching the supplied query."""
        return self._discovery().search_skills(query)

    def show(self, skill_id):
        """Inspect published metadata only."""
        return self._discovery().show(skill_id)

    def resolve_skill(self, reference):
        """Resolve an ID or unique slug for readable slash navigation."""
        return self._discovery().resolve_skill(reference)

    def versions(self, skill_id):
        """Read published version history."""
        return self._discovery().versions(skill_id)

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
        llm: Any,
        spawn_task: Callable[..., Any],
        config: dict[str, Any] | None = None,
        get_config: Callable[[str, Any], Any] | None = None,
        set_config: Callable[[str, Any], Any] | None = None,
    ):
        from .native_runtime import NativeSurfaceRuntime  # pylint: disable=import-outside-toplevel

        self.root = Path(root).resolve()
        self.profile = profile
        self.llm = llm
        self.config = config or {}
        self.get_config = get_config
        self.set_config = set_config
        self._lock = threading.RLock()
        self._closed = False
        self._children = set()
        self._services = set()
        self.registrations = []
        self.native = NativeSurfaceRuntime(
            self.root, profile[0], self._service, llm, spawn_task,
            session_context=current_session_context,
        )

    def _service(self):
        with self._lock:
            if self._closed:
                raise WisdomError("Collective Wisdom plugin is unloaded")
            service = InvocationService(
                self.root, self.profile, llm=self.llm, config=self.config,
                get_config=self.get_config, set_config=self.set_config,
            )
            self._services.add(service)
            return service

    def cli(self, args) -> int:
        """Synchronous native CLI entry, including interactive terminal setup."""
        from .commands import run_cli  # pylint: disable=import-outside-toplevel

        service = self._service()
        try:
            return run_cli(args, service)
        finally:
            service.close()
            with self._lock:
                self._services.discard(service)

    def slash(self, raw_args: str):
        """Route exact effects to native review after real host admission."""
        invocation = self.native.consume_request(raw_args)
        if invocation is not None:
            words = shlex.split(raw_args)
            command, target = words[0], " ".join(words[1:])
            if command.casefold() == "install":
                return self.native.begin_install_modes(invocation, target)
            if command.casefold() == "publish":
                return self.native.begin_publication(invocation, target)
            if command.casefold() in {"browse", "show", "versions"}:
                return self.native.begin_catalog(invocation, command, target)
            if command.casefold() == "settings":
                return self.native.begin_settings(invocation)
            return self.native.begin(invocation, target)
        # Capture the calling profile before the host moves an awaitable to a thread.
        context = contextvars.copy_context()

        async def resolve():
            task = context.run(asyncio.create_task, self._slash(raw_args))
            return await task

        return resolve()

    async def _slash(self, raw_args: str) -> str:
        """Run network work off-loop; cancellation stops the owned worker too."""
        from .commands import run_slash  # pylint: disable=import-outside-toplevel

        service = self._service()
        cancel = threading.Event()
        task = None
        try:
            try:
                words = shlex.split(raw_args)
            except ValueError:
                return "Invalid Wisdom command. Use /wisdom help."
            command = words[0].casefold() if words else "help"
            if command in {"help", "--help", "-h", "setup", "install", "installed", "uninstall"}:
                return run_slash("help" if command in {"--help", "-h"} else raw_args, service)
            binding = service.binding()
            task = asyncio.create_task(asyncio.to_thread(
                self._run_worker, raw_args, binding, cancel
            ))
            result = await asyncio.shield(task)
            service.check_owner()
            return result
        except Exception:  # pylint: disable=broad-exception-caught
            return _UNAVAILABLE
        finally:
            cancel.set()
            if task is not None and not task.done():
                await asyncio.shield(task)
            service.close()
            with self._lock:
                self._services.discard(service)

    def _run_worker(self, raw_args, binding, cancel):
        # A process boundary provides a real deadline even for stalled DNS or a
        # slow-drip HTTP response. No bearer is serialized or passed on argv.
        deadline = time.monotonic() + SLASH_BUDGET_SECONDS
        live_config = InvocationService(
            self.root, self.profile, config=self.config, get_config=self.get_config
        ).notification_config()
        request = {
            "package": str(Path(__file__).resolve().parent), "sys_path": sys.path,
            "root": str(self.root), "profile": [self.profile[0], str(self.profile[1])],
            "binding": binding, "args": raw_args, "config": live_config,
        }
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
                    return "Collective Wisdom request timed out. Please retry."
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

    service = InvocationService(
        Path(request["root"]), (request["profile"][0], Path(request["profile"][1])),
        config=request.get("config"),
    )
    try:
        if service.binding() != request["binding"]:
            return _UNAVAILABLE
        result = run_slash(request["args"], service)
        service.check_owner()
        return result
    except WisdomError:
        return _UNAVAILABLE
    finally:
        service.close()
