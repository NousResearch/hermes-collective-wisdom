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


class WisdomConflict(WisdomError):
    """The requested operation conflicts with current Wisdom state."""

    exit_code = 5
