"""Unload versus delivery: no suggestion turn starts once the plugin is unloading."""
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import threading
import time

import pytest
from test_auto_suggest import DUE
from test_auto_suggest_fork import _Host, _learn, _turn

from hermes_wisdom_plugin import auto_suggest
from hermes_wisdom_plugin.auto_suggest import approval_prompt


def _unload_at(host, controller, boundary):
    """Close the controller at one step of the delivery, as a concurrent unload would."""
    store = host.store
    reserve, add_pending = store.reserve_today, store.add_pending

    def reserve_today(limit):
        if boundary == "after_closed_check":
            controller.close()
        reserved = reserve(limit)
        if boundary == "after_reservation":
            controller.close()
        return reserved

    def pending(suggestion):
        add_pending(suggestion)
        if boundary == "after_pending_write":
            controller.close()

    store.reserve_today, store.add_pending = reserve_today, pending


def _state(host):
    snapshot = host.store.snapshot()
    reasons = [row.get("why") for row in snapshot["decisions"] if row["event"] == "not_shown"]
    return snapshot, reasons


@pytest.mark.parametrize("fork", [True, False], ids=["fork", "side_question"])
@pytest.mark.parametrize("boundary", ["after_closed_check", "after_reservation",
                                      "after_pending_write"])
def test_unload_during_delivery_injects_nothing_and_rolls_back(tmp_path, boundary, fork):
    host = _Host(tmp_path)
    controller = host.build(fork=fork)
    _unload_at(host, controller, boundary)
    _learn(controller)
    _turn(controller, signals=DUE)
    snapshot, reasons = _state(host)
    assert controller._closed is True
    assert not host.injected, "an unload that began before the inject starts no turn"
    assert snapshot["pending"] == {}, "the reserved pending suggestion is removed"
    assert int(snapshot["day"].get("shown") or 0) == 0, "today's count is given back"
    assert not snapshot["day"].get("blocked"), "an unload is not a failed delivery"
    assert reasons == ["unloaded"]
    assert not host.store.seen("incident", "sha-i"), "asked again after a reload"
    assert not controller._busy


def test_delivery_without_unload_is_unchanged(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    _learn(controller)
    _turn(controller, signals=DUE)
    snapshot, reasons = _state(host)
    assert host.injected == [(approval_prompt("incident"), "user", {})]
    assert [row["state"] for row in snapshot["pending"].values()] == ["pending"]
    assert snapshot["day"]["shown"] == 1 and not reasons
    started = time.monotonic()
    controller.close()
    assert time.monotonic() - started < 1.0, "nothing in flight: close() does not wait"


class _SlowInject:  # pylint: disable=too-few-public-methods
    """A host inject that blocks until the test releases it."""

    def __init__(self, host):
        self.host = host
        self.entered = threading.Event()
        self.release = threading.Event()
        self.order: list[str] = []

    def __call__(self, content, role="user", **target):
        self.entered.set()
        assert self.release.wait(10)
        self.order.append("inject returned")
        return self.host.inject(content, role, **target)


def _deliver_in_background(controller):
    _learn(controller)
    worker = threading.Thread(target=lambda: _turn(controller, signals=DUE), daemon=True)
    worker.start()
    return worker


def test_close_waits_for_an_inject_the_host_already_started(tmp_path):
    host = _Host(tmp_path)
    slow = _SlowInject(host)
    controller = host.build(inject=slow)
    worker = _deliver_in_background(controller)
    assert slow.entered.wait(5)
    closed = threading.Event()

    def unload():
        controller.close()
        slow.order.append("close returned")
        closed.set()

    threading.Thread(target=unload, daemon=True).start()
    assert not closed.wait(0.3), "close() returned while the host was still injecting"
    slow.release.set()
    assert closed.wait(5)
    worker.join(5)
    assert slow.order == ["inject returned", "close returned"]
    assert len(host.injected) == 1 and host.store.snapshot()["day"]["shown"] == 1


def test_close_does_not_deadlock_on_a_stuck_inject(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(auto_suggest, "CLOSE_DELIVERY_WAIT", 0.2, raising=False)
    host = _Host(tmp_path)
    slow = _SlowInject(host)
    controller = host.build(inject=slow)
    worker = _deliver_in_background(controller)
    assert slow.entered.wait(5)
    started = time.monotonic()
    controller.close()
    assert time.monotonic() - started < 2.0, "close() is bounded by CLOSE_DELIVERY_WAIT"
    assert "still injecting" in caplog.text
    slow.release.set()
    worker.join(5)
    assert not worker.is_alive() and len(host.injected) == 1  # Handed over before unload.


def test_close_from_inside_the_inject_returns_at_once(tmp_path):
    host = _Host(tmp_path)
    controller = host.build()
    elapsed: list[float] = []

    def inject(content, role="user", **target):
        started = time.monotonic()
        controller.close()  # The host unloads the plugin while handling its inject.
        elapsed.append(time.monotonic() - started)
        return host.inject(content, role, **target)

    controller.inject = inject
    _learn(controller)
    _turn(controller, signals=DUE)
    assert elapsed and elapsed[0] < 1.0, "close() does not wait for its own caller's inject"
    assert controller._closed and len(host.injected) == 1
