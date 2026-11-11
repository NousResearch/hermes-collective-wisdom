"""The Slack/Telegram card for one automatic share suggestion.

Three choices, bound to the owner's private chat: Share turns this card into the
normal share review in place (package, scanner findings, acknowledge, exact
consent); Not now dismisses this revision; Don't ask again for this skill stops
suggestions for it until the owner allows it again. The callback allowlist is
the projected view, so an old or forged control is refused.
"""
# pylint: disable=protected-access

from __future__ import annotations

import copy
from typing import Any

from .auto_suggest import SuggestionStore
from .errors import ShareCopyError, WisdomConflict, WisdomValidationError
from .native_review import (
    _SHARE_HANDLING, OPERATION_CARDS, NativeAction, NativeItem, NativeView, OperationCard,
    _refused_preparation, _safe, _suggest_prepared_view,
)
from .package import SkillRefusal
from .publication_copy import publication_copy
from .top_skills import clean_text

OPERATION = "auto_suggest"
SHARE = "autosuggest.share"
DISMISS = "autosuggest.dismiss"
NEVER = "autosuggest.never"
CONTROLS = (("Share", SHARE), ("Not now", DISMISS), ("Don't ask again for this skill", NEVER))
LABELS = dict((action, label) for label, action in CONTROLS)
QUESTION = "Share it with your team?"


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
        # Not now, then Share rightmost; Don't ask again is a quiet link below them.
        actions=[NativeAction(LABELS[DISMISS], DISMISS, align="right"),
                 NativeAction(LABELS[SHARE], SHARE, primary=True, align="right"),
                 NativeAction(LABELS[NEVER], NEVER, row=1, link=True, align="right")],
        prompt=QUESTION,
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


# The same card in the Desktop chat (the plugin's ``::wisdom-share{id="..."}`` directive).
# The suggestion row in the SuggestionStore is the card's only record; the copy is the
# messaging card's, projected from ``auto_suggest_view``.
DESKTOP_SOURCE = "desktop"
DESKTOP_CHOICES = {SHARE: "share", DISMISS: "dismiss", NEVER: "never"}


def _desktop_open(store: SuggestionStore, row: dict[str, Any]) -> bool:
    return (row.get("state") == "presented" and row.get("expires_at", 0) > store.clock()
            and not store.suppressed(str(row.get("skill") or "")))


def _desktop_record(store: SuggestionStore, row: dict[str, Any]) -> dict[str, Any]:
    """The native record ``auto_suggest_view`` projects for this suggestion row."""
    plan = card_plan(row)
    if _desktop_open(store, row):
        return {"state": "pending", "plan": plan}
    if row.get("state") == "dismissed":
        return {"state": "completed", "plan": plan, "result": {"choice": "dismiss"}}
    if row.get("state") == "never" or store.suppressed(plan["skill_name"]):
        return {"state": "completed", "plan": plan, "result": {"choice": "never"}}
    return {"state": "closed", "plan": plan}


def _view_json(view: NativeView) -> dict[str, Any]:
    # Retry on a "Skill needs attention" card prepares the draft again, like Share.
    choices = {**DESKTOP_CHOICES, "suggest.refresh": "share"}
    return {
        "title": view.title, "summary": view.summary, "notice": view.notice,
        "prompt": view.prompt,
        "items": [{"title": item.title, "detail": item.detail} for item in view.items],
        "actions": [{"label": action.label, "choice": choices[action.action],
                     "primary": action.primary or action.action == "suggest.refresh",
                     "link": action.link}
                    for action in view.actions if action.action in choices],
    }


def _confirmed_card(row: dict[str, Any], publication: dict[str, Any] | None) -> dict[str, Any]:
    """After Share: the draft's review in the card, or how it ended (kept or submitted)."""
    name = _safe(clean_text(row.get("skill"), 64))
    draft = {"draft_id": str(row.get("draft_id") or ""), "skill": name}
    if isinstance(publication, dict) and publication.get("phase") == "policy_review_required":
        # Gateway's findings need an acknowledgement, which the Collective Wisdom page holds.
        return {"state": "review_required", **draft, **_view_json(NativeView(
            "Hermes Collective Wisdom", name, [NativeItem(
                "Security review needed", "This skill needs a security review before it is "
                "shared.")]))}
    if isinstance(publication, dict):
        title, detail = publication_copy(publication)
        return {"state": "settled", **_view_json(NativeView(
            "Hermes Collective Wisdom", name, [NativeItem(title, detail)]))}
    if row.get("kept"):
        # The messaging "Draft kept" card; on Desktop the draft resumes from Contribute.
        return {"state": "settled", **_view_json(NativeView(
            "Hermes Collective Wisdom", name, [NativeItem(
                "Draft kept", "This skill is still a local draft. Nothing was shared. "
                "Resume it from Contribute in Collective Wisdom.")]))}
    # The Desktop plugin walks the draft's share review inside this card.
    return {"state": "confirmed", **draft, "handling": dict(_SHARE_HANDLING),
            **_view_json(NativeView("Hermes Collective Wisdom",
                                    f"{name}: you chose {LABELS[SHARE]}."))}


def desktop_card(store: SuggestionStore, row: dict[str, Any],
                 publication: dict[str, Any] | None = None) -> dict[str, Any]:
    """The card the Desktop chat shows for one suggestion row, in the messaging card's words.

    ``publication`` is the retained outcome of the row's draft once it was submitted."""
    if row.get("state") == "confirmed":
        return _confirmed_card(row, publication)
    record = _desktop_record(store, row)
    return {"state": "pending" if record["state"] == "pending" else "settled",
            **_view_json(auto_suggest_view(record))}


def _keep(store: SuggestionStore, row: dict[str, Any]) -> None:
    """Cancel on the in-card share review: the private draft stays, nothing is shared."""
    if row.get("state") != "confirmed" or row.get("kept") or store.transition(
            str(row["id"]), "confirmed", "confirmed", kept=True) is None:
        raise WisdomConflict("Collective Wisdom suggestion is no longer open")


def desktop_answer(store: SuggestionStore, row: dict[str, Any], choice: str,
                   prepare: Any) -> dict[str, Any]:
    """Apply one owner choice from the Desktop card exactly once.

    Not now and Don't ask again write what ``/wisdom suggestions dismiss|never`` write. Share
    prepares the private draft through ``prepare(skill)`` (the Desktop share path) and keeps it
    on the row; the card then walks its review. A skill that cannot be shared as written says
    why in the card and stays open. Keep (the review's Cancel) keeps the draft unshared.
    """
    if choice == "keep":
        _keep(store, row)
        return desktop_card(store, store.get(str(row["id"])) or row)
    if choice not in DESKTOP_CHOICES.values():
        raise WisdomValidationError("Collective Wisdom action was not recognized")
    suggestion_id, name = str(row["id"]), str(row.get("skill") or "")
    content_hash = str(row.get("content_hash") or "")
    claimed = {"dismiss": "dismissed", "never": "never", "share": "preparing"}[choice]
    if not _desktop_open(store, row) or store.transition(
            suggestion_id, "presented", claimed) is None:
        raise WisdomConflict("Collective Wisdom suggestion is no longer open")
    if choice == "dismiss":
        store.dismiss(name, content_hash, source=DESKTOP_SOURCE)
    elif choice == "never":
        store.set_suppressed(name, True, source=DESKTOP_SOURCE)
    else:
        try:
            draft = prepare(name)
            draft_id = str((draft or {}).get("draft_id") or "")
            if not draft_id:
                raise WisdomValidationError("Collective Wisdom draft was not prepared")
        except (ShareCopyError, SkillRefusal) as exc:
            # Owner-fixable and plugin-authored: the messaging "Skill needs attention" card.
            store.transition(suggestion_id, "preparing", "presented")
            plan = (_refused_preparation(name, exc) if isinstance(exc, SkillRefusal) else {
                "skill_name": name, "editorial_status": "incomplete",
                "editorial_problem": str(exc)})
            return {"state": "attention", **_view_json(_suggest_prepared_view(plan))}
        except Exception:
            store.transition(suggestion_id, "preparing", "presented")
            raise
        store.transition(suggestion_id, "preparing", "confirmed", draft_id=draft_id)
        store.record("choice", name, choice="share", source=DESKTOP_SOURCE)
    current = store.get(suggestion_id)
    return desktop_card(store, current if current is not None else row)
