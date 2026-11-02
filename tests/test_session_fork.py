"""The session-fork wrapper turns a host handle into plain data and never raises."""
# pylint: disable=missing-function-docstring

from __future__ import annotations

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
