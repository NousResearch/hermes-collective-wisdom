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
