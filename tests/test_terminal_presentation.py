"""TTY-only Rich presentation stays safe and preserves machine/plain output."""

from __future__ import annotations

import io
import json
import re
import sys

import pytest

from hermes_wisdom_plugin.commands import _emit


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _TTYBuffer(io.StringIO):
    """Small real-stream stand-in that advertises terminal capabilities."""

    def isatty(self) -> bool:
        return True


def _plain(value: str) -> str:
    """ plain."""
    return _ANSI.sub("", value)


def test_browse_tty_uses_bordered_wrapping_table_with_safe_rich_text(monkeypatch):
    """Browse tty uses bordered wrapping table with safe rich text."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLUMNS", "60")
    payload = {
        "skills": [
            {
                "id": "skill-1",
                "slug": "incident-response-with-a-deliberately-long-name",
                "latest_version": 12,
                "author_description": (
                    "A long description that must wrap cleanly in a narrow terminal instead of "
                    "being clipped or spilling beyond the table border."
                ),
                "installation_status": {
                    "state": "installed",
                    "text": "Installed v11 · update to v12 available",
                },
            },
            {
                "slug": "[bold red]owned[/]",
                "latest_version": 1,
                "author_description": "literal markup\x1b]8;;https://evil.invalid\a",
                "installation_status": {"state": "not_installed", "text": "Not installed"},
            },
        ]
    }

    _emit(payload, as_json=False, command="browse")

    rendered = output.getvalue()
    plain = _plain(rendered)
    assert "Shared skills" in plain
    assert all(heading in plain for heading in ("Name", "Status", "Version", "Description"))
    assert "Installed" in plain and "v11" in plain
    assert all(token in plain for token in ("bold", "red", "owned"))
    assert "\x1b]8;" not in rendered
    assert "being" in plain and "clipped" in plain
    assert "┏" in plain and "┘" in plain
    assert max(map(len, plain.splitlines())) <= 60
    assert "\x1b[" in rendered


def test_browse_non_tty_remains_existing_plain_output(monkeypatch):
    """Browse non tty remains existing plain output."""
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    payload = {"skills": [{"slug": "incident", "latest_version": 2,
                            "author_description": "Plain pipe output"}]}

    _emit(payload, as_json=False, command="browse")

    assert output.getvalue() == (
        "Shared skills\nResults: 1\n  - incident (v2): Plain pipe output\n"
        "    Installation status unavailable\n"
    )
    assert "\x1b" not in output.getvalue()


def test_browse_json_bytes_are_unchanged_even_for_tty(monkeypatch):
    """Browse json bytes are unchanged even for tty."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    payload = {"skills": [{"slug": "[green]literal[/]", "latest_version": 2}]}

    _emit(payload, as_json=True, command="browse")

    assert output.getvalue() == json.dumps(
        payload, indent=2, sort_keys=True, ensure_ascii=False, default=str
    ) + "\n"
    assert "\x1b" not in output.getvalue()


def test_versions_tty_formats_review_states_and_metadata_as_table(monkeypatch):
    """Versions tty formats review states and metadata as table."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "80")
    payload = {"versions": [{
        "version": 7,
        "published_at": "2026-09-07",
        "state": "published",
        "system_spec": {"hermes": {"minimum_version": "0.20.5"},
                        "platforms": ["macOS"], "runtime": {"shell": True}},
        "security_check": {"status": "pass"},
        "professionalism_check": {"status": "unavailable"},
    }]}

    _emit(payload, as_json=False, command="versions")

    rendered = output.getvalue()
    plain = _plain(rendered)
    assert "Published versions" in plain
    assert all(value in plain for value in ("Version 7", "2026-09-07", "pass", "unavailable"))
    assert "Requirements" not in plain
    assert "\x1b" not in rendered


def test_show_tty_keeps_complete_description_and_advisory_facts(monkeypatch):
    """Show tty keeps complete description and advisory facts."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "60")
    description = " ".join(["Complete owner description"] * 30) + " FINAL-SAFETY-FACT"
    payload = {
        "skill": {"id": "skill-1", "slug": "[red]incident-handoff[/]"},
        "versions": [{"version": 7, "state": "published"}],
        "latest_version_detail": {"version": {
            "version": 7,
            "author_description": description,
            "system_spec": {"credentials": ["never-render-this-secret"],
                            "known_limitations": ["private limitation"]},
            "security_check": {"status": "advisory"},
            "professionalism_check": {"status": "pass"},
        }},
        "installation_status": {"state": "unmanaged", "text": "Unmanaged local copy"},
    }

    _emit(payload, as_json=False, command="show")

    rendered = output.getvalue()
    plain = _plain(rendered)
    flattened = " ".join(plain.replace("│", " ").split())
    assert all(value in flattened for value in (
        "incident-handoff", "FINAL-SAFETY-FACT", "Latest", "v7",
        "Unmanaged local copy", "advisory", "pass", "1 credential item(s)",
        "1 limitation item(s)",
    ))
    assert "never-render-this-secret" not in plain
    assert "private limitation" not in plain
    assert "Requirements" not in plain


@pytest.mark.parametrize("width", [60, 80])
def test_installed_tty_uses_actual_installation_fields(monkeypatch, width):
    """Installed rows retain known state without claiming unknown files are clean."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", str(width))
    payload = {"installations": [
        {"skill_id": "skill-1", "slug": "incident", "version": 2,
         "state": "active", "modified": False, "update_mode": "MANUAL"},
        {"skill_id": "skill-2", "installed_version": 4, "skill_state": "published",
         "latest_version": 5, "update_mode": "REQUIRED"},
    ]}

    _emit(payload, as_json=False, command="installed")

    rendered = output.getvalue()
    plain = _plain(rendered)
    assert "Managed installations" in plain
    assert "incident" in plain
    assert "skill-2" not in plain
    assert "Unnamed skill" in plain


@pytest.mark.parametrize(("command", "payload", "expected"), [
    ("status", {"configured": True, "state": "active"},
     ("Collective Wisdom status", "Setup", "ready", "State")),
    ("check", {"skill_id": "skill-1", "slug": "Friendly Skill", "state": "update_available"},
     ("Update check", "Skill", "Friendly Skill", "update_available")),
    ("settings", {"delivery_mode": "agent", "writable": True},
     ("Notification settings", "Delivery mode", "agent", "Writable", "Yes")),
])
def test_summary_commands_use_semantic_terminal_tables(monkeypatch, command, payload, expected):
    """Summary commands use semantic terminal tables."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")

    _emit(payload, as_json=False, command=command)

    rendered = output.getvalue()
    plain = _plain(rendered)
    assert all(value in plain for value in expected)
    if command == "check":
        assert "skill-1" not in rendered
    else:
        assert "┌" in plain and "┘" in plain


@pytest.mark.parametrize(("command", "payload", "message"), [
    ("browse", {"skills": []}, "No matching shared skills."),
    ("versions", {"versions": []}, "No published versions."),
    ("installed", {"installations": []}, "No managed installations."),
])
def test_empty_tty_tables_are_explicit(monkeypatch, command, payload, message):
    """Empty tty tables are explicit."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")

    _emit(payload, as_json=False, command=command)

    assert message in _plain(output.getvalue())


def test_term_dumb_and_no_color_are_respected(monkeypatch):
    """Term dumb and no color are respected."""
    payload = {"skills": [{"slug": "one", "latest_version": 1}]}
    dumb = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", dumb)
    monkeypatch.setenv("TERM", "dumb")
    _emit(payload, as_json=False, command="browse")
    assert dumb.getvalue().startswith("Shared skills\nResults: 1")
    assert "\x1b" not in dumb.getvalue()

    no_color = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", no_color)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    _emit(payload, as_json=False, command="browse")
    assert "┏" in no_color.getvalue()
    assert "\x1b" not in no_color.getvalue()


def test_catalog_update_status_is_not_coloured_as_current(monkeypatch):
    """A newer available version is amber, even while the current copy is installed."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    _emit({"skills": [{
        "slug": "incident", "latest_version": 2,
        "installation_status": {"state": "installed", "text": "Installed v1",
                                "installed_version": 1, "latest_version": 2},
    }]}, as_json=False, command="browse")
    assert "\x1b[33mInstalled v1" in output.getvalue()
    assert "Results: 1" in _plain(output.getvalue())


def test_versions_retain_installation_status_and_withdrawn_rows(monkeypatch):
    """Styling cannot drop the installed revision or silently filter version states."""
    output = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")
    _emit({"versions": [{"version": 1, "state": "withdrawn",
                          "installation_status": {"state": "installed", "text": "Installed v1"}}]},
          as_json=False, command="versions")
    plain = _plain(output.getvalue())
    assert "Installed v1" in plain and "withdrawn" in plain
    assert "v1" in plain and "Results: 1" in plain


def test_human_errors_stay_on_stderr_and_never_interpret_controls(monkeypatch):
    """Human errors stay on stderr and never interpret controls."""
    output = _TTYBuffer()
    errors = _TTYBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", errors)
    monkeypatch.setenv("TERM", "xterm-256color")

    _emit({"error": "bad [bold]value[/]\x1b]8;;https://evil.invalid\a"},
          as_json=False, error=True)

    assert output.getvalue() == ""
    assert "Error: bad [bold]value[/]" in errors.getvalue()
    assert "\x1b]8;" not in errors.getvalue()
