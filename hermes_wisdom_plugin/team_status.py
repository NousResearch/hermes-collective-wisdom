"""One status model for installed team skills, shared by Desktop, CLI and the session note.

Every installed team skill is in exactly one state: ``up_to_date``, ``updated``,
``needs_look`` (reason ``edited`` | ``flagged`` | ``cant_combine``) or
``no_longer_shared``. Receipts, hashes and journals stay underneath; this module
only projects the small owner-bound facts recorded by automatic maintenance,
Combine and explicit updates (``local_state.TeamUpdateState``).
"""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

StatusKind = Literal["up_to_date", "updated", "needs_look", "no_longer_shared"]
NeedsLookReason = Literal["edited", "flagged", "cant_combine"]

_AUTO_MODES = frozenset({"AUTO_WITH_NOTICE", "REQUIRED"})
_LOCAL_NOTE_PLATFORMS = frozenset({"", "cli", "tui", "desktop"})


@dataclass(frozen=True)
class SkillStatus:  # pylint: disable=too-many-instance-attributes
    """One installed team skill as the user sees it."""

    skill_id: str
    slug: str
    kind: StatusKind
    reason: NeedsLookReason | None
    installed_version: int | None
    latest_version: int | None
    new_capabilities: tuple[str, ...]   # non-empty only for kind="updated" when access expanded, or for reason="flagged"
    summary: str | None                 # one-line Combine summary when reason="edited"
    auto_update: bool
    updated_at: str | None              # ISO time of last local apply
    seen: bool                          # False until the user views an "updated" item

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection (tuples become lists)."""
        value = asdict(self)
        value["new_capabilities"] = list(self.new_capabilities)
        return value


@contextmanager
def service_scope(runtime: Any) -> Iterator[Any]:
    """Accept an ``InvocationService`` or a ``CommandRuntime`` (opens one service)."""

    opener = getattr(runtime, "_service", None)
    closer = getattr(runtime, "_close_service", None)
    if callable(opener) and callable(closer) and not hasattr(runtime, "_installation_owner"):
        service = opener()
        try:
            yield service
        finally:
            closer(service)
        return
    yield runtime


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _team_installations(service: Any, owner: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in service.installed.installations(owner=owner):
        provenance = row.get("target_provenance")
        if row.get("state") != "active" or _positive(row.get("version")) is None:
            continue
        # Your own shared skills are listed as "Shared", not as installed team skills.
        if isinstance(provenance, dict) and provenance.get("kind") == "publisher_source":
            continue
        rows.append(row)
    return rows


def _remote_facts(service: Any) -> dict[str, dict[str, Any]]:
    """Best-effort Gateway facts (latest version, skill state); empty when offline."""
    try:
        owner = service._installation_owner()  # pylint: disable=protected-access
        rows = service.client.installations(owner["installation_id"])
    except Exception:  # pylint: disable=broad-exception-caught
        return {}
    return {
        str(row.get("skill_id")): row for row in rows
        if isinstance(row, dict) and row.get("skill_id")
    }


def _status_for(  # pylint: disable=too-many-locals
    installation: dict[str, Any], record: dict[str, Any], remote: dict[str, Any] | None,
) -> SkillStatus:
    skill_id = str(installation["skill_id"])
    installed = _positive(installation.get("version"))
    latest = _positive((remote or {}).get("latest_version")) or _positive(record.get("version"))
    if latest is not None and installed is not None and latest < installed:
        latest = installed
    mode = (remote or {}).get("update_mode") or installation.get("update_mode")
    kind = record.get("kind")
    reason = record.get("reason")
    skill_state = (remote or {}).get("skill_state")
    if skill_state in {"archived", "taken_down"} or kind == "no_longer_shared":
        kind, reason = "no_longer_shared", None
    elif kind == "needs_look":
        version = _positive(record.get("version"))
        # Resolved elsewhere (manual update, Keep mine) or superseded by an apply.
        if version is None or (installed is not None and version <= installed) or (
            record.get("skipped_version") == version
        ):
            kind, reason = "up_to_date", None
    elif kind == "updated":
        if _positive(record.get("version")) != installed:
            kind = "up_to_date"
    else:
        kind = "up_to_date"
    if kind not in {"needs_look"}:
        reason = None
    elif reason not in {"edited", "flagged", "cant_combine"}:
        reason = "flagged"
    capabilities = tuple(
        str(item) for item in (record.get("new_capabilities") or []) if isinstance(item, str)
    ) if kind == "updated" or reason == "flagged" else ()
    raw_prepared = record.get("prepared")
    prepared: dict[str, Any] = raw_prepared if isinstance(raw_prepared, dict) else {}
    return SkillStatus(
        skill_id=skill_id,
        slug=str(installation.get("slug") or record.get("slug") or skill_id),
        kind=kind,
        reason=reason,
        installed_version=installed,
        latest_version=latest,
        new_capabilities=capabilities[:8],
        summary=(str(record.get("summary") or prepared.get("summary") or "") or None)
        if reason == "edited" else None,
        auto_update=mode in _AUTO_MODES,
        updated_at=str(record["updated_at"]) if record.get("updated_at") else None,
        seen=bool(record.get("seen", True)) if kind == "updated" else True,
    )


def team_statuses(runtime: Any) -> list[SkillStatus]:
    """Return one status per active installed team skill for the bound owner."""

    with service_scope(runtime) as service:
        owner = service._installation_owner()  # pylint: disable=protected-access
        records = service.team_state.records(owner)
        remote = _remote_facts(service)
        statuses = [
            _status_for(row, records.get(str(row["skill_id"])) or {}, remote.get(str(row["skill_id"])))
            for row in _team_installations(service, owner)
        ]
        order = {"needs_look": 0, "updated": 1, "no_longer_shared": 2, "up_to_date": 3}
        return sorted(statuses, key=lambda item: (order[item.kind], item.slug))


def local_owner(service: Any) -> dict[str, Any] | None:
    """The enrolled owner binding from local state only (no credential read)."""
    record = service._local_identity()  # pylint: disable=protected-access
    if not isinstance(record, dict) or record.get("state") != "active":
        return None
    owner = record.get("owner")
    if not isinstance(owner, dict) or not record.get("installation_id"):
        return None
    return {**owner, "installation_id": str(record["installation_id"])}


def local_statuses(service: Any, owner: dict[str, Any] | None = None) -> list[SkillStatus]:
    """Offline projection used by the session note (no Gateway or credential read)."""

    owner = owner or local_owner(service)
    if owner is None:
        return []
    records = service.team_state.records(owner)
    return [
        _status_for(row, records.get(str(row["skill_id"])) or {}, None)
        for row in _team_installations(service, owner)
    ]


def mark_seen(runtime: Any, skill_id: str) -> None:
    """Clear the "Updated" badge once the user has viewed the item."""

    with service_scope(runtime) as service:
        owner = service._installation_owner()  # pylint: disable=protected-access
        service.team_state.mark_seen(owner, skill_id)


def reason_sentence(status: SkillStatus) -> str:
    """One plain sentence for a needs-a-look or no-longer-shared item."""

    if status.kind == "no_longer_shared":
        return "No longer shared. It keeps working but stops updating."
    if status.kind == "updated":
        return updated_phrase(status)
    if status.reason == "edited":
        lead = "You edited this skill."
        return f"{lead} {status.summary}" if status.summary else lead
    if status.reason == "cant_combine":
        return "You edited this skill and Hermes can't combine automatically."
    if status.reason == "flagged":
        if status.new_capabilities:
            return ("The new version asks for more access ("
                    + ", ".join(status.new_capabilities)
                    + ") and the security scan flagged it.")
        return "The new version needs a look before it applies."
    return "Up to date."


def updated_phrase(status: SkillStatus) -> str:
    """'Updated' or 'Updated — now runs scripts' when access expanded."""
    if status.new_capabilities:
        return "Updated — now " + ", ".join(status.new_capabilities)
    return "Updated"


def updated_ago(value: object, *, now: datetime | None = None) -> str | None:
    """'Updated today' / 'Updated 2 days ago' from an ISO time; ``None`` if unknown."""
    if not isinstance(value, str) or not value:
        return None
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = max(0, ((now or datetime.now(timezone.utc)) - then).days)
    if days == 0:
        return "Updated today"
    if days == 1:
        return "Updated yesterday"
    if days < 60:
        return f"Updated {days} days ago"
    return f"Updated {days // 30} months ago"


def update_command(status: SkillStatus) -> str:
    """The exact terminal command that resolves a needs-a-look item."""
    return "hermes wisdom update " + shlex.quote(status.slug)


def session_note(runtime: Any, *, consume: bool = False) -> str | None:
    """One line when something updated or needs a look, else ``None``.

    Local-only (no Gateway request). ``consume=True`` (used by the session-start
    hook, which freezes the line for that session) marks the updated items as
    noted so the same update is announced once.
    """

    try:
        with service_scope(runtime) as service:
            owner = local_owner(service)
            if owner is None:
                return None
            statuses = local_statuses(service, owner)
            records = service.team_state.records(owner)
            fresh = [
                item for item in statuses
                if item.kind == "updated" and not records.get(item.skill_id, {}).get("noted")
            ]
            needs = [item for item in statuses if item.kind == "needs_look"]
            if not fresh and not needs:
                return None
            parts: list[str] = []
            if fresh:
                named = []
                for item in fresh[:3]:
                    if item.new_capabilities:
                        named.append(
                            f"{item.slug} was updated — now " + ", ".join(item.new_capabilities)
                        )
                    else:
                        named.append(f"{item.slug} was updated")
                extra = len(fresh) - len(named)
                parts.append("; ".join(named) + (f" (+{extra} more)" if extra > 0 else "") + ".")
            if needs:
                count = len(needs)
                noun = "skill needs" if count == 1 else "skills need"
                parts.append(f"{count} {noun} a look (`hermes wisdom status`).")
            if consume and fresh:
                service.team_state.mark_noted(owner, [item.skill_id for item in fresh])
            return "Collective Wisdom: " + " ".join(parts)
    except Exception:  # pylint: disable=broad-exception-caught
        return None  # Signed out, not enrolled or offline: no note.


def note_platform_allowed(platform: object) -> bool:
    """Only local, private surfaces get the note; shared chats never see skill names."""
    return str(platform or "").casefold() in _LOCAL_NOTE_PLATFORMS


__all__ = [
    "NeedsLookReason", "SkillStatus", "StatusKind", "mark_seen", "reason_sentence",
    "service_scope", "session_note", "team_statuses", "update_command", "updated_ago",
    "updated_phrase",
]
