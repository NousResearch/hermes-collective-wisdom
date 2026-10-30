"""Owner-local advisory installation status for catalog reads."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .managed_target import removed_outside_wisdom, validate_managed_target
from .package import PackagePolicyError
from .qualification import _eligible_path  # Reuse the profile-safe local skill resolver fence.


_STATUS_UNAVAILABLE = {
    "state": "unavailable",
    "text": "Installation status unavailable",
    "installed_version": None,
    "latest_version": None,
}


class CatalogInstallationStatus:  # pylint: disable=too-few-public-methods
    """Project one owner-scoped aggregate into cheap catalog status labels."""

    def __init__(
        self, installed: Any, owner: dict[str, str], profile_home: Path, host: Any = None,
    ) -> None:
        self.owner = dict(owner)
        self.skills_root = Path(profile_home).expanduser().absolute() / "skills"
        self.host = host or _CatalogHost(self.skills_root)
        try:
            self._find_skill_dir = getattr(import_module("tools.skill_usage"), "_find_skill_dir")
        except (AttributeError, ImportError, RuntimeError):
            self._find_skill_dir = None
        try:
            rows = installed.installations(owner=self.owner)
            self._managed = {
                row.get("skill_id"): row
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("skill_id"), str)
            }
            self._managed_available = True
        except (OSError, RuntimeError, TypeError, ValueError):
            self._managed = {}
            self._managed_available = False

    def for_skill(
        self, skill_id: object, slug: object, latest_version: object = None
    ) -> dict[str, Any]:
        """Return display-ready local status without hashing package trees."""

        latest = _positive_version(latest_version)
        if not self._managed_available or not isinstance(skill_id, str) or not skill_id:
            return dict(_STATUS_UNAVAILABLE)
        record = self._managed.get(skill_id)
        if record is not None:
            return self._managed_status(record, latest)
        return self._unmanaged_status(slug, latest)

    def _managed_status(self, record: dict[str, Any], latest: int | None) -> dict[str, Any]:
        version = _positive_version(record.get("version"))
        if version is not None and removed_outside_wisdom(self.host, record, self.owner):
            # Install is offered again; the stale record is replaced by that install.
            return {
                "state": "removed", "text": "Removed from this computer",
                "installed_version": version, "latest_version": latest,
            }
        if (
            record.get("state") != "active" or version is None
            or not self._record_target_available(record)
        ):
            return {**_STATUS_UNAVAILABLE, "latest_version": latest}
        text = f"Installed v{version}"
        if latest is not None and latest > version:
            text += f" · update to v{latest} available"
        return {
            "state": "installed", "text": text,
            "installed_version": version, "latest_version": latest,
        }

    def _record_target_available(self, record: dict[str, Any]) -> bool:
        try:
            target = validate_managed_target(
                self.host, record, self.owner, require_tree=False,
            )
            for raw_path in record["baseline"]:
                relative = PurePosixPath(raw_path)
                if (
                    relative.is_absolute() or not relative.parts or ".." in relative.parts
                    or not target.joinpath(*relative.parts).is_file()
                ):
                    return False
            return True
        except (KeyError, OSError, PackagePolicyError, RuntimeError, TypeError, ValueError):
            return False

    def _unmanaged_status(self, slug: object, latest: int | None) -> dict[str, Any]:
        if not isinstance(slug, str) or _safe_segment(slug, fail=False) is None:
            return {**_STATUS_UNAVAILABLE, "latest_version": latest}
        if self._find_skill_dir is None:
            return {**_STATUS_UNAVAILABLE, "latest_version": latest}
        try:
            found = self._find_skill_dir(slug)
        except (OSError, RuntimeError, TypeError, ValueError):
            return {**_STATUS_UNAVAILABLE, "latest_version": latest}
        if found is None:
            return {
                "state": "not_installed", "text": "Not installed",
                "installed_version": None, "latest_version": latest,
            }
        eligible = _eligible_path(
            slug, skills_root=self.skills_root,
            skill_resolver=lambda _name: Path(found),
        )
        if eligible is None:
            return {**_STATUS_UNAVAILABLE, "latest_version": latest}
        return {
            "state": "unmanaged_local",
            "text": "Local copy · published version unknown",
            "installed_version": None, "latest_version": latest,
        }


def _positive_version(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _safe_segment(value: object, *, fail: bool = True) -> str | None:
    if isinstance(value, str) and value and Path(value).name == value and value not in {".", ".."}:
        return value
    if fail:
        raise ValueError("catalog skill slug is invalid")
    return None


class _CatalogHost:  # pylint: disable=too-few-public-methods
    """Fixture-safe lexical fallback; runtime supplies the actual host validator."""

    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root

    def validate_target(self, target: Path) -> None:
        """Require the fallback target to remain below the profile skills root."""

        if target == self.skills_root or not target.is_relative_to(self.skills_root):
            raise PackagePolicyError("install target must be contained by skills")
