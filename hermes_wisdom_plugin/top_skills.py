"""Rank the owner's own local skills by Hermes-recorded usage.

Usage counts come from Hermes core ``tools.skill_usage`` (the per-profile
``skills/.usage.json`` sidecar). That module is a core internal, not a
PluginContext API, so it is imported lazily behind a capability probe: a Hermes
without the expected functions yields an ``unavailable`` state instead of an
error. Reads are bound to the plugin's own profile home and never write.
"""

from __future__ import annotations

import contextlib
import math
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 50
SUGGEST_LIMIT = 30
UNAVAILABLE_TEXT = (
    "Skill usage data is unavailable on this Hermes version. "
    "Update Hermes to see your top skills."
)
EMPTY_TEXT = (
    "No recorded usage for your own local skills yet. "
    "Use your skills in Hermes and try again."
)
_REQUIRED_API = ("load_usage", "is_bundled", "is_hub_installed")
_EXCLUDED_ROOTS = frozenset({"_org", "_wisdom", ".archive", ".hub"})
_PUBLISHER_KIND = "publisher_source"
_FRONTMATTER_CHARS = 4000
_DESCRIPTION_LIMIT = 200
# Mirrors gateway.session_context.NON_MESSAGING_SESSION_SURFACES; unknown
# identities count as messaging (default deny).
_LOCAL_SURFACES = frozenset({
    "", "api_server", "cli", "codex", "desktop", "gateway", "kanban", "local",
    "msgraph_webhook", "tool", "tui", "webhook",
})
_PRIVATE_CHAT_TYPES = frozenset({"dm", "private", "im", "direct"})


@dataclass(frozen=True, slots=True)
class TopSkill:
    """One eligible local skill with its recorded usage."""

    name: str
    description: str
    uses: int
    edits: int
    last_used: str | None
    shared: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON row."""
        return {
            "name": self.name, "uses": self.uses, "edits": self.edits,
            "last_used": self.last_used, "shared": self.shared,
        }


@dataclass(frozen=True, slots=True)
class TopSkills:
    """A ranked result or an explicit empty/unavailable state."""

    state: str
    items: tuple[TopSkill, ...] = ()

    def page(self, page: int = 1, per_page: int = DEFAULT_PER_PAGE) -> dict[str, Any]:
        """Return one bounded page; out-of-range pages are empty, never an error."""
        page, per_page = validate_paging(page, per_page)
        total = len(self.items)
        start = (page - 1) * per_page
        return {
            "state": self.state,
            "items": [item.as_dict() for item in self.items[start:start + per_page]],
            "page": page, "per_page": per_page, "total": total,
            "pages": math.ceil(total / per_page) if total else 0,
        }


def validate_paging(page: int, per_page: int) -> tuple[int, int]:
    """Reject pages below one and page sizes outside 1..MAX_PER_PAGE."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if (isinstance(per_page, bool) or not isinstance(per_page, int)
            or not 1 <= per_page <= MAX_PER_PAGE):
        raise ValueError(f"per-page must be between 1 and {MAX_PER_PAGE}")
    return page, per_page


def rank_key(skill: TopSkill) -> tuple[Any, ...]:
    """Uses desc, edits desc, last used desc (missing last), then name asc."""
    stamp = _timestamp(skill.last_used)
    return (-skill.uses, -skill.edits, stamp is None, -(stamp or 0.0), skill.name)


def load_usage_api() -> Any | None:
    """Probe Hermes core skill usage; ``None`` when this Hermes lacks it."""
    try:
        module = import_module("tools.skill_usage")
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if not all(callable(getattr(module, name, None)) for name in _REQUIRED_API):
        return None
    return module


@contextlib.contextmanager
def _profile_home(home: Path) -> Iterator[None]:
    """Bind core helpers to this profile without mutating process environment."""
    try:
        constants = import_module("hermes_constants")
        set_override = getattr(constants, "set_hermes_home_override")
        reset_override = getattr(constants, "reset_hermes_home_override")
    except (ImportError, AttributeError):
        yield
        return
    token = set_override(str(home))
    try:
        yield
    finally:
        reset_override(token)


def collect_top_skills(  # pylint: disable=too-many-locals
    profile_home: Path,
    *,
    installations: Iterable[dict[str, Any]] = (),
    published_paths: Iterable[str] = (),
    usage_api: Callable[[], Any | None] | None = None,
) -> TopSkills:
    """Rank the owner's own local skills that have recorded uses or edits."""
    api = (usage_api or load_usage_api)()
    if api is None:
        return TopSkills("unavailable")
    home = Path(profile_home).expanduser().absolute()
    team, shared = _managed_paths(installations)
    shared |= {_resolved(path) for path in published_paths if path}
    try:
        with _profile_home(home):
            usage = api.load_usage()
            if not isinstance(usage, dict):
                return TopSkills("unavailable")
            archived = set(_call(api, "list_archived_skill_names", ()))
            items = []
            seen: set[str] = set()
            for name, description, path in _local_skills(home / "skills"):
                record = usage.get(name)
                if (name in seen or name in archived or not isinstance(record, dict)
                        or _team_installed(team, name, path) or _not_owned(api, name, record)):
                    continue
                uses, edits = _count(record.get("use_count")), _count(record.get("patch_count"))
                if not uses and not edits:
                    continue
                seen.add(name)
                items.append(TopSkill(
                    name=name, description=description, uses=uses, edits=edits,
                    last_used=_iso(record.get("last_used_at")),
                    shared=str(path) in shared,
                ))
    except Exception:  # pylint: disable=broad-exception-caught
        return TopSkills("unavailable")
    items.sort(key=rank_key)
    return TopSkills("ok" if items else "empty", tuple(items))


def published_source_paths(direct_state: Any) -> list[str]:
    """Return local source paths of completed publications in the direct store."""
    if not isinstance(direct_state, dict):
        return []
    drafts = direct_state.get("drafts")
    drafts = drafts if isinstance(drafts, dict) else {}
    publications = direct_state.get("publications")
    paths = []
    for row in (publications.values() if isinstance(publications, dict) else ()):
        if (isinstance(row, dict) and row.get("status") == "done"
                and row.get("publication_state") == "published"):
            draft = drafts.get(row.get("local_draft_id"))
            if isinstance(draft, dict) and isinstance(draft.get("source_path"), str):
                paths.append(draft["source_path"])
    return paths


def session_surface() -> dict[str, str]:
    """Read the host-bound session identity; empty outside a Hermes session."""
    try:
        get_session_env = import_module("gateway.session_context").get_session_env
    except Exception:  # pylint: disable=broad-exception-caught
        get_session_env = None

    def value(name: str) -> str:
        if get_session_env is None:
            return ""
        try:
            return str(get_session_env(name, "") or "")
        except Exception:  # pylint: disable=broad-exception-caught
            return ""

    return {
        "platform": os.environ.get("HERMES_PLATFORM", "") or value("HERMES_SESSION_PLATFORM"),
        "source": value("HERMES_SESSION_SOURCE"),
        "chat_type": value("HERMES_SESSION_CHAT_TYPE"),
        "chat_id": value("HERMES_SESSION_CHAT_ID"),
        "thread_id": value("HERMES_SESSION_THREAD_ID"),
        "user_id": value("HERMES_SESSION_USER_ID"),
        "session_key": value("HERMES_SESSION_KEY"),
    }


def is_messaging(surface: dict[str, str]) -> bool:
    """Whether the bound session is a human messaging chat (default deny)."""
    return any(
        ident and ident not in _LOCAL_SURFACES
        for ident in (surface.get("platform", "").strip().casefold(),
                      surface.get("source", "").strip().casefold())
    )


def is_private_chat(chat_type: str) -> bool:
    """Whether a messaging chat type is a one-to-one conversation."""
    return chat_type.strip().casefold() in _PRIVATE_CHAT_TYPES


def clean_text(value: object, limit: int = _DESCRIPTION_LIMIT) -> str:
    """Single-line printable text bounded for chat and prompts."""
    text = " ".join(
        "".join(char for char in str(value or "") if unicodedata.category(char)[0] != "C"
                or char in " \t\n").split()
    )
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _managed_paths(installations: Iterable[dict[str, Any]]):
    team: dict[str, set[str]] = {"paths": set(), "names": set()}
    shared: set[str] = set()
    for row in installations or ():
        if not isinstance(row, dict) or row.get("state") != "active":
            continue
        target = row.get("target_path")
        provenance = row.get("target_provenance")
        if isinstance(provenance, dict) and provenance.get("kind") == _PUBLISHER_KIND:
            if isinstance(target, str) and target:
                shared.add(_resolved(target))
            continue
        if isinstance(target, str) and target:
            team["paths"].add(_resolved(target))
        if isinstance(row.get("slug"), str) and row["slug"]:
            team["names"].add(row["slug"])
    return team, shared


def _local_skills(root: Path) -> Iterator[tuple[str, str, Path]]:
    """Yield (name, description, path) for real skill dirs inside this profile."""
    if not root.is_dir() or root.is_symlink():
        return
    resolved_root = root.resolve()
    for skill_md in sorted(root.rglob("SKILL.md")):
        relative = skill_md.relative_to(root)
        parts = relative.parts[:-1]
        if not parts or parts[0] in _EXCLUDED_ROOTS or any(
            part.startswith(".") for part in parts
        ):
            continue
        if any(root.joinpath(*relative.parts[:index]).is_symlink()
               for index in range(1, len(relative.parts) + 1)):
            continue
        directory = skill_md.parent.resolve()
        try:
            directory.relative_to(resolved_root)
        except ValueError:
            continue
        name, description = _frontmatter(skill_md, fallback=directory.name)
        yield name, description, directory


def _frontmatter(skill_md: Path, *, fallback: str) -> tuple[str, str]:
    try:
        with skill_md.open(encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read(_FRONTMATTER_CHARS)
    except OSError:
        return fallback, ""
    lines = [line.rstrip() for line in text.split("\n")]
    if not lines or lines[0].strip() != "---":
        return fallback, ""
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, raw = line.partition(":")
        if separator and key.strip() in {"name", "description"} and not line[:1].isspace():
            values[key.strip()] = raw.strip().strip("\"'")
    return clean_text(values.get("name") or fallback, 128) or fallback, clean_text(
        values.get("description", "")
    )


def _team_installed(team: dict[str, set[str]], name: str, path: Path) -> bool:
    return str(path) in team["paths"] or name in team["names"]


def _not_owned(api: Any, name: str, record: dict[str, Any]) -> bool:
    """Archived, bundled, Hub-installed or protected built-in skills are not the owner's."""
    if record.get("state") == "archived" or record.get("archived_at"):
        return True
    return bool(_call(api, "is_protected_builtin", False, name)
                or api.is_bundled(name) or api.is_hub_installed(name))


def _resolved(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _call(api: Any, name: str, default: Any, *args: Any) -> Any:
    function = getattr(api, name, None)
    return function(*args) if callable(function) else default


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value: object) -> str | None:
    return value if isinstance(value, str) and _timestamp(value) is not None else None


def last_used_label(value: str | None) -> str:
    """Render a recorded timestamp as a date, or ``never``."""
    return value[:10] if value else "never"
