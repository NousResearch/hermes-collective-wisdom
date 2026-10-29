"""Optional checks of the Collective Wisdom service's publication driver.

Both tests skip unless WISDOM_GATEWAY_DIR points at a checkout of the service.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Test names carry the behavior contract.
# pylint: disable=missing-function-docstring

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_configured_publication_driver_imports_external_package_without_alias():
    gateway = os.environ.get("WISDOM_GATEWAY_DIR")
    if not gateway:
        pytest.skip("WISDOM_GATEWAY_DIR is required for actual Gateway driver imports")
    # Execute only the drivers' real Wisdom imports, never their service-backed main().
    # This checks exported names as well as package provenance without starting services.
    program = """
import ast
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
path = pathlib.Path(sys.argv[2])
source = path.read_text(encoding='utf-8')
imports = [node for node in ast.walk(ast.parse(source))
           if isinstance(node, ast.ImportFrom)
           and (node.module or '').startswith('hermes_wisdom')]
if imports:
    exec(compile(ast.Module(body=imports, type_ignores=[]), str(path), 'exec'), {})
else:
    assert path.name == 'wisdom_publication_driver.py'
    assert 'load_external_plugin(home)' in source and 'PluginManager' in source
assert not any(name == 'hermes_wisdom' or name.startswith('hermes_wisdom.')
               for name in sys.modules), 'retired package alias was imported'
for name, module in list(sys.modules.items()):
    if name == 'hermes_wisdom_plugin' or name.startswith('hermes_wisdom_plugin.'):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
print('External package imports verified without running the service driver')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(PROJECT_ROOT),
         str(Path(gateway) / "test" / "e2e" / "wisdom_publication_driver.py")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_publication_driver_calls_existing_client_methods():
    """Import-only bootstrap must not hide dead calls in the service-backed lane."""
    from hermes_wisdom_plugin.client import WisdomClient  # pylint: disable=import-outside-toplevel

    gateway = os.environ.get("WISDOM_GATEWAY_DIR")
    if not gateway:
        pytest.skip("WISDOM_GATEWAY_DIR is required for publication driver contracts")
    source = Path(gateway) / "test/e2e/wisdom_publication_driver.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    methods = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"publisher", "manager_client"}
    }
    assert methods, "Publication driver client calls were not inspected"
    missing = sorted(name for name in methods if not callable(getattr(WisdomClient, name, None)))
    assert not missing, f"Publication driver calls absent WisdomClient methods: {missing}"
