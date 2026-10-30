"""Suggest sharing one of the owner's own skills after Hermes learns it.

Hermes reviews its own skills while it works (``skills.creation_nudge_interval``
tool calls, ``auxiliary.background_review``). When one of the owner's local skills
is created or patched in a session, or is used and ranks among the owner's top
skills, a later attended turn of that session may ask the session's own agent
whether the skill is worth sharing, the same way Hermes runs its background review:

- ``ctx.fork_session`` (``llm.session_fork``) first: a bounded background copy of
  the session agent sees the candidates and may call ``wisdom_share_suggestion``
  once, for one of them. That call records the suggestion; delivery starts when the
  fork has ended.
- ``ctx.side_question`` (``llm.side_question``) when the fork is unavailable or the
  host refused it (then until restart): one
  JSON verdict about the first candidate.
- Otherwise off.

A suggestion starts a turn in the owner's private chat that asks the model to call
``wisdom_share_suggestion``; the tool shows a card for that one pending suggestion
with three choices. Only this plugin's own fork may propose a skill; the live
agent can only show a card that is already pending and cannot answer it. Nothing
is shared until the owner confirms the normal share review.

Cadence follows core's ``post_llm_call`` ``review_signals`` when the host sends
them (the skill review is due and background review is on); older hosts fall
back to counting tool calls against ``skills.creation_nudge_interval``.

Off unless every condition holds: the host has one of the two methods and the
user granted its capability; self-learning is on; the plugin setting
``auto_suggest`` is not false; the profile is set up; the turn is the owner's
CLI, TUI or Desktop session or a private Slack or Telegram chat. At most
``auto_suggest_daily_limit`` (default 1) suggestions are shown per profile per
day, a skill revision is asked about once, and a skill the owner chose
"Never for this skill" for is not suggested again until they allow it.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import shlex
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .setup_lock import managed_operation_guard
from .share_suggest import _inject, hermes_command
from .top_command import private_route
from .top_skills import clean_text, is_messaging

logger = logging.getLogger(__name__)

CAPABILITY = "llm.side_question"
FORK_CAPABILITY = "llm.session_fork"
# The plugin's auxiliary task for the fork; it inherits core's background review routing.
FORK_TASK = "hermes_collective_wisdom_share_suggest"
FORK_MAX_ITERATIONS = 4
FORK_TIMEOUT = 300.0
FORK_CANDIDATES = 3
HOOKS = ("post_llm_call", "post_tool_call", "on_skill_lifecycle")
TOOL = "wisdom_share_suggestion"
SETTING = "auto_suggest"
DAILY_LIMIT_SETTING = "auto_suggest_daily_limit"
DEFAULT_DAILY_LIMIT = 1
PENDING_TTL_SECONDS = 30 * 60
SIDE_QUESTION_TIMEOUT = 20.0
NATIVE_PLATFORMS = frozenset({"slack", "telegram"})
LOCAL_SURFACES = frozenset({"cli", "tui", "desktop"})
_CREATED = frozenset({"created", "patched", "edited"})
_OWN_PROVENANCE = frozenset({"local", "agent_created"})
_STATE_KEY = "wisdom-auto-suggest"
_LOCK = "\0wisdom-auto-suggest"
_MAX_SESSIONS = 64
_MAX_DECISIONS = 200
_MAX_PENDING = 20
_NAME_LIMIT = 64
_REASON_LIMIT = 160

TOOL_SCHEMA = {
    "name": TOOL,
    "description": (
        "Show the owner the Collective Wisdom card for a skill Hermes suggested sharing. "
        "Call it only when a Collective Wisdom message asks you to, with that exact skill "
        "name (and, in a Collective Wisdom background check, a one-line reason). It only "
        "displays a card: the owner chooses Confirm share, Don't share or Never for this "
        "skill on it. It never shares, uploads or publishes anything."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False, "required": ["skill"],
        "properties": {
            "skill": {"type": "string", "minLength": 1, "maxLength": _NAME_LIMIT},
            "reason": {"type": "string", "maxLength": _REASON_LIMIT},
        },
    },
}


def host_supports(ctx: Any) -> bool:
    """Whether this Hermes can fork the session agent or ask it a side question."""
    return callable(getattr(ctx, "has_capability", None)) and (
        callable(getattr(ctx, "fork_session", None))
        or callable(getattr(ctx, "side_question", None)))


def host_forks(ctx: Any) -> bool:
    """Whether this Hermes offers ``ctx.fork_session`` (plugin session forks)."""
    return callable(getattr(ctx, "fork_session", None)) and callable(
        getattr(ctx, "has_capability", None))


def review_due(signals: Mapping[str, Any]) -> bool:
    """Core's skill-review cadence from ``post_llm_call``'s ``review_signals``.

    Due only when background review is on, the skill nudge interval is positive and
    core decided the skill review is due on this turn (the same flag its own review uses).
    """
    try:
        interval = int(signals.get("skill_nudge_interval") or 0)
    except (TypeError, ValueError):
        return False
    return (signals.get("background_review_enabled") is True and interval > 0
            and signals.get("skill_review_due") is True)


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec="seconds")


def _day(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock()).date().isoformat()


def _name(value: object) -> str:
    return clean_text(value, _NAME_LIMIT)


def _own_skill(facts: Mapping[str, Any]) -> str | None:
    """The skill name of a lifecycle fact about the owner's own local skill."""
    name = facts.get("skill_name")
    if not isinstance(name, str) or not name or ":" in name:
        return None  # Team installs are named "<org>:<skill>".
    return name if str(facts.get("provenance") or "") in _OWN_PROVENANCE else None


class SuggestionStore:
    """Durable choices and decisions in this profile's plugin state."""

    def __init__(self, state: Any, root: Path, *, clock: Callable[[], float] = time.time):
        if state is None or not callable(getattr(state, "get", None)):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root)
        self.clock = clock

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_STATE_KEY)
        if not isinstance(value, dict) or value.get("version") != 1:
            value = {}
        return {
            "version": 1,
            "suppressed": dict(value.get("suppressed") or {}),
            "seen": dict(value.get("seen") or {}),
            "day": dict(value.get("day") or {}),
            "pending": dict(value.get("pending") or {}),
            "decisions": list(value.get("decisions") or [])[-_MAX_DECISIONS:],
        }

    @contextmanager
    def change(self) -> Iterator[dict[str, Any]]:
        """One serialized read-modify-write, shared with other processes of this profile."""
        with managed_operation_guard(self.root, _LOCK, wait=True):
            value = self._load()
            yield value
            self.state.set(_STATE_KEY, value)

    def snapshot(self) -> dict[str, Any]:
        """A detached copy of the stored state."""
        return copy.deepcopy(self._load())

    def _log(self, value: dict[str, Any], event: str, skill: str, **detail: Any) -> None:
        value["decisions"] = [*value["decisions"], {
            "at": _now_iso(self.clock), "event": event, "skill": skill,
            **{key: item for key, item in detail.items() if item is not None},
        }][-_MAX_DECISIONS:]

    def record(self, event: str, skill: str, **detail: Any) -> None:
        """Append one timestamped decision."""
        with self.change() as value:
            self._log(value, event, skill, **detail)

    # Suppression ("Never for this skill") and revisions already asked about.
    def suppressed(self, skill: str) -> bool:
        """Whether the owner chose Never for this skill."""
        return skill in self._load()["suppressed"]

    def set_suppressed(self, skill: str, suppressed: bool, *, source: str) -> bool:
        """Record or undo Never; return whether anything changed."""
        with self.change() as value:
            changed = (skill in value["suppressed"]) != suppressed
            if suppressed:
                value["suppressed"].setdefault(skill, _now_iso(self.clock))
            else:
                value["suppressed"].pop(skill, None)
            self._log(value, "choice", skill, choice="never" if suppressed else "allow",
                      source=source)
            return changed

    def seen(self, skill: str, content_hash: str) -> bool:
        """Whether this exact revision was already asked about or dismissed."""
        return content_hash in (self._load()["seen"].get(skill) or {})

    def mark_seen(self, skill: str, content_hash: str, outcome: str) -> None:
        """Remember that this exact revision was asked about or dismissed."""
        with self.change() as value:
            revisions = dict(value["seen"].get(skill) or {})
            revisions[content_hash] = {"at": _now_iso(self.clock), "outcome": outcome}
            value["seen"][skill] = dict(list(revisions.items())[-20:])

    def forget_seen(self, skill: str, content_hash: str) -> None:
        """Undo an ``asked`` mark when no suggestion reached the owner."""
        with self.change() as value:
            revisions = dict(value["seen"].get(skill) or {})
            if (revisions.get(content_hash) or {}).get("outcome") == "asked":
                revisions.pop(content_hash)
            value["seen"][skill] = revisions

    def dismiss(self, skill: str, content_hash: str, *, source: str) -> None:
        """Don't share: never suggest this exact revision again."""
        with self.change() as value:
            revisions = dict(value["seen"].get(skill) or {})
            revisions[content_hash] = {"at": _now_iso(self.clock), "outcome": "dismissed"}
            value["seen"][skill] = dict(list(revisions.items())[-20:])
            for row in value["pending"].values():
                if row.get("skill") == skill and row.get("content_hash") == content_hash:
                    row["state"] = "dismissed"
            self._log(value, "choice", skill, choice="dismiss", source=source)

    # The daily limit and the no-retry-today fence after a failed delivery.
    def day_open(self, limit: int) -> bool:
        """Whether another suggestion may be shown today."""
        day = self._load()["day"]
        if day.get("date") != _day(self.clock):
            return limit > 0
        return not day.get("blocked") and int(day.get("shown") or 0) < limit

    def reserve_today(self, limit: int) -> bool:
        """Atomically count one suggestion against today's limit."""
        with self.change() as value:
            today = _day(self.clock)
            day = value["day"] if value["day"].get("date") == today else {"date": today}
            if day.get("blocked") or int(day.get("shown") or 0) >= limit:
                return False
            value["day"] = {**day, "shown": int(day.get("shown") or 0) + 1}
            return True

    def block_today(self) -> None:
        """No more attempts today (delivery was denied or unavailable)."""
        with self.change() as value:
            today = _day(self.clock)
            day = value["day"] if value["day"].get("date") == today else {"date": today}
            value["day"] = {**day, "blocked": True}

    # Pending suggestions waiting for the model's tool call.
    def add_pending(self, suggestion: dict[str, Any]) -> None:
        """Store one suggestion for exactly one later tool call."""
        with self.change() as value:
            pending = {key: row for key, row in value["pending"].items()
                       if row.get("expires_at", 0) > self.clock()}
            pending[suggestion["id"]] = copy.deepcopy(suggestion)
            value["pending"] = dict(list(pending.items())[-_MAX_PENDING:])

    def set_pending_state(self, suggestion_id: str, state: str) -> None:
        """Move one pending suggestion to a final state."""
        with self.change() as value:
            row = value["pending"].get(suggestion_id)
            if isinstance(row, dict):
                row["state"] = state

    def claim_pending(self, skill: str, *, session_id: str, session_key: str) -> dict | None:
        """Take the one live suggestion for ``skill`` in this conversation, once."""
        with self.change() as value:
            for row in value["pending"].values():
                route = row.get("route") or {}
                same_conversation = (
                    bool(session_id) and row.get("session_id") == session_id
                    or bool(session_key) and route.get("session_key") == session_key
                )
                if (row.get("skill") == skill and row.get("state") == "pending"
                        and row.get("expires_at", 0) > self.clock() and same_conversation):
                    row["state"] = "presented"
                    return copy.deepcopy(row)
        return None

    def latest(self, skill: str) -> dict[str, Any] | None:
        """The newest pending or shown suggestion for ``skill``."""
        rows = [row for row in self._load()["pending"].values()
                if row.get("skill") == skill and row.get("state") in {"pending", "presented"}]
        return copy.deepcopy(max(rows, key=lambda row: row.get("created_at", 0))) if rows else None


def _why(candidate: Mapping[str, Any]) -> str:
    return {"created": "was just created", "patched": "was just improved",
            "edited": "was just improved"}.get(str(candidate.get("action")),
                                               "is one of my most used")


def verdict_prompt(candidate: Mapping[str, Any]) -> str:
    """A short structured question about one skill, from its local facts only."""
    return "\n".join((
        "Collective Wisdom check, answer with JSON only. One of my own Hermes skills "
        f"{_why(candidate)} in this session. Would teammates in my organization benefit if I "
        "shared it through Collective Wisdom? Answer no if it looks personal, specific to this "
        "machine or project, unfinished, or likely to contain secrets.",
        f"Skill: {_name(candidate.get('name'))}",
        f"Description: {clean_text(candidate.get('description')) or '(none)'}",
        f"Usage: {int(candidate.get('uses') or 0)} uses, {int(candidate.get('edits') or 0)} edits",
        'Reply with exactly: {"share": true or false, "reason": "<one short line>"}',
    ))


def fork_prompt(candidates: list[Mapping[str, Any]]) -> str:
    """The background fork's task: judge these skills, suggest at most one through the tool."""
    lines = [
        "Collective Wisdom background check. This is not a message to reply to, and nothing "
        "here asks you to change files or run commands.",
        "These are my own Hermes skills from this session:",
    ]
    lines.extend(
        f"- {_name(item.get('name'))}: {_why(item)}. "
        f"{clean_text(item.get('description')) or '(no description)'} "
        f"({int(item.get('uses') or 0)} uses, {int(item.get('edits') or 0)} edits)"
        for item in candidates
    )
    lines.extend((
        "Using what you saw in this session, decide whether one of them would clearly help "
        "teammates in my organization if I shared it through Collective Wisdom. Skip any that "
        "look personal, specific to this machine or project, unfinished, or likely to contain "
        "secrets.",
        f"If one clearly qualifies, call {TOOL} once with its exact name as skill and a "
        "one-line reason. Call it at most once and for at most one skill; if none qualifies, "
        "call no tool. The tool only shows me a card to choose from; it never shares anything.",
        "Finish with one short line.",
    ))
    return "\n".join(lines)


_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(answer: object) -> tuple[bool, str]:
    """Yes only for a JSON object whose ``share`` is the boolean true; anything else is no."""
    if not isinstance(answer, str) or not answer.strip():
        return False, ""
    for match in _JSON_OBJECT.finditer(answer):
        try:
            value = json.loads(match.group(0))
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and "share" in value:
            reason = value.get("reason")
            return value["share"] is True, clean_text(reason if isinstance(reason, str) else "",
                                                      _REASON_LIMIT)
    return False, ""


def approval_prompt(skill: str) -> str:
    """The injected user turn: ask for the card, never for a share."""
    return (
        f'Collective Wisdom: my skill "{skill}" may be worth sharing with my team. Call the '
        f'{TOOL} tool with skill "{skill}" so I can choose. Don\'t share, upload or publish '
        "anything yourself. After the tool returns, reply with one short sentence."
    )


def local_card(suggestion: Mapping[str, Any], profile: str = "default") -> str:
    """The text form of the card for the CLI, TUI and Desktop."""
    skill = _name(suggestion.get("skill"))
    quoted = shlex.quote(skill)
    lines = [f"Collective Wisdom: share {skill} with your team?"]
    if suggestion.get("description"):
        lines.append(clean_text(suggestion["description"]))
    if suggestion.get("reason"):
        lines.append(f"Hermes's reason: {clean_text(suggestion['reason'], _REASON_LIMIT)}")
    lines.extend((
        "", "Choose one:",
        f"  Confirm share: run `{hermes_command(profile)} wisdom share {quoted}` in your terminal",
        f"  Don't share: /wisdom suggestions dismiss {quoted}",
        f"  Never for this skill: /wisdom suggestions never {quoted}",
        "Nothing is shared until you approve the share review.",
    ))
    return "\n".join(lines)


def self_learning_interval(load_config: Callable[[], Mapping[str, Any] | None]) -> int:
    """Core's skill-review interval, or 0 when self-learning is off or unreadable."""
    try:
        config = load_config() or {}
        skills = config.get("skills") or {}
        raw = skills.get("creation_nudge_interval", 10) if isinstance(skills, Mapping) else 10
        interval = 10 if isinstance(raw, bool) else int(raw)
        auxiliary = config.get("auxiliary") or {}
        review = auxiliary.get("background_review") if isinstance(auxiliary, Mapping) else None
        enabled = not isinstance(review, Mapping) or review.get("enabled", True) is not False
    except Exception:  # pylint: disable=broad-exception-caught
        return 0  # Unknown settings: stay off rather than guess self-learning is on.
    return interval if enabled and interval > 0 else 0


class AutoSuggest:  # pylint: disable=too-many-instance-attributes
    """Count, choose, ask and deliver; every dependency is injected."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
        self, *, store: SuggestionStore, side_question: Callable[..., Any] | None,
        has_capability: Callable[[str], bool], inject: Callable[..., Any] | None,
        setting: Callable[[str, Any], Any], self_learning: Callable[[], int],
        candidate: Callable[[str], dict[str, Any] | None],
        session: Callable[[], Mapping[str, str]],
        native_available: Callable[[str], bool],
        present: Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]],
        profile: str = "default", profile_current: Callable[[], bool] = lambda: True,
        start: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.time,
        fork_session: Callable[..., Any] | None = None, fork_task: str | None = None,
        plugin_id: str = "collective-wisdom",
    ) -> None:
        self.store = store
        self.side_question = side_question
        self.fork_session = fork_session
        self.fork_task = fork_task
        self.fork_origin = f"plugin_fork:{plugin_id}"
        self.has_capability = has_capability
        self.inject = inject
        self.setting = setting
        self.self_learning = self_learning
        self.candidate = candidate
        self.session = session
        self.native_available = native_available
        self.present = present
        self.profile = profile
        self.profile_current = profile_current
        self.start = start or _thread
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._offer: dict[str, Any] | None = None  # The running fork's candidates.
        self._busy = False
        self._closed = False
        # The host refused a fork (an older or differently configured core): ask by side
        # question from then on in this process. A restart tries the fork again.
        self._fork_refused = False

    def close(self) -> None:
        """Forget in-memory counters; later hooks do nothing."""
        with self._lock:
            self._closed = True
            self._sessions.clear()
            self._offer = None

    def available(self) -> bool:
        """Tool reachability for the host."""
        return not self._closed

    def mode(self) -> str | None:
        """How to ask: ``fork`` first, then ``side_question``, each only when granted.

        After the host refused a fork, the side question is used until the process restarts.
        """
        if (self.fork_session is not None and not self._fork_refused
                and self.has_capability(FORK_CAPABILITY)):
            return "fork"
        if self.side_question is not None and self.has_capability(CAPABILITY):
            return "side_question"
        return None

    def _session_state(self, session_id: str) -> dict[str, Any]:
        state = self._sessions.pop(session_id, None) or {"tools": 0, "candidates": {}}
        self._sessions[session_id] = state
        while len(self._sessions) > _MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return state

    # Hooks -------------------------------------------------------------------------------
    def on_skill_lifecycle(self, **facts: Any) -> None:
        """Remember the owner's own skills created, patched or used in a session."""
        action = str(facts.get("action") or "")
        name = _own_skill(facts)
        session_id = str(facts.get("session_id") or "")
        if (action not in _CREATED | {"loaded"} or name is None or not session_id
                or self._closed or not self.profile_current()):
            return
        with self._lock:
            candidates = self._session_state(session_id)["candidates"]
            if candidates.get(name) not in _CREATED:
                candidates[name] = action

    def on_tool_call(self, **facts: Any) -> None:
        """Count tool calls per session since the last suggestion check."""
        session_id = str(facts.get("session_id") or "")
        if not session_id or self._closed:
            return
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                state["tools"] += 1

    def on_llm_call(self, **facts: Any) -> None:
        """Fire point: an attended turn that carries a side-question token."""
        token = facts.get("side_question_token")
        session_id = str(facts.get("session_id") or "")
        if (not isinstance(token, str) or not token or not session_id or self._closed
                or not self.profile_current()):
            return
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or not state["candidates"] or self._busy:
                return
        if self.setting(SETTING, True) is False or not self._cadence_due(facts, state):
            return
        with self._lock:
            if self._busy:
                return
            state["tools"] = 0
            candidates = dict(state["candidates"])
            self._busy = True
        try:
            route = self._route(facts)
            mode = None if route is None else self.mode()
            if route is None or mode is None or not self.store.day_open(self._daily_limit()):
                self._release()
                return
            check = self._fork_check if mode == "fork" else self._check
            self.start(lambda: check(token, session_id, route, candidates))
        except Exception:  # pylint: disable=broad-exception-caught
            self._release()
            logger.warning("Collective Wisdom suggestion check could not start", exc_info=True)

    def _cadence_due(self, facts: Mapping[str, Any], state: dict[str, Any]) -> bool:
        """Core's review cadence when the host sends it; else this plugin's tool-call count."""
        signals = facts.get("review_signals")
        if isinstance(signals, Mapping):
            return review_due(signals)
        interval = self.self_learning()
        with self._lock:
            return 0 < interval <= state["tools"]

    def _release(self) -> None:
        with self._lock:
            self._busy = False

    def _daily_limit(self) -> int:
        raw = self.setting(DAILY_LIMIT_SETTING, DEFAULT_DAILY_LIMIT)
        if isinstance(raw, bool):
            return DEFAULT_DAILY_LIMIT
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return DEFAULT_DAILY_LIMIT

    def _route(self, facts: Mapping[str, Any]) -> dict[str, Any] | None:
        """Where a suggestion may appear: the owner's local session or a private DM."""
        platform = str(facts.get("platform") or "").strip().casefold()
        surface = {key: str(value or "") for key, value in self.session().items()}
        if platform in NATIVE_PLATFORMS:
            if surface.get("platform", "").casefold() != platform:
                return None
            route = private_route(None, False, surface)
            if (route is None or not surface.get("user_id") or not surface.get("session_key")
                    or not self.native_available(platform)):
                return None
            return {**route, "platform": platform, **{key: surface.get(key, "") for key in (
                "chat_type", "chat_id", "user_id", "thread_id", "scope_id", "session_key")}}
        if platform in LOCAL_SURFACES and not is_messaging(surface):
            return {"kind": "local", "native": False, "platform": platform,
                    "session_key": surface.get("session_key", "")}
        return None

    # Background check --------------------------------------------------------------------
    def _check(self, token: str, session_id: str, route: dict[str, Any],
               candidates: dict[str, str]) -> None:
        try:
            self._ask(token, session_id, route, candidates)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom suggestion check failed", exc_info=True)
        finally:
            self._release()

    def _side_question_ready(self) -> bool:
        return self.side_question is not None and bool(self.has_capability(CAPABILITY))

    def _ask(self, token: str, session_id: str, route: dict[str, Any],
             candidates: dict[str, str], *, keep_on_silence: bool = False) -> None:
        """One side-question verdict about the first viable candidate; deliver when yes.

        ``keep_on_silence``: no answer leaves the skill pending for a later due turn instead
        of counting as "no" (used after a refused fork, which may have spent the token).
        """
        chosen = self._choose(session_id, candidates)
        if chosen is None:
            return
        name, content_hash = chosen["name"], chosen["content_hash"]
        self.store.record("candidate", name, action=chosen["action"],
                          content_hash=content_hash, session_id=session_id)
        self.store.mark_seen(name, content_hash, "asked")
        self.store.record("asked", name, content_hash=content_hash)
        try:
            ask = self.side_question
            answer = ask(token, verdict_prompt(chosen),
                         timeout=SIDE_QUESTION_TIMEOUT) if ask is not None else None
        except Exception:  # pylint: disable=broad-exception-caught
            answer = None
        if answer is None and keep_on_silence:
            self.store.forget_seen(name, content_hash)
            self.store.record("not_asked", name, why="no_answer")
            with self._lock:
                if not self._closed:
                    pending = self._session_state(session_id)["candidates"]
                    if pending.get(name) not in _CREATED:
                        pending[name] = chosen["action"]
            return
        share, reason = parse_verdict(answer)
        self.store.record("verdict", name, share=share, reason=reason or None)
        if share:
            self._deliver(chosen, reason, session_id, route)

    def _choose(self, session_id: str, candidates: dict[str, str]) -> dict[str, Any] | None:
        """The first viable candidate, created or patched skills before used ones."""
        for found in self._viable(session_id, candidates, limit=1):
            return found
        return None

    def _viable(self, session_id: str, candidates: dict[str, str], *,
                limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` viable candidates, created or patched skills before used ones."""
        found: list[dict[str, Any]] = []
        ordered = sorted(candidates.items(), key=lambda item: item[1] not in _CREATED)
        for name, action in ordered:
            if len(found) >= limit:
                break
            facts = None if self.store.suppressed(name) else self.candidate(name)
            viable = (
                isinstance(facts, dict) and not facts.get("shared")
                and (action in _CREATED or facts.get("top") is True)
                and (facts.get("description") or self.setting(
                    "qualification.allow_model_review", None) is not False)
                and not self.store.seen(name, str(facts.get("content_hash") or ""))
            )
            with self._lock:
                state = self._sessions.get(session_id)
                if state is not None and state["candidates"].get(name) == action:
                    state["candidates"].pop(name)  # Evaluated; a new fact re-adds it.
            if viable:
                assert isinstance(facts, dict)
                found.append({**facts, "name": name, "action": action})
        return found

    # Background fork -----------------------------------------------------------------------
    def _fork_check(self, token: str, session_id: str, route: dict[str, Any],
                    candidates: dict[str, str]) -> None:
        """Fork the session agent over the viable candidates; it may suggest one."""
        offer: dict[str, Any] | None = None
        try:
            offered = self._viable(session_id, candidates, limit=FORK_CANDIDATES)
            if not offered:
                return
            for item in offered:
                self.store.record("candidate", item["name"], action=item["action"],
                                  content_hash=item["content_hash"], session_id=session_id)
                self.store.record("asked", item["name"], content_hash=item["content_hash"],
                                  via="fork")
            offer = {"session_id": session_id, "route": route, "suggested": None,
                     "closed": False, "skills": {item["name"]: item for item in offered},
                     "expires_at": self.clock() + PENDING_TTL_SECONDS}
            with self._lock:
                self._offer = offer  # Before the fork starts: it may call the tool at once.
            outcome = self._run_fork(token, offered)
            self._settle(offer, outcome)
            with self._lock:
                suggested = offer["suggested"]
            if suggested is not None:
                self._deliver(offer["skills"][suggested], str(offer.get("reason") or ""),
                              session_id, route)
            if outcome == "refused" and self._side_question_ready():
                self._fork_refused = True
                logger.info("Collective Wisdom: the host refused the suggestion fork; "
                            "using the side question until restart")
                with self._lock:
                    offer["closed"] = True
                    if self._offer is offer:
                        self._offer = None
                self._ask(token, session_id, route, {
                    name: item["action"] for name, item in offer["skills"].items()},
                    keep_on_silence=True)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom suggestion fork failed", exc_info=True)
        finally:
            with self._lock:
                if offer is not None:
                    offer["closed"] = True
                if self._offer is offer:
                    self._offer = None
            self._release()

    def _run_fork(self, token: str, offered: list[dict[str, Any]]) -> str:
        """Start the fork with only this plugin's tool and wait for its outcome."""
        fork = self.fork_session
        handle = None if fork is None else fork(
            token, fork_prompt(offered), tools=(TOOL,), max_iterations=FORK_MAX_ITERATIONS,
            task=self.fork_task, timeout=FORK_TIMEOUT,
        )
        if handle is None:
            return "refused"
        if not handle.wait(FORK_TIMEOUT + 30.0):
            handle.cancel()  # Still deferred or stuck: give up; the next due turn may retry.
        return str(getattr(handle, "outcome", "") or "error")

    def _settle(self, offer: dict[str, Any], outcome: str) -> None:
        """A finished fork judged every offered skill; an interrupted one judged none."""
        with self._lock:
            offer["closed"] = True
            suggested = offer["suggested"]
        for name, item in offer["skills"].items():
            if name == suggested:
                continue
            if outcome == "ok":
                self.store.mark_seen(name, item["content_hash"], "asked")
                self.store.record("verdict", name, share=False, via="fork")
                continue
            self.store.record("not_asked", name, why=outcome)
            with self._lock:  # Ask again on a later due turn of this session.
                if not self._closed:
                    pending = self._session_state(offer["session_id"])["candidates"]
                    if pending.get(name) not in _CREATED:
                        pending[name] = item["action"]

    def _deliver(self, chosen: dict[str, Any], reason: str, session_id: str,
                 route: dict[str, Any]) -> bool:
        name, content_hash = chosen["name"], chosen["content_hash"]
        if not self.store.reserve_today(self._daily_limit()):
            self.store.forget_seen(name, content_hash)
            self.store.record("not_shown", name, why="daily_limit")
            return False
        suggestion = {
            "id": secrets.token_urlsafe(12), "skill": name,
            "description": clean_text(chosen.get("description")), "reason": reason,
            "content_hash": content_hash, "session_id": session_id, "route": route,
            "state": "pending", "created_at": self.clock(),
            "expires_at": self.clock() + PENDING_TTL_SECONDS,
        }
        self.store.add_pending(suggestion)
        accepted = False
        if callable(self.inject):
            try:
                accepted = _inject(self.inject, approval_prompt(name), route)
            except Exception:  # pylint: disable=broad-exception-caught
                accepted = False
        if accepted:
            self.store.record("injected", name, surface=route.get("platform"))
            return True
        # Denied or unavailable: nothing was dismissed, and no retry today.
        self.store.set_pending_state(suggestion["id"], "undelivered")
        self.store.forget_seen(name, content_hash)
        self.store.block_today()
        self.store.record("not_shown", name, why="injection_unavailable")
        return False

    # Model tool ----------------------------------------------------------------------------
    def tool(self, args: dict[str, Any], **kwargs: Any) -> str:
        """In this plugin's fork, suggest one offered skill; live, show its pending card."""
        skill = _name(args.get("skill"))
        origin = kwargs.get("turn_origin")
        if origin is not None:
            raw = args.get("reason")
            reason = clean_text(raw, _REASON_LIMIT) if isinstance(raw, str) else ""
            return self._suggest(skill, reason, str(origin), str(kwargs.get("session_id") or ""))
        suggestion: dict[str, Any] | None = None
        if skill and not self._closed:
            suggestion = self.store.claim_pending(
                skill, session_id=str(kwargs.get("session_id") or ""),
                session_key=str(self.session().get("session_key") or ""),
            )
        if not isinstance(suggestion, dict):
            raise PermissionError(
                "No Collective Wisdom suggestion is waiting for that skill in this conversation."
            )
        route = suggestion["route"]
        if route.get("kind") == "messaging":
            delivery = self.present(route, suggestion["session_id"], suggestion)
            if not isinstance(delivery, dict) or delivery.get("state") != "queued":
                self.store.set_pending_state(suggestion["id"], "undelivered")
                self.store.record("not_shown", skill, why="card_unavailable")
                raise PermissionError("The Collective Wisdom card could not be shown here.")
            self.store.record("shown", skill, surface=route.get("platform"))
            return json.dumps({
                "status": "queued", "executed": False,
                "instruction": ("The Collective Wisdom card appears after your reply. Tell the "
                                "user in one short sentence; do not share anything yourself."),
            }, sort_keys=True)
        self.store.record("shown", skill, surface=route.get("platform") or "local")
        return json.dumps({
            "status": "shown", "executed": False, "card": local_card(suggestion, self.profile),
            "instruction": ("Show the user the card text exactly as written. Do not run any of "
                            "its commands; the user chooses."),
        }, sort_keys=True)

    def _offered(self, offer: dict[str, Any] | None, skill: str,
                 session_id: str) -> dict[str, Any] | None:
        """The facts of ``skill`` when the running fork may still suggest it; call locked."""
        if offer is None or self._closed or offer["closed"] or offer["suggested"] is not None:
            return None
        if offer["expires_at"] <= self.clock() or session_id not in {"", offer["session_id"]}:
            return None
        return offer["skills"].get(skill)

    def _suggest(self, skill: str, reason: str, origin: str, session_id: str) -> str:
        """The fork's one suggestion: record it; _fork_check delivers it after the fork."""
        if origin != self.fork_origin:
            raise PermissionError(
                "Only Collective Wisdom's own background check can suggest a skill.")
        with self._lock:
            offer = self._offer
            item = self._offered(offer, skill, session_id)
            if offer is None or item is None:
                raise PermissionError(
                    "Collective Wisdom can suggest only one of the skills it asked about, once.")
            offer["suggested"] = skill
            offer["reason"] = reason
        self.store.mark_seen(skill, item["content_hash"], "asked")
        self.store.record("verdict", skill, share=True, reason=reason or None, via="fork")
        # Delivery waits until the fork has ended (see _fork_check): a turn started from inside
        # the fork would inherit its context and cancel the fork that started it.
        return json.dumps({
            "status": "suggested", "executed": False,
            "instruction": "Done. Don't call this tool again; finish with one short line.",
        }, sort_keys=True)


def _thread(work: Callable[[], None]) -> None:
    """Ask off the turn thread: a side question blocks up to its timeout, a fork until done."""
    threading.Thread(target=work, name="wisdom-auto-suggest", daemon=True).start()


def suggestions_text(store: SuggestionStore, profile: str = "default",
                     plugin_id: str = "collective-wisdom") -> str:
    """What Hermes suggested and decided, and how to undo Never."""
    value = store.snapshot()
    command = hermes_command(profile)
    lines = ["Collective Wisdom: automatic share suggestions", ""]
    never = sorted(value["suppressed"])
    lines.append("Never suggested: " + (", ".join(never) if never else "none"))
    if never:
        lines.append("To allow one again: /wisdom suggestions allow <skill>")
    lines.extend(("", "Recent decisions:"))
    recent = [row for row in value["decisions"] if row.get("event") != "candidate"][-10:]
    lines.extend(f"  {row.get('at', '')[:16].replace('T', ' ')}  {row.get('skill')}  "
                 + _decision_label(row) for row in reversed(recent))
    if not recent:
        lines.append("  none yet")
    lines.extend(("", "To turn suggestions off: "
                  f"{command} config set plugins.entries.{plugin_id}.settings.{SETTING} false"))
    return "\n".join(lines)


def _decision_label(row: Mapping[str, Any]) -> str:
    event = str(row.get("event") or "")
    if event == "verdict":
        return "Hermes said " + ("share" if row.get("share") else "don't share")
    if event == "choice":
        return {"share": "you chose Confirm share", "dismiss": "you chose Don't share",
                "never": "you chose Never for this skill",
                "allow": "you allowed suggestions again"}.get(str(row.get("choice")), event)
    return {"asked": "asked Hermes", "injected": "suggested in chat", "shown": "card shown",
            "not_shown": "not shown (" + str(row.get("why") or "unavailable") + ")",
            "not_asked": "check stopped (" + str(row.get("why") or "unavailable") + ")"}.get(
                event, event)


SUGGESTIONS_USAGE = (
    "Usage: /wisdom suggestions [list | dismiss <skill> | never <skill> | allow <skill>]"
)


def run_suggestions(  # pylint: disable=too-many-return-statements,too-many-arguments
    action: str, skill: str, store: SuggestionStore, *,
    current_hash: Callable[[str], str | None], profile: str = "default",
    plugin_id: str = "collective-wisdom", source: str,
) -> str:
    """Read the log, or record Don't share / Never / allow for one skill."""
    if action in {"", "list"}:
        return suggestions_text(store, profile, plugin_id) if not skill else SUGGESTIONS_USAGE
    name = _name(skill)
    if action not in {"dismiss", "never", "allow"} or not name:
        return SUGGESTIONS_USAGE
    if action == "never":
        store.set_suppressed(name, True, source=source)
        return (f"Hermes won't suggest sharing {name} again. "
                f"To undo: /wisdom suggestions allow {shlex.quote(name)}")
    if action == "allow":
        changed = store.set_suppressed(name, False, source=source)
        return (f"Hermes may suggest sharing {name} again." if changed
                else f"{name} wasn't blocked from suggestions.")
    latest = store.latest(name)
    content_hash = (latest or {}).get("content_hash") or current_hash(name)
    if not content_hash:
        return f"Couldn't find your local skill {name}. Nothing was changed."
    store.dismiss(name, str(content_hash), source=source)
    return f"Not shared. Hermes won't suggest this version of {name} again."


SUGGESTIONS_COMMAND = "suggestions"
SUGGESTIONS_ACTIONS = ("list", "dismiss", "never", "allow")


def suggestions_refusal() -> str:
    """Refuse the suggestions log outside the owner's own sessions, naming no skill."""
    return ("Your Collective Wisdom suggestions are private. Open /wisdom suggestions in a "
            "private chat with Hermes; nothing was shown here.")


def _service_store(service: Any) -> SuggestionStore:
    return SuggestionStore(getattr(service, "_state"), Path(getattr(service, "root")))


def _service_hash(service: Any) -> Callable[[str], str | None]:
    def current(name: str) -> str | None:
        facts = service.share_candidate(name)
        return str(facts["content_hash"]) if isinstance(facts, dict) else None
    return current


def _service_run(service: Any, action: str, skill: str, source: str) -> str:
    profile = getattr(service, "profile", ("default", None))[0]
    return run_suggestions(
        action, skill, _service_store(service), current_hash=_service_hash(service),
        profile=profile, plugin_id=getattr(service, "plugin_id", "collective-wisdom"),
        source=source,
    )


def slash_suggestions(args: list[str], service: Any) -> str:
    """``/wisdom suggestions [action] [skill]`` in the owner's private session."""
    if len(args) > 2 or (args and args[0].casefold() not in SUGGESTIONS_ACTIONS):
        return SUGGESTIONS_USAGE
    action = args[0].casefold() if args else "list"
    return _service_run(service, action, args[1] if len(args) == 2 else "", "slash")


def run_suggestions_cli(args: Any, service: Any) -> int:
    """``hermes wisdom suggestions [action] [skill]``."""
    action = str(getattr(args, "suggestions_action", "") or "list")
    skill = str(getattr(args, "skill", "") or "")
    text = _service_run(service, action, skill, "cli")
    print(text)
    return 2 if text == SUGGESTIONS_USAGE else 0
