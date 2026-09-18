"""Small owner-bound review records over the injected PluginState."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .setup_lock import managed_operation_guard

_KEY = "wisdom-reviews"
_LOCK = "\0wisdom-reviews"


class ReviewState:
    """Persist explicit reviews only; notification attempts never enter this record."""

    # Independent domains intentionally share this small public-state guard.
    # pylint: disable=duplicate-code
    def __init__(self, state: Any, root: Path) -> None:
        if state is None or not callable(getattr(state, "get", None)) or not callable(
            getattr(state, "set", None)
        ):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root).resolve(strict=False)
    # pylint: enable=duplicate-code

    def _check_path(self) -> None:
        if Path(self.state.path).resolve(strict=False).parent != self.root:
            raise RuntimeError("Collective Wisdom profile root changed during review")

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_KEY)
        self._check_path()
        if value is None:
            return {"version": 1, "records": {}}
        if (
            not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("records"), dict)
        ):
            raise RuntimeError("Collective Wisdom review state is invalid")
        return value

    @contextmanager
    def change(self) -> Iterator[dict[str, Any]]:
        """Serialize one aggregate mutation, checking the active root at each write."""
        with managed_operation_guard(self.root, _LOCK, wait=True):
            value = self._load()
            yield value
            self._check_path()
            self.state.set(_KEY, value)
            self._check_path()

    def save(self, record: dict[str, Any]) -> None:
        """Persist a detached review record."""
        with self.change() as value:
            value["records"][record["id"]] = copy.deepcopy(record)

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Return a detached record, never a mutable reference to stored authority."""
        row = self._load()["records"].get(record_id)
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def replace(self, record_id: str, expected_state: str, record: dict[str, Any]) -> bool:
        """Compare-and-set the lifecycle state under the domain writer lock."""
        with self.change() as value:
            current = value["records"].get(record_id)
            if not isinstance(current, dict) or current.get("state") != expected_state:
                return False
            value["records"][record_id] = copy.deepcopy(record)
            return True

    def pending(self, owner: dict[str, str], *, limit: int = 100) -> list[dict[str, Any]]:
        """List the current owner's pending reviews newest-first."""
        rows = [
            copy.deepcopy(row) for row in self._load()["records"].values()
            if row.get("owner") == owner and row.get("state") == "pending"
        ]
        rows.sort(key=lambda row: (row.get("created_at", 0), row["id"]), reverse=True)
        return rows[:max(0, min(limit, 100))]
