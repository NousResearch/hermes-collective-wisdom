"""Direct PluginState-owned enrollment/setup/publication service behavior."""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# pylint: disable=import-outside-toplevel,redefined-outer-name

from __future__ import annotations

import errno
import hashlib
import multiprocessing
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from installed_test_support import JsonState
from test_installed_setup import OWNER, installed  # pylint: disable=unused-import

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomValidationError


class Capability(BaseModel):
    features: list[str]


class Identity(BaseModel):
    installation_id: str
    state: str


class Account:
    profile_name = "fixture"
    profile_home = "/profiles/fixture"
    account_id = "user-1"
    organization_id = "org-1"
    base_url = "https://gateway.invalid"

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def credentials(self, scope="wisdom:read"):
        self.scopes.append(scope)
        return {"access_token": "not-persisted"}


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def capability(self):
        self.calls.append(("capability", None))
        return Capability(features=["wisdom"])

    def register_identity(self, installation_id):
        self.calls.append(("register", installation_id))
        return Identity(installation_id=installation_id, state="active")


class ConflictOnceClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.conflicted = False

    def register_identity(self, installation_id):
        self.calls.append(("register", installation_id))
        if not self.conflicted:
            self.conflicted = True
            raise WisdomConflict("fixture identity conflict", code="identity_conflict")
        return Identity(installation_id=installation_id, state="active")


def service(tmp_path: Path):
    from hermes_wisdom_plugin.direct_services import DirectState, EnrollmentService

    account = Account()
    client = Client()
    state = DirectState(JsonState(tmp_path), tmp_path)
    return account, client, state, EnrollmentService(state, account, client)


def _write_reviews(root: str, prefix: str) -> None:
    from hermes_wisdom_plugin.direct_services import DirectState

    state = DirectState(JsonState(Path(root)), Path(root))
    for index in range(20):
        state.save_setup_review(f"{prefix}-{index}", {"writer": prefix, "index": index})


def test_managed_operation_guard_uses_atomic_create_then_open(tmp_path, monkeypatch) -> None:
    """Model Darwin's concurrent O_CREAT ENOENT while retaining real filesystem I/O."""

    from hermes_wisdom_plugin import setup_lock

    real_open = os.open
    lock_flags = []

    def darwin_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.endswith(".lock"):
            lock_flags.append(flags)
            if flags & os.O_CREAT and not flags & os.O_EXCL:
                raise FileNotFoundError(errno.ENOENT, "injected concurrent create race", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(setup_lock.os, "open", darwin_open)
    with setup_lock.managed_operation_guard(tmp_path, "skill-1"):
        pass
    lock_path = tmp_path / "operation-locks" / (
        hashlib.sha256(b"skill-1").hexdigest() + ".lock"
    )
    first = lock_path.stat()
    with setup_lock.managed_operation_guard(tmp_path, "skill-1"):
        pass

    assert lock_flags[0] & os.O_EXCL
    assert lock_path.stat().st_ino == first.st_ino


def test_managed_operation_guard_rejects_redirected_lock_directory(tmp_path) -> None:
    from hermes_wisdom_plugin.setup_lock import managed_operation_guard
    from hermes_wisdom_plugin.package import PackagePolicyError

    root = tmp_path / "state"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "operation-locks").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackagePolicyError, match="unavailable or unsafe"):
        with managed_operation_guard(root, "skill-1"):
            pass
    assert not list(outside.iterdir())


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "fifo"])
def test_managed_operation_guard_rejects_unsafe_lock_file(
    tmp_path, unsafe_kind: str,
) -> None:
    from hermes_wisdom_plugin.setup_lock import managed_operation_guard
    from hermes_wisdom_plugin.package import PackagePolicyError

    root = tmp_path / "state"
    locks = root / "operation-locks"
    locks.mkdir(parents=True)
    lock_path = locks / (hashlib.sha256(b"skill-1").hexdigest() + ".lock")
    if unsafe_kind == "hardlink":
        lock_path.write_bytes(b"")
        os.link(lock_path, tmp_path / "duplicate-lock")
    else:
        os.mkfifo(lock_path)

    with pytest.raises(PackagePolicyError, match="unavailable or unsafe"):
        with managed_operation_guard(root, "skill-1"):
            pass


def test_enrollment_status_is_local_only_and_setup_requires_disclosure(tmp_path: Path) -> None:
    account, client, state, enrollment = service(tmp_path)

    assert enrollment.status() == {
        "configured": False,
        "state": "not_configured",
        "reason": "not_configured",
    }
    assert not account.scopes
    assert not client.calls
    assert not state.state.path.exists()

    with pytest.raises(WisdomValidationError, match="explicit"):
        enrollment.setup(False)
    assert not account.scopes
    assert not client.calls


def test_enrollment_persists_intent_before_network_and_activates_same_identity(
    tmp_path: Path,
) -> None:
    _account, client, state, enrollment = service(tmp_path)
    observed = []
    original_capability = client.capability

    def capability():
        observed.append(state.identity())
        return original_capability()

    client.capability = capability
    result = enrollment.setup(True)

    assert observed[0]["state"] == "registering"
    assert observed[0]["installation_id"].startswith("hwi_")
    assert result["configured"] is True
    assert result["installation_id"] == observed[0]["installation_id"]
    assert [call[0] for call in client.calls] == ["capability", "register"]
    assert "not-persisted" not in repr(state.identity())


# The scenario intentionally covers each retained owner-bound state domain.
# pylint: disable-next=too-many-locals
def test_setup_switches_owner_without_relabeling_retained_records(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.installed_state import InstalledState

    account, _client, state, enrollment = service(tmp_path)
    first = enrollment.setup(True)
    old_owner = enrollment.owner()
    state.save_draft({
        "id": "draft-old", "owner": old_owner, "skill_id": "local-skill",
        "source_hash": "sha256:old", "state": "prepared",
    })
    state.save_setup_review("setup-review-old", {"owner": old_owner})
    setup_operation_id = state.begin_setup("setup-review-old", {
        "skill_id": "setup-old", "receipt": "setup-review-old", "owner": old_owner,
    })
    publication_receipt = state.save_publication_review({"draft_id": "draft-old"})
    publication_operation_id = state.begin_publication(publication_receipt, {
        "local_draft_id": "draft-old", "owner": old_owner, "skill_id": "local-skill",
    })
    installed = InstalledState(state.state, tmp_path)
    installed.record_install({
        "skill_id": "managed-old", "slug": "managed-old", "version": 1,
        "content_hash": "sha256:managed", "baseline": {}, "target_path": "/tmp/old",
        "update_mode": "MANUAL", "state": "active", "owner": old_owner,
    })

    account.account_id = "user-2"
    account.organization_id = "org-2"
    account.base_url = "https://gateway.staging.invalid"
    switched = enrollment.setup(True)

    assert switched["configured"] is True
    assert switched["installation_id"] != first["installation_id"]
    saved = state.read()
    assert saved["drafts"]["draft-old"]["owner"] == old_owner
    assert len(saved["enrollment_history"]) == 1
    assert saved["enrollment_history"][0]["owner"] == {
        "profile_name": old_owner["profile_name"],
        "profile_home": old_owner["profile_home"],
        "account_id": old_owner["account_id"],
        "organization_id": old_owner["organization_id"],
        "base_url": old_owner["base_url"],
    }
    assert saved["enrollment_history"][0]["installation_id"] == first["installation_id"]
    assert saved["setup_operations"][setup_operation_id]["owner"] == old_owner
    assert saved["publications"][publication_operation_id]["owner"] == old_owner
    assert installed.installation("managed-old")["owner"] == old_owner


# Three independently bound services model interleaved setup invocations.
# pylint: disable-next=too-many-locals
def test_setup_rejects_stale_a_b_a_response_before_registration(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.direct_services import EnrollmentService

    account_a1, client_a1, state, enrollment_a1 = service(tmp_path)
    account_b = Account()
    account_b.account_id = "user-2"
    account_b.organization_id = "org-2"
    client_b = Client()
    enrollment_b = EnrollmentService(state, account_b, client_b)
    account_a2 = Account()
    client_a2 = Client()
    enrollment_a2 = EnrollmentService(state, account_a2, client_a2)
    original_capability = client_a1.capability

    def switch_away_and_back():
        enrollment_b.setup(True)
        enrollment_a2.setup(True)
        return original_capability()

    client_a1.capability = switch_away_and_back

    with pytest.raises(WisdomConflict, match="superseded"):
        enrollment_a1.setup(True)

    assert client_a1.calls == [("capability", None)]
    active = state.identity()
    assert active is not None
    assert active["owner"]["account_id"] == account_a1.account_id
    installation_ids = {
        active["installation_id"],
        *(row["installation_id"] for row in state.read()["enrollment_history"]),
    }
    assert len(installation_ids) == 3


def test_setup_stops_when_account_changes_during_capability_read(tmp_path: Path) -> None:
    account, client, _state, enrollment = service(tmp_path)
    original_capability = client.capability

    def capability_then_switch():
        result = original_capability()
        account.account_id = "user-2"
        return result

    client.capability = capability_then_switch

    with pytest.raises(WisdomAuthError, match="account changed during setup"):
        enrollment.setup(True)
    assert client.calls == [("capability", None)]


def test_owner_switch_gets_an_independent_identity_conflict_retry(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.direct_services import DirectState, EnrollmentService

    state = DirectState(JsonState(tmp_path), tmp_path)
    first_account = Account()
    first_client = ConflictOnceClient()
    first = EnrollmentService(state, first_account, first_client).setup(True)
    first_identity = state.identity()
    assert first_identity is not None
    assert first_identity["conflict_rotated"] is True

    second_account = Account()
    second_account.account_id = "user-2"
    second_account.organization_id = "org-2"
    second_client = ConflictOnceClient()
    switched = EnrollmentService(state, second_account, second_client).setup(True)

    assert switched["configured"] is True
    assert switched["installation_id"] != first["installation_id"]
    assert [name for name, _value in second_client.calls] == [
        "capability", "register", "register",
    ]


def test_direct_state_serializes_overlapping_process_writers(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_write_reviews, args=(str(tmp_path), prefix))
               for prefix in ("first", "second")]

    started = []
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        for worker in started:
            worker.join(timeout=10)
    finally:
        for worker in started:
            if worker.is_alive():
                worker.terminate()
        for worker in started:
            worker.join(timeout=10)

    assert [worker.exitcode for worker in workers] == [0, 0]
    from hermes_wisdom_plugin.direct_services import DirectState
    saved = DirectState(JsonState(tmp_path), tmp_path).read()["setup_reviews"]
    assert len(saved) == 40


def test_direct_state_fences_plugin_state_to_profile_root(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.direct_services import DirectState

    host_state = JsonState(tmp_path)
    direct = DirectState(host_state, tmp_path)
    host_state.path = tmp_path.parent / "other-profile" / "state.json"

    with pytest.raises(RuntimeError, match="profile root changed"):
        direct.read()


class SetupExecutionHost:
    def __init__(self) -> None:
        self.calls = []
        self.result = {"state": "passed", "exit_code": 0, "output": "ok"}

    def execute(self, command, *, workdir, task_id, on_started, timeout=600):
        del timeout
        self.calls.append((command, workdir, task_id))
        handle = {"task_id": task_id, "workdir": str(workdir), "session_id": "test"}
        on_started(handle)
        return dict(self.result)

    def status(self, handle, *, command, workdir, task_id):
        del handle, command, workdir, task_id
        return {"state": "unknown"}


def test_direct_setup_review_apply_is_exact_and_idempotent(installed) -> None:
    from hermes_wisdom_plugin.direct_services import DirectState, SetupService

    installed_state, progress, inspection_host, target = installed
    state = DirectState(installed_state.state, progress.root)
    execution = SetupExecutionHost()
    setup = SetupService(
        state, installed_state, inspection_host, execution, lambda: dict(OWNER), clock=lambda: 10.0
    )
    command = "printf configured  "

    review = setup.plan("skill-1", version=1, phase="setup", index=0, command=command)
    assert not execution.calls
    assert review["operation"] == "installed_setup"
    result = setup.apply(review["receipt"])

    assert result["state"] == "passed"
    assert setup.apply(review["receipt"]) == result
    assert execution.calls == [(command, target, execution.calls[0][2])]


def test_direct_setup_unknown_launch_requires_acknowledged_recovery(installed) -> None:
    from hermes_wisdom_plugin.direct_services import DirectState, SetupService

    installed_state, progress, inspection_host, _target = installed
    state = DirectState(installed_state.state, progress.root)
    execution = SetupExecutionHost()
    execution.result = {"state": "unknown", "reason": "launch_acknowledgement_lost"}
    setup = SetupService(
        state, installed_state, inspection_host, execution, lambda: dict(OWNER)
    )
    review = setup.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf maybe"
    )

    first = setup.apply(review["receipt"])
    second = setup.apply(review["receipt"])
    assert first["state"] == second["state"] == "unknown"
    assert len(execution.calls) == 1
    pending = setup.recover("skill-1", first["operation_id"])
    assert pending["recovery_review"] is True
    abandoned = setup.recover(
        "skill-1", first["operation_id"],
        acknowledged_effects_checked_and_stopped=True,
    )
    assert abandoned["state"] == "abandoned"
    assert len(execution.calls) == 1


@pytest.mark.parametrize(
    "started,expire_during_inspection", [(False, False), (False, True), (True, False)]
)
# The scenarios share a real setup fixture and an independently advanced clock.
# pylint: disable-next=too-many-locals
def test_setup_expiry_denies_new_effects_but_preserves_started_result(
    installed, started, expire_during_inspection,
):
    from hermes_wisdom_plugin.direct_services import DirectState, SetupService

    installed_state, progress, inspection_host, _target = installed
    execution = SetupExecutionHost()
    clock = [10.0]
    setup = SetupService(
        DirectState(installed_state.state, progress.root), installed_state,
        inspection_host, execution, lambda: dict(OWNER), clock=lambda: clock[0],
    )
    review = setup.plan(
        "skill-1", version=1, phase="setup", index=0, command="printf configured  "
    )
    result = setup.apply(review["receipt"]) if started else None
    if expire_during_inspection:
        inspect = setup.inspect

        def delayed_inspect(*args, **kwargs):
            inspection = inspect(*args, **kwargs)
            clock[0] = review["expires_at"] + 1
            return inspection

        setup.inspect = delayed_inspect
    else:
        clock[0] = review["expires_at"] + 1
    if started:
        assert setup.apply(review["receipt"]) == result
        assert len(execution.calls) == 1
    else:
        with pytest.raises(WisdomConflict, match="expired"):
            setup.apply(review["receipt"])
        assert not execution.calls
