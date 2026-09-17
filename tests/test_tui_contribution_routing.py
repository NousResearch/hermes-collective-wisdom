"""Contribution commands use the admitted invocation, never the shared worker."""
# pylint: disable=protected-access

import asyncio
import json
from unittest.mock import Mock

import pytest
from test_tui_actions import Action, Outcome

from hermes_wisdom_plugin.runtime import CommandRuntime


@pytest.fixture(name="runtime")
def runtime_fixture(tmp_path):
    """Construct an inert runtime scoped to a disposable profile."""
    runtime = CommandRuntime(
        tmp_path / "state", ("default", tmp_path),
        llm=object(), spawn_task=lambda *_args, **_kwargs: None,
        request_review=lambda **_kwargs: pytest.fail("inspection must not ask approval"),
        can_request_review=lambda: True, is_review_current=lambda: True,
        review_action_type=Action, review_outcome_type=Outcome,
    )
    yield runtime
    runtime.close()


def test_private_inspection_uses_admitted_worker_and_closes_service(runtime):
    """A private export must use the TUI worker rather than shared subprocess routing."""
    payload = {"draft_id": "draft-1", "files": [{"path": "SKILL.md", "content": "private"}]}
    service = Mock()
    service.prepared_inspect.return_value = payload
    runtime._service = Mock(return_value=service)
    runtime._run_worker = lambda *_args: pytest.fail("private export reached shared worker")

    result = asyncio.run(runtime.slash("prepared inspect draft-1"))

    assert json.loads(result.split("\n\n", 1)[0]) == payload
    service.prepared_inspect.assert_called_once()
    service.close.assert_called_once_with()


@pytest.mark.parametrize("raw", [
    "candidate event-1", "suggest skill-1", "prepared inspect draft-1",
    "prepared save /private/payload.json", "publish draft-1",
    "publication review draft-1", "publication status operation-1",
    "publication submit receipt-1", "prepared submit draft-1",
])
def test_unadmitted_contribution_never_constructs_private_service(runtime, raw):
    """Shared/background invocations get only static instructions, even for receipt syntax."""
    runtime.can_request_review = lambda: False
    runtime._service = lambda: pytest.fail("unadmitted contribution opened private state")

    result = asyncio.run(runtime.slash(raw))

    assert "terminal" in result.casefold()
    assert "private" in result.casefold()


@pytest.mark.parametrize("raw", [
    "candidate assessment-1 share", "candidate assessment-1 portal",
    "candidate assessment-1 later", "publication approve receipt-1",
])
def test_candidate_actions_work_without_host_review_capability(runtime, raw):
    """A deliberate stock-TUI slash action uses plugin authority, not request_review."""
    runtime.request_review = None
    runtime.can_request_review = None
    runtime.is_review_current = None
    service = Mock()
    service.candidate_slash_action.return_value = {
        "action": raw.rsplit(" ", 1)[-1], "candidate_id": "assessment-1",
        "review": {"receipt": "receipt-1", "hashes": {"content": "sha256:a"}},
    }
    service.candidate_publication_approve.return_value = {
        "state": "pending", "publication_state": "pending_moderation",
    }
    runtime._service = Mock(return_value=service)
    runtime._run_worker = lambda *_args: pytest.fail("candidate action reached shared worker")

    result = asyncio.run(runtime.slash(raw))

    assert "terminal" not in result.casefold()
    service.close.assert_called_once_with()
    if "approve" in raw:
        service.candidate_publication_approve.assert_called_once()
    else:
        service.candidate_slash_action.assert_called_once()
