"""First native admission composes the real provider, core worker and reservation store."""
# pylint: disable=missing-function-docstring,duplicate-code
import os
from pathlib import Path
import subprocess
import sys

import pytest


_HOSTS = [path for path in os.environ.get("WISDOM_HOST_AGENT_DIRS", "").split(os.pathsep)
          if path and (Path(path) / "hermes_cli/skill_catalog/facade.py").is_file()]

_PROBE = r'''
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
entry = registry.Entry(provider.source, provider.plugin_key,
    Descriptor(1, "team", provider.label, provider.implementation_revision),
    provider, scope, None)
entry.authorized = lambda: True
projection = Projection(home)
handle = projection.start(entry)
entry.mutations = Mutations(entry, projection)
entry.ready = True
registry._entries[scope] = {entry.source_id: entry}
try:
    projection.publish(entry, [provider.installation(row, owner)
                              for row in store.installations(owner=owner)])
    api = ContextMutations(SimpleNamespace(plugin_id=provider.plugin_key,
        _manager=SimpleNamespace(scope_key=scope)))
    native = NativeCatalog(api, provider.plugin_key, executor.host)
    operation_id = native.admit(executor, plan, action, {})
    row, = projection.read()["reservations"].values()
    assert row["operation_id"] == operation_id
    assert row["action"] == action and row["recovery"] is False
    assert row["target_relative_path"] == target.relative_to(executor.host.skills_root).as_posix()
    assert not store.pending_operations(), "admission alone must not execute the operation"
finally:
    registry._entries.pop(scope, None)
    handle.close()
'''


@pytest.mark.parametrize("host", _HOSTS or [None])
@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
def test_real_core_first_native_admission_reserves(host, action, tmp_path):
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS to a host with the skill catalog facade")
    home = tmp_path / "isolated-home"
    home.mkdir()
    environment = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    environment.update(HOME=str(home), HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, host, str(Path(__file__).resolve().parents[1]),
         str(tmp_path / "case"), action],
        cwd=tmp_path, env=environment, text=True, capture_output=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
