"""Direct PluginState-owned enrollment/setup/publication service behavior."""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods
# pylint: disable=import-outside-toplevel,redefined-outer-name

from __future__ import annotations

from pathlib import Path
import multiprocessing

import pytest
from pydantic import BaseModel

from installed_test_support import JsonState
from test_installed_setup import OWNER, installed  # pylint: disable=unused-import

from hermes_wisdom_plugin.errors import WisdomValidationError


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


def test_direct_state_serializes_overlapping_process_writers(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_write_reviews, args=(str(tmp_path), prefix))
               for prefix in ("first", "second")]

    for worker in workers:
        worker.start()
    for worker in workers:
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
    from hermes_wisdom_plugin.errors import WisdomConflict

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
