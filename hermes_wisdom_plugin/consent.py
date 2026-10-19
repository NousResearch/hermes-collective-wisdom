"""First-run data-use notice, recorded once per person per organization.

The notice is the existing setup disclosure. Consent lives server-side
(``GET/PUT consent``) so it is honoured across devices and profiles; a profile's
local enrolment (``setup``) still happens on accept. Cloud agents (both
``HERMES_WISDOM_AGENT_NAME`` and ``HERMES_WISDOM_AGENT_REF`` set) never prompt
and rely on the owner's org consent.
"""

from __future__ import annotations

from typing import Any

from .client import agent_identity
from .direct_services import WISDOM_DISCLOSURE
from .errors import WisdomAuthError, WisdomNotFound
from .team_status import service_scope

DISCLOSURE_VERSION: int = 2
DISCLOSURE_TEXT = WISDOM_DISCLOSURE + " Enrolling does not upload local skill contents."


def is_agent() -> bool:
    """Both cloud-agent environment variables are set."""
    return agent_identity() is not None


def _remote_accepted(service: Any) -> bool | None:
    """Server-side consent; ``None`` when the Gateway cannot answer (older server)."""
    try:
        service.check_owner()
        service._open()  # pylint: disable=protected-access
        state = service.client.consent()
    except WisdomNotFound:
        return None
    except WisdomAuthError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    version = state.disclosure_version
    return bool(state.accepted and (version is None or version >= 1))


def consent_state(runtime: Any) -> dict:
    """``{"accepted": bool, "disclosure_text": str}`` for this person and org."""

    with service_scope(runtime) as service:
        accepted = _remote_accepted(service)
        if accepted is None:
            # Older Gateway without the consent route: local enrolment is the record.
            try:
                accepted = service.status().get("configured") is True
            except Exception:  # pylint: disable=broad-exception-caught
                accepted = False
        return {
            "accepted": bool(accepted),
            "disclosure_text": DISCLOSURE_TEXT,
            "disclosure_version": DISCLOSURE_VERSION,
            "agent": is_agent(),
        }


def _record_remote(service: Any) -> None:
    """Best-effort server-side record so other devices skip the notice.

    Local enrolment is the authority on this device; an older Gateway (404), a
    transient error, or a service without a client must not undo it.
    """
    try:
        opener = getattr(service, "_open", None)
        if callable(opener):
            opener()
        client = getattr(service, "client", None)
        if client is not None:
            client.accept_consent(DISCLOSURE_VERSION)
    except WisdomAuthError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        return


def accept(runtime: Any) -> dict:
    """Record consent server-side (idempotent), then enrol this profile."""

    with service_scope(runtime) as service:
        status = service.setup(accept_disclosure=True)
        if not is_agent():
            _record_remote(service)
        return {"accepted": True, "disclosure_text": DISCLOSURE_TEXT, "status": status}


def ensure_consent(service: Any, *, interactive: bool, prompt=input, emit=print) -> bool:
    """Gate a protected command: return whether this profile may proceed.

    Already enrolled → proceed. Server says accepted (another device, or the
    owner of a cloud agent) → enrol silently. Otherwise show the notice once with
    Continue in an interactive terminal; never prompt agents or non-TTY callers.
    """

    try:
        if service.status().get("configured") is True:
            return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False
    try:
        accepted = _remote_accepted(service)
    except Exception:  # pylint: disable=broad-exception-caught
        return False  # Not signed in or not entitled: the command reports it.
    if accepted:
        service.setup(accept_disclosure=True)
        return True
    if is_agent() or not interactive:
        return False
    emit("Collective Wisdom\n\n" + DISCLOSURE_TEXT + "\n")
    answer = prompt("Continue? [Y/n] ").strip().casefold()
    if answer not in {"", "y", "yes", "continue"}:
        return False
    accept(service)
    return True


__all__ = ["DISCLOSURE_TEXT", "DISCLOSURE_VERSION", "accept", "consent_state", "ensure_consent"]
