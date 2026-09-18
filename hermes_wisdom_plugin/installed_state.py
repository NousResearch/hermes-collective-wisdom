"""PluginState authority for managed installations and exact reviews."""

# Public aggregate methods intentionally mirror the small host state facade.
# pylint: disable=missing-function-docstring,too-many-boolean-expressions

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import WisdomConflict
from .setup_lock import managed_operation_guard

_KEY = "installed-core"
_AGGREGATE_LOCK = "\0installed-core-aggregate"
_OWNER_FIELDS = {
    "profile_name", "profile_home", "account_id", "organization_id", "base_url",
    "installation_id",
}



def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _owner(value: Any, *, optional: bool = False) -> dict[str, str] | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict) or set(value) != _OWNER_FIELDS or any(
        not isinstance(value[key], str) or not value[key] for key in _OWNER_FIELDS
    ):
        raise ValueError("managed installation requires an exact owner binding")
    return {key: value[key] for key in sorted(_OWNER_FIELDS)}


class InstalledState:
    """One clean-format aggregate injected from the host PluginState facade."""

    def __init__(self, state: Any, root: Path) -> None:
        if state is None or not callable(getattr(state, "get", None)) or not callable(
            getattr(state, "set", None)
        ):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self) -> Path:
        path = Path(self.state.path).resolve(strict=False)
        if path.parent != self.root:
            raise RuntimeError("Collective Wisdom profile root changed during state operation")
        return path

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1, "installations": {}, "reviews": {}, "operations": {},
            "automatic_cursor": None,
        }

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_KEY)
        self._path()
        if value is None:
            return self._empty()
        if (
            not isinstance(value, dict) or value.get("version") != 1
            or any(not isinstance(value.get(key), dict) for key in (
                "installations", "reviews", "operations"
            ))
        ):
            raise RuntimeError("Collective Wisdom installed state is invalid")
        return value

    @contextmanager
    def _change(self) -> Iterator[dict[str, Any]]:
        with managed_operation_guard(self.root, _AGGREGATE_LOCK):
            value = self._load()
            yield value
            self._path()
            self.state.set(_KEY, value)
            self._path()

    def _read(self) -> dict[str, Any]:
        return _copy(self._load())

    def record_install(self, values: dict[str, Any]) -> None:
        item = _copy(values)
        skill_id = item.get("skill_id")
        if (
            not isinstance(skill_id, str)
            or not skill_id
            or not isinstance(item.get("baseline"), dict)
        ):
            raise ValueError("managed installation is invalid")
        item["owner"] = _owner(item.get("owner"))
        with self._change() as value:
            value["installations"][skill_id] = item

    def installations(self, owner: dict[str, str] | None = None) -> list[dict[str, Any]]:
        expected = _owner(owner, optional=True)
        return sorted(
            (_copy(row) for row in self._read()["installations"].values()
             if expected is None or row.get("owner") == expected),
            key=lambda row: str(row.get("slug") or ""),
        )

    def installation(
        self, skill_id: str, owner: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        expected = _owner(owner, optional=True)
        row = self._read()["installations"].get(skill_id)
        return _copy(row) if row is not None and (
            expected is None or row.get("owner") == expected
        ) else None

    def deactivate_install(
        self, skill_id: str, result: dict[str, Any] | None = None
    ) -> None:
        with self._change() as value:
            row = value["installations"].get(skill_id)
            if row is not None:
                row["state"] = "inactive"
                if result is not None:
                    row["uninstall_result"] = _copy(result)

    def save_review(self, key: str, record: dict[str, Any]) -> None:
        with self._change() as value:
            if key in value["reviews"]:
                raise WisdomConflict("Install review already exists")
            value["reviews"][key] = _copy(record)

    def review(self, key: str) -> dict[str, Any] | None:
        return _copy(self._read()["reviews"].get(key))

    def bind_review(self, key: str, operation_id: str, *, kind: str) -> None:
        with self._change() as value:
            review = value["reviews"].get(key)
            operation = value["operations"].get(operation_id)
            payload = operation.get("payload") if isinstance(operation, dict) else None
            receipt = key.split(":", 1)[-1]
            if (
                not isinstance(review, dict) or not isinstance(operation, dict)
                or operation.get("kind") != kind or not isinstance(payload, dict)
                or payload.get("owner") != review.get("owner")
                or payload.get("receipt") != receipt
                or review.get("operation_id") not in (None, operation_id)
            ):
                raise WisdomConflict("Install operation does not match its review")
            review["operation_id"] = operation_id

    def complete_review(self, key: str, result: dict[str, Any], *, kind: str) -> None:
        with self._change() as value:
            review = value["reviews"].get(key)
            operation = value["operations"].get(review.get("operation_id")) \
                if isinstance(review, dict) else None
            if not isinstance(operation, dict) or operation.get("kind") != kind:
                raise WisdomConflict("Install operation does not match its review")
            if review.get("result") is not None and review["result"] != result:
                raise WisdomConflict("Install receipt already has a different outcome")
            review["result"] = _copy(result)

    def begin(
        self, kind: str, skill_id: str, payload: dict[str, Any], *, stage: str = "prepared"
    ) -> str:
        """Persist a replacement-created operation before its first filesystem effect."""

        with self._change() as value:
            pending = [row for row in value["operations"].values()
                       if row.get("skill_id") == skill_id and row.get("status") == "pending"]
            if pending:
                same = pending[0]
                if (
                    same.get("kind") == kind
                    and same.get("payload", {}).get("receipt") == payload.get("receipt")
                ):
                    return str(same["id"])
                raise WisdomConflict("Another managed operation is pending for this skill")
            operation_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            value["operations"][operation_id] = {
                "id": operation_id, "kind": kind, "skill_id": skill_id,
                "stage": stage, "status": "pending", "payload": _copy(payload),
                "created_at": now, "updated_at": now,
            }
            return operation_id

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        return _copy(self._read()["operations"].get(operation_id))

    def pending_operations(self, skill_id: str | None = None) -> list[dict[str, Any]]:
        return sorted(
            (_copy(row) for row in self._read()["operations"].values()
             if row.get("status") == "pending" and (
                 skill_id is None or row.get("skill_id") == skill_id
             )),
            key=lambda row: row["created_at"],
        )

    def checkpoint(
        self, operation_id: str, payload: dict[str, Any] | None = None, *,
        stage: str | None = None,
    ) -> None:
        with self._change() as value:
            row = value["operations"].get(operation_id)
            if not isinstance(row, dict) or row.get("status") != "pending":
                raise WisdomConflict("Managed operation is unavailable")
            if stage is not None:
                row["stage"] = stage
            if payload is not None:
                row["payload"] = _copy(payload)
            row["updated_at"] = datetime.now(timezone.utc).isoformat()

    def finish(self, operation_id: str, result: dict[str, Any], *, stage: str) -> None:
        with self._change() as value:
            row = value["operations"].get(operation_id)
            if not isinstance(row, dict):
                raise WisdomConflict("Managed operation is unavailable")
            existing = row.get("result")
            if existing is not None and existing != result:
                raise WisdomConflict("Managed operation already has a different outcome")
            row.update({
                "stage": stage, "status": "done", "result": _copy(result),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def automatic_cursor(self, owner: dict[str, str]) -> str | None:
        expected = _owner(owner)
        cursor = self._read().get("automatic_cursor")
        if isinstance(cursor, dict) and cursor.get("owner") == expected:
            return str(cursor["after"])
        return None

    def set_automatic_cursor(self, owner: dict[str, str], after: str) -> None:
        with self._change() as value:
            value["automatic_cursor"] = {"owner": _owner(owner), "after": after}

    def completed_automatic_updates(self, owner: dict[str, str]) -> list[dict[str, Any]]:
        expected = _owner(owner)
        return [
            _copy(row) for row in self._read()["operations"].values()
            if row.get("kind") == "update" and row.get("status") == "done"
            and row.get("stage") == "complete"
            and row.get("payload", {}).get("owner") == expected
            and row.get("payload", {}).get("authorization", {}).get("kind") == "automatic_policy"
            and row.get("result", {}).get("updated") is True
        ]
