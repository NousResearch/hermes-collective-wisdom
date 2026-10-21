"""A Gateway advisory after upload completes on the same native card, never Uncertain."""
# pylint: disable=missing-function-docstring,too-few-public-methods
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import sys
from collections import Counter

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_publication import RecordingPublicationClient
from test_reshare_pending import Kernel

from hermes_wisdom_plugin import share_command
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.errors import WisdomConflict
from hermes_wisdom_plugin.native_review import project_view
from hermes_wisdom_plugin.share_command import run_share

FINDINGS = [
    {"category": "secret_like", "severity": "advisory", "path": "SKILL.md",
     "detail": "Looks like an API key assignment."},
    {"category": "pii", "severity": "advisory", "path": "references/notes.md",
     "detail": "Contains an email address."},
]


class AdvisoryClient(RecordingPublicationClient):
    """Gateway fake whose draft creation returns an advisory stored scan."""

    def submit_draft(self, **payload):
        draft = super().submit_draft(**payload)
        draft.scan = {"verdict": draft.scanVerdict, "findings": (
            [dict(finding) for finding in FINDINGS] if draft.scanVerdict == "advisory" else []
        )}
        return draft


class AdvisoryKernel(Kernel):
    """Real preparation and publication; only the Gateway client is replaced."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.client = AdvisoryClient(self.owner)
        self.client.state.scan_verdict = "advisory"
        self.publication.client = self.client

    def publication_gateway_review(self, operation_id, *, may_apply=None):
        return self.publication.gateway_review(operation_id, may_apply=may_apply)

    def publication_gateway_shown(self, operation_id, evidence_hash):
        return self.publication.mark_gateway_shown(operation_id, evidence_hash)

    def publication_review(self, draft, *, accept_warnings=False, may_apply=None):
        del may_apply
        return self.publication.review(draft, accept_warnings=accept_warnings)


def _controls(view):
    return [(action.label, action.action) for action in view.actions]


ADVISORY_CONTROLS = [("Acknowledge", "publish.ack"), ("Cancel", "cancel"),
                     ("View details", "details")]


def _share_card_actions(view):
    return [action.action for action in view.actions if action.primary]


def test_service_confirms_diagnosis_advisory_is_pending_not_an_error(tmp_path):
    """Before the fix this raised "Gateway scan verdict requires a fresh review"."""
    kernel = AdvisoryKernel(tmp_path)
    prepared = kernel.suggest("reviewed-skill")

    result = kernel.publication.submit(kernel.publication_review(prepared["draft_id"])["receipt"])

    assert result["state"] == "pending" and result["phase"] == "policy_review_required"
    assert result["review_required"] == "gateway_advisory"
    assert result["server_checks"]["security_check"]["findings"] == FINDINGS
    operation = kernel.publication.state.publication(result["operation_id"])
    assert operation["remote_scan_verdict"] == "advisory"
    assert operation["resume_phase"] == "submitted"
    assert kernel.client.state.calls == Counter(upload=1, submit=1)


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_share_turns_same_card_into_gateway_checks_then_acknowledge_share(tmp_path, surface):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        assert ui.view(identity).title == "📤 Share skill"
        posts, edits = len(ui.posts), len(ui.edits)

        await ui.click(identity, "confirm")

        record, view = ui.record(identity), ui.view(identity)
        assert record["state"] == "pending", "advisory is a defined outcome, not Uncertain"
        assert view.title == "⚠️ Gateway checks"
        assert "found 2 items to review" in view.summary
        assert "Uncertain" not in view.to_text() and "hermes wisdom" not in view.to_text()
        assert "Looks like an API key assignment." in view.to_text()
        assert _controls(view) == ADVISORY_CONTROLS
        assert len(ui.posts) == posts and len(ui.edits) > edits, "same card, edited in place"
        assert kernel.client.state.calls == Counter(upload=1, submit=1)

        # Share before Acknowledge is not offered: the projection allowlist rejects it.
        await ui.click(identity, "confirm")
        assert kernel.client.state.calls == Counter(upload=1, submit=1)
        assert ui.record(identity)["screen"] == "gateway_advisory"

        await ui.click(identity, "details")
        details = ui.view(identity).to_text()
        while await ui.next_page(identity):
            details += ui.view(identity).to_text()
        assert "Contains an email address." in details and "Looks like an API key" in details
        assert "Back" in [action.label for action in ui.view(identity).actions]
        await ui.click(identity, "back")
        assert ui.view(identity).title == "⚠️ Gateway checks"

        await ui.click(identity, "publish.ack")
        view = ui.view(identity)
        assert view.title == "📤 Share skill" and _share_card_actions(view) == ["confirm"]
        assert kernel.client.state.calls == Counter(upload=1, submit=1)

        await ui.click(identity, "confirm")
        record = ui.record(identity)
        assert record["state"] == "completed"
        assert record["result"]["publication_state"] == "published"
        assert ui.view(identity).title == "✅ Shared"
        assert kernel.client.state.calls == Counter(
            upload=1, submit=1, approve=1, head=1, publish=1,
        )
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_cancel_on_gateway_checks_leaves_operation_pending_without_remote_effect(
    tmp_path, surface,
):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        await ui.click(identity, "confirm")
        operation_id = ui.record(identity)["plan"]["operation_id"]

        await ui.click(identity, "cancel")

        view = ui.view(identity)
        assert ui.record(identity)["state"] == "cancelled" and not view.actions
        assert "Run /wisdom share again" in view.to_text()
        assert kernel.publication.state.publication(operation_id)["status"] == "pending"
        assert kernel.client.state.calls == Counter(upload=1, submit=1)
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_resume_after_abandon_opens_the_same_gateway_checks_card(tmp_path, surface):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        prepared = kernel.suggest("reviewed-skill")
        kernel.publication.submit(kernel.publication_review(prepared["draft_id"])["receipt"])
        ui = surface(tmp_path, kernel)

        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()

        view = ui.view(identity)
        assert view.title == "⚠️ Gateway checks" and _controls(view) == ADVISORY_CONTROLS
        assert "receipt" not in ui.record(identity)["plan"], "display grants nothing"
        await ui.click(identity, "publish.ack")
        await ui.click(identity, "confirm")
        assert ui.record(identity)["result"]["publication_state"] == "published"
        assert kernel.client.state.calls == Counter(
            upload=1, submit=1, approve=1, head=1, publish=1,
        )
        ui.runtime.close()
    asyncio.run(scenario())


def test_terminal_accept_warnings_finishes_gateway_advisory_once(tmp_path, capsys):
    kernel = AdvisoryKernel(tmp_path)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)

    code = run_cli(parser.parse_args(["share", "reviewed-skill", "--yes", "--json"]), kernel)
    first = json.loads(capsys.readouterr().out)
    assert (code, first["outcome"]) == (1, "gateway_review_required")
    assert "Looks like an API key assignment." in first["detail"]

    code = run_cli(parser.parse_args(["share", "reviewed-skill", "--yes", "--json"]), kernel)
    assert json.loads(capsys.readouterr().out)["outcome"] == "warnings_required"

    code = run_cli(parser.parse_args(
        ["share", "reviewed-skill", "--yes", "--accept-warnings"]), kernel)
    output = capsys.readouterr().out
    assert code == 0 and "Gateway findings (2)" in output and "Shared with" in output
    assert kernel.client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


def _mutate_gateway_finding(kernel, **changes):
    draft = kernel.client.state.drafts["draft-1"]
    draft.scan = {**draft.scan, **changes.pop("scan", {})}
    draft.scan["findings"] = [{**draft.scan["findings"][0], **changes},
                              *draft.scan["findings"][1:]]


def test_terminal_accept_warnings_never_acknowledges_findings_it_did_not_show(
    tmp_path, capsys,
):
    """Salt R1 B1: evidence changed after the shown JSON result; nothing is submitted."""
    kernel = AdvisoryKernel(tmp_path)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    share = ["share", "reviewed-skill", "--accept-warnings", "--yes", "--json"]

    run_cli(parser.parse_args(["share", "reviewed-skill", "--yes", "--json"]), kernel)
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "gateway_review_required"
    assert "NEW SECRET WARNING" not in first["detail"]

    _mutate_gateway_finding(kernel, category="credential_exposure",
                            detail="NEW SECRET WARNING")
    code = run_cli(parser.parse_args(share), kernel)
    second = json.loads(capsys.readouterr().out)
    assert (code, second["outcome"]) == (1, "gateway_review_required")
    assert "NEW SECRET WARNING" in second["detail"], "the new findings are returned"
    assert kernel.client.state.calls == Counter(upload=1, submit=1), "nothing approved"

    # Those findings have now been shown; acknowledging them finishes the share once.
    code = run_cli(parser.parse_args(share), kernel)
    assert json.loads(capsys.readouterr().out)["outcome"] == "shared" and code == 0
    assert kernel.client.state.calls == Counter(upload=1, submit=1, approve=1, head=1, publish=1)


def test_terminal_accept_warnings_without_any_shown_evidence_submits_nothing(tmp_path, capsys):
    kernel = AdvisoryKernel(tmp_path)
    prepared = kernel.suggest("reviewed-skill")
    # The advisory exists, but no terminal has displayed it yet.
    kernel.publication.submit(kernel.publication_review(prepared["draft_id"])["receipt"])
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)

    code = run_cli(parser.parse_args(
        ["share", "reviewed-skill", "--accept-warnings", "--yes", "--json"]), kernel)
    result = json.loads(capsys.readouterr().out)
    assert (code, result["outcome"]) == (1, "gateway_review_required")
    assert kernel.client.state.calls == Counter(upload=1, submit=1)


class _BrokenStdout:
    """A caller that went away: every write fails (EPIPE)."""

    def write(self, _value):
        raise BrokenPipeError("caller closed the pipe")

    def flush(self):
        raise BrokenPipeError("caller closed the pipe")


def _fresh_process(kernel):
    """A new invocation process over the same retained on-disk state."""
    old = kernel.publication.state
    kernel.publication.state = kernel.publication.store = DirectState(old.state, old.root)


def _emit_raises(_args, _result):
    raise RuntimeError("emit failed")


@pytest.mark.parametrize("failure", ["stdout", "emit"])
def test_unwritten_gateway_result_is_never_recorded_as_shown(
    tmp_path, capsys, monkeypatch, failure,
):
    """Salt R2 B1: the caller never received the findings; a fresh run publishes nothing."""
    kernel = AdvisoryKernel(tmp_path)
    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    with monkeypatch.context() as patched:
        if failure == "stdout":
            patched.setattr(sys, "stdout", _BrokenStdout())
        else:
            patched.setattr(share_command, "_emit_result", _emit_raises)
        with pytest.raises((BrokenPipeError, RuntimeError)):
            run_share(parser.parse_args(["share", "reviewed-skill", "--yes", "--json"]), kernel)
    capsys.readouterr()
    operation_id = kernel.publication.share_status("reviewed-skill")["operation_id"]
    assert kernel.publication.state.publication(operation_id).get(
        "gateway_evidence_shown") is None, "nothing recorded"

    _fresh_process(kernel)
    code = run_cli(parser.parse_args(
        ["share", "reviewed-skill", "--accept-warnings", "--yes", "--json"]), kernel)
    result = json.loads(capsys.readouterr().out)
    assert (code, result["outcome"]) == (1, "gateway_review_required")
    assert "Looks like an API key assignment." in result["detail"]
    assert kernel.client.state.calls == Counter(upload=1, submit=1), "no approve or publish"


def test_mark_gateway_shown_accepts_only_evidence_this_process_rendered(tmp_path):
    kernel = AdvisoryKernel(tmp_path)
    prepared = kernel.suggest("reviewed-skill")
    pending = kernel.publication.submit(kernel.publication_review(prepared["draft_id"])["receipt"])
    operation_id = pending["operation_id"]
    live = kernel.publication.state.publication(operation_id)["gateway_evidence_hash"]
    assert pending["gateway_evidence_hash"] == live

    with pytest.raises(WisdomConflict, match="displayed by this process"):
        kernel.publication.mark_gateway_shown(operation_id, "sha256:" + "0" * 64)
    _fresh_process(kernel)
    # The live hash, supplied by a caller that never rendered it, is refused too.
    with pytest.raises(WisdomConflict, match="displayed by this process"):
        kernel.publication.mark_gateway_shown(operation_id, live)
    assert kernel.publication.state.publication(operation_id).get(
        "gateway_evidence_shown") is None

    displayed = kernel.publication_gateway_review(operation_id)
    kernel.publication.mark_gateway_shown(operation_id, displayed["gateway_evidence_hash"])
    assert kernel.publication.state.publication(operation_id)[
        "gateway_evidence_shown"] == live


@pytest.mark.parametrize("change", [
    {"scan": {"summary": "Detector revision 2 found new evidence"}},
    {"scan": {"detector_revision": "2026-10-21"}},
    {"detail": "Looks like an API key assignment." + " " * 400 + "and also a NEW secret."},
    {"unrecognized_metadata": "hash-covered but never projected"},
])
@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_acknowledge_requires_the_exact_rendered_gateway_evidence(tmp_path, surface, change):
    """Salt R1 B2: evidence outside the displayed projection still changes the hash."""
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        await ui.click(identity, "confirm")
        assert ui.view(identity).title == "⚠️ Gateway checks"
        rendered = ui.record(identity)["plan"]["gateway_evidence_hash"]
        assert rendered

        _mutate_gateway_finding(kernel, **copy.deepcopy(change))
        await ui.click(identity, "publish.ack")

        record = ui.record(identity)
        assert record["screen"] == "gateway_advisory" and ui.view(identity).title == (
            "⚠️ Gateway checks")
        assert "confirm" not in [action.action for action in ui.view(identity).actions]
        assert record["plan"]["gateway_evidence_hash"] not in {None, rendered}
        assert kernel.client.state.calls == Counter(upload=1, submit=1)

        # The card now renders the new evidence; acknowledging it reaches Share.
        await ui.click(identity, "publish.ack")
        assert ui.view(identity).title == "📤 Share skill"
        await ui.click(identity, "confirm")
        assert ui.record(identity)["result"]["publication_state"] == "published"
        assert kernel.client.state.calls == Counter(
            upload=1, submit=1, approve=1, head=1, publish=1)
        ui.runtime.close()
    asyncio.run(scenario())


def test_acknowledge_on_a_card_without_a_rendered_hash_shows_checks_again(tmp_path):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        ui = SlackHarness(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        await ui.click(identity, "confirm")
        record = ui.record(identity)
        legacy = {**record, "plan": {key: value for key, value in record["plan"].items()
                                     if key != "gateway_evidence_hash"}}
        assert ui.runtime.reviews.state.compare_replace(record, legacy)
        await ui.click(identity, "publish.ack")
        assert ui.record(identity)["screen"] == "gateway_advisory"
        assert kernel.client.state.calls == Counter(upload=1, submit=1)
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_transport_error_after_upload_stays_uncertain_and_logs_once(
    tmp_path, surface, caplog,
):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        kernel.client.state.scan_verdict = "pass"
        kernel.client.state.fail_after_effect.add("publish")
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        with caplog.at_level(logging.WARNING, logger="hermes_wisdom_plugin.native_review"):
            await ui.click(identity, "confirm")
        record = ui.record(identity)
        assert record["state"] == "completed" and record["result"]["state"] == "uncertain"
        assert ui.view(identity).title == "Uncertain"
        operation_id = record["result"]["operation_id"]
        lines = [r.getMessage() for r in caplog.records if "outcome unknown" in r.getMessage()]
        assert len(lines) == 1 and f"operation={operation_id}" in lines[0]
        assert record["plan"]["receipt"] not in lines[0]
        ui.runtime.close()
    asyncio.run(scenario())


def test_raised_unknown_failure_is_uncertain_and_logs_redacted(tmp_path, caplog):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        kernel.client.state.scan_verdict = "pass"
        kernel.client.state.approve_error = ConnectionResetError(
            "reset token=sk-live-abcdefghijklmnopqrstuvwxyz0123456789")
        ui = SlackHarness(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        with caplog.at_level(logging.WARNING, logger="hermes_wisdom_plugin.native_review"):
            await ui.click(identity, "confirm")
        assert ui.view(identity).title == "Uncertain"
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "sk-live" not in text
        ui.runtime.close()
    asyncio.run(scenario())


def test_definite_refusal_is_not_uncertain_and_logs(tmp_path, caplog):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        kernel.client.state.scan_verdict = "pass"
        ui = SlackHarness(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        # The team policy changed after review: a definite local refusal, nothing sent.
        kernel.client.state.policy_version = 99
        with caplog.at_level(logging.WARNING, logger="hermes_wisdom_plugin.native_review"):
            await ui.click(identity, "confirm")
        view = ui.view(identity)
        assert ui.record(identity)["state"] == "failed"
        assert view.title == "❌ Not shared" and "Uncertain" not in view.to_text()
        assert "Run /wisdom share again" in view.to_text()
        assert any("refused" in r.getMessage() for r in caplog.records)
        assert kernel.client.state.calls == Counter()
        ui.runtime.close()
    asyncio.run(scenario())


def test_blocked_gateway_verdict_keeps_the_rejected_card(tmp_path):
    async def scenario():
        kernel = AdvisoryKernel(tmp_path)
        kernel.client.state.scan_verdict = "blocked"
        ui = SlackHarness(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        await ui.click(identity, "confirm")
        assert ui.record(identity)["result"]["phase"] == "approval_rejected"
        assert ui.view(identity).title == "❌ Not shared"
        ui.runtime.close()
    asyncio.run(scenario())


def test_forged_share_on_gateway_checks_projection_is_not_offered():
    record = {"operation": "publish", "state": "pending", "screen": "gateway_advisory",
              "gateway_advisory": True, "team_name": "Team",
              "plan": {"candidate_flow": True, "slug": "s", "operation_id": "op",
                       "server_checks": {"security_check": {
                           "status": "advisory", "findings": FINDINGS}}}}
    offered = {action.action for action in project_view(record).actions}
    assert "confirm" not in offered and "publish.ack" in offered
