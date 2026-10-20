"""Shared error contracts for the Collective Wisdom plugin."""


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


def describe_exception(exc: BaseException, *, limit: int = 300) -> str:
    """Return "Class: message" for logs, host-redacted, bounded, without controls."""
    import importlib  # pylint: disable=import-outside-toplevel
    import re  # pylint: disable=import-outside-toplevel

    message = str(exc)
    try:
        redact = importlib.import_module("agent.redact").redact_sensitive_text
        message = redact(message, force=True)
    except Exception:  # pylint: disable=broad-exception-caught
        # Without the host redactor, mask anything shaped like a credential.
        message = re.sub(r"(?i)(bearer\s+|token[=:]\s*|key[=:]\s*)\S+", r"\1[REDACTED]", message)
        message = re.sub(r"[A-Za-z0-9_\-]{32,}", "[REDACTED]", message)
    message = " ".join("".join(
        char if char.isprintable() else " " for char in message
    ).split())[:limit]
    return type(exc).__name__ + (": " + message if message else "")
