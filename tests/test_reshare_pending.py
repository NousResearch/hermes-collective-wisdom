"""Repeat /wisdom share after an abandoned flow resumes the retained operation."""
# pylint: disable=missing-function-docstring,too-few-public-methods
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace

import pytest

from installed_test_support import JsonState
from test_commands import _native_invocation
from test_share_command import ShareCli

from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.review_state import ReviewState


class Kernel(ShareCli):
    """The invocation-service surface over real preparation and publication."""

    def native_owner(self):
        return dict(self.owner)

    def native_team_name(self):
        return "Incident Response"

    def publication_submit(self, receipt, *, may_apply=None):
        return self.publication.submit(receipt, may_apply=may_apply)

    def close(self):
        return None


def _abandoned_private_draft(kernel):
    """The live sequence: a private draft upload, then the flow is abandoned."""
    prepared = kernel.suggest("reviewed-skill")
    staged = kernel.publication.submit_private_draft(
        kernel.publication_review(prepared["draft_id"])["receipt"]
    )
    operation = kernel.publication.state.publication(staged["operation_id"])
    assert operation["phase"] == "policy_review_required"
    assert operation["requested_action"] == "private_draft"
    assert kernel.publication.state.draft(prepared["draft_id"])["state"] == "submitted"
    # The exact live failure that reached chat as "Couldn't open Collective Wisdom".
    again = kernel.suggest("reviewed-skill")
    with pytest.raises(WisdomConflict, match="cannot be edited while it is submitted"):
        kernel.prepared_review(again["draft_id"])
    return staged["operation_id"]


def test_second_native_share_offers_the_pending_operation_and_share_publishes(tmp_path):
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()

    record = controller.request_suggest(kernel, caller, "reviewed-skill")

    assert record["operation"] == "publish" and record["screen"] == "approval"
    assert record["plan"]["operation_id"] == operation_id
    controller.bind_message(record["id"], caller, "card-1")
    clicked = replace(caller, message_id="card-1")
    view = controller.view(controller.load(record["id"]))
    # The pending operation resumes on the same one-click Share card.
    assert view.title == "📤 Share skill"
    assert [a.action for a in view.actions if a.primary] == ["confirm"]
    assert {a.action for a in view.actions} >= {"confirm", "cancel", "details"}
    assert "reviewed-skill" in view.to_text()
    assert kernel.client.state.calls == Counter(upload=1, submit=1)

    finished = controller.act(record["id"], "confirm", kernel, clicked)

    assert finished["state"] == "completed"
    assert finished["result"]["publication_state"] == "published"
    operation = kernel.publication.state.publication(operation_id)
    assert operation["requested_action"] is None and operation["status"] == "done"
    # The retained remote draft is approved; nothing is uploaded twice.
    assert kernel.client.state.calls == Counter(
        upload=1, submit=1, approve=1, head=1, publish=1,
    )


def test_second_native_share_shows_status_for_other_unfinished_phase(tmp_path):
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    kernel.publication.state.update_publication(operation_id, phase="publication_unknown")
    controller = NativeReviewController(ReviewState(JsonState(tmp_path), tmp_path))
    caller = _native_invocation()

    record = controller.request_suggest(kernel, caller, "reviewed-skill")

    assert record["operation"] == "share_status"
    view = controller.view(record)
    assert view.title == "Hermes Collective Wisdom"
    assert "hasn't finished" in view.to_text()
    assert not any(a.primary for a in view.actions)
    controller.bind_message(record["id"], caller, "card-1")
    cancelled = controller.act(record["id"], "cancel", kernel, replace(caller, message_id="card-1"))
    assert cancelled["state"] == "cancelled"
    assert kernel.client.state.calls == Counter(upload=1, submit=1)


def test_terminal_share_parity_finishes_the_pending_operation(tmp_path, capsys):
    kernel = Kernel(tmp_path)
    operation_id = _abandoned_private_draft(kernel)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)

    code = run_cli(parser.parse_args(["share", "reviewed-skill", "--yes", "--json"]), kernel)

    result = json.loads(capsys.readouterr().out)
    assert (code, result["outcome"]) == (0, "shared")
    operation = kernel.publication.state.publication(operation_id)
    assert operation["requested_action"] is None and operation["status"] == "done"
    assert kernel.client.state.calls == Counter(
        upload=1, submit=1, approve=1, head=1, publish=1,
    )
