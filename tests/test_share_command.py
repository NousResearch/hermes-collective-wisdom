"""One-shot sharing through real preparation and publication services."""
# pylint: disable=missing-function-docstring,too-few-public-methods
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from test_contribution_review import reviewer
from test_publication import RecordingPublicationClient
from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.direct_services import PublicationService
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict, WisdomNotFound


class ShareCli:
    """Real domain services with only host/account/network boundaries replaced."""

    def __init__(self, tmp_path):
        self.reviewer, self.source, self.owner = reviewer(tmp_path)
        self.client = RecordingPublicationClient(self.owner)
        self.publication = PublicationService(
            self.reviewer.store, self.reviewer, self.client, lambda: dict(self.owner)
        )
        self.detail = None

    def binding(self):
        return dict(self.owner)

    def share_status(self, skill):
        return self.publication.share_status(skill)

    def suggest(self, skill, *, allow_model_review=False):
        return self.reviewer.prepare(skill, allow_model_review=allow_model_review)

    def prepared_review(self, draft, *, allow_model_review=False):
        return self.reviewer.reopen(draft, allow_model_review=allow_model_review)

    def prepared_inspect(self, draft):
        return self.reviewer.inspect_prepared(draft)

    def prepared_save(self, draft, **kwargs):
        return self.reviewer.save_prepared(draft, **kwargs)

    def publication_review(self, draft, *, accept_warnings=False):
        return self.publication.review(draft, accept_warnings=accept_warnings)

    def publication_submit(self, receipt):
        return self.publication.submit(receipt)

    def show(self, _skill):
        if self.detail is None:
            raise WisdomNotFound("not found")
        return self.detail


def run(service, *flags):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return run_cli(parser.parse_args(["share", "reviewed-skill", *flags]), service)


def test_share_exact_receipt_publishes_once(tmp_path, capsys):
    service = ShareCli(tmp_path)
    assert run(service, "--yes", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "shared"
    assert result["skill"] == "reviewed-skill"
    assert result["version"] == 1
    assert "portal_url" in result and "detail" in result
    assert service.client.state.calls["publish"] == 1
    assert service.client.state.calls["submit"] == 1


@pytest.mark.parametrize("json_mode", [False, True])
def test_noninteractive_requires_yes(tmp_path, capsys, monkeypatch, json_mode):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run(service, *(["--json"] if json_mode else [])) == 1
    output = capsys.readouterr().out
    assert "--yes" in output
    assert not service.client.state.calls["upload"]
    if json_mode:
        assert json.loads(output)["outcome"] == "confirmation_required"
    else:
        assert "Team" in output and "org-1" in output and "SKILL.md" in output


@pytest.mark.parametrize("phase", ["submit", "publish"])
def test_uncertain_is_not_resent_on_second_share(tmp_path, capsys, phase):
    service = ShareCli(tmp_path)
    service.client.state.fail_after_effect.add(phase)
    for _attempt in range(2):
        assert run(service, "--yes", "--json") == 2
        result = json.loads(capsys.readouterr().out)
        assert result["outcome"] == "uncertain"
        assert "hermes wisdom publication status " in result["detail"]
    assert service.client.state.calls[phase] == 1


def test_scan_blocked_has_distinct_outcome(tmp_path, capsys):
    service = ShareCli(tmp_path)
    service.client.state.scan_verdict = "blocked"
    assert run(service, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "blocked_by_scan"
    assert "Not shared" in result["detail"]
    assert not service.client.state.calls["publish"]


def test_slug_owned_is_not_uncertain(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)

    def owned(*_args, **_kwargs):
        raise WisdomConflict("slug_owned", status=409, code="slug_owned")

    monkeypatch.setattr(service.client, "publish", owned)
    assert run(service, "--yes", "--json") == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "slug_owned"


def test_not_entitled(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)

    def denied():
        raise WisdomAuthError("missing scope")

    monkeypatch.setattr(service, "binding", denied)
    assert run(service, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "not_entitled"
    assert "not enabled for this account and team" in result["detail"]
    assert not service.client.state.calls["upload"]


def test_already_shared_compares_author_and_latest_exact_hashes(tmp_path, capsys):
    service = ShareCli(tmp_path)
    prepared = service.suggest("reviewed-skill")
    service.detail = {
        "skill": {"state": "active", "created_by_user_id": "member-1"},
        "latest_version_detail": {"version": {
            "version": 3, "content_hash": prepared["hashes"]["content"],
            "author_description_hash": prepared["hashes"]["author_description"],
            "package_manifest_hash": prepared["hashes"]["package_manifest"],
        }},
        "portal_url": "https://portal.invalid/wisdom/skills/skill-1",
    }
    assert run(service, "--yes", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "already_shared"
    assert result["version"] == 3
    assert "v3 already matches" in result["detail"]
    assert not service.client.state.calls["upload"]


def test_compiled_file_refused_before_screen(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    (service.source / "program").write_bytes(b"\x7fELF" + b"\x00" * 100)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert run(service) == 1
    output = capsys.readouterr().out
    assert "compiled executable" in output and "program" in output
    assert "text and scripts only" in output
    assert "[s] Share" not in output
    assert not service.client.state.calls["upload"]


def test_interactive_share_has_one_confirmation(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "s"

    monkeypatch.setattr("builtins.input", answer)
    assert run(service) == 0
    output = capsys.readouterr().out
    assert len(prompts) == 1
    assert "Everyone in org-1, immediately. No reviewer." in output
    assert "bytes" in output
    assert "Shared with org-1 as v1" in output
    assert "hermes wisdom install reviewed-skill" in output
    assert "hermes wisdom unshare reviewed-skill" in output


def test_default_enter_cancels(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert run(service) == 0
    assert "cancel" in capsys.readouterr().out.lower()
    assert not service.client.state.calls["upload"]


def test_warning_flag_is_separate_from_yes(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    original = service.reviewer.host.scan

    def warning(path):
        return {**original(path), "allowed": False, "findings": [{
            "severity": "warning", "category": "instructions", "file": "SKILL.md",
            "line": 4, "description": "Review this instruction", "pattern_id": "fixture",
        }]}

    monkeypatch.setattr(service.reviewer.host, "scan", warning)
    assert run(service, "--yes", "--json") == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "warnings_required"
    assert not service.client.state.calls["upload"]
    assert run(service, "--yes", "--accept-warnings", "--json") == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "shared"


def test_manifest_declares_minimum_and_current_description():
    from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel
    manifest = YAML(typ="safe").load(Path("plugin.yaml").read_text(encoding="utf-8"))
    assert manifest.get("requires_hermes")
    assert "read-only" not in manifest["description"]


def _host_version_tuple(version):
    # Mirrors the host gate: Hermes compares requires_hermes to its semver base
    # version (hermes_cli.plugins_manifest.version_satisfies), never the release date.
    return tuple(int(part) for part in version.split(".")[:3])


def test_manifest_minimum_loads_on_the_2026_9_24_release_host():
    from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel
    manifest = YAML(typ="safe").load(Path("plugin.yaml").read_text(encoding="utf-8"))
    spec = manifest["requires_hermes"]
    assert spec.startswith(">=")
    minimum = _host_version_tuple(spec[2:].strip())
    assert _host_version_tuple("0.21.5") >= minimum  # v2026.9.24 release base version
    assert _host_version_tuple("0.21.3") < minimum  # older host lacks the required API


def test_changed_source_after_confirmation_is_not_uploaded(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def answer(_prompt):
        (service.source / "SKILL.md").write_text("Changed source", encoding="utf-8")
        return "s"

    monkeypatch.setattr("builtins.input", answer)
    assert run(service) == 1
    assert "changed" in capsys.readouterr().out.lower()
    assert not service.client.state.calls["upload"]


def test_description_edit_is_in_submitted_exact_package(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["e", "A revised incident checklist.", "s"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert run(service) == 0
    assert "Shared with" in capsys.readouterr().out
    description = service.client.state.drafts["draft-1"].authorDescription
    assert description == "A revised incident checklist."


def test_slug_owned_repeated_share_retains_definite_rejection(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)

    def owned(*_args, **_kwargs):
        raise WisdomConflict("slug_owned", status=409, code="slug_owned")

    monkeypatch.setattr(service.client, "publish", owned)
    for _attempt in range(2):
        assert run(service, "--yes", "--json") == 1
        assert json.loads(capsys.readouterr().out)["outcome"] == "slug_owned"
    assert service.client.state.calls["submit"] == 1


def test_script_is_labeled_before_share(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    script = service.source / "example.sh"
    script.write_text("#!/bin/sh\nprintf safe\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    assert run(service) == 0
    assert "example.sh [script]" in capsys.readouterr().out


def test_edit_resets_flag_warning_acknowledgement(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    original = service.reviewer.host.scan

    def warning(path):
        return {**original(path), "allowed": False, "findings": [{
            "severity": "warning", "category": "instructions", "file": "SKILL.md",
            "line": 4, "description": "Review instruction", "pattern_id": "fixture",
        }]}

    monkeypatch.setattr(service.reviewer.host, "scan", warning)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["e", "New description", "q"])
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    assert run(service, "--accept-warnings") == 1
    assert any("Acknowledge" in prompt for prompt in prompts)
    assert not service.client.state.calls["upload"]
    capsys.readouterr()


@pytest.mark.parametrize("mode,line,code", [
    ("blocked", "Not shared. The server scan", 1),
    ("owned", "already shared by another author", 1),
    ("uncertain", "Nothing will be resent", 2),
    ("denied", "not enabled for this account and team", 1),
    ("moderated", "not shared yet", 0),
])
def test_human_outcome_rows(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    tmp_path, capsys, monkeypatch, mode, line, code,
):
    service = ShareCli(tmp_path)
    if mode == "blocked":
        service.client.state.scan_verdict = "blocked"
    if mode == "uncertain":
        service.client.state.fail_after_effect.add("publish")
    if mode == "moderated":
        service.client.state.mode = "moderated"
    if mode == "owned":
        def owned(*_args, **_kwargs):
            raise WisdomConflict("slug_owned", status=409, code="slug_owned")
        monkeypatch.setattr(service.client, "publish", owned)
    if mode == "denied":
        def denied():
            raise WisdomAuthError("missing scope")
        monkeypatch.setattr(service, "binding", denied)
    assert run(service, "--yes") == code
    assert line in capsys.readouterr().out


def test_json_unexpected_preparation_failure_has_stable_envelope(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)

    def broken(*_args, **_kwargs):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(service, "suggest", broken)
    assert run(service, "--yes", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "not_shared"
    assert "private filesystem detail" not in result["detail"]
    assert not service.client.state.calls["upload"]


def test_post_submit_failure_is_uncertain_not_failure(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    original = service.publication_submit

    def lost(receipt):
        original(receipt)
        raise OSError("lost local acknowledgement")

    monkeypatch.setattr(service, "publication_submit", lost)
    assert run(service, "--yes", "--json") == 2
    assert json.loads(capsys.readouterr().out)["outcome"] == "uncertain"
    assert service.client.state.calls["publish"] == 1


def test_team_change_before_review_screen_refuses(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    original = service.publication_review

    def changed(*args, **kwargs):
        review = original(*args, **kwargs)
        service.owner["organization_id"] = "other-team"
        return review

    monkeypatch.setattr(service, "publication_review", changed)
    assert run(service, "--yes", "--json") == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "not_entitled"
    assert not service.client.state.calls["upload"]


def test_view_file_is_read_only(tmp_path, capsys, monkeypatch):
    service = ShareCli(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["v", "1", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert run(service) == 0
    assert "Use the checklist" in capsys.readouterr().out
    assert not service.client.state.calls["upload"]


def test_repeat_success_is_already_shared_without_upload(tmp_path, capsys):
    service = ShareCli(tmp_path)
    assert run(service, "--yes", "--json") == 0
    capsys.readouterr()
    draft = service.client.state.drafts["draft-1"]
    service.detail = {
        "skill": {"state": "active", "created_by_user_id": "member-1"},
        "latest_version_detail": {"version": {
            "version": 1, "content_hash": draft.contentHash,
            "author_description_hash": draft.authorDescriptionHash,
            "package_manifest_hash": draft.packageManifestHash,
        }},
    }
    assert run(service, "--yes", "--json") == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "already_shared"
    assert service.client.state.calls["upload"] == 1
