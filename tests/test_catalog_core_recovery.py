"""Real writer death inside core's first hold, before any filesystem intent is journaled.

Both lanes that re-admit a durable operation are driven: native re-admission through
``NativeCatalog`` and generic confirmation through core ``Service.confirm``.
"""
# pylint: disable=missing-function-docstring,duplicate-code
import os
from pathlib import Path
import subprocess
import sys

import pytest


_HOSTS = [path for path in os.environ.get("WISDOM_HOST_AGENT_DIRS", "").split(os.pathsep)
          if path and (Path(path) / "hermes_cli/skill_catalog/facade.py").is_file()]

_PROBE = r'''
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

host, plugin, temporary, action = sys.argv[1:]
sys.path[:0] = [host, plugin, str(Path(plugin) / "tests")]
from hermes_cli.skill_catalog import registry
from hermes_cli.skill_catalog.facade import ContextMutations
from hermes_cli.skill_catalog.identity import Descriptor
from hermes_cli.skill_catalog.mutations import Mutations
from hermes_cli.skill_catalog.projection import Projection
from hermes_constants import hermes_home_key
from hermes_wisdom_plugin.catalog_native import NativeCatalog
from hermes_wisdom_plugin.catalog_provider import CatalogProvider
from hermes_wisdom_plugin.package_application import tree_hashes
from hermes_wisdom_plugin.uninstall_review import UninstallReceipts, UninstallReviewer
from test_install_execution import invocation_executor
from test_update_execution import executor as update_executor
from test_uninstall_review import _installed

root = Path(temporary)
if action == "install":
    executor, store, client, receipts, plan, target, _ = invocation_executor.__wrapped__(root)
elif action == "update":
    executor, store, client, receipts, plan, target, _ = update_executor.__wrapped__(root)
else:
    executor, store, client, owner, target = _installed(root)
    receipts = UninstallReceipts(store, lambda: dict(owner))
    plan = UninstallReviewer(store, receipts, executor.host, lambda: dict(owner)).plan("skill-1")
owner = plan["owner"]
service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner))
@contextmanager
def services():
    yield service
provider = CatalogProvider(services, "collective-wisdom", executor.host)
home = executor.host.skills_root.parent
scope = hermes_home_key(home)
projection = Projection(home)


def register():
    """One serving process: fresh registration, generation and IPC worker."""
    entry = registry.Entry(provider.source, provider.plugin_key,
        Descriptor(1, "team", provider.label, provider.implementation_revision),
        provider, scope, None)
    entry.authorized = lambda: True
    handle = projection.start(entry)
    entry.mutations = Mutations(entry, projection)
    entry.ready = True
    registry._entries[scope] = {entry.source_id: entry}
    projection.publish(entry, [provider.installation(row, owner)
                              for row in store.installations(owner=owner)])
    return entry, handle


def run():
    if action == "uninstall":
        return executor.uninstall_reviewed(plan["receipt"], receipts=receipts)
    return executor.apply(plan["receipt"])


entry, handle = register()
api = ContextMutations(SimpleNamespace(plugin_id=provider.plugin_key,
    _manager=SimpleNamespace(scope_key=scope)))
executor.catalog = None
executor.admitter = NativeCatalog(api, provider.plugin_key, executor.host)
entry.mutations.close()
pid = os.fork()
if pid == 0:
    entry.mutations = Mutations(entry, projection)
    original = entry.mutations.commit

    @contextmanager
    def die_inside_hold(*args):
        with original(*args):
            os._exit(73)
            yield

    entry.mutations.commit = die_inside_hold
    try:
        run()
    finally:
        os._exit(74)
_, status = os.waitpid(pid, 0)
try:
    assert os.waitstatus_to_exitcode(status) == 73, status
    pending, = store.pending_operations("skill-1")
    assert pending["payload"].get("catalog_files") is None
    assert not pending["payload"].get("catalog_local_committed")
    before = tree_hashes(target)
    # Restart: the old registration leaves; the provider re-registers and re-admits.
    entry.mutations = Mutations(entry, projection)
    entry.stopped = True
    projection.stop(entry)
    handle.close()
    entry.mutations.close()
    entry, handle = register()
    executor.catalog = None
    expected = pending["payload"].get("baseline")
    result = run()
    assert result[{"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]]
    assert tree_hashes(target) == (None if action == "uninstall" else expected), before
    assert not store.pending_operations("skill-1")
    assert not projection.read()["reservations"]
    completed = store.operation(pending["id"])
    assert completed["status"] == "done" and completed["payload"]["catalog_local_committed"]
    assert completed["payload"]["catalog_files"]["phase"] == "files_committed"
finally:
    entry.mutations.close()
    registry._entries.pop(scope, None)
    entry.stopped = True
    projection.stop(entry)
    handle.close()
'''

_CONFIRM_PROBE = r'''
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

host, plugin, temporary, action = sys.argv[1:]
sys.path[:0] = [host, plugin, str(Path(plugin) / "tests")]
from hermes_cli.skill_catalog import registry
from hermes_cli.skill_catalog.facade import ContextMutations
from hermes_cli.skill_catalog.identity import Descriptor
from hermes_cli.skill_catalog.mutations import Mutations
from hermes_cli.skill_catalog.projection import Projection
from hermes_cli.skill_catalog.service import Service
from hermes_constants import hermes_home_key
from hermes_wisdom_plugin.catalog_provider import CatalogProvider
from hermes_wisdom_plugin.package_application import tree_hashes
from test_catalog_reviews import reviews
from test_update_execution import executor as update_executor
from test_uninstall_review import _installed

root = Path(temporary)
root.mkdir(parents=True, exist_ok=True)
if action == "install":
    _adapter, service, _reviewer, host_, client, _clock = reviews.__wrapped__(root)
    client.record_install = lambda **_values: SimpleNamespace(effective_update_mode="MANUAL")
    client.installations = lambda _installation_id: []
    store, owner, version = service.installed, service.native_owner(), "1"
else:
    if action == "update":
        executor, store, client, receipts, plan, _target, owner = update_executor.__wrapped__(root)
        reviewer = SimpleNamespace(receipts=receipts, plan=lambda *_a, **_kw: dict(plan))
    else:
        executor, store, client, owner, _target = _installed(root)
        reviewer = None
    host_ = executor.host
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner),
                              _installation_services=lambda: (reviewer, executor),
                              _update_services=lambda: (reviewer, executor))
    version = None
@contextmanager
def services():
    yield service
home = host_.skills_root.parent
scope = hermes_home_key(home)
projection = Projection(home)
api = ContextMutations(SimpleNamespace(plugin_id="collective-wisdom",
    _manager=SimpleNamespace(scope_key=scope)))
provider = CatalogProvider(services, "collective-wisdom", host_, mutations=api)
identifier = provider.source + ":skill-1"


def register():
    """One serving process: fresh registration, generation and IPC worker."""
    entry = registry.Entry(provider.source, provider.plugin_key,
        Descriptor(1, "team", provider.label, provider.implementation_revision),
        provider, scope, None)
    entry.authorized = lambda: True
    handle = projection.start(entry)
    entry.mutations = Mutations(entry, projection)
    entry.ready = True
    registry._entries[scope] = {entry.source_id: entry}
    projection.publish(entry, [provider.installation(row, owner)
                              for row in store.installations(owner=owner)])
    return entry, handle


def confirm(review):
    """The generic lane: core Service.confirm selects the worker and calls the provider."""
    return Service(home).confirm(identifier, action, review["review_token"],
                                 list(review["acknowledgements_required"]))


entry, handle = register()
try:
    review = Service(home).review(identifier, action, version)
    assert review["confirmable"], review
    target = host_.skills_root / review["target_relative_path"]
    before = tree_hashes(target)
    entry.mutations.close()
    pid = os.fork()
    if pid == 0:
        entry.mutations = Mutations(entry, projection)
        original = entry.mutations.commit

        @contextmanager
        def die_inside_hold(*args):
            with original(*args):
                os._exit(73)
                yield

        entry.mutations.commit = die_inside_hold
        try:
            confirm(review)
        finally:
            os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73, status
    pending, = store.pending_operations("skill-1")
    assert pending["payload"].get("catalog_files") is None
    assert not pending["payload"].get("catalog_local_committed")
    assert tree_hashes(target) == before
    # Restart: the old registration leaves; the provider re-registers before the retry.
    entry.mutations = Mutations(entry, projection)
    entry.stopped = True
    projection.stop(entry)
    handle.close()
    entry.mutations.close()
    entry, handle = register()
    expected = pending["payload"].get("baseline")
    result = confirm(review)
    outcome = {"install": "installed", "update": "updated", "uninstall": "uninstalled"}[action]
    assert result["state"] == "completed" and result["outcome"] == outcome, result
    assert result["operation_id"] == pending["id"], result
    assert tree_hashes(target) == (None if action == "uninstall" else expected), before
    assert not store.pending_operations("skill-1")
    assert not projection.read()["reservations"]
    completed = store.operation(pending["id"])
    assert completed["status"] == "done" and completed["payload"]["catalog_local_committed"]
    assert completed["payload"]["catalog_files"]["phase"] == "files_committed"
finally:
    entry.mutations.close()
    registry._entries.pop(scope, None)
    entry.stopped = True
    projection.stop(entry)
    handle.close()
'''


def _run(probe, host, action, tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir()
    environment = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    environment.update(HOME=str(home), HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-c", probe, host, str(Path(__file__).resolve().parents[1]),
         str(tmp_path / "case"), action],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False, timeout=120,
    )


@pytest.mark.parametrize("host", _HOSTS or [None])
@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_real_core_death_before_journal_recovers_after_re_registration(host, action, tmp_path):
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS to a host with the skill catalog facade")
    result = _run(_PROBE, host, action, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("host", _HOSTS or [None])
@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_real_core_generic_confirm_recovers_after_death_before_journal(host, action, tmp_path):
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS to a host with the skill catalog facade")
    result = _run(_CONFIRM_PROBE, host, action, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
