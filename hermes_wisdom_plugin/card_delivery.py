"""Post a Collective Wisdom card into a conversation with no model turn.

Hermes hosts with plugin conversation cards offer ``ctx.post_card(card, *, session_key=None,
origin=None)``: the card (``id``, ``text``, optional transcript ``directive``) is saved in the
session's history at the next turn boundary, Desktop renders the directive this plugin's Desktop
half registers, other local surfaces print the text, and on Slack and Telegram core only records
the row while this plugin sends its own native card. It never starts a model turn.

``CardPoster`` is the one seam every Collective Wisdom card raised by background work goes
through: share suggestions today; install suggestions from the background skill review and
curator-driven promotion later (the latter targets the owner with no session, which core does not
offer yet). It is used only when the host has the method and the user granted
``conversation.cards``; otherwise callers keep their older delivery (an injected turn).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

CARD_CAPABILITY = "conversation.cards"
POSTED = "posted"
REFUSED = "refused"
UNCERTAIN = "uncertain"
_ACCEPTED = frozenset({"queued", "posted", "duplicate"})
# Core may have written the card before the hand-off timed out; the card id makes a re-post a
# duplicate, so the caller must not deliver the same suggestion another way.
_UNCERTAIN_ERRORS = frozenset({"timeout"})


class CardPoster:  # pylint: disable=too-few-public-methods
    """Feature-detected ``ctx.post_card``, gated by the ``conversation.cards`` grant."""

    def __init__(self, post_card: Callable[..., Any] | None,
                 has_capability: Callable[[str], bool]) -> None:
        self._post_card = post_card if callable(post_card) else None
        self._has_capability = has_capability

    def ready(self) -> bool:
        """Whether a card can be posted now: the host offers it and the user granted it."""
        if self._post_card is None:
            return False
        try:
            return self._has_capability(CARD_CAPABILITY) is True
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    def post(self, card_id: str, text: str, *, directive: Mapping[str, Any] | None = None,
             session_key: str = "") -> str:
        """Post one card into ``session_key``; ``posted``, ``uncertain`` or ``refused``.

        ``refused`` means core wrote nothing, so the caller may deliver another way.
        """
        if self._post_card is None or not session_key:
            return REFUSED
        card: dict[str, Any] = {"id": card_id, "text": text}
        if directive is not None:
            card["directive"] = {"name": str(directive["name"]),
                                 "attrs": dict(directive.get("attrs") or {})}
        try:
            result = self._post_card(card, session_key=session_key)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Collective Wisdom: posting a card failed", exc_info=True)
            return REFUSED
        if not isinstance(result, Mapping):
            return REFUSED
        if result.get("ok") is True and result.get("status") in _ACCEPTED:
            return POSTED
        error = str(result.get("error") or "unknown")
        logger.info("Collective Wisdom: card not posted (%s)", error)
        return UNCERTAIN if error in _UNCERTAIN_ERRORS else REFUSED
