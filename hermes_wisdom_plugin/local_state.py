"""Owner-local qualification records backed by the injected PluginState."""
# State verbs are intentionally self-describing.
# pylint: disable=missing-function-docstring

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .setup_lock import managed_operation_guard

_KEY = "wisdom-qualification"
_LOCK = "\0wisdom-qualification"


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "owner": None,
        "skills": {},
        "usage": {},
        "refinements": [],
        "stability": {},
        "candidates": {},
        "inactive": [],
    }


class QualificationState:  # pylint: disable=too-many-public-methods
    """Small qualification-domain record; it owns no network or delivery state."""

    # These small public-state guards intentionally repeat across independent domains.
    # pylint: disable=duplicate-code
    def __init__(self, state: Any, root: Path) -> None:
        if state is None or not callable(getattr(state, "get", None)) or not callable(
            getattr(state, "set", None)
        ):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root).resolve(strict=False)
    # pylint: enable=duplicate-code

    def _path(self) -> None:
        if Path(self.state.path).resolve(strict=False).parent != self.root:
            raise RuntimeError("Collective Wisdom profile root changed during state operation")

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_KEY)
        self._path()
        if value is None:
            return _empty()
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("Collective Wisdom qualification state is invalid")
        value.setdefault("inactive", [])
        if not isinstance(value["inactive"], list):
            raise RuntimeError("Collective Wisdom qualification state is invalid")
        return value

    # pylint: disable=duplicate-code
    @contextmanager
    def change(self) -> Iterator[dict[str, Any]]:
        with managed_operation_guard(self.root, _LOCK, wait=True):
            value = self._load()
            yield value
            self._path()
            self.state.set(_KEY, value)
            self._path()
    # pylint: enable=duplicate-code

    def owner(self) -> dict[str, str] | None:
        return copy.deepcopy(self._load()["owner"])

    def bind_owner(self, owner: dict[str, str]) -> None:
        if not owner.get("organization_id") or not owner.get("account_id"):
            raise ValueError("qualification owner is incomplete")
        with self.change() as value:
            if value["owner"] != owner:
                fresh = _empty()
                fresh["owner"] = copy.deepcopy(owner)
                fresh["inactive"] = copy.deepcopy(value["inactive"])
                if value["owner"] is not None:
                    fresh["inactive"].append({
                        key: copy.deepcopy(value[key])
                        for key in (
                            "owner", "skills", "usage", "refinements", "stability", "candidates"
                        )
                    })
                value.clear()
                value.update(fresh)

    def register_skill(
        self, path: Path, *, content_hash: str | None, source_kind: str,
        tree: dict[str, Any] | None = None, snapshot_text: str | None = None,
    ) -> str:
        canonical = str(Path(path).resolve())
        identity = hashlib.sha256(canonical.encode()).hexdigest()
        with self.change() as value:
            row = value["skills"].setdefault(identity, {
                "id": identity, "canonical_path": canonical, "source_kind": source_kind,
                "current_hash": None, "snapshots": [], "deleted_at": None,
            })
            row.update({
                "canonical_path": canonical, "source_kind": source_kind, "deleted_at": None,
            })
            if content_hash is not None:
                row["current_hash"] = content_hash
                if not row["snapshots"] or row["snapshots"][-1]["content_hash"] != content_hash:
                    row["snapshots"].append({
                        "content_hash": content_hash,
                        "tree": copy.deepcopy(tree or {}),
                        "snapshot_text": snapshot_text,
                    })
            return identity

    def local_skill(self, skill_id: str) -> dict[str, Any] | None:
        row = self._load()["skills"].get(skill_id)
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def latest_snapshot(self, skill_id: str) -> dict[str, Any] | None:
        row = self.local_skill(skill_id)
        snapshots = row.get("snapshots") if row else None
        return copy.deepcopy(snapshots[-1]) if snapshots else None

    def record_usage_day(
        self, skill_id: str, day: str, *, timezone_name: str, retain_after: str
    ) -> None:
        with self.change() as value:
            usage = value["usage"].setdefault(skill_id, {})
            for key in list(usage):
                if key.partition("|")[2] < retain_after:
                    usage.pop(key)
            identity = f"{timezone_name}|{day}"
            usage[identity] = int(usage.get(identity, 0)) + 1

    def usage_days(self, skill_id: str, *, since: str, timezone_name: str) -> list[str]:
        prefix = timezone_name + "|"
        return sorted(
            key[len(prefix):] for key in self._load()["usage"].get(skill_id, {})
            if key.startswith(prefix) and key[len(prefix):] >= since
        )

    def usage_summary(
        self, skill_id: str, *, start: str, end: str, timezone_name: str
    ) -> tuple[int, int]:
        prefix = timezone_name + "|"
        counts = [
            count for key, count in self._load()["usage"].get(skill_id, {}).items()
            if key.startswith(prefix) and start <= key[len(prefix):] <= end
        ]
        return sum(counts), len(counts)

    def record_refinement(  # pylint: disable=too-many-arguments
        self, skill_id: str, *, from_hash: str, to_hash: str,
        classification: str, structural: dict[str, Any], at: datetime | None = None,
    ) -> None:
        observed = at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("refinement timestamp must include a timezone")
        with self.change() as value:
            value["refinements"].append({
                "skill_id": skill_id, "from_hash": from_hash, "to_hash": to_hash,
                "classification": classification, "structural": copy.deepcopy(structural),
                "at": observed.astimezone(timezone.utc).isoformat(),
            })

    def meaningful_refinement_count(self, skill_id: str, *, since: str) -> int:
        return sum(
            row["skill_id"] == skill_id and row["classification"] == "meaningful"
            and row["at"] >= since for row in self._load()["refinements"]
        )

    def schedule_stability(
        self, skill_id: str, content_hash: str, due_at: str,
        *, session_id: str | None, task_id: str | None,
    ) -> None:
        with self.change() as value:
            value["stability"][skill_id + ":" + content_hash] = {
                "skill_id": skill_id, "content_hash": content_hash, "due_at": due_at,
                "session_id": session_id, "task_id": task_id,
            }

    def due_stability_jobs(self, now: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row) for row in self._load()["stability"].values()
            if row["due_at"] <= now
        ]

    def finish_stability_job(self, skill_id: str, content_hash: str) -> None:
        with self.change() as value:
            value["stability"].pop(skill_id + ":" + content_hash, None)

    def resolve_ambiguous_refinement(self, *args: Any, **kwargs: Any) -> None:
        skill_id = str(args[0])
        self.record_refinement(
            skill_id,
            from_hash=str(kwargs["from_hash"]),
            to_hash=str(kwargs["to_hash"]),
            classification=str(kwargs["classification"]),
            structural=dict(kwargs["structural"]),
            at=kwargs.get("observed_at"),
        )
        if kwargs.get("due_at"):
            self.schedule_stability(
                skill_id, str(kwargs["to_hash"]), str(kwargs["due_at"]),
                session_id=kwargs.get("session_id"), task_id=kwargs.get("task_id"),
            )

    def emit_candidate(  # pylint: disable=too-many-arguments
        self, *, skill_id: str, content_hash: str, qualification: str,
        payload: dict[str, Any], session_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        owner = self.owner()
        if owner is None:
            raise ValueError("qualification owner is unavailable")
        identity = uuid.uuid4().hex
        key = skill_id + ":" + content_hash
        with self.change() as value:
            existing = value["candidates"].get(key)
            if existing:
                return str(existing["event_id"])
            value["candidates"][key] = {
                "event_id": identity,
                "kind": "wisdom.candidate",
                "organization_id": owner["organization_id"],
                "local_skill_id": skill_id,
                "skill_id": skill_id,
                "content_hash": content_hash,
                "qualification": qualification,
                "payload": copy.deepcopy(payload),
                "session_id": session_id,
                "task_id": task_id,
                "state": "unread",
            }
        return identity

    def candidates(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(row) for row in self._load()["candidates"].values()]

    def local_event(self, event_id: str) -> dict[str, Any] | None:
        return next((row for row in self.candidates() if row["event_id"] == event_id), None)

    def save_candidate_preparation(  # pylint: disable=too-many-boolean-expressions
        self, event_id: str, content_hash: str, prepared: dict[str, Any], *,
        previous: dict[str, Any] | None = None,
    ) -> None:
        """Bind first-offer facts to the exact owner and qualified source revision."""
        owner = self.owner()
        if owner is None:
            raise ValueError("qualification owner is unavailable")
        with self.change() as value:
            row = next((item for item in value["candidates"].values()
                        if item["event_id"] == event_id), None)
            skill = value["skills"].get(row["skill_id"]) if row else None
            if (value["owner"] != owner or row is None or row["content_hash"] != content_hash
                or row["organization_id"] != owner["organization_id"]
                or not skill or skill.get("current_hash") != content_hash
                or (row.get("payload") or {}).get("preparation") != previous
                or (previous is not None and prepared.get("draft_id") != previous.get("draft_id"))):
                raise ValueError("candidate changed during preparation")
            row["payload"]["preparation"] = copy.deepcopy(prepared)

    def candidate_editorial_metadata(self, skill_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.candidates() if row["skill_id"] == skill_id]
        return copy.deepcopy(rows[-1]["payload"]) if rows else None

    def mark_missing_skills(self, active_paths: set[str]) -> None:
        with self.change() as value:
            for row in value["skills"].values():
                if row["canonical_path"] not in active_paths:
                    row["deleted_at"] = datetime.now(timezone.utc).isoformat()


_TEAM_KEY = "wisdom-team-updates"
_TEAM_LOCK = "\0wisdom-team-updates"
_TEAM_FIELDS = (
    "slug", "kind", "reason", "version", "new_capabilities", "summary", "updated_at",
    "seen", "noted", "skipped_version", "prepared", "combine_attempt_tick",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def owner_key(owner: dict[str, Any]) -> str:
    """Stable, non-secret key for one exact installation owner binding."""
    return hashlib.sha256(
        json.dumps(owner, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


class TeamUpdateState:
    """Owner-bound team-skill status: updated / needs-a-look / prepared Combine.

    Receipts and journals stay in ``installed-core``; this record only holds the
    small facts the status model, session note and Combine choice need.
    """

    # pylint: disable=duplicate-code
    def __init__(self, state: Any, root: Path) -> None:
        if state is None or not callable(getattr(state, "get", None)) or not callable(
            getattr(state, "set", None)
        ):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root).resolve(strict=False)

    def _path(self) -> None:
        if Path(self.state.path).resolve(strict=False).parent != self.root:
            raise RuntimeError("Collective Wisdom profile root changed during state operation")

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_TEAM_KEY)
        self._path()
        if value is None:
            return {"version": 1, "owners": {}, "ticks": {}}
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("owners"), dict)):
            raise RuntimeError("Collective Wisdom team update state is invalid")
        value.setdefault("ticks", {})
        return value

    @contextmanager
    def change(self) -> Iterator[dict[str, Any]]:
        with managed_operation_guard(self.root, _TEAM_LOCK, wait=True):
            value = self._load()
            yield value
            self._path()
            self.state.set(_TEAM_KEY, value)
            self._path()
    # pylint: enable=duplicate-code

    @staticmethod
    def _skills(value: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
        bucket = value["owners"].setdefault(owner_key(owner), {"skills": {}})
        return bucket.setdefault("skills", {})

    def records(self, owner: dict[str, Any]) -> dict[str, dict[str, Any]]:
        bucket = self._load()["owners"].get(owner_key(owner)) or {}
        return copy.deepcopy(bucket.get("skills") or {})

    def record(self, owner: dict[str, Any], skill_id: str) -> dict[str, Any]:
        return self.records(owner).get(skill_id) or {}

    def update(self, owner: dict[str, Any], skill_id: str, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - set(_TEAM_FIELDS)
        if unknown:
            raise ValueError(f"unknown team update fields: {sorted(unknown)}")
        with self.change() as value:
            row = self._skills(value, owner).setdefault(skill_id, {})
            row.update(copy.deepcopy(changes))
            return copy.deepcopy(row)

    def set_needs_look(  # pylint: disable=too-many-arguments
        self, owner: dict[str, Any], skill_id: str, *, slug: str | None, reason: str,
        version: int, new_capabilities: list[str] | tuple[str, ...] = (),
        summary: str | None = None, prepared: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.record(owner, skill_id)
        # A newer version discards any prepared result built for an older one.
        keep_prepared = prepared if prepared is not None else (
            current.get("prepared")
            if (current.get("prepared") or {}).get("theirs_version") == version else None
        )
        return self.update(
            owner, skill_id, slug=slug, kind="needs_look", reason=reason, version=version,
            new_capabilities=list(new_capabilities)[:8], summary=summary,
            prepared=keep_prepared,
        )

    def set_updated(  # pylint: disable=too-many-arguments
        self, owner: dict[str, Any], skill_id: str, *, slug: str | None, version: int,
        new_capabilities: list[str] | tuple[str, ...] = (), seen: bool = False,
    ) -> dict[str, Any]:
        return self.update(
            owner, skill_id, slug=slug, kind="updated", reason=None, version=version,
            new_capabilities=list(new_capabilities)[:8], summary=None,
            updated_at=_now_iso(), seen=seen, noted=seen, prepared=None,
        )

    def set_skipped(self, owner: dict[str, Any], skill_id: str, version: int) -> dict[str, Any]:
        """Keep mine: skip exactly this version; the next one asks again."""
        return self.update(
            owner, skill_id, kind=None, reason=None, skipped_version=version,
            prepared=None, summary=None, new_capabilities=[],
        )

    def set_no_longer_shared(
        self, owner: dict[str, Any], skill_id: str, slug: str | None,
    ) -> dict[str, Any]:
        return self.update(
            owner, skill_id, slug=slug, kind="no_longer_shared", reason=None,
            prepared=None, summary=None, new_capabilities=[],
        )

    def clear_needs_look(self, owner: dict[str, Any], skill_id: str) -> None:
        with self.change() as value:
            row = self._skills(value, owner).get(skill_id)
            if isinstance(row, dict) and row.get("kind") in {"needs_look", "no_longer_shared"}:
                row.update(kind=None, reason=None, prepared=None, summary=None,
                           new_capabilities=[])

    def mark_seen(self, owner: dict[str, Any], skill_id: str) -> None:
        with self.change() as value:
            row = self._skills(value, owner).get(skill_id)
            if isinstance(row, dict):
                row["seen"] = True

    def mark_noted(self, owner: dict[str, Any], skill_ids: list[str]) -> None:
        with self.change() as value:
            skills = self._skills(value, owner)
            for skill_id in skill_ids:
                if isinstance(skills.get(skill_id), dict):
                    skills[skill_id]["noted"] = True

    def tick_due(self, profile: str, interval_seconds: float) -> bool:
        """Claim one maintenance tick for a profile; never more than once per interval."""
        now = datetime.now(timezone.utc)
        with self.change() as value:
            last = value["ticks"].get(profile)
            try:
                elapsed = (now - datetime.fromisoformat(str(last))).total_seconds()
            except (TypeError, ValueError):
                elapsed = None
            if elapsed is not None and 0 <= elapsed < interval_seconds:
                return False
            value["ticks"][profile] = now.isoformat()
            return True

    def current_tick(self, profile: str) -> str | None:
        tick = self._load()["ticks"].get(profile)
        return str(tick) if tick else None
