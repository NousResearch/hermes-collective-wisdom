"""Disposable PluginState-shaped fixture for installed-skill behavior tests."""
# pylint: disable=missing-function-docstring

from __future__ import annotations

import copy
import json
from pathlib import Path

from hermes_wisdom_plugin.installed_state import InstalledState


class JsonState:
    """Small file-backed public get/set facade; host integration is tested separately."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "state.json"

    def get(self, key, default=None):
        if not self.path.is_file():
            return copy.deepcopy(default)
        return copy.deepcopy(json.loads(self.path.read_text(encoding="utf-8")).get(key, default))

    def set(self, key, value) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.is_file() else {}
        )
        current[key] = copy.deepcopy(value)
        pending = self.path.with_suffix(".tmp")
        pending.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        pending.replace(self.path)


def installed_store(root: Path) -> InstalledState:
    root = Path(root)
    return InstalledState(JsonState(root), root)
