"""PluginState-backed installed-skill authority and cutover tests."""
# pylint: disable=missing-class-docstring,missing-function-docstring,import-outside-toplevel

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from host_probe_support import (
    immutable_host_checkout,
    isolated_host_environment,
    run_host_python,
)

from hermes_wisdom_plugin.errors import WisdomConflict


HOST_REVISION = "bf51fee548ceaa28d2c299a41c0f65066c04c061"


def _owner(account: str = "user-1") -> dict[str, str]:
    return {
        "profile_name": "fixture",
        "profile_home": "/fixture",
        "account_id": account,
        "organization_id": "org-1",
        "base_url": "https://gateway.invalid",
        "installation_id": "hwi_fixture",
    }


def _install(skill_id: str, owner: dict[str, str] | None = None) -> dict:
    return {
        "skill_id": skill_id,
        "org_id": "org-1",
        "slug": skill_id,
        "version": 1,
        "content_hash": f"sha256:{skill_id}",
        "baseline": {"SKILL.md": f"sha256:{skill_id}"},
        "target_path": f"/fixture/skills/{skill_id}",
        "update_mode": "MANUAL",
        "state": "active",
        "owner": owner or _owner(),
    }


def test_actual_host_plugin_state_round_trip_uses_manifest_namespace(tmp_path: Path) -> None:
    """The integration seam is the host facade, not a dictionary-shaped fake."""

    script = """
import json, os
from hermes_cli.plugins_state import PluginState
from hermes_wisdom_plugin.installed_state import InstalledState
state = PluginState('hermes-collective-wisdom')
installed = InstalledState(state, state.data_dir)
if os.environ['ACTION'] == 'write':
    installed.record_install(json.loads(os.environ['RECORD']))
else:
    print(json.dumps(installed.installations(json.loads(os.environ['OWNER']))))
"""
    with immutable_host_checkout(
        HOST_REVISION, required_path="hermes_cli/plugins_state.py"
    ) as host:
        env = isolated_host_environment(
            home=tmp_path / "home",
            hermes_home=tmp_path / "profile",
            host_path=host,
            python_paths=(host, Path.cwd()),
        )
        run_host_python(
            ["-c", script], cwd=host,
            environment={**env, "ACTION": "write", "RECORD": json.dumps(_install("skill-a"))},
            timeout=30,
        )
        result = run_host_python(
            ["-c", script], cwd=host,
            environment={**env, "ACTION": "read", "OWNER": json.dumps(_owner())},
            timeout=30,
        )

    assert json.loads(result.stdout) == [_install("skill-a")]
    state_file = next((tmp_path / "profile" / "plugin-data").glob("*/state.json"))
    assert list(json.loads(state_file.read_text())) == ["installed-core"]


def test_actual_host_overlapping_aggregate_writes_are_busy_then_retryable(
    tmp_path: Path,
) -> None:
    """Two acknowledged aggregate writes must never lose one review."""

    script = """
import json, threading
from concurrent.futures import ThreadPoolExecutor
from hermes_cli.plugins_state import PluginState
from hermes_wisdom_plugin.installed_state import InstalledState
state = PluginState('hermes-collective-wisdom')
installed = InstalledState(state, state.data_dir)
original_set = state.set
entered = threading.Event()
release = threading.Event()
calls_lock = threading.Lock()
calls = 0
def blocking_set(key, value):
    global calls
    with calls_lock:
        calls += 1
        first = calls == 1
    if first:
        entered.set()
        if not release.wait(5):
            raise RuntimeError('timed out waiting for competing writer')
    return original_set(key, value)
state.set = blocking_set
with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(installed.save_review, 'review-a', {'receipt': 'review-a'})
    if not entered.wait(5):
        raise RuntimeError('first writer did not reach state.set')
    second = pool.submit(installed.save_review, 'review-b', {'receipt': 'review-b'})
    try:
        second.result(timeout=5)
    except Exception as exc:
        busy = 'starting or running' in str(exc)
    else:
        busy = False
    release.set()
    first.result(timeout=5)
installed.save_review('review-b', {'receipt': 'review-b'})
print(json.dumps({'busy': busy, 'reviews': sorted(state.get('installed-core')['reviews'])}))
"""
    with immutable_host_checkout(
        HOST_REVISION, required_path="hermes_cli/plugins_state.py"
    ) as host:
        env = isolated_host_environment(
            home=tmp_path / "home",
            hermes_home=tmp_path / "profile",
            host_path=host,
            python_paths=(host, Path.cwd()),
        )
        result = run_host_python(["-c", script], cwd=host, environment=env, timeout=30)

    assert json.loads(result.stdout) == {
        "busy": True,
        "reviews": ["review-a", "review-b"],
    }



def test_state_refuses_dynamic_profile_root_change(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.installed_state import InstalledState

    class MovingState:
        def __init__(self) -> None:
            self.path = tmp_path / "a" / "state.json"

        def get(self, _key, default=None):
            self.path = tmp_path / "b" / "state.json"
            return default

        def set(self, _key, _value):
            raise AssertionError("wrong profile must not be written")

    with pytest.raises(RuntimeError, match="profile root changed"):
        InstalledState(MovingState(), tmp_path / "a").record_install(_install("skill-a"))


def test_review_binding_is_owner_hash_bound_and_replay_safe(tmp_path: Path) -> None:
    from hermes_wisdom_plugin.install_receipts import InstallReceipts
    from hermes_wisdom_plugin.installed_state import InstalledState

    class JsonState:
        path = tmp_path / "state.json"

        def __init__(self) -> None:
            self.value = None

        def get(self, _key, default=None):
            return json.loads(json.dumps(self.value)) if self.value is not None else default

        def set(self, _key, value):
            self.value = json.loads(json.dumps(value))

    owner = _owner()
    state = InstalledState(JsonState(), tmp_path)
    receipts = InstallReceipts(state, lambda: owner, clock=lambda: 10.0)
    plan = receipts.create({"skill_id": "skill-a", "content_hash": "sha256:reviewed"})
    operation_id = state.begin(
        "install", "skill-a", {**plan, "owner": owner, "receipt": plan["receipt"]},
        stage="prepared",
    )
    receipts.start(plan["receipt"], operation_id)
    result = {"installed": True, "content_hash": "sha256:reviewed"}
    receipts.complete(plan["receipt"], result)

    assert receipts.outcome(plan["receipt"]) == result
    owner["account_id"] = "other"
    with pytest.raises(Exception, match="does not belong"):
        receipts.load(plan["receipt"], allow_started=True)


def test_publisher_install_record_is_idempotent_and_cannot_clobber_id_or_path(tmp_path: Path):
    from hermes_wisdom_plugin.installed_state import InstalledState

    class JsonState:
        path = tmp_path / "state.json"

        def __init__(self) -> None:
            self.value = None

        def get(self, _key, default=None):
            return copy.deepcopy(self.value) if self.value is not None else default

        def set(self, _key, value):
            self.value = copy.deepcopy(value)

    store = InstalledState(JsonState(), tmp_path)
    first = _install("skill-a")
    first["target_path"] = "/fixture/skills/authored"

    store.record_publisher_install(first)
    store.record_publisher_install(first)
    assert store.installation("skill-a") == first

    with pytest.raises(Exception, match="skill id"):
        store.record_publisher_install({**first, "target_path": "/fixture/skills/other"})
    with pytest.raises(Exception, match="source target"):
        store.record_publisher_install({
            **first, "skill_id": "skill-b", "target_path": first["target_path"],
        })


def test_publisher_install_advance_is_full_record_compare_and_swap(tmp_path: Path):
    from hermes_wisdom_plugin.installed_state import InstalledState

    class JsonState:
        path = tmp_path / "state.json"

        def __init__(self) -> None:
            self.value = None

        def get(self, _key, default=None):
            return copy.deepcopy(self.value) if self.value is not None else default

        def set(self, _key, value):
            self.value = copy.deepcopy(value)

    store = InstalledState(JsonState(), tmp_path)
    first = _install("skill-a")
    first["target_path"] = "/fixture/skills/authored"
    second = {
        **first,
        "version": 2,
        "content_hash": "sha256:second",
        "baseline": {"SKILL.md": "sha256:second"},
    }
    third = {
        **second,
        "version": 3,
        "content_hash": "sha256:third",
        "baseline": {"SKILL.md": "sha256:third"},
    }
    store.record_publisher_install(first)

    store.record_publisher_install(second, previous=first)
    assert store.installation("skill-a") == second

    with pytest.raises(WisdomConflict, match="changed during managed enrollment"):
        store.record_publisher_install(third, previous=first)
    assert store.installation("skill-a") == second
