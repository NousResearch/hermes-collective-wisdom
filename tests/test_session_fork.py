"""The session-fork wrapper turns a host handle into plain data and never raises."""
# pylint: disable=missing-function-docstring

from __future__ import annotations

import threading
import time

import pytest

from hermes_wisdom_plugin.session_fork import REFUSED, ForkRunner


class _Handle:
    def __init__(self, outcome="ok", finishes=True, wait_raises=False):
        self.outcome = outcome
        self.finishes = finishes
        self.wait_raises = wait_raises
        self.cancelled = False

    def wait(self, timeout=None):  # pylint: disable=unused-argument
        if self.wait_raises:
            raise RuntimeError("handle changed")
        return self.finishes

    def cancel(self):
        self.cancelled = True
        self.outcome = "cancelled"


def test_a_finished_fork_is_reported_as_plain_data():
    calls = []
    handle = _Handle()
    result = ForkRunner().run(lambda *a, **k: calls.append((a, k)) or handle, "tok", "p",
                              wait=1.0, tools=("t",), timeout=5.0)
    assert set(result) == {"fork_id", "outcome"} and result["outcome"] == "ok"
    assert isinstance(result["fork_id"], str) and result["fork_id"]
    assert calls == [(("tok", "p"), {"tools": ("t",), "timeout": 5.0})]
    assert not handle.cancelled


@pytest.mark.parametrize("fork", [
    None,
    lambda *_a, **_k: None,
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unsupported in the plugin host")),
    lambda *_a, **_k: (_ for _ in ()).throw(TypeError("unexpected keyword")),
])
def test_no_handle_or_a_raising_fork_is_refused(fork):
    assert ForkRunner().run(fork, "tok", "p", wait=1.0)["outcome"] == REFUSED


def test_a_fork_still_running_after_the_wait_is_cancelled():
    handle = _Handle(outcome="deferred", finishes=False)
    assert ForkRunner().run(lambda *_a, **_k: handle, "tok", "p", wait=0.0)["outcome"] == (
        "cancelled")
    assert handle.cancelled


def test_a_handle_that_cannot_be_read_is_an_error_not_a_crash():
    handle = _Handle(wait_raises=True)
    assert ForkRunner().run(lambda *_a, **_k: handle, "tok", "p", wait=1.0)["outcome"] == "error"


class _Blocking(_Handle):
    """A fork that runs until it is cancelled."""

    def __init__(self):
        super().__init__(outcome="running")
        self.finished = threading.Event()

    def wait(self, timeout=None):
        return self.finished.wait(timeout)

    def cancel(self):
        super().cancel()
        self.finished.set()


def test_cancel_all_stops_a_running_fork_from_another_thread_and_refuses_new_ones():
    runner, handle, results = ForkRunner(), _Blocking(), []
    worker = threading.Thread(target=lambda: results.append(
        runner.run(lambda *_a, **_k: handle, "tok", "p", wait=30.0)))
    worker.start()
    deadline = time.monotonic() + 5
    while runner.running() != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.running() == 1
    assert runner.cancel_all() == 1
    worker.join(5)
    assert not worker.is_alive() and handle.cancelled
    assert results[0]["outcome"] == "cancelled" and runner.running() == 0
    late = _Blocking()
    assert runner.run(lambda *_a, **_k: late, "tok", "p", wait=1.0)["outcome"] == REFUSED
    assert not late.cancelled  # Never started.


def test_a_fork_that_returns_after_the_unload_is_cancelled_at_once():
    runner = ForkRunner()
    handle = _Blocking()

    def fork(*_args, **_kwargs):
        runner.cancel_all()  # The plugin unloads while the host is starting the fork.
        return handle

    assert runner.run(fork, "tok", "p", wait=30.0)["outcome"] == "cancelled"
    assert handle.cancelled and runner.running() == 0
