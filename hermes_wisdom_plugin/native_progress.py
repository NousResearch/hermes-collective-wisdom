"""Bound the worker-to-provider bridge for temporary native card edits."""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Awaitable, Callable

from .errors import WisdomConflict

PROGRESS_TIMEOUT = 30.0  # Same default as the Gateway HTTP transport.


def edit_progress(
    loop: asyncio.AbstractEventLoop, edit: Callable[[], Awaitable[Any]],
    current: Callable[[], bool], *, slack: bool = False,
    allowed: Callable[[], bool] = lambda: True,
) -> None:
    """Never continue to a package effect without a current, acknowledged edit."""
    # This function runs in the action worker. Host ownership can refresh auth;
    # never call current() on the provider's asyncio loop.
    if not current():
        raise WisdomConflict("Collective Wisdom progress owner changed")
    if not allowed():
        raise WisdomConflict("Collective Wisdom progress owner changed")

    async def guarded() -> None:
        if not allowed():
            raise WisdomConflict("Collective Wisdom progress owner changed")
        response = await edit()
        if slack and (not callable(getattr(response, "get", None))
                      or response.get("ok") is not True):
            raise WisdomConflict("Collective Wisdom progress edit was not confirmed")

    future = asyncio.run_coroutine_threadsafe(guarded(), loop)
    try:
        future.result(timeout=PROGRESS_TIMEOUT)
    except FutureTimeout:
        future.cancel()
        raise WisdomConflict("Collective Wisdom progress edit timed out") from None
    if not current():
        raise WisdomConflict("Collective Wisdom progress owner changed")
