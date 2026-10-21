"""Shared cell values for catalog listings rendered as tables (/wisdom browse)."""

from __future__ import annotations

import unicodedata

DESCRIPTION_LIMIT = 80
COLUMNS = ("Skill", "Version", "Installed", "Description")


def flatten(value: object) -> str:
    """One line: every line break, control and format character becomes a space.

    Covers ``\\n``, ``\\r``, U+0085, U+2028 and U+2029 (``str.split`` whitespace)
    and every Unicode ``C*`` category, so a value never adds a physical row.
    """
    return " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in str(value or "")
        ).split()
    )


def one_line(value: object, limit: int = DESCRIPTION_LIMIT) -> str:
    """Collapse whitespace and controls to one line, trimmed with an ellipsis."""
    text = flatten(value)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def version_cell(value: object) -> str:
    """``v<N>`` for a positive published version, ``-`` when unknown."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return f"v{value}"
    return "-"


def _positive(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def installed_cell(status: object) -> str:
    """Short, never-empty Installed column value from the advisory local status."""
    data = status if isinstance(status, dict) else {}
    state = data.get("state")
    if state == "installed":
        installed = _positive(data.get("installed_version"))
        latest = _positive(data.get("latest_version"))
        if installed is not None and latest is not None and latest > installed:
            return f"v{installed} (update to v{latest})"
        return "Yes"
    if state == "not_installed":
        return "No"
    if state == "unmanaged_local":
        return "Local copy"
    text = data.get("text")
    if state is None and isinstance(text, str) and text.strip():
        return one_line(text, 40)
    return "Unknown"
