"""Hermes PM must stage this plugin as a virtual workspace member.

Hermes shares one Python environment per install and stages each enabled
plugin as a uv workspace member, once per profile (hermes-agent
pm/plugins_state.py dependency_homes). A member without a build backend is
renamed to a unique ``hermes-plugin-<key>``; a buildable member keeps its
``[project].name`` (pm/workspace.py _workspace_member), so the same plugin
enabled in two profiles of one install claims one name twice and ``uv lock``
fails. Hermes loads this plugin from its directory, never from a built wheel.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_declares_no_build_backend() -> None:
    """No build backend: Hermes PM renames the member, so profiles never collide."""
    document = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "build-system" not in document
    assert document.get("tool", {}).get("uv", {}).get("package") is not True
    assert document["project"]["dependencies"], "dependencies must stay declared for Hermes PM"
