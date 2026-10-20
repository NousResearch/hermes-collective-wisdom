"""Shared error contracts for the Collective Wisdom plugin."""

import re


class WisdomError(RuntimeError):
    """Base error carrying the legacy CLI status and code contract."""

    exit_code = 8

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ):
        super().__init__(message)
        self.status = status
        self.code = code


class WisdomUnavailable(WisdomError):
    """Transient Gateway failure; never implies that a write can be replayed."""

    def __init__(self, *, status: int | None = None, code: str | None = None):
        super().__init__(
            "Collective Wisdom is temporarily unavailable.",
            status=status, code=code or "gateway_unavailable",
        )


class SharePackagingError(WisdomError):
    """A local candidate could not be packaged; no upload was attempted."""

    def __init__(self):
        super().__init__("Share Packaging failed", code="share_packaging_failed")


class WisdomAuthError(WisdomError):
    """Authentication or the required Wisdom entitlement is unavailable."""

    exit_code = 3


class WisdomNotFound(WisdomError):
    """The requested Wisdom resource does not exist or is not visible."""

    exit_code = 4


class WisdomConflict(WisdomError):
    """The requested operation conflicts with current Wisdom state."""

    exit_code = 5


class WisdomValidationError(WisdomError):
    """A request or Gateway response violates the Wisdom contract."""

    exit_code = 6


class ShareCopyError(WisdomValidationError):
    """The skill's name or description cannot be shared; nothing was prepared.

    The message names the measured problem and is safe to show the owner.
    """

    def __init__(self, message: str):
        super().__init__(message, code="share_copy_incomplete")


class StaleControlError(WisdomValidationError):
    """A callback names a control the current card projection does not offer.

    Old cards (pre-upgrade controls such as Portal draft, snooze or review
    checks) and forged callback ids land here. The transports answer with a
    bounded "no longer available" notice instead of the exception text.
    """

    def __init__(self, message: str = "Collective Wisdom action is not offered on this screen"):
        super().__init__(message, code="stale_control")


STALE_CONTROL_MESSAGE = "This control is no longer available. Run /wisdom share <skill> again."
CALLBACK_FAILED_MESSAGE = "Couldn't complete that. Please try again."


_CREDENTIAL_PARAMS = (
    "access_token|api_key|apikey|token|key|secret|password|passwd|signature|receipt|code"
)
_MASKS = (
    # URL userinfo: scheme://user:pass@host -> scheme://***:***@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://|//)[^/\s@?#]+@"), r"\1***:***@"),
    # Credential-named query/fragment parameters, any value length.
    (re.compile(r"(?i)([?&#;])(" + _CREDENTIAL_PARAMS + r")=[^&#\s]*"), r"\1\2=***"),
    # Credential fields outside a URL query (receipt, Authorization, token=, api_key:, ...):
    # a value can carry a scheme, spaces or line breaks, so the field value AND everything
    # after it is masked. Query parameters were already masked above, hence the lookbehind.
    (re.compile(r"(?i)(?<![?&#;\w])(receipt[\w\-]*|authorization|proxy-authorization"
                r"|access_token|api_key|apikey|token|key|secret|password|passwd|signature)"
                r"(['\"]?\s*[=:])[\s\S]*"),
     r"\1\2 ***"),
    # Bearer credentials outside a header field: mask the value and the rest of the text.
    (re.compile(r"(?i)\b(bearer)\s+[\s\S]*"), r"\1 ***"),
    # Long opaque strings are treated as credentials.
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "***"),
)


def _host_redact(message: str) -> str:
    """Apply the Hermes redactor in strict URL-credential mode when available."""
    import importlib  # pylint: disable=import-outside-toplevel

    redact = importlib.import_module("agent.redact").redact_sensitive_text
    try:
        return redact(message, force=True, redact_url_credentials=True)
    except TypeError:
        # Older hosts lack the strict URL flag; plugin masking still applies.
        return redact(message, force=True)


def mask_credentials(message: str) -> str:
    """Plugin-side masking that is applied whether or not the host redactor ran."""
    for pattern, replacement in _MASKS:
        message = pattern.sub(replacement, message)
    return message


def describe_exception(exc: BaseException, *, limit: int = 300) -> str:
    """Return "Class: message" for logs, redacted, bounded, without controls."""
    message = str(exc)
    try:
        message = _host_redact(message)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # Fallback: the plugin masking below is always applied.
    message = mask_credentials(message)
    message = " ".join("".join(
        char if char.isprintable() else " " for char in message
    ).split())[:limit]
    return type(exc).__name__ + (": " + message if message else "")
