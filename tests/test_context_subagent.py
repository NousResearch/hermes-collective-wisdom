"""Bounded private lifecycle selection, without a live model or remote effects."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.context_relevance import current_excerpt, relevant_skill

pytestmark = pytest.mark.usefixtures("private_lifecycle_contract")


class Lifecycle:
    def __init__(self, summary="0", state="SUCCEEDED", timed_out=False):
        self.summary, self.state, self.timed_out = summary, state, timed_out
        self.requests = []
        self.waits = []
        self.cancellations = []
        self.handle = object()

    def launch(self, request):
        self.requests.append(request)
        return self.handle

    def wait(self, handle, *, timeout_seconds):
        self.waits.append((handle, timeout_seconds))
        return SimpleNamespace(state=self.state, completed=not self.timed_out,
                               timed_out=self.timed_out)

    def result(self, handle):
        assert handle is self.handle
        return SimpleNamespace(ready=True, terminal_state=self.state,
                               summary=self.summary)

    def cancel(self, handle, *, reason):
        self.cancellations.append((handle, reason))


def candidates(count=1):
    return [{"slug": f"skill-{i}", "author_description": "Incident triage",
             "skill_id": f"private-id-{i}", "version": 1} for i in range(count)]


def test_bounded_metadata_original_request_and_one_child_per_turn():
    lifecycle = Lifecycle("7")
    rows = candidates(12)
    rows[8]["author_description"] = "SHOULD NEVER BE TRANSMITTED"
    assert relevant_skill(lifecycle, "current request", rows, "session-1") is rows[7]
    request = lifecycle.requests[0]
    data = json.loads(request.context)
    assert request.private_default_route is True and request.role == "leaf"
    assert request.parent_session_id == "session-1"
    assert len(request.goal) <= 1200 and "current request" not in request.goal
    assert data["current_user_request"] == "current request"
    assert len(data["published_skills"]) == 8
    assert "SHOULD NEVER" not in request.context
    assert "private-id" not in request.context
    assert lifecycle.waits == [(lifecycle.handle, 10)]
    assert len(lifecycle.requests) == 1


@pytest.mark.parametrize("summary", ["-1", "8", "01", "true", "0 and 1", "{\"index\":0}",
                                      "", None, "-2", "1.0"])
def test_invalid_or_unrelated_response_never_matches(summary):
    lifecycle = Lifecycle(summary)
    assert relevant_skill(lifecycle, "incident", candidates(), "session-1") is None
    assert len(lifecycle.requests) == 1


def test_timeout_cancels_once_without_result():
    lifecycle = Lifecycle(timed_out=True)
    assert relevant_skill(lifecycle, "incident", candidates(), "session-1") is None
    assert lifecycle.cancellations == [(lifecycle.handle, "Wisdom context relevance deadline")]


@pytest.mark.parametrize("excerpt,rows,session", [
    ("incident", candidates(), ""),
    ("x" * 1201, candidates(), "session-1"),
    ("incident", [{**candidates()[0], "author_description": "token: real-secret"}], "session-1"),
    ("incident", [{**candidates()[0], "author_description": "sk-" + "a" * 32}], "session-1"),
])
def test_bad_context_never_launches(excerpt, rows, session):
    lifecycle = Lifecycle()
    assert relevant_skill(lifecycle, excerpt, rows, session) is None
    assert not lifecycle.requests


def test_unsupported_host_never_falls_back_to_raw_llm():
    old = SimpleNamespace(complete_structured=lambda **_kwargs: pytest.fail("raw model called"))
    assert relevant_skill(old, "incident", candidates(), "session-1") is None


def test_excerpt_is_bounded_to_current_user_and_rejects_credentials():
    text = "x" * 1200 + "not sent"
    excerpt = current_excerpt([{"role": "user", "content": "old request"},
                               {"role": "tool", "content": "old tool"},
                               {"role": "user", "content": text}])
    lifecycle = Lifecycle()
    rows = candidates()
    assert relevant_skill(lifecycle, excerpt, rows, "session-1") is rows[0]
    assert json.loads(lifecycle.requests[0].context)["current_user_request"] == "x" * 1200
    assert current_excerpt([{"role": "user", "content": "token: secret"}]) == ""
