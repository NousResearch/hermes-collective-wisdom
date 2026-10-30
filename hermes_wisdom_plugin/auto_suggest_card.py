"""The Slack/Telegram card for one automatic share suggestion.

Three choices, bound to the owner's private chat: Confirm share turns this card
into the normal share review in place (package, scanner findings, acknowledge,
exact consent); Don't share dismisses this revision; Never for this skill stops
suggestions for it until the owner allows it again. The callback allowlist is
the projected view, so an old or forged control is refused.
"""
# pylint: disable=protected-access

from __future__ import annotations

import copy
from typing import Any

from .auto_suggest import SuggestionStore
from .errors import WisdomConflict, WisdomValidationError
from .native_review import (
    OPERATION_CARDS, NativeAction, NativeItem, NativeView, OperationCard, _safe,
)
from .top_skills import clean_text

OPERATION = "auto_suggest"
SHARE = "autosuggest.share"
DISMISS = "autosuggest.dismiss"
NEVER = "autosuggest.never"
CONTROLS = (("Confirm share", SHARE), ("Don't share", DISMISS), ("Never for this skill", NEVER))


def card_plan(suggestion: dict[str, Any]) -> dict[str, Any]:
    """The facts the card shows and the choice handlers need."""
    return {
        "operation": OPERATION, "allowed": False,
        "skill_name": clean_text(suggestion.get("skill"), 64),
        "description": clean_text(suggestion.get("description")),
        "reason": clean_text(suggestion.get("reason"), 160),
        "content_hash": str(suggestion.get("content_hash") or ""),
        "suggestion_id": str(suggestion.get("id") or ""),
    }


def auto_suggest_view(record: dict[str, Any]) -> NativeView:
    """Pending: the three choices. Done: one line that says what happened."""
    raw_plan, raw_result = record.get("plan"), record.get("result")
    plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
    name = _safe(plan.get("skill_name"))
    choice = raw_result.get("choice") if isinstance(raw_result, dict) else None
    if record.get("state") == "completed" and choice == "dismiss":
        return NativeView("Not shared", f"Hermes won't suggest this version of {name} again.",
                          dismissed=True)
    if record.get("state") == "completed" and choice == "never":
        return NativeView("Not shared", f"Hermes won't suggest {name} again. To undo: "
                          f"/wisdom suggestions allow {name}", dismissed=True)
    if record.get("state") != "pending":
        return NativeView("Hermes Collective Wisdom", f"Suggestion for {name} closed.",
                          dismissed=True)
    detail = [line for line in (
        plan.get("description") and _safe(plan.get("description")),
        plan.get("reason") and "Hermes's reason: " + _safe(plan.get("reason")),
    ) if line]
    return NativeView(
        "📤 Share this skill?",
        f"Hermes thinks {name} could help your team.",
        [NativeItem(name, "\n".join(detail), literal_detail=True)],
        actions=[NativeAction("Confirm share", SHARE, primary=True),
                 NativeAction("Don't share", DISMISS),
                 NativeAction("Never for this skill", NEVER, row=1)],
        notice="Nothing is shared until you approve the share review.",
    )


def _store(controller: Any) -> SuggestionStore:
    return SuggestionStore(controller.state.state, controller.state.root, clock=controller.clock)


def act_auto_suggest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    controller: Any, record: dict[str, Any], action: str, service: Any, invocation: Any,
) -> dict[str, Any]:
    """Apply one owner choice exactly once; the record is already owner-checked."""
    if action in {DISMISS, NEVER}:
        return _decline(controller, record, "dismiss" if action == DISMISS else "never",
                        invocation.platform)
    if action != SHARE:
        raise WisdomValidationError("Collective Wisdom action was not recognized")
    return _confirm(controller, record, service, invocation)


def _decline(controller: Any, record: dict[str, Any], choice: str, source: str) -> dict[str, Any]:
    plan = record["plan"]
    done = {**record, "state": "completed", "result": {"choice": choice},
            "display_ready": False, "updated_at": controller.clock()}
    if not controller.state.compare_replace(record, done):
        raise WisdomConflict("Collective Wisdom review changed; open it again")
    if choice == "dismiss":
        _store(controller).dismiss(plan["skill_name"], plan["content_hash"], source=source)
    else:
        _store(controller).set_suppressed(plan["skill_name"], True, source=source)
    return copy.deepcopy(done)


def _confirm(controller: Any, record: dict[str, Any], service: Any,
             invocation: Any) -> dict[str, Any]:
    """Turn this card into the normal share review, in place."""
    plan = record["plan"]
    claimed = {**record, "state": "preparing", "effect_action": SHARE,
               "updated_at": controller.clock()}
    if not controller.state.compare_replace(record, claimed):
        raise WisdomConflict("Collective Wisdom review changed; open it again")
    try:
        review = controller.request_suggest(service, invocation, plan["skill_name"])
    except Exception:
        controller.state.compare_replace(claimed, record)
        raise
    # The share review takes over this card; its unposted twin is retired.
    merged = {**review, "id": record["id"], "message_id": record["message_id"],
              "created_at": record["created_at"], "display_ready": False,
              "auto_suggest": copy.deepcopy(plan)}
    replaced = controller.state.compare_replace(claimed, merged)
    controller.state.compare_replace(review, {**review, "state": "cancelled"})
    if not replaced:
        raise WisdomConflict("Collective Wisdom review changed; open it again")
    _store(controller).record("choice", plan["skill_name"], choice="share",
                              source=invocation.platform)
    return copy.deepcopy(merged)


OPERATION_CARDS[OPERATION] = OperationCard(auto_suggest_view, act_auto_suggest)
