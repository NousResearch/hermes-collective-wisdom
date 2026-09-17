"""Invocation-scoped TUI contribution editing and publication reviews.

The host bridge can render and scroll complete bounded text plus one deliberate
action. It cannot edit arbitrary files. This adapter therefore exports/imports
the existing complete JSON draft format, reviews the complete resulting payload
before a private save, and uses the existing hash-bound publication receipt for
the single final submit confirmation. Oversized reviews fail closed to the
terminal workflow rather than truncating bytes the owner is asked to approve.
"""

from __future__ import annotations

import json
import re
import shlex
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .commands import _read_prepared_payload  # pylint: disable=protected-access
from .errors import WisdomConflict

# Small command adapters share the host's review boundary explicitly.
# pylint: disable=too-many-positional-arguments


MAX_TUI_REVIEW_CHARS = 65_536
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_CANDIDATE_USAGE = (
    "Usage: /wisdom candidate <qualified-event-id> OR "
    "/wisdom candidate <candidate-id> <share|portal|later>"
)
_APPROVAL_USAGE = "Usage: /wisdom publication approve <receipt>"
_UNAVAILABLE = (
    "Interactive TUI review is unavailable. No further action was taken; "
    "inspect private state before retrying in a fully interactive terminal."
)
_CANCELLED = "Review cancelled. Nothing was changed."
_OVERSIZED = (
    "The TUI cannot display the complete exact review without truncation. "
    "Use the terminal `hermes wisdom` contribution workflow instead. Nothing was changed."
)
_CANDIDATE_OVERSIZED = (
    "The complete exact candidate review is too large for the TUI. "
    "No approval is available and nothing was published."
)
_CANDIDATE_UNAVAILABLE = (
    "This candidate action is no longer available. No action was taken."
)
_CANDIDATE_APPROVAL_UNAVAILABLE = (
    "This candidate publication approval is no longer available. No action was taken."
)


class _InvocationEnded(RuntimeError):
    """Internal fail-closed signal for a stale interactive invocation."""


def _require_current(may_apply: Callable[[], bool]) -> None:
    try:
        current = may_apply() is True
    except Exception as exc:  # authority callbacks fail closed
        raise _InvocationEnded from exc
    if not current:
        raise _InvocationEnded


def _read(
    service_method: Callable[..., Any], *args: Any,
    may_apply: Callable[[], bool], **kwargs: Any,
) -> Any:
    _require_current(may_apply)
    result = service_method(*args, may_apply=may_apply, **kwargs)
    _require_current(may_apply)
    return result


def _safe_identifier(value: object, placeholder: str = "<draft-id>") -> str:
    # This tiny renderer-local escaping rule is shared in shape, not state.
    # pylint: disable=duplicate-code
    text = str(value or "")
    if not text or any(
        unicodedata.category(character).startswith("C") for character in text
    ):
        return placeholder
    return shlex.quote(text)


def _words(raw_args: str) -> list[str]:
    try:
        return shlex.split(raw_args)
    except ValueError:
        return []


def handles_tui_contribution(raw_args: str) -> bool:
    """Return whether the complete contribution adapter owns this syntax."""

    words = _words(raw_args)
    if not words:
        return False
    command = words[0].casefold()
    if command in {"candidate", "suggest", "publish"}:
        return True
    if command == "prepared" and len(words) > 1:
        return words[1].casefold() in {"inspect", "save", "review"}
    if command == "publication" and len(words) > 1:
        # A bare receipt must never bypass the complete interactive review.
        return words[1].casefold() in {"approve", "review", "status", "reopen", "decline"}
    return False


def handles_plugin_only_candidate_action(raw_args: str) -> bool:
    """Recognize deliberate stock-TUI actions that need no host review API."""

    words = _words(raw_args)
    if not words:
        return False
    command = words[0].casefold()
    if command == "candidate":
        # Preserve the existing two-word candidate preparation route. Any other
        # candidate form belongs here so malformed action syntax gets safe usage.
        return len(words) != 2
    return command == "publication" and len(words) > 1 and words[1].casefold() == "approve"


def _json(value: object, *, pretty: bool = False) -> str:
    """Render complete literal JSON with all controls escaped."""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _selected(result: Any, outcome_type: Any, action_id: str) -> bool:
    return (
        getattr(result, "outcome", None) is outcome_type.SELECTED
        and getattr(result, "action_id", None) == action_id
    )


def _review(  # pylint: disable=too-many-arguments
    *, title: str, body: object, action_id: str, label: str,
    request_review: Callable[..., Any], action_type: Any, outcome_type: Any,
    may_apply: Callable[[], bool],
) -> tuple[bool, str | None]:
    literal = _json(body, pretty=True)
    if len(literal) > MAX_TUI_REVIEW_CHARS:
        return False, _OVERSIZED
    result = request_review(
        title=title,
        body=literal,
        actions=[action_type(action_id, label)],
    )
    if not _selected(result, outcome_type, action_id):
        outcome = getattr(result, "outcome", None)
        if outcome is outcome_type.CANCELLED:
            return False, _CANCELLED
        return False, _UNAVAILABLE
    if may_apply() is not True:
        return False, _UNAVAILABLE
    return True, None


def _prepared_payload_text(value: dict[str, Any]) -> str:
    literal = _json(value)
    if len(literal) > MAX_TUI_REVIEW_CHARS:
        draft_id = _safe_identifier(value.get("draft_id"))
        return (
            "The TUI cannot display the complete editable draft without truncation. "
            "Nothing was uploaded. Export and edit it in a terminal instead:\n"
            f"hermes wisdom prepared inspect {draft_id} --json"
        )
    return (
        literal
        + "\n\nNothing was uploaded. To edit add/remove/rename/file contents or the manifest, "
        "save this JSON payload to a private regular file, keep draft_id and "
        "expected_hashes unchanged, then run `hermes wisdom prepared save <payload.json>`."
    )


def _candidate(
    words: list[str], service: Any, may_apply: Callable[[], bool],
) -> str:
    if len(words) == 3:
        assessment_id, action = words[1], words[2].casefold()
        if action not in {"share", "portal", "later"} or not _ACTION_ID.fullmatch(
            assessment_id
        ):
            raise ValueError(_CANDIDATE_USAGE)
        try:
            result = _read(
                service.candidate_slash_action, assessment_id, action,
                may_apply=may_apply,
            )
        except (PermissionError, WisdomConflict):
            return _CANDIDATE_UNAVAILABLE
        literal = _json(result, pretty=True)
        if action != "share":
            return literal
        if len(literal) > MAX_TUI_REVIEW_CHARS:
            return _CANDIDATE_OVERSIZED
        review = result.get("review") if isinstance(result, dict) else None
        receipt = review.get("receipt") if isinstance(review, dict) else None
        if not isinstance(receipt, str) or not _ACTION_ID.fullmatch(receipt):
            raise ValueError("Candidate publication review is invalid")
        command = shlex.join(("/wisdom", "publication", "approve", receipt))
        return (
            literal
            + "\n\nNothing was published. After reviewing every exact field above, "
            + f"approve this receipt in a separate TUI action: {command}"
        )
    if len(words) != 2:
        raise ValueError(_CANDIDATE_USAGE)
    _require_current(may_apply)
    draft_id = service.native_candidate_prepare(words[1], may_apply=may_apply)
    return _prepared_payload_text(
        _read(service.prepared_inspect, draft_id, may_apply=may_apply)
    )


def _suggest(  # pylint: disable=too-many-arguments
    words: list[str], service: Any, request_review: Callable[..., Any],
    action_type: Any, outcome_type: Any, may_apply: Callable[[], bool],
) -> str:
    allow_model = "--allow-model-review" in words[2:]
    extras = [word for word in words[2:] if word != "--allow-model-review"]
    if len(words) < 2 or extras:
        raise ValueError("Usage: /wisdom suggest <skill> [--allow-model-review]")
    _require_current(may_apply)
    prepared = service.suggest(
        words[1], allow_model_review=False, may_apply=may_apply
    )
    _require_current(may_apply)
    if allow_model:
        approved, _message = _review(
            title="Send exact private Wisdom draft for model review",
            body={
                "disclosure": (
                    "The complete exact owner-private package below will be sent to the "
                    "current model provider for editorial and professionalism review."
                ),
                "draft": prepared,
            },
            action_id="model-review",
            label="Send exact private draft for model review",
            request_review=request_review,
            action_type=action_type,
            outcome_type=outcome_type,
            may_apply=may_apply,
        )
        if not approved:
            return (
                f"{_prepared_payload_text(prepared)}\n\nModel disclosure was cancelled. "
                "The owner-private draft remains prepared; inspect it before retrying."
            )
        prepared = _read(
            service.prepared_review, prepared["draft_id"],
            allow_model_review=True, reviewed_hashes=prepared.get("hashes"),
            may_apply=may_apply,
        )
    return _prepared_payload_text(prepared)


def _prepared(  # pylint: disable=too-many-arguments
    words: list[str], service: Any, request_review: Callable[..., Any],
    action_type: Any, outcome_type: Any, may_apply: Callable[[], bool],
) -> str:
    if len(words) < 3:
        raise ValueError("Usage: /wisdom prepared <inspect|save|review> <target>")
    action, target = words[1].casefold(), words[2]
    if action == "inspect" and len(words) == 3:
        return _prepared_payload_text(
            _read(service.prepared_inspect, target, may_apply=may_apply)
        )
    if action == "save" and len(words) == 3:
        payload = _read_prepared_payload(Path(target))
        current = _read(
            service.prepared_inspect, payload["draft_id"], may_apply=may_apply
        )
        approved, message = _review(
            title="Save exact private Wisdom draft",
            body={
                "network_submission": False,
                "current_hashes": current.get(
                    "expected_hashes", current.get("hashes")
                ),
                "resulting_payload": payload,
            },
            action_id="save",
            label="Save exact private draft",
            request_review=request_review,
            action_type=action_type,
            outcome_type=outcome_type,
            may_apply=may_apply,
        )
        if not approved:
            return str(message)
        result = service.prepared_save(
            payload["draft_id"],
            author_description=payload["author_description"],
            files=payload["files"],
            expected_hashes=payload["expected_hashes"],
            may_apply=may_apply,
        )
        return _json(result)
    if action == "review":
        allow_model = words[3:] == ["--allow-model-review"]
        if len(words) != 3 and not allow_model:
            raise ValueError(
                "Usage: /wisdom prepared review <draft> [--allow-model-review]"
            )
        if not allow_model:
            return _json(_read(
                service.prepared_review, target, allow_model_review=False,
                reviewed_hashes=None, may_apply=may_apply,
            ))
        current = _read(service.prepared_inspect, target, may_apply=may_apply)
        approved, message = _review(
            title="Send exact private Wisdom draft for model review",
            body={
                "disclosure": (
                    "The complete exact owner-private package below will be sent to the "
                    "current model provider for editorial and professionalism review."
                ),
                "draft": current,
            },
            action_id="model-review",
            label="Send exact private draft for model review",
            request_review=request_review,
            action_type=action_type,
            outcome_type=outcome_type,
            may_apply=may_apply,
        )
        if not approved:
            return str(message)
        return _json(_read(
            service.prepared_review, target, allow_model_review=True,
            reviewed_hashes=current.get("expected_hashes", current.get("hashes")),
            may_apply=may_apply,
        ))
    raise ValueError("Usage: /wisdom prepared <inspect|save|review> <target>")


def _publication_review(  # pylint: disable=too-many-arguments
    draft_id: str, service: Any, request_review: Callable[..., Any],
    action_type: Any, outcome_type: Any, may_apply: Callable[[], bool],
) -> str:
    review = _read(service.publication_review, draft_id, may_apply=may_apply)
    approved, message = _review(
        title="Publish exact Collective Wisdom contribution",
        body=review,
        action_id="publish",
        label="Submit exact reviewed contribution",
        request_review=request_review,
        action_type=action_type,
        outcome_type=outcome_type,
        may_apply=may_apply,
    )
    if not approved:
        return str(message)
    return _json(service.publication_submit(review["receipt"], may_apply=may_apply))


def _publication(  # pylint: disable=too-many-arguments
    words: list[str], service: Any, request_review: Callable[..., Any],
    action_type: Any, outcome_type: Any, may_apply: Callable[[], bool],
) -> str:
    if len(words) == 3 and words[1].casefold() == "approve":
        receipt = words[2]
        if not _ACTION_ID.fullmatch(receipt):
            raise ValueError(_APPROVAL_USAGE)
        try:
            return _json(_read(
                service.candidate_publication_approve, receipt, may_apply=may_apply
            ))
        except (PermissionError, WisdomConflict):
            return _CANDIDATE_APPROVAL_UNAVAILABLE
    if words[1:2] == ["approve"]:
        raise ValueError(_APPROVAL_USAGE)
    if len(words) != 3:
        raise ValueError(
            "Usage: /wisdom publication <approve|review|status|reopen|decline> <target>"
        )
    action, target = words[1].casefold(), words[2]
    if action == "review":
        return _publication_review(
            target, service, request_review, action_type, outcome_type, may_apply
        )
    if action == "status":
        return _json(_read(service.publication_status, target, may_apply=may_apply))
    if action in {"reopen", "decline"}:
        status = _read(service.publication_status, target, may_apply=may_apply)
        label = (
            "Reconcile exact publication"
            if action == "reopen"
            else "Decline exact remote draft"
        )
        approved, message = _review(
            title=(
                "Reconcile Collective Wisdom publication"
                if action == "reopen"
                else "Decline Collective Wisdom publication"
            ),
            body=status,
            action_id=action,
            label=label,
            request_review=request_review,
            action_type=action_type,
            outcome_type=outcome_type,
            may_apply=may_apply,
        )
        if not approved:
            return str(message)
        result = (
            service.publication_reopen(target, may_apply=may_apply)
            if action == "reopen"
            else service.publication_decline(target, may_apply=may_apply)
        )
        return _json(result)
    raise ValueError(
        "Usage: /wisdom publication <approve|review|status|reopen|decline> <target>"
    )


def run_tui_contribution(  # pylint: disable=too-many-arguments
    raw_args: str,
    service: Any,
    request_review: Callable[..., Any],
    action_type: Any,
    outcome_type: Any,
    *,
    may_apply: Callable[[], bool],
) -> str:
    """Run one complete contribution command through the host review bridge."""

    words = _words(raw_args)
    if not words:
        raise ValueError("Invalid Wisdom contribution command")
    command = words[0].casefold()
    try:
        _require_current(may_apply)
        if command == "candidate":
            return _candidate(words, service, may_apply)
        if command == "suggest":
            return _suggest(
                words, service, request_review, action_type, outcome_type, may_apply
            )
        if command == "prepared":
            return _prepared(
                words, service, request_review, action_type, outcome_type, may_apply
            )
        if command == "publish" and len(words) == 2:
            return _publication_review(
                words[1], service, request_review, action_type, outcome_type, may_apply
            )
        if command == "publication":
            return _publication(
                words, service, request_review, action_type, outcome_type, may_apply
            )
    except _InvocationEnded:
        return _UNAVAILABLE
    raise ValueError("Unsupported Wisdom contribution command")
