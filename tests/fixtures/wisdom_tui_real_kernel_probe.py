"""Disposable real-kernel probe for the registered TUI review command."""
# This configured-host subprocess intentionally imports and probes host-private seams.
# pylint: disable=import-error,protected-access

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import plugins
from tools.terminal_tool import set_approval_callback
from tui_gateway import server, server_requests
from tui_review_rpc import TuiReviewRpcDriver

from hermes_wisdom_plugin.contract import canonical_json_bytes
from hermes_wisdom_plugin.client import WisdomMuteResponse, WisdomSuppressionResponse
from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.install_execution import InstallExecutor
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.install_receipts import InstallReceipts
from hermes_wisdom_plugin.install_review import InstallReviewer
from hermes_wisdom_plugin.mediation_store import MediationStore
from hermes_wisdom_plugin.package import verify_content_files
from hermes_wisdom_plugin.preferences import WisdomPreferences
from hermes_wisdom_plugin.runtime import InvocationService
from hermes_wisdom_plugin.setup_document import PREFIX, SETUP_PATH, SUFFIX
from hermes_wisdom_plugin.setup_execution import SetupController
from hermes_wisdom_plugin.setup_host import SetupHost
from hermes_wisdom_plugin.setup_receipts import SetupReceipts
from hermes_wisdom_plugin.store import WisdomStore
from hermes_wisdom_plugin.sync_recovery import WisdomSyncRecovery
from hermes_wisdom_plugin.update_execution import UpdateExecutor
from hermes_wisdom_plugin.update_receipts import UpdateReceipts
from hermes_wisdom_plugin.update_review import UpdateReviewer

PLUGIN_ID = "hermes-collective-wisdom"
PROFILE = Path(os.environ["HERMES_HOME"]).resolve()
STATE = WisdomStore(PROFILE.parent / "real-kernel-state")
MARKER = PROFILE.parent / "setup-marker"
OWNER = {
    "profile_name": "default",
    "profile_home": str(PROFILE),
    "account_id": "synthetic-member",
    "organization_id": "synthetic-org",
    "base_url": "https://gateway.invalid",
    "installation_id": "hwi_synthetic",
}
SKILL_ID = "skill-real-tui"


def package(version: int):
    """Build a deterministic synthetic package for one fixture version."""
    manifest = canonical_json_bytes({
        "schema_version": 1,
        "name": "managed-tui",
        "requirements": {
            "hermes": {"minimum_version": "0.1.0"},
            "platforms": [],
            "architectures": [],
            "model": {},
            "tools": [],
            "plugins": [],
            "credentials": [],
            "connections": [],
            "filesystem": {},
            "network": {},
            "runtime": {},
            "hardware": [],
            "known_limitations": [],
        },
    })
    guidance = canonical_json_bytes({
        "schema_version": 1,
        "execution_requires_user_approval": True,
        "requirements": [],
        "setup_instructions": ["Create the disposable setup marker."],
        "credential_handoff": [],
        "compatibility_limits": [],
        "verification_step": "Verify the disposable setup marker.",
        "removed_or_generalized": [],
        "related_skills": [],
    })
    files = [
        ("SKILL.md", "file", f"# Managed TUI v{version}\n".encode()),
        ("skill.manifest.json", "file", manifest),
        (SETUP_PATH, "file", PREFIX.encode() + guidance + SUFFIX.encode()),
    ]
    _records, content_hash = verify_content_files(files)
    return files, content_hash, json.loads(manifest)["requirements"]


PACKAGES = {version: package(version) for version in (1, 2)}


class Client:
    """In-memory Gateway transport around the real local receipt kernels."""

    def __init__(self):
        self.installed_version = 1
        self.mute_revision = 1
        self.owner_user_id = OWNER["account_id"]
        self.display_org_id = OWNER["organization_id"]

    def skill(self, skill_id):
        """Return the fixture skill and both published versions."""
        assert skill_id == SKILL_ID
        return SimpleNamespace(
            skill={
                "id": skill_id,
                "slug": "managed-tui",
                "state": "active",
                "takedown_generation": 0,
            },
            versions=[{"version": 1, "state": "published"}, {"version": 2, "state": "published"}],
        )

    def version(self, skill_id, version):
        """Return exact fixture metadata for a requested version."""
        assert skill_id == SKILL_ID
        _files, content_hash, system_spec = PACKAGES[version]
        return SimpleNamespace(
            skill={
                "id": skill_id,
                "slug": "managed-tui",
                "state": "active",
                "takedown_generation": 0,
            },
            version={
                "version": version,
                "content_hash": content_hash,
                "commit": f"synthetic-{version}",
                "system_spec": system_spec,
            },
        )

    def content(self, skill_id, version, **_kwargs):
        """Return exact fixture package bytes and their content record."""
        assert skill_id == SKILL_ID
        files, content_hash, _system_spec = PACKAGES[version]
        return SimpleNamespace(content_hash=content_hash, commit=f"synthetic-{version}"), files

    def installations(self, installation_id):
        """Return the fixture's managed version-one installation."""
        assert installation_id == OWNER["installation_id"]
        return [{
            "skill_id": SKILL_ID,
            "installed_version": self.installed_version,
            "latest_version": 2,
            "takedown_generation": 0,
            "update_mode": "MANUAL",
            "skill_state": "active",
        }]

    def record_install(self, **kwargs):
        """Return the authoritative effective update mode."""
        self.installed_version = kwargs["version"]
        return {"effective_update_mode": "MANUAL"}

    def agent_led_policy(self):
        """Return the exact cooldown policy used by production deferral binding."""
        return {
            "org_id": OWNER["organization_id"],
            "usage_evidence_window_days": 30,
            "min_aggregate_invocations": 3,
            "consecutive_day_usage_counts": True,
            "repeated_edits_count": True,
            "max_recommendations_per_user_per_week": 5,
            "publication_mode": "managed",
            "install_popularity_threshold": 2,
            "notification_defaults": {
                "skill_ready_to_share": True,
                "teammate_published": True,
                "update_available": True,
            },
            "manager_review_email_cadence": "daily",
            "not_now_suppression_days": 7,
            "version": 1,
            "updated_by_user_id": OWNER["account_id"],
        }

    def recommendation_mute(self):
        """Return revisioned organization mute authority."""
        return WisdomMuteResponse(
            org_id=OWNER["organization_id"], muted=False, duration=None,
            muted_until=None, forever=False, revision=self.mute_revision,
        )

    def set_recommendation_mute(
        self, duration, *, expected_revision, mutation_id, requested_at
    ):
        """Acknowledge one exact revision-bound mute mutation."""
        assert expected_revision == self.mute_revision
        self.mute_revision += 1
        days = {"1_day": 1, "1_week": 7, "30_days": 30}.get(duration)
        requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        muted_until = (requested + timedelta(days=days)).isoformat() if days else None
        return WisdomMuteResponse(
            org_id=OWNER["organization_id"], muted=duration is not None,
            duration=duration, muted_until=muted_until, forever=duration == "forever",
            revision=self.mute_revision, mutation_id=mutation_id,
        )

    def suppress_recommendation(self, key):
        """Acknowledge one idempotent exact-reference cooldown."""
        return WisdomSuppressionResponse(
            org_id=OWNER["organization_id"], key=key,
            suppress_until=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )

    def close(self):
        """Close the in-memory client."""


CLIENT = Client()
HOST = InstallationHost(PROFILE)
INSTALL_RECEIPTS = InstallReceipts(STATE, lambda: dict(OWNER))
INSTALL_REVIEWER = InstallReviewer(
    CLIENT, STATE, INSTALL_RECEIPTS, HOST, lambda: dict(OWNER)
)
INSTALLER = InstallExecutor(
    CLIENT, STATE, INSTALL_RECEIPTS, INSTALL_REVIEWER, HOST, lambda: dict(OWNER)
)
UPDATE_RECEIPTS = UpdateReceipts(STATE, lambda: dict(OWNER))
UPDATE_REVIEWER = UpdateReviewer(
    CLIENT, STATE, UPDATE_RECEIPTS, HOST, lambda: dict(OWNER)
)
UPDATER = UpdateExecutor(
    CLIENT, STATE, UPDATE_RECEIPTS, UPDATE_REVIEWER, HOST, lambda: dict(OWNER)
)
SETUP = SetupController(
    STATE,
    SetupReceipts(STATE, lambda: dict(OWNER)),
    HOST,
    SetupHost(),
    lambda: dict(OWNER),
)


class Service:
    """The plugin service seam backed only by production Wisdom kernels."""

    # The probe mirrors the public InvocationService surface so every exercised
    # operation still crosses the production receipt and authority kernels.
    # pylint: disable=too-many-public-methods

    store = STATE
    client = CLIENT
    _mode = "agent"

    @staticmethod
    def require_setup():
        """The disposable fixture has a complete synthetic enrollment."""

    @staticmethod
    def native_owner():
        """Return the exact synthetic enrollment owner."""
        return dict(OWNER)

    def notification_mode(self):
        """Return the fixture's current notification mode."""
        return type(self)._mode

    @staticmethod
    def notification_mode_writable():
        """Report that the disposable fixture permits mode changes."""
        return True

    def set_notification_mode(self, mode, *, expected_mode=None, may_apply=None):
        """Apply one current-authority notification mode change."""
        assert expected_mode is None or expected_mode == type(self)._mode
        assert may_apply is None or may_apply() is True
        type(self)._mode = mode
        return {"delivery_mode": mode}

    notification_settings = InvocationService.notification_settings
    notification_mute_status = InvocationService.notification_mute_status
    choose_notification_mute = InvocationService.choose_notification_mute
    def prepare_review_deferral(self, plan):
        """Prepare a real receipt-bound install deferral."""
        STATE.activate_installation_identity(
            OWNER["installation_id"], OWNER["organization_id"]
        )
        STATE.set_metadata("qualification_owner", dict(OWNER))
        MediationStore(STATE, owner=OWNER)
        return InvocationService.prepare_review_deferral(self, plan)  # type: ignore[arg-type]

    defer_review = InvocationService.defer_review

    def prepare_update_deferral(self, plan):
        """Prepare a real receipt-bound update deferral."""
        return InvocationService.prepare_update_deferral(self, plan)
    defer_update = InvocationService.defer_update

    def install_plan(self, reference, *, update_mode=None):
        """Create a real install receipt-backed plan."""
        return INSTALL_REVIEWER.plan(reference, update_mode=update_mode)

    def native_install_inspect(self, receipt):
        """Read the complete reviewed install package."""
        return InvocationService._native_package_inspect(self, INSTALL_REVIEWER, receipt, "Install")

    def install_apply(self, receipt, *, accept_partial=False):
        """Apply the exact reviewed install receipt."""
        return INSTALLER.apply(receipt, accept_partial=accept_partial)

    def list_installations(self):
        """Read existing installations without replaying an operation."""
        return INSTALLER.installed()

    def check_update(self, skill_id):
        """Check available versions without applying them."""
        return UPDATE_REVIEWER.check(skill_id)


    def update_plan(self, skill_id, **choices):
        """Create a real update receipt-backed plan."""
        return UPDATE_REVIEWER.plan(skill_id, **choices)

    def native_update_inspect(self, receipt):
        """Read the complete reviewed update package."""
        return InvocationService._native_package_inspect(self, UPDATE_REVIEWER, receipt, "Update")

    def update_apply(self, receipt, *, may_apply=None):
        """Apply the exact reviewed update receipt."""
        return UPDATER.apply(receipt, may_apply=may_apply)

    def update_recovery_inspect(self, skill_id, operation_id):
        """Inspect durable update recovery state without resuming it."""
        return UPDATER.recovery_status(skill_id, operation_id)

    def update_recovery_resume(
        self, skill_id, operation_id, receipt, *, may_apply
    ):
        """Resume only the exact inspected update receipt."""
        status = UPDATER.recovery_status(skill_id, operation_id)
        assert status["receipt"] == receipt
        return UPDATER.apply(receipt, may_apply=may_apply)

    def installed_setup_inspect(self, skill_id, *, version=None):
        """Inspect real setup guidance without executing it."""
        return SETUP.inspect(skill_id, version=version)

    def installed_setup_plan(self, skill_id, **values):
        """Create a separately approved setup receipt."""
        return SETUP.plan(skill_id, **values)

    def installed_setup_apply(self, receipt, *, may_apply=None):
        """Apply the exact reviewed setup receipt."""
        return SETUP.apply(receipt, may_apply=may_apply)

    def installed_setup_recover(
        self, skill_id, operation_id, *, acknowledged=False, may_apply=None
    ):
        """Reconcile or explicitly abandon one setup operation."""
        return SETUP.recover(
            skill_id, operation_id,
            acknowledged_effects_checked_and_stopped=acknowledged,
            may_apply=may_apply,
        )

    def mute(self, choice="status"):
        """Run the production mute command against fixture authority."""
        return WisdomPreferences(self).native_mute_command(choice)

    def sync_status(self):
        """Inspect production outbox recovery state."""
        return WisdomSyncRecovery(self).status()

    def sync_retry(self, *, may_apply=None):
        """Retry only known-safe production outbox work."""
        return WisdomSyncRecovery(self).retry(may_apply=may_apply)

    @staticmethod
    def check_owner():
        """Verify the fixture remains bound to its disposable profile."""
        assert OWNER["profile_home"] == str(PROFILE)

    @staticmethod
    def close():
        """Close the stateless fixture service."""


class Transport:
    """Record host RPC frames for the synchronous fixture driver."""

    supports_local_presentation = True

    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def write(self, frame):
        """Append one host frame under the transport lock."""
        with self.lock:
            self.frames.append(frame)
        return True

    def close(self):
        """Close the in-memory transport."""


def recheck_expired_install(invoke, reviews):
    """A real expired receipt cannot apply; a fresh RPC review still needs approval."""
    raw = f"install {SKILL_ID}@v1 --accept-partial"
    with patch.object(INSTALL_RECEIPTS, "clock", return_value=100) as clock:
        stale = INSTALL_REVIEWER.plan(f"{SKILL_ID}@v1")
        clock.return_value = stale["expires_at"]
        with patch.object(Service, "install_plan", return_value=stale):
            denied = invoke(raw, None)
        assert f"Recheck: /wisdom {raw}" in denied
        assert not reviews
        assert STATE.installation(SKILL_ID) is None
        try:
            INSTALLER.apply(stale["receipt"], accept_partial=True)
        except WisdomConflict:
            pass
        else:
            raise AssertionError("expired receipt was accepted")
        cancelled = invoke(raw, "cancel")
        assert "cancelled" in cancelled
        assert reviews[-1]["plan"]["receipt"] != stale["receipt"]
        assert STATE.installation(SKILL_ID) is None
        assert not STATE.pending_operations()


def inspect_pending_setup(invoke, sid, session):
    """Reading pending setup cannot poll/finalize it, even across revocation."""
    def start_recorded(_command, *, on_started, **_kwargs):
        on_started({"session_id": "recording-only"})
        return {"state": "running"}

    with patch.object(SETUP.execution_host, "execute", side_effect=start_recorded):
        started = json.loads(invoke(
            f"installed-setup plan {SKILL_ID} --version 2 --phase setup "
            "--command 'printf snapshot'", "run_setup",
        ))
    assert started["state"] == "running"
    before = STATE.operation(started["operation_id"])
    inspect = Service.installed_setup_inspect

    def revoke_after_read(self, *args, **kwargs):
        value = inspect(self, *args, **kwargs)
        server._begin_tui_review_input(sid, session)
        return value

    raw = f"installed-setup status {SKILL_ID} --version 2"
    with patch.object(SETUP.execution_host, "status", return_value={"state": "passed"}) as poll:
        assert started["operation_id"] in invoke(raw, None)
        with patch.object(Service, "installed_setup_inspect", revoke_after_read):
            assert started["operation_id"] not in invoke(raw, None)
        poll.assert_not_called()
    assert STATE.operation(started["operation_id"]) == before
    assert SETUP.receipts.outcome(started["receipt"]) is None
    recover = f"installed-setup recover {SKILL_ID} {started['operation_id']}"
    with (
        patch.object(SETUP.execution_host, "status", return_value={"state": "unknown"}),
        patch.object(SETUP.execution_host, "execute") as rerun,
    ):
        abandoned = json.loads(invoke(recover, "abandon_setup"))
    assert abandoned["state"] == "abandoned"
    rerun.assert_not_called()


# Keep this disposable end-to-end authority journey linear: extracting phases
# would hide their shared review sequence and mutable real-kernel state.
def main():  # pylint: disable=too-many-locals,too-many-statements
    """Exercise install, setup, and update through real host review authority."""
    manager = plugins.PluginManager()
    manager.discover_and_load()
    entry = manager._plugin_commands["wisdom"]
    runtime = entry["handler"].__self__
    assert callable(runtime.can_request_review)
    runtime._service = Service

    # No admitted TUI invocation means no service construction or package disclosure.
    denied = plugins.resolve_plugin_command_result(entry["handler"](f"install {SKILL_ID}@v1"))
    assert "terminal" in denied.lower()
    assert STATE.installation(SKILL_ID) is None

    transport = Transport()
    sid = "wisdom-real-kernel-review"
    session = {
        "history_lock": threading.RLock(),
        "profile_home": str(PROFILE),
        "running": False,
        "session_key": sid,
        "transport": transport,
    }
    server._sessions[sid] = session
    server_requests.reset_for_tests()
    server_requests.bind_sinks(transport.write, lambda *_args: None)
    reviews = []

    rpc = TuiReviewRpcDriver(
        lambda raw: server._run_tui_plugin_command(entry, raw, sid, session, transport),
        lambda response: server_requests.resolve_response(response, transport=transport),
        transport,
        timeout=10,
    )

    def invoke(raw, action_id):
        output, params = rpc.invoke(raw, action_id)
        if params is not None:
            reviews.append(json.loads(params["body"]))
        return output

    set_approval_callback(lambda *_args, **_kwargs: "once")
    try:
        recheck_expired_install(invoke, reviews)
        blocked = INSTALL_REVIEWER.plan(f"{SKILL_ID}@v1")
        blocked["allowed"] = False
        assert Service().prepare_review_deferral(blocked)["reference"]["version"] == 1
        with patch.object(Service, "install_plan", return_value=blocked):
            deferred = invoke(f"install {SKILL_ID}@v1", "defer")
        assert json.loads(deferred.split("\n\n", 1)[0])["deferred"] is True
        assert STATE.installation(SKILL_ID) is None

        direct_settings = Service().notification_settings(may_apply=lambda: True)
        assert direct_settings["mode_writable"] is True
        cancelled_settings = invoke("settings", "cancel")
        assert cancelled_settings == "Review cancelled or unavailable. Nothing was changed."
        assert Service._mode == "agent"
        mode = json.loads(invoke("settings", "mode_fixed"))
        assert mode == {"delivery_mode": "fixed"}
        assert Service._mode == "fixed"
        assert "manual review remains" in reviews[-1]["mode_meanings"]["fixed"]
        mute = json.loads(invoke("mute 1w", "mute_1w"))
        assert mute["preference_sync"] == "synced"
        assert (
            json.loads(invoke("mute status", None))["sync"]["preference_sync"]
            == "synced"
        )

        with STATE.transaction() as db:
            db.execute(
                "UPDATE wisdom_preference_outbox SET state='failed',attempts=3"
            )
        status = json.loads(invoke("sync status", None))
        assert status["preference"]["retryable"] == 1
        assert status["can_retry"] is True
        retried = json.loads(invoke("sync retry", "retry_sync"))
        assert retried["preference"]["retryable"] == 0
        assert retried["can_retry"] is False

        installed = invoke(f"install {SKILL_ID}@v1 --accept-partial", "install")
        assert json.loads(installed.split("\n\n", 1)[0])["installed"] is True
        target = PROFILE / "skills" / "_wisdom" / "synthetic-org" / "managed-tui"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Managed TUI v1\n"
        assert not MARKER.exists()
        assert SKILL_ID in invoke("installed", None)
        assert SKILL_ID in invoke(f"check {SKILL_ID}", None)
        assert "# Managed TUI v1\n" in "".join(
            page["content"] for page in reviews[-1]["package_files"]
            if page["path"] == "SKILL.md"
        )

        setup = invoke(
            f"installed-setup plan {SKILL_ID} --version 1 --phase setup --index 0 "
            f"--command \"printf configured > '{MARKER}'\"",
            "run_setup",
        )
        assert json.loads(setup)["state"] == "passed"
        assert MARKER.read_text(encoding="utf-8") == "configured"
        review_count = len(reviews)
        assert SKILL_ID in invoke(f"installed-setup status {SKILL_ID} --version 1", None)
        assert len(reviews) == review_count

        with patch.object(CLIENT, "record_install", side_effect=ConnectionError("offline")):
            pending = json.loads(invoke(f"update {SKILL_ID}", "update"))
        update_review = reviews[-1]
        assert pending["pending"] is True
        assert pending["phase"] == "local_ledger_committed"
        updated = invoke(
            f"update recover {SKILL_ID} {pending['operation_id']}", "resume_update"
        )
        assert json.loads(updated)["updated"] is True
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Managed TUI v2\n"
        assert "# Managed TUI v2\n" in "".join(
            page["content"] for page in update_review["package_files"]
            if page["path"] == "SKILL.md"
        )
        inspect_pending_setup(invoke, sid, session)
    finally:
        set_approval_callback(None)
        server._sessions.pop(sid, None)
        runtime.close()
        manager.unload()

    (PROFILE / "real-kernel-probe-passed").write_text("passed", encoding="utf-8")


if __name__ == "__main__":
    main()
