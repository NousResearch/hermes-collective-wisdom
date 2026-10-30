"""Host-exposed surfaces for the current Collective Wisdom release.

The package implements more than the current release offers. Hermes sharing
suggestions, agent-led recommendations and notices, notification settings,
automatic update maintenance, model tools and model-written editorial copy stay
implemented and tested, but the current release registers only manual sharing
plus automatic share suggestions. Widen ``ACTIVE`` deliberately when a release
ships more.
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
    # Propose a name/description with the owner's model only when the skill's
    # own copy cannot be shared as written. Independent of model_editorial.
    model_copy: bool
    # Suggest sharing one of the owner's own skills after Hermes learns it
    # (auto_suggest.py). Its hooks and tool register only on a host with
    # ctx.fork_session or ctx.side_question; elsewhere this scope behaves like
    # MANUAL_SHARING.
    auto_suggest: bool = False

    def allows_cli(self, command: str) -> bool:
        """Return whether ``hermes wisdom <command>`` is registered."""
        return command in self.cli_commands

    def allows_slash(self, command: str) -> bool:
        """Return whether ``/wisdom <command>`` is accepted."""
        return command in self.slash_commands


_MANUAL_CLI = (
    "setup", "status", "list", "browse", "show", "versions", "top", "suggest", "share",
    "share-suggest", "prepared", "publication", "install", "installed", "check", "update",
    "installed-setup", "uninstall", "help",
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
        "installed-setup", "uninstall", "help",
    ),
    slash_commands=_MANUAL_SLASH | frozenset({
        "settings", "mute", "candidate", "suggest", "publish",
    }),
    hooks=(
        "pre_gateway_dispatch", "pre_llm_call", "on_skill_lifecycle",
        "transform_llm_output", "on_session_end",
    ),
    model_tools=("wisdom_inbox", "wisdom_inspect", "present_wisdom_consent"),
    auxiliary_tasks=("hermes_collective_wisdom_refinement",),
    desktop_route_groups=frozenset({"core", "candidates", "notices", "settings"}),
    notification_settings=True,
    model_editorial=True,
    model_copy=True,
)

# User-initiated sharing and installation only. The slash gateway hook stays
# registered because private /wisdom reviews arrive through it.
MANUAL_SHARING = ReleaseScope(
    name="manual-sharing",
    cli_commands=_MANUAL_CLI,
    slash_commands=_MANUAL_SLASH,
    hooks=("pre_gateway_dispatch",),
    model_tools=(),
    auxiliary_tasks=(),
    desktop_route_groups=frozenset({"core"}),
    notification_settings=False,
    model_editorial=False,
    model_copy=True,
)

# Manual sharing plus automatic share suggestions: the owner's own agent judges,
# through a ctx.fork_session background fork (or ctx.side_question on hosts
# without forks), whether a skill it just learned is worth sharing, and the owner
# answers a three-choice card in their private chat. Nothing else from FULL
# (notices, recommendations, settings reviews) is registered. The auxiliary task
# routes the fork and registers only on a host with ctx.fork_session.
AUTO_SUGGEST = ReleaseScope(
    name="auto-suggest",
    cli_commands=(*_MANUAL_CLI[:-1], "suggestions", "help"),
    slash_commands=_MANUAL_SLASH | frozenset({"suggestions"}),
    hooks=("pre_gateway_dispatch", "post_llm_call", "post_tool_call", "on_skill_lifecycle"),
    model_tools=("wisdom_share_suggestion",),
    auxiliary_tasks=("hermes_collective_wisdom_share_suggest",),
    desktop_route_groups=frozenset({"core"}),
    notification_settings=False,
    model_editorial=False,
    model_copy=True,
    auto_suggest=True,
)

ACTIVE = AUTO_SUGGEST


def active() -> ReleaseScope:
    """Read the release scope at call time so tests can widen it explicitly."""
    return ACTIVE
