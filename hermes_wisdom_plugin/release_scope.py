"""Host-exposed surfaces for the current Collective Wisdom release.

The package implements more than the current release offers. Hermes sharing
suggestions, agent-led recommendations and notices, notification settings,
automatic update maintenance, model tools and model-written editorial copy stay
implemented and tested, but the manual skill-sharing release does not register
them with the host. Widen ``ACTIVE`` deliberately when a release ships them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReleaseScope:  # pylint: disable=too-many-instance-attributes
    """One named set of surfaces the plugin registers with Hermes."""

    name: str
    cli_commands: tuple[str, ...]
    slash_commands: frozenset[str]
    hooks: tuple[str, ...]
    model_tools: tuple[str, ...]
    auxiliary_tasks: tuple[str, ...]
    desktop_route_groups: frozenset[str]
    notification_settings: bool
    model_editorial: bool

    def allows_cli(self, command: str) -> bool:
        """Return whether ``hermes wisdom <command>`` is registered."""
        return command in self.cli_commands

    def allows_slash(self, command: str) -> bool:
        """Return whether ``/wisdom <command>`` is accepted."""
        return command in self.slash_commands


_MANUAL_CLI = (
    "setup", "status", "list", "browse", "show", "versions", "top", "suggest", "share",
    "share-suggest", "prepared", "publication", "install", "installed", "check", "update",
    "auto-update", "installed-setup", "uninstall", "help",
)
_MANUAL_SLASH = frozenset({
    "help", "status", "setup", "list", "browse", "show", "versions", "top", "share",
    "share-suggest", "prepared", "publication", "install", "installed", "check", "update",
    "uninstall", "installed-setup",
})

# Every surface the package currently implements, for the release that ships
# proactive suggestions and recommendations, and for tests of dormant code.
FULL = ReleaseScope(
    name="full",
    cli_commands=(
        "setup", "status", "settings", "notifications", "mute", "inbox",
        "maintenance", "list", "browse", "show", "versions", "top", "suggest", "share",
        "share-suggest",
        "prepared", "publication", "install", "installed", "check", "update",
        "auto-update", "installed-setup", "uninstall", "help",
    ),
    slash_commands=_MANUAL_SLASH | frozenset({
        "settings", "mute", "candidate", "suggest", "publish",
    }),
    hooks=(
        "pre_gateway_dispatch", "pre_llm_call", "on_skill_lifecycle",
        "transform_llm_output", "on_session_end", "on_session_start",
    ),
    model_tools=("wisdom_inbox", "wisdom_inspect", "present_wisdom_consent"),
    auxiliary_tasks=(
        "hermes_collective_wisdom_refinement", "hermes_collective_wisdom_assist",
    ),
    desktop_route_groups=frozenset({"core", "candidates", "notices", "settings"}),
    notification_settings=True,
    model_editorial=True,
)

# User-initiated sharing and installation, plus (cw/v1) auto-update of installed
# team skills. The slash gateway hook stays registered because private /wisdom
# reviews arrive through it; it also drives the rate-limited messaging-gateway
# auto-update check. ``on_session_start`` freezes the one-line session note and
# triggers the same check; ``transform_llm_output`` shows that frozen line once on
# the session's first local reply. The assist auxiliary task is the one model
# route for Combine and the share-folder suggestion.
MANUAL_SHARING = ReleaseScope(
    name="manual-sharing",
    cli_commands=_MANUAL_CLI,
    slash_commands=_MANUAL_SLASH,
    hooks=("pre_gateway_dispatch", "on_session_start", "transform_llm_output"),
    model_tools=(),
    auxiliary_tasks=("hermes_collective_wisdom_assist",),
    desktop_route_groups=frozenset({"core"}),
    notification_settings=False,
    model_editorial=False,
)

ACTIVE = MANUAL_SHARING


def active() -> ReleaseScope:
    """Read the release scope at call time so tests can widen it explicitly."""
    return ACTIVE
