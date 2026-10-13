"""Rank the owner's own local skills by Hermes-recorded usage.

Usage counts come from Hermes core ``tools.skill_usage`` (the per-profile
``skills/.usage.json`` sidecar). That module is a core internal, not a
PluginContext API, so it is imported lazily behind a capability probe: a Hermes
without the expected functions yields an ``unavailable`` state instead of an
error. Reads are bound to the plugin's own profile home and never write.
"""

from __future__ import annotations

import contextlib
import logging
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
ERROR_TEXT = (
    "Couldn't read your local skill usage. Check the Hermes logs and try again."
)
EMPTY_TEXT = (
    "No recorded usage for your own local skills yet. "
    "Use your skills in Hermes and try again."
)
# ``_iter_skill_mds`` and ``_find_skill_dir`` are private but already the
# resolver ``/wisdom share`` uses (qualification.py); both exist from 0.21.5.
_REQUIRED_API = ("load_usage", "is_bundled", "is_hub_installed", "_iter_skill_mds",
                 "_find_skill_dir")
_PUBLISHER_KIND = "publisher_source"
_FRONTMATTER_CHARS = 4000
_DESCRIPTION_LIMIT = 200
# The owner's own interactive sessions. Webhook, API-server, Kanban and other
# non-messaging routes can deliver replies to shared destinations, so they are
# refused like any non-private chat.
_OWNER_LOCAL_SURFACES = frozenset({"", "cli", "local", "tui", "desktop"})
# Same private chat types native admission accepts (native_context._consume).
_PRIVATE_CHAT_TYPES = frozenset({"dm", "private", "im"})
logger = logging.getLogger(__name__)


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


def _home_override() -> tuple[Callable[[str], Any], Callable[[Any], None]] | None:
    try:
        constants = import_module("hermes_constants")
        return (getattr(constants, "set_hermes_home_override"),
                getattr(constants, "reset_hermes_home_override"))
    except (ImportError, AttributeError):
        return None


@contextlib.contextmanager
def _profile_home(home: Path, override) -> Iterator[None]:
    """Bind core helpers to this profile without mutating process environment."""
    set_override, reset_override = override
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
    """Rank the owner's own local skills that have recorded uses or edits.

    Candidates come from the host's own local skill walk and must resolve through
    the same profile-fenced resolver ``/wisdom share`` uses, so every listed skill
    can be shared by that name.
    """
    api = (usage_api or load_usage_api)()
    override = _home_override()
    if api is None or override is None:
        return TopSkills("unavailable")
    from .qualification import _eligible_path  # pylint: disable=import-outside-toplevel

    home = Path(profile_home).expanduser().absolute()
    skills_root = home / "skills"
    team, shared = _managed_paths(installations)
    shared |= {_resolved(path) for path in published_paths if path}
    try:
        with _profile_home(home, override):
            usage = api.load_usage()
            if not isinstance(usage, dict):
                return TopSkills("unavailable")
            hidden = (set(_call(api, "list_archived_skill_names", ()))
                      | set(_call(api, "read_suppressed_names", ())))
            not_owned = _ownership_filter(api)
            items = []
            candidates = (
                sorted({name for name, _md in api._iter_skill_mds(  # pylint: disable=protected-access
                    skills_root, local_only=True)}) if skills_root.is_dir() else []
            )
            resolver = _skill_resolver(api, skills_root)
            for name in candidates:
                record = usage.get(name)
                if name in hidden or not isinstance(record, dict) or not_owned(name, record):
                    continue
                uses, edits = _count(record.get("use_count")), _count(record.get("patch_count"))
                if not uses and not edits:
                    continue
                path = _eligible_path(name, skills_root=skills_root, skill_resolver=resolver)
                if path is None or _team_installed(team, name, path):
                    continue
                items.append(TopSkill(
                    name=name, description=_description(path / "SKILL.md"),
                    uses=uses, edits=edits, last_used=_iso(record.get("last_used_at")),
                    shared=str(path) in shared,
                ))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Collective Wisdom could not read skill usage (%s)", type(exc).__name__)
        logger.debug("Collective Wisdom skill usage failure", exc_info=True)
        return TopSkills("error")
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
        "platform": value("HERMES_SESSION_PLATFORM") or os.environ.get("HERMES_PLATFORM", ""),
        "source": value("HERMES_SESSION_SOURCE"),
        "chat_type": value("HERMES_SESSION_CHAT_TYPE"),
        "chat_id": value("HERMES_SESSION_CHAT_ID"),
        "thread_id": value("HERMES_SESSION_THREAD_ID"),
        "user_id": value("HERMES_SESSION_USER_ID"),
        "session_key": value("HERMES_SESSION_KEY"),
    }


def is_messaging(surface: dict[str, str]) -> bool:
    """Whether the session is anything but the owner's own local session (default deny)."""
    return any(
        ident not in _OWNER_LOCAL_SURFACES
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


def _description(skill_md: Path) -> str:
    """The SKILL.md frontmatter description, single-line and bounded; empty if absent."""
    try:
        with skill_md.open(encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read(_FRONTMATTER_CHARS)
    except OSError:
        return ""
    lines = [line.rstrip() for line in text.split("\n")]
    if "---" not in (line.strip() for line in lines):
        return ""
    start = next(index for index, line in enumerate(lines) if line.strip() == "---")
    for line in lines[start + 1:]:
        if line.strip() == "---":
            break
        key, separator, raw = line.partition(":")
        if separator and key == "description":
            return clean_text(raw.strip().strip("\"'"))
    return ""


def _team_installed(team: dict[str, set[str]], name: str, path: Path) -> bool:
    return str(path) in team["paths"] or name in team["names"]


def _ownership_filter(api: Any) -> Callable[[str, dict[str, Any]], bool]:
    """Archived, bundled, Hub-installed or protected built-in skills are not the owner's.

    The bundled and Hub name sets are read once per ranking when the host exposes
    them; ``is_bundled``/``is_hub_installed`` re-read both manifests per name.
    """
    try:
        names = (set(api._read_bundled_names())  # pylint: disable=protected-access
                 | set(api._read_hub_installed_names()))  # pylint: disable=protected-access
    except (AttributeError, TypeError):
        names = None

    def listed(name: str) -> bool:
        if names is not None:
            return name in names
        return bool(api.is_bundled(name) or api.is_hub_installed(name))

    def not_owned(name: str, record: dict[str, Any]) -> bool:
        if record.get("state") == "archived" or record.get("archived_at"):
            return True
        return bool(_call(api, "is_protected_builtin", False, name) or listed(name))

    return not_owned


def _skill_resolver(api: Any, skills_root: Path) -> Callable[[str], Path | None]:
    """First-match name → dir, built in one walk with the host's own ``_find_skill_dir`` parts.

    ``_find_skill_dir`` walks the tree once per name. When the host exposes the
    pieces it composes (``iter_skill_index_files``, ``_read_skill_name``,
    ``is_external_skill_path``), build the same first-match map once instead.
    """
    try:
        iterate = import_module("agent.skill_utils").iter_skill_index_files
        read_name = api._read_skill_name  # pylint: disable=protected-access
        external = api.is_external_skill_path
    except (ImportError, AttributeError):
        return api._find_skill_dir  # pylint: disable=protected-access
    found: dict[str, Path] = {}
    if skills_root.exists():
        for skill_md in iterate(skills_root, "SKILL.md"):
            if not external(skill_md):
                found.setdefault(read_name(skill_md, fallback=skill_md.parent.name),
                                 skill_md.parent)
    return found.get


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
