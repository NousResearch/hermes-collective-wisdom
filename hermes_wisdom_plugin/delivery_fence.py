"""Unload versus delivery: once unload begins, this plugin starts no host inject."""

from __future__ import annotations

import threading
from collections import Counter


class DeliveryFence:
    """Leases around the host's inject; unload shuts the fence, then drains open leases.

    A delivery takes its lease under the fence's lock immediately before calling the host,
    so no inject starts after ``shut()``. ``drain()`` waits up to its timeout for injects the
    host was already running. One still running then, or the caller's own (an unload from
    inside that inject), may complete after unload returns; it was handed over before.
    """

    def __init__(self) -> None:
        self._idle = threading.Condition()
        self._open: Counter[int] = Counter()  # Leases by thread.
        self._shut = False

    def enter(self) -> bool:
        """Take a lease for one inject; False once unload has begun."""
        with self._idle:
            if self._shut:
                return False
            self._open[threading.get_ident()] += 1
            return True

    def leave(self) -> None:
        """Return this thread's lease after the host's inject returned or raised."""
        with self._idle:
            ident = threading.get_ident()
            self._open[ident] -= 1
            if self._open[ident] <= 0:
                del self._open[ident]
            self._idle.notify_all()

    def shut(self) -> None:
        """Refuse every later lease."""
        with self._idle:
            self._shut = True

    def drain(self, timeout: float) -> bool:
        """Wait for other threads' open leases; whether none remained within ``timeout``."""
        caller = threading.get_ident()
        with self._idle:
            return self._idle.wait_for(
                lambda: not any(ident != caller for ident in self._open), timeout)
