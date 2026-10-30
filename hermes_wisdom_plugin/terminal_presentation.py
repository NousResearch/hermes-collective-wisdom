"""Rich presentation for human-facing Wisdom terminal commands only."""

from __future__ import annotations

import os
import shutil
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .terminal_ui import removed_install_hint, without_controls, _requirements_summary


_SUPPORTED_COMMANDS = frozenset({
    "installed", "list", "browse", "show", "versions", "status", "check", "settings",
})
_STATUS_STYLES = {
    "installed": "green",
    "not_installed": "yellow",
    "unmanaged_local": "yellow",
    "removed": "yellow",
    "unavailable": "yellow",
}


def _safe_text(value: object, *, style: str | None = None) -> Text:
    """Build literal Rich text after the shared terminal control sanitization."""

    safe = " ".join(without_controls(value).split())
    return Text(safe, style=style, no_wrap=False, overflow="fold")


def _terminal_width() -> int:
    """Resolve the current terminal width while keeping narrow output usable."""

    return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _console(stream: TextIO) -> Console:
    return Console(
        file=stream,
        force_terminal="NO_COLOR" not in os.environ,
        color_system=None if "NO_COLOR" in os.environ else "auto",
        no_color="NO_COLOR" in os.environ,
        width=_terminal_width(),
        highlight=False,
        soft_wrap=False,
    )


def _installation_status(row: dict[str, Any], slug: object = None) -> tuple[str, str | None]:
    status = row.get("installation_status")
    if not isinstance(status, dict):
        return "Installation status unavailable", "yellow"
    value = status.get("text")
    label = value if isinstance(value, str) and value else "Installation status unavailable"
    label += removed_install_hint(status, slug or row.get("slug"))
    state = str(status.get("state") or "").casefold()
    versions = (status.get("installed_version"), status.get("latest_version"))
    if state == "installed" and all(
        isinstance(version, int) and not isinstance(version, bool) for version in versions
    ):
        if versions[1] > versions[0]:
            return label, "yellow"
    return label, _STATUS_STYLES.get(state)


def _catalog_table(value: object) -> Table:
    data = value if isinstance(value, dict) else {}
    rows = data.get("skills") if isinstance(data.get("skills"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    table = Table(title="Shared skills", show_lines=False, expand=True,
                  caption=f"Results: {len(rows)}", caption_style="dim")
    table.add_column("Name", style="bold", min_width=8, ratio=2, overflow="fold")
    table.add_column("Status", min_width=10, ratio=2, overflow="fold")
    table.add_column("Version", style="dim", width=7, no_wrap=True)
    table.add_column("Description", min_width=12, ratio=3, overflow="fold")
    for item in rows:
        name = item.get("slug") or item.get("id") or "Unnamed skill"
        version = item.get("latest_version")
        version_label = (
            f"v{version}"
            if isinstance(version, int) and not isinstance(version, bool) and version > 0
            else "—"
        )
        status, status_style = _installation_status(item)
        description = item.get("author_description") or "No description"
        table.add_row(
            _safe_text(name, style="bold"),
            _safe_text(status, style=status_style),
            _safe_text(version_label, style="dim"),
            _safe_text(description),
        )
    if not rows:
        table.caption = Text("No matching shared skills.", style="dim")
    return table


_CHECK_LABELS = {
    "pass": ("Pass", "green"),
    "advisory": ("Advisory", "yellow"),
    "blocked": ("Blocked", "red"),
    "pending": ("Pending", "yellow"),
    "retry": ("Pending", "yellow"),
    "running": ("Pending", "yellow"),
    "unavailable": ("Unavailable", "dim"),
}


def _review_checks(row: dict[str, Any]) -> Text:
    result = Text()
    for key, label in (
        ("security_check", "Security"),
        ("professionalism_check", "Professionalism"),
    ):
        if result:
            result.append(" · ", style="dim")
        check = row.get(key) if isinstance(row.get(key), dict) else {}
        state = str(check.get("status") or "unavailable").casefold()
        state_label, style = _CHECK_LABELS.get(state, ("Unavailable", "dim"))
        result.append(label + ": ", style="dim")
        result.append(state_label, style=style)
    return result


def _requirements(value: object) -> str:
    return _requirements_summary(value) if isinstance(value, dict) else "Unavailable"


def _versions_table(value: object) -> Table:
    data = value if isinstance(value, dict) else {}
    raw_rows = data.get("versions")
    rows = raw_rows if isinstance(raw_rows, list) else []
    table = Table(title="Published versions", show_lines=False, expand=True)
    table.add_column("Version", style="bold", width=8, no_wrap=True)
    table.add_column("Published", style="dim", min_width=10, ratio=2)
    table.add_column("Checks", min_width=14, ratio=3, overflow="fold")
    table.add_column("Requirements", min_width=14, ratio=3, overflow="fold")
    valid = [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("version"), int)
        and not isinstance(row.get("version"), bool)
        and row["version"] > 0
    ]
    valid.sort(key=lambda row: row["version"], reverse=True)
    status, _style = _installation_status(valid[0] if valid else {})
    table.caption = _safe_text(f"{status} · Results: {len(valid)}", style="dim")
    for row in valid:
        published = row.get("published_at") or row.get("created_at") or "Published"
        if row.get("state") not in (None, "published"):
            published = f"{row['state']} · {published}"
        table.add_row(
            _safe_text(f"v{row['version']}", style="bold"),
            _safe_text(published, style="dim"),
            _review_checks(row),
            _safe_text(_requirements(row.get("system_spec"))),
        )
    if not valid:
        table.caption = Text("No published versions.", style="dim")
    return table


def _show_table(value: object) -> Table:
    data = value if isinstance(value, dict) else {}
    skill = data.get("skill") if isinstance(data.get("skill"), dict) else {}
    latest_detail = (
        data.get("latest_version_detail")
        if isinstance(data.get("latest_version_detail"), dict) else {}
    )
    latest = (
        latest_detail.get("version")
        if isinstance(latest_detail.get("version"), dict) else {}
    )
    name = skill.get("slug") or skill.get("id") or "Shared skill"
    table = Table(title=_safe_text(name, style="bold"), show_header=False, expand=True)
    table.add_column("Field", style="bold", width=13, no_wrap=True)
    table.add_column("Value", ratio=1, overflow="fold")
    description = latest.get("author_description") or skill.get("author_description")
    table.add_row("Description", _safe_text(description or "No description"))
    versions = data.get("versions") if isinstance(data.get("versions"), list) else []
    numbers = [
        row["version"] for row in versions
        if isinstance(row, dict)
        and isinstance(row.get("version"), int)
        and not isinstance(row.get("version"), bool)
        and row["version"] > 0
    ]
    table.add_row("Latest", _safe_text(f"v{max(numbers)}" if numbers else "Unknown", style="dim"))
    status, status_style = _installation_status(data, skill.get("slug"))
    table.add_row("Status", _safe_text(status, style=status_style))
    table.add_row("Checks", _review_checks(latest))
    table.add_row("Requirements", _safe_text(_requirements(latest.get("system_spec"))))
    return table


def _installed_table(value: object) -> Table:
    data = value if isinstance(value, dict) else {}
    raw_rows = value if isinstance(value, list) else data.get("installations")
    rows = raw_rows if isinstance(raw_rows, list) else []
    table = Table(title="Managed installations", show_lines=False, expand=True)
    table.add_column("Name", style="bold", min_width=10, ratio=2, overflow="fold")
    table.add_column("Version", style="dim", width=9, no_wrap=True)
    table.add_column("State", min_width=8, ratio=1, overflow="fold")
    table.add_column("Update mode", style="dim", min_width=11, ratio=1, overflow="fold")
    valid = [row for row in rows if isinstance(row, dict)]
    table.caption = Text(f"Results: {len(valid)}", style="dim")
    for row in valid:
        name = row.get("slug") or row.get("skill_id") or "unknown"
        version = row.get("version", row.get("installed_version"))
        version_label = (
            f"v{version}"
            if isinstance(version, int) and not isinstance(version, bool) and version > 0
            else "Unknown"
        )
        state = row.get("state") or row.get("skill_state") or "unknown"
        state_style = "green" if str(state).casefold() in {"active", "published"} else "yellow"
        if state == "removed":
            state = row.get("status_text") or "Removed"
        update_mode = row.get("update_mode") or row.get("effective_update_mode") or "Unknown"
        table.add_row(
            _safe_text(name, style="bold"),
            _safe_text(version_label, style="dim"),
            _safe_text(state, style=state_style),
            _safe_text(update_mode, style="dim"),
        )
    if not valid:
        table.caption = Text("No managed installations.", style="dim")
    return table


_VALUE_STYLES = {
    "ready": "green", "active": "green", "current": "green", "Yes": "green",
    "required": "yellow", "update_available": "yellow", "No": "dim",
    "not configured": "yellow", "unverifiable": "yellow", "unavailable": "yellow",
    "archived": "red", "taken_down": "red", "denied": "red",
}


def _summary_table(command: str, value: object, summary: str) -> Table:
    """Style the existing safe summary rather than duplicating status policy."""
    data = value if isinstance(value, dict) else {}
    lines = summary.splitlines()
    title = lines[0] if lines else command.title()
    facts = [line.partition(": ")[::2] for line in lines[1:] if ": " in line]
    if command == "settings":
        title = "Notification settings"
        writable = data.get("writable")
        facts = [
            ("Delivery mode", data.get("delivery_mode") or "Unknown"),
            ("Writable", "Yes" if writable is True else "No" if writable is False else "Unknown"),
        ]
    if command == "check":
        for key, label in (("installed_version", "Installed version"),
                           ("latest_version", "Latest version")):
            version = data.get(key)
            if isinstance(version, int) and not isinstance(version, bool) and version > 0:
                facts.append((label, f"v{version}"))
    table = Table(title=_safe_text(title), show_header=False, expand=True)
    table.add_column("Field", style="bold", ratio=1, overflow="fold")
    table.add_column("Value", ratio=3, overflow="fold")
    for label, content in facts:
        table.add_row(_safe_text(label), _safe_text(content, style=_VALUE_STYLES.get(str(content))))
    return table


def render_terminal(
    command: str | None, value: object, console: Console, summary: str,
) -> bool:
    """Render one supported human command to ``console``; return whether handled."""

    if command not in _SUPPORTED_COMMANDS:
        return False
    console.print()
    if command in {"status", "check", "settings"}:
        presentation = _summary_table(command, value, summary)
    elif command == "versions":
        presentation = _versions_table(value)
    elif command == "show":
        presentation = _show_table(value)
    elif command == "installed":
        presentation = _installed_table(value)
    else:
        presentation = _catalog_table(value)
    console.print(presentation)
    console.print()
    return True


def emit_terminal(
    command: str | None, value: object, stream: TextIO, summary: str,
) -> bool:
    """Render only when ``stream`` is an interactive, capable terminal."""

    if command not in _SUPPORTED_COMMANDS or os.environ.get("TERM", "").casefold() == "dumb":
        return False
    if isinstance(value, dict) and isinstance(value.get("error"), str):
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    return render_terminal(command, value, _console(stream), summary)
