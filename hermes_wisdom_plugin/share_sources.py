"""One read-only source catalog for sharing, rankings and suggestions.

Candidates follow Hermes core (``agent.skill_utils``, ``tools.skills_tool``):
recursive discovery that prunes dependency, cache and package-support folders,
declared names read with the core frontmatter parser and its malformed-YAML
fallback, cross-tier precedence, and identical same-root copies resolving to
the strictly shallower copy. Hosts that expose those helpers decide with them;
the ports below keep the same rules on hosts without them. Sharing adds its own
safety on top: no symlinked root, folder or SKILL.md, every source inside its
root, and never the root itself.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import WisdomNotFound
from .package import GENERATED_SKILL_DIRS, SkillRefusal

# Core EXCLUDED_SKILL_DIRS: the hub-shared generated set plus the distribution swap dir.
EXCLUDED_SKILL_DIRS = GENERATED_SKILL_DIRS | {".hermes-dist"}
# Core SKILL_SUPPORT_DIRS: progressive-disclosure folders inside a skill package.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))
# Team installs live under these top-level folders of a skills root (install_execution,
# managed_target). They are never the owner's own skill to share (qualification agrees).
MANAGED_SKILL_ROOTS = frozenset(("_org", "_wisdom"))
_FRONTMATTER_END = re.compile(r"\n---\s*\n")


def _host_utility() -> Any:
    try:
        return import_module("agent.skill_utils")
    except ImportError:
        return None


def hermes_search_roots(local: Path) -> list[tuple[int, Path]]:
    """Use the host's non-project precedence, with profile-only legacy fallback."""
    utility = _host_utility()
    discover = getattr(utility, "get_skill_search_roots", None)
    return discover(local, include_project=False) if callable(discover) else [(1, local)]


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Port of core ``parse_frontmatter``: YAML first, key/value lines on malformed YAML."""
    content = content.removeprefix("\ufeff")
    end = _FRONTMATTER_END.search(content[3:]) if content.startswith("---") else None
    if end is None:
        return {}
    block = content[3:end.start() + 3]
    try:
        from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel

        parsed = YAML(typ="safe").load(block)
    except Exception:  # pylint: disable=broad-exception-caught
        fallback: dict[str, Any] = {}
        for line in block.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                fallback[key.strip()] = value.strip()
        return fallback
    return parsed if isinstance(parsed, dict) else {}


def skill_candidate_rank(skill_md: Path, root: Path) -> tuple[bool, int]:
    """Port of core ``skill_candidate_rank``: a SKILL.md first, then the shallower path."""
    return (skill_md.name != "SKILL.md", len(skill_md.relative_to(root).parts))


def provably_same_skill(skill_mds: list[Path]) -> bool:
    """Port of core ``provably_same_skill``: one resolved file or byte-identical copies."""
    try:
        if len({os.path.realpath(path) for path in skill_mds}) == 1:
            return True
        return len({hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in skill_mds}) == 1
    except OSError:
        return False


def pick_skill_candidate(candidates: list[tuple[Any, ...]]) -> tuple[int | None, list[int]]:
    """Port of core ``pick_skill_candidate`` over ``(tier, root, rank, skill_md)`` rows."""
    top = min(candidate[0] for candidate in candidates)
    contenders = [index for index, candidate in enumerate(candidates) if candidate[0] == top]
    if (len(contenders) > 1 and len({candidates[index][1] for index in contenders}) == 1
            and provably_same_skill([candidates[index][3] for index in contenders])):
        ranked = sorted(contenders, key=lambda index: candidates[index][2])
        if candidates[ranked[0]][2] != candidates[ranked[1]][2]:
            return ranked[0], contenders
    return (contenders[0] if len(contenders) == 1 else None), contenders


def _first_of(host_parse: Callable[[str], Any]) -> Callable[[str], Any]:
    """Core ``parse_frontmatter`` returns ``(frontmatter, body)``; only the mapping is needed."""
    return lambda text: host_parse(text)[0]


@dataclass(frozen=True, slots=True)
class _Core:
    """Core candidate rules, taken from the host when it exposes them."""

    excluded: frozenset[str]
    support: frozenset[str]
    parse: Callable[[str], Any]
    rank: Callable[[Path, Path], Any]
    pick: Callable[[list[tuple[Any, ...]]], tuple[int | None, list[int]]]

    @classmethod
    def load(cls) -> "_Core":
        """Bind each rule to the host's helper when it exists, otherwise to its port."""
        utility = _host_utility()

        def helper(name: str, port: Any) -> Any:
            """The host's *name* when it exposes one, else the local *port*."""
            found = getattr(utility, name, None)
            return found if found is not None else port

        host_parse = helper("parse_frontmatter", None)
        return cls(
            excluded=frozenset(helper("EXCLUDED_SKILL_DIRS", EXCLUDED_SKILL_DIRS)),
            support=frozenset(helper("SKILL_SUPPORT_DIRS", SKILL_SUPPORT_DIRS)),
            parse=_first_of(host_parse) if callable(host_parse) else parse_frontmatter,
            rank=helper("skill_candidate_rank", skill_candidate_rank),
            pick=helper("pick_skill_candidate", pick_skill_candidate),
        )

    def declared_name(self, skill_md: Path) -> str | None:
        """The frontmatter ``name`` as core reads it, or None when unreadable or absent."""
        try:
            frontmatter = self.parse(skill_md.read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None


def _safe_sources(root: Path, core: _Core) -> Iterator[tuple[Path, Path]]:
    """Core index traversal under *root*, keeping only folders safe to share.

    Yields ``(resolved skill dir, SKILL.md)``. Like ``iter_skill_index_files``,
    excluded folders are pruned everywhere and support folders only inside a
    folder that has its own SKILL.md; managed team-install roots are pruned at
    the top level. Symlinked components are pruned instead of followed, and
    every kept folder must resolve inside the resolved root.
    """
    if root.is_symlink() or not root.is_dir():
        return
    real_root = root.resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        has_skill_md = "SKILL.md" in files
        dirs[:] = sorted(
            name for name in dirs
            if name not in core.excluded and not (has_skill_md and name in core.support)
            and not (here == root and name in MANAGED_SKILL_ROOTS)
            and not (here / name).is_symlink())
        if not has_skill_md or here == root:
            continue
        skill_md = here / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            continue
        resolved = here.resolve()
        if resolved != real_root and resolved.is_relative_to(real_root):
            yield resolved, resolved / "SKILL.md"


class LocalSkillNotFound(WisdomNotFound):
    """Plugin-authored source lookup failure, safe to show unlike remote 404s."""

    def __init__(self, name: str, roots: list[tuple[int, Path]]) -> None:
        searched = ", ".join(str(root) for _tier, root in roots)
        super().__init__(
            f"`{name}` isn't a local skill on this profile. Hermes searches: {searched}."
        )


def _share_name_ok(name: Any) -> bool:
    return (isinstance(name, str) and bool(name) and name == name.strip()
            and "/" not in name and "\\" not in name and name not in {".", ".."})


class ShareSources:
    """Resolve share names with core precedence, refusing same-tier ambiguity."""

    def __init__(self, host: Any) -> None:
        local = Path(host.skills_root)
        discover = getattr(host, "skill_search_roots", None)
        self.roots = discover() if callable(discover) else [(1, local)]
        self._core = core = _Core.load()
        # alias -> (tier, resolved root, rank, SKILL.md, skill dir), one row per source.
        self._names: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        self._canonical: set[str] = set()
        for tier, root in self.roots:
            real_root = Path(root).resolve()
            for source, skill_md in _safe_sources(Path(root), core):
                declared = core.declared_name(skill_md)
                canonical = declared if declared is not None and _share_name_ok(declared) \
                    else source.name
                self._canonical.add(canonical)
                row = (tier, str(real_root), core.rank(skill_md, real_root), skill_md, source)
                for alias in (source.name, declared):
                    if alias is not None and row not in self._names[alias]:
                        self._names[alias].append(row)

    def names(self) -> list[str]:
        """Canonical names, including ambiguous ones which resolve() will refuse."""
        return sorted(self._canonical)

    def resolve(self, name: str) -> Path:
        """The one source core would load for *name*, or the same refusal core gives."""
        if not _share_name_ok(name):
            raise LocalSkillNotFound(name, self.roots)
        candidates = self._names.get(name, [])
        if not candidates:
            raise LocalSkillNotFound(name, self.roots)
        won, contenders = self._core.pick([row[:4] for row in candidates])
        if won is None:
            paths = sorted({str(candidates[index][4]) for index in contenders})
            raise SkillRefusal(f"Ambiguous skill name {name!r}: " + ", ".join(paths))
        return candidates[won][4]
