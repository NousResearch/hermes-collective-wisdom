"""One read-only source catalog for sharing, rankings and suggestions."""
from __future__ import annotations

from collections import defaultdict
from importlib import import_module
from pathlib import Path
from typing import Any

from .editorial import _overlay_frontmatter
from .errors import WisdomNotFound
from .package import SkillRefusal


def hermes_search_roots(local: Path) -> list[tuple[int, Path]]:
    """Use the host's non-project precedence, with profile-only legacy fallback."""
    try:
        utility = import_module("agent.skill_utils")
    except ImportError:
        return [(1, local)]
    discover = getattr(utility, "get_skill_search_roots", None)
    return discover(local, include_project=False) if callable(discover) else [(1, local)]


def _safe_sources(root: Path):
    """Flat and category/skill folders only, with no redirected path components."""
    if root.is_symlink() or not root.is_dir():
        return
    for pattern in ("*/SKILL.md", "*/*/SKILL.md"):
        for markdown in sorted(root.glob(pattern)):
            relative = markdown.relative_to(root)
            if any(root.joinpath(*relative.parts[:index]).is_symlink()
                   for index in range(1, len(relative.parts) + 1)):
                continue
            resolved = markdown.parent.resolve()
            if (markdown.is_file() and resolved != root.resolve()
                    and resolved.is_relative_to(root.resolve())):
                yield resolved


class ShareSources:
    """Resolve share names by Hermes tier, refusing same-tier ambiguity."""

    def __init__(self, host: Any) -> None:
        local = Path(host.skills_root)
        discover = getattr(host, "skill_search_roots", None)
        self.roots = discover() if callable(discover) else [(1, local)]
        self._names: dict[str, list[tuple[int, Path]]] = defaultdict(list)
        for tier, root in self.roots:
            for source in _safe_sources(Path(root)):
                try:
                    declared = _overlay_frontmatter(source).get("name")
                except (OSError, ValueError):
                    declared = None
                aliases = {source.name}
                if isinstance(declared, str) and declared.strip():
                    aliases.add(declared.strip())
                for alias in aliases:
                    entry = (tier, source)
                    if entry not in self._names[alias]:
                        self._names[alias].append(entry)

    def names(self) -> list[str]:
        """All aliases, including ambiguous ones which resolve() will refuse."""
        return sorted(self._names)

    def resolve(self, name: str) -> Path:
        """Pick only the highest-precedence tier's single safe source."""
        if not isinstance(name, str) or not name:
            raise WisdomNotFound("local skill not found")
        if name != name.strip() or "/" in name or "\\" in name or name in {".", ".."}:
            raise WisdomNotFound("local skill not found")
        candidates = self._names.get(name, [])
        if not candidates:
            raise WisdomNotFound("local skill not found")
        tier = min(item[0] for item in candidates)
        paths = sorted({path for found_tier, path in candidates if found_tier == tier})
        if len(paths) != 1:
            raise SkillRefusal(f"Ambiguous skill name {name!r}: " + ", ".join(map(str, paths)))
        return paths[0]
