"""Sharing names the exact reason a skill's own name or description cannot be shared,
and Hermes proposes that copy with the owner's model when it can (manual sharing)."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_contribution_review import RecordingLlm
from test_reshare_pending import Kernel
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.editorial import local_copy_problem
from hermes_wisdom_plugin.errors import ShareCopyError

_FIX = "Edit the skill's SKILL.md frontmatter, then retry."
_NO_MODEL = "Hermes has no model available to propose it."
_DISABLED = ("Hermes is set not to send skills to your model "
             "(qualification.allow_model_review is false).")
_SECRET = "Hermes did not send this skill to your model because it may contain a secret."
_FAILED = "Hermes could not propose it with your model."
_LONG = "x" * 839
_PROPOSED = "Review incident notes with a consistent checklist."
_PROVENANCE = "Description written by Hermes from your SKILL.md."


@pytest.mark.parametrize(("name", "description", "expected"), [
    ("", "Review incident notes.", "Name is missing."),
    ("reviewed-skill", "", "Description is missing."),
    ("reviewed-skill", "   ", "Description is missing."),
    ("reviewed-skill", _LONG, "Description is 839 characters; the limit is 320."),
    ("n" * 101, "Review incident notes.", "Name is 101 characters; the limit is 100."),
    ("", _LONG, "Name is missing. Description is 839 characters; the limit is 320."),
    ("reviewed-skill", "x" * 320, None),
])
def test_copy_problem_names_the_measured_cause(name, description, expected):
    assert local_copy_problem(name, description) == expected


def _write_skill(kernel, frontmatter: dict[str, str], body: str = "# Incident review\n") -> None:
    lines = "".join(f"{key}: {value}\n" for key, value in frontmatter.items())
    (kernel.source / "SKILL.md").write_text(f"---\n{lines}---\n{body}", encoding="utf-8")
    kernel.reviewer.host.frontmatter = dict(frontmatter)


_CASES = {
    "missing-name": ({"description": "Review incident notes."}, "Name is missing."),
    "missing-description": ({"name": "reviewed-skill"}, "Description is missing."),
    "over-limit": ({"name": "reviewed-skill", "description": _LONG},
                   "Description is 839 characters; the limit is 320."),
}


def _share(kernel, *flags):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return run_cli(parser.parse_args(["share", "reviewed-skill", *flags]), kernel)


def _with_model(tmp_path, frontmatter, **llm):
    kernel = Kernel(tmp_path)
    _write_skill(kernel, frontmatter)
    kernel.reviewer.llm = RecordingLlm(**llm)
    return kernel


# Without a model: the exact problem, why Hermes could not propose copy, nothing uploaded.

@pytest.mark.parametrize("case", sorted(_CASES))
def test_terminal_share_reports_the_exact_problem_and_uploads_nothing(tmp_path, capsys, case):
    frontmatter, problem = _CASES[case]
    kernel = Kernel(tmp_path)
    _write_skill(kernel, frontmatter)
    assert _share(kernel, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "copy_incomplete"
    assert result["detail"] == f"{problem} {_NO_MODEL} {_FIX}"
    assert not kernel.client.state.calls


@pytest.mark.parametrize("case", sorted(_CASES))
def test_preparation_refuses_copy_it_cannot_share_and_saves_nothing(tmp_path, case):
    frontmatter, problem = _CASES[case]
    kernel = Kernel(tmp_path)
    _write_skill(kernel, frontmatter)
    with pytest.raises(ShareCopyError) as raised:
        kernel.suggest("reviewed-skill")
    assert str(raised.value) == f"{problem} {_NO_MODEL} {_FIX}"
    assert not kernel.reviewer.store.prepared_drafts(kernel.owner)
    assert not kernel.client.state.calls


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_native_share_card_names_the_exact_problem(tmp_path, surface, case):
    frontmatter, problem = _CASES[case]

    async def scenario():
        kernel = Kernel(tmp_path)
        _write_skill(kernel, frontmatter)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        view = ui.view(identity)
        assert view.title == "Skill needs attention"
        assert view.items[0].detail == f"{problem} {_NO_MODEL} {_FIX}"
        assert "Add a name and a description" not in str(ui.posts[-1])
        assert problem in str(ui.posts[-1])
        assert [action.action for action in view.actions] == ["suggest.refresh", "suggest.keep"]
        # Fixing SKILL.md and pressing Retry prepares the current source again.
        _write_skill(kernel, {"name": "reviewed-skill", "description": "Review incident notes."})
        await ui.click(identity, "suggest.refresh")
        assert ui.view(identity).title == "📤 Share skill"
        assert not kernel.client.state.calls["publish"]
        ui.runtime.close()
    asyncio.run(scenario())


# With the owner's model: one call proposes only the failing fields; Share publishes them.

def test_valid_copy_never_calls_the_model(tmp_path):
    kernel = _with_model(tmp_path, {"name": "reviewed-skill",
                                    "description": "Review incident notes."})
    prepared = kernel.suggest("reviewed-skill")
    assert prepared["editorial_status"] == "complete"
    assert prepared["editorial_description"] == "Review incident notes."
    assert "copy_provenance_line" not in prepared
    assert not kernel.reviewer.llm.calls


def test_over_limit_description_is_proposed_with_exactly_one_model_call(tmp_path):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    prepared = kernel.suggest("reviewed-skill")
    calls = kernel.reviewer.llm.calls
    assert [call["schema_name"] for call in calls] == ["wisdom_share_copy"]
    assert "coworker_benefit" not in json.dumps(calls[0]["json_schema"])
    assert "untrusted" in calls[0]["instructions"]
    assert prepared["editorial_status"] == "complete"
    assert "editorial_problem" not in prepared
    # Only the failing field is replaced; the valid name stays the skill's own.
    assert prepared["editorial_name"] == "reviewed-skill"
    assert prepared["editorial_description"] == prepared["drafted_description"] == _PROPOSED
    assert prepared["copy_written_by_hermes"] == ["editorial_description"]
    assert prepared["copy_provenance_line"] == _PROVENANCE
    assert prepared["copy_original"] == {"editorial_description": _LONG}
    draft = kernel.reviewer.store.draft(prepared["draft_id"])
    assert draft["copy_provenance"] == {
        "kind": "agent_assessed", "provider": "recording", "model": "recording-model"}
    # Reopening the saved draft reuses the proposal without another model call.
    assert kernel.suggest("reviewed-skill")["copy_provenance_line"] == _PROVENANCE
    assert len(calls) == 1


def test_missing_name_is_proposed_and_the_description_kept(tmp_path):
    kernel = _with_model(tmp_path, _CASES["missing-name"][0])
    prepared = kernel.suggest("reviewed-skill")
    assert prepared["editorial_name"] == "Incident Review"
    assert prepared["editorial_description"] == "Review incident notes."
    assert prepared["copy_provenance_line"] == "Name written by Hermes from your SKILL.md."
    assert len(kernel.reviewer.llm.calls) == 1


def test_terminal_share_publishes_exactly_the_proposed_copy(tmp_path, capsys):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    assert _share(kernel, "--yes") == 0
    output = capsys.readouterr().out
    assert _PROPOSED in output and _PROVENANCE in output
    assert "Original description in SKILL.md: " + _LONG[:40] in output
    assert kernel.client.state.calls["publish"] == 1
    assert kernel.client.state.drafts["draft-1"].authorDescription == _PROPOSED
    assert len(kernel.reviewer.llm.calls) == 1


def test_owner_edit_replaces_the_proposal_and_drops_the_provenance(tmp_path):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    prepared = kernel.suggest("reviewed-skill")
    payload = kernel.prepared_inspect(prepared["draft_id"])
    edited = kernel.prepared_save(
        prepared["draft_id"], author_description="Owner wording for the team.",
        files=payload["files"], expected_hashes=payload["expected_hashes"],
    )
    assert edited["drafted_description"] == "Owner wording for the team."
    assert "copy_provenance_line" not in edited
    assert len(kernel.reviewer.llm.calls) == 1


def test_explicit_refusal_never_calls_the_model(tmp_path):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    with pytest.raises(ShareCopyError) as raised:
        kernel.reviewer.prepare("reviewed-skill", allow_model_review=False,
                                allow_model_copy=False)
    assert str(raised.value) == f"{_CASES['over-limit'][1]} {_DISABLED} {_FIX}"
    assert not kernel.reviewer.llm.calls


def test_secret_looking_content_is_never_sent_to_the_model(tmp_path):
    kernel = Kernel(tmp_path)
    _write_skill(kernel, _CASES["over-limit"][0],
                 body="# Incident review\nexport KEY=AKIA" + "A" * 16 + "\n")
    kernel.reviewer.llm = RecordingLlm()
    with pytest.raises(ShareCopyError) as raised:
        kernel.suggest("reviewed-skill")
    assert str(raised.value) == f"{_CASES['over-limit'][1]} {_SECRET} {_FIX}"
    assert not kernel.reviewer.llm.calls
    assert not kernel.client.state.calls


def test_scanner_block_on_model_inputs_is_never_sent_to_the_model(tmp_path):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    kernel.reviewer.host.allowed = False
    with pytest.raises(ShareCopyError, match="may contain a secret"):
        kernel.suggest("reviewed-skill")
    assert not kernel.reviewer.llm.calls


def test_model_failure_reports_the_exact_problem_and_uploads_nothing(tmp_path, capsys):
    kernel = _with_model(tmp_path, _CASES["over-limit"][0], fail=True)
    assert _share(kernel, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "copy_incomplete"
    assert result["detail"] == f"{_CASES['over-limit'][1]} {_FAILED} {_FIX}"
    assert len(kernel.reviewer.llm.calls) == 1
    assert not kernel.client.state.calls
    assert not kernel.reviewer.store.prepared_drafts(kernel.owner)


def test_runtime_config_refusal_is_the_documented_off_switch(tmp_path, monkeypatch):
    from installed_test_support import JsonState  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.runtime import InvocationService  # pylint: disable=import-outside-toplevel
    kernel = _with_model(tmp_path, _CASES["over-limit"][0])
    service = InvocationService(tmp_path / "runtime", ("fixture", tmp_path),
                                state=JsonState(tmp_path / "runtime"), llm=kernel.reviewer.llm)
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(kernel.owner))
    monkeypatch.setattr(service, "check_owner", lambda: None)
    monkeypatch.setattr(service, "_contribution_reviewer", lambda: kernel.reviewer)
    monkeypatch.setattr(service.local_state, "bind_owner", lambda _owner: None)
    service._get_config = lambda key, default=None: (  # pylint: disable=protected-access
        False if key == "qualification.allow_model_review" else default)
    with pytest.raises(ShareCopyError, match="qualification.allow_model_review is false"):
        service.suggest("reviewed-skill")
    assert not kernel.reviewer.llm.calls
    # Unset (the default) allows the proposal in manual sharing.
    service._get_config = lambda key, default=None: default  # pylint: disable=protected-access
    assert service.suggest("reviewed-skill")["copy_provenance_line"] == _PROVENANCE
    assert len(kernel.reviewer.llm.calls) == 1


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_share_card_shows_proposed_copy_and_share_publishes_it(tmp_path, surface):
    async def scenario():
        kernel = _with_model(tmp_path, _CASES["over-limit"][0])
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        view = ui.view(identity)
        assert view.title == "📤 Share skill"
        assert view.summary == _PROPOSED
        assert _PROVENANCE in view.notice and "View details shows your original" in view.notice
        assert [a.action for a in view.actions if a.primary] == ["confirm"]
        assert _PROVENANCE in str(ui.posts[-1])
        await ui.click(identity, "details")
        details = ui.view(identity).to_text()
        assert "Original description in SKILL.md: " + _LONG[:40] in details
        await ui.click(identity, "back")
        await ui.click(identity, "confirm")
        assert ui.record(identity)["state"] == "completed"
        assert kernel.client.state.calls["publish"] == 1
        assert kernel.client.state.drafts["draft-1"].authorDescription == _PROPOSED
        assert len(kernel.reviewer.llm.calls) == 1
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_model_failure_shows_the_exact_message_with_retry(tmp_path, surface):
    async def scenario():
        kernel = _with_model(tmp_path, _CASES["over-limit"][0], fail=True)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        view = ui.view(identity)
        assert view.title == "Skill needs attention"
        assert view.items[0].detail == f"{_CASES['over-limit'][1]} {_FAILED} {_FIX}"
        assert [action.action for action in view.actions] == ["suggest.refresh", "suggest.keep"]
        # Retry once the model is back proposes copy and opens the Share card.
        kernel.reviewer.llm.fail = False
        await ui.click(identity, "suggest.refresh")
        assert ui.view(identity).title == "📤 Share skill"
        assert ui.view(identity).summary == _PROPOSED
        assert not kernel.client.state.calls
        ui.runtime.close()
    asyncio.run(scenario())
