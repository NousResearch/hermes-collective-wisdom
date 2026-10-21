"""Author-facing copy for confirmed publication operation results."""
# pylint: disable=too-many-return-statements
from __future__ import annotations

from typing import Any


def publication_copy(result: dict[str, Any]) -> tuple[str, str]:
    """Interpret the service envelope, not a draft's unrelated local state."""
    state = result.get("state")
    if state == "done" and result.get("phase") not in {
        "publication_unknown", "approval_unknown", "revision_unknown",
    }:
        outcome = result.get("publication_state")
        if outcome == "published":
            return "Published", "Available to your team."
        if outcome == "pending_moderation":
            return ("Waiting for team review", "Submitted for review; not available to "
                    "your team yet. Check publication status for the decision.")
        if outcome == "changes_requested":
            return ("Changes requested", "A reviewer returned this draft. Review the "
                    "decision in Portal, revise, then re-review before submitting again. "
                    "Not published.")
        if outcome == "declined":
            return ("Rejected", "A reviewer declined this draft. Check publication "
                    "status for the decision before preparing a new submission. Not published.")
        if outcome == "invalidated":
            return ("Rejected", "This draft was invalidated. Check publication status "
                    "before preparing a new submission. Not published.")
    if state == "rejected" and result.get("phase") == "approval_rejected":
        return ("Failed", "Gateway's stored scan blocked approval of the existing "
                "draft. Check publication status and review the retained operation "
                "before authorizing a rescan. Not published.")
    if state == "rejected" and (result.get("phase") == "rejected" or
                                "before creation" in str(result.get("detail") or "")):
        if result.get("code") == "instruction_only_violation":
            # ``reason`` is plugin-authored (the local package rules' own
            # words); Gateway's detail never reaches this copy.
            reason = result.get("reason")
            return ("Failed", "Gateway's package rules refused a file in this skill. "
                    "Nothing was published." + (
                        f" {reason}" if isinstance(reason, str) and reason else ""))
        return ("Failed", "Gateway rejected the submission before creating a draft. "
                "Edit and re-review the local draft before a fresh submission. "
                "Nothing was published.")
    if state == "rejected":
        return ("Failed", "The submission failed. Check publication status and the "
                "recorded operation before another attempt; do not assume no draft "
                "was created.")
    return ("Uncertain", "Publication result is uncertain. Check publication "
            "status and reconcile the recorded operation; do not submit again blindly.")
