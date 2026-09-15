"""Owner-private contribution preparation and review tests."""
# Keep the private-draft authority matrix and its shared security fixtures together.
# pylint: disable=missing-function-docstring,too-few-public-methods,too-many-lines

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.editorial import apply_editorial_metadata_to_overlay
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomConflict
from hermes_wisdom_plugin.package import (
    MAX_FILE_BYTES,
    MAX_TREE_BYTES,
    PackagePolicyError,
    PackageSecurityDecision,
)
from hermes_wisdom_plugin.store import WisdomStore


class Host:
    """Deterministic host boundary for local preparation."""

    hermes_version = "1.2.3"

    def __init__(
        self,
        skills_root: Path,
        *,
        allowed: bool | None = True,
        frontmatter: dict[str, object] | None = None,
    ) -> None:
        self.skills_root = skills_root
        self.allowed = allowed
        self.frontmatter = frontmatter or {
            "name": "reviewed-skill",
            "description": "Review incident notes.",
        }
        self.scans: list[Path] = []

    def parse_skill_frontmatter(self, source: Path) -> dict[str, object]:
        del source
        return dict(self.frontmatter)

    def scan(self, overlay: Path) -> dict[str, object]:
        self.scans.append(overlay)
        return {"guard": {"allowed": self.allowed, "findings": []}}

    def check_package_security(self, overlay: Path) -> PackageSecurityDecision:
        return PackageSecurityDecision(allowed=self.scan(overlay)["guard"]["allowed"])


class RecordingLlm:
    """Recording host transport; never reaches a provider."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs, "schema_name": "author_description"})
        if self.fail:
            raise RuntimeError("offline")
        return SimpleNamespace(
            text="Draft incident reviews with a repeatable checklist.",
            provider="recording",
            model="recording-model",
        )

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("offline")
        if kwargs["schema_name"] == "wisdom_editorial_metadata":
            parsed = {
                "editorial_name": "Incident Review",
                "editorial_description": "Review incident notes with a consistent checklist.",
            }
        else:
            parsed = {
                "status": "pass",
                "summary": "No professionalism concerns detected.",
                "checks": [
                    {"key": key, "status": "pass", "finding_count": 0, "details": []}
                    for key in (
                        "profanity_or_abuse",
                        "hate_or_harassment",
                        "sexual_or_graphic_language",
                        "manipulative_or_spam",
                    )
                ],
            }
        return SimpleNamespace(
            parsed=parsed, provider="recording", model="recording-model"
        )


def make_skill(root: Path) -> Path:
    skill = root / "skills" / "reviewed-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: reviewed-skill\ndescription: Review incident notes.\n---\n"
        "# Incident review\nUse the checklist.\n",
        encoding="utf-8",
    )
    return skill


def owner(profile: Path) -> dict[str, str]:
    return {
        "profile_name": "default",
        "profile_home": str(profile),
        "account_id": "member-1",
        "organization_id": "org-1",
        "base_url": "https://gateway.invalid",
        "installation_id": "installation-1",
    }


def reviewer(tmp_path: Path, *, llm=None, allowed=True):
    profile = tmp_path / "profile"
    source = make_skill(profile)
    binding = owner(profile)
    return (
        ContributionReviewer(
            WisdomStore(tmp_path / "state"),
            Host(profile / "skills", allowed=allowed),
            lambda: dict(binding),
            llm=llm,
        ),
        source,
        binding,
    )


def test_local_preparation_is_private_exact_and_does_not_mutate_source(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    original = (source / "SKILL.md").read_bytes()

    result = value.prepare("reviewed-skill")

    assert result["state"] == "prepared"
    assert result["network_submission"] is False
    assert result["publisher_evidence"] is None
    assert "usage" not in json.dumps(result).casefold()
    assert result["security_check"]["local_status"] == "pass"
    assert result["security_check"]["status"] == "pending"
    assert result["professionalism_check"]["status"] == "unavailable"
    assert result["local_draft_id"] == result["draft_id"]
    assert result["drafted_description"] == "Review incident notes."
    assert result["local_scan"] == {
        "guard": {"allowed": True, "findings": []}
    }
    assert result["security_check"]["content_hash"] == result["hashes"]["content"]
    assert result["security_check"]["author_description_hash"] == (
        result["hashes"]["author_description"]
    )
    assert result["system_specification"]["hermes"]["minimum_version"] == "1.2.3"
    assert result["editorial_name"] == "reviewed-skill"
    assert result["editorial_description"] == "Review incident notes."
    assert result["next_step"] == (
        "Review and save the complete local package, then send its exact bytes "
        "for owner-only server review."
    )
    assert set(result["hashes"]) == {"content", "author_description", "package_manifest"}
    assert {item["path"] for item in result["files"]} == {
        "SKILL.md", "skill.manifest.json"
    }
    assert all(item["hash"].startswith("sha256:") for item in result["files"])
    assert (source / "SKILL.md").read_bytes() == original
    overlay = Path(result["overlay_path"])
    assert overlay.is_relative_to(tmp_path / "state" / "contribution-prepared")
    assert overlay != source


def test_saved_prepared_edit_updates_exact_hashes_without_mutating_source(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    original = (source / "SKILL.md").read_bytes()
    prepared = value.prepare("reviewed-skill")
    manifest = json.loads(next(
        item["content_utf8"]
        for item in prepared["files"]
        if item["path"] == "skill.manifest.json"
    ))
    manifest["requirements"]["known_limitations"] = ["Owner reviewed limitation"]
    edited_files = [
        {
            "path": item["path"],
            "content_utf8": (
                item["content_utf8"].replace(
                    "# Incident review", "# Owner edited incident review"
                )
                if item["path"] == "SKILL.md"
                else json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            ),
        }
        for item in prepared["files"]
    ]

    saved = value.save_prepared(
        prepared["draft_id"],
        author_description="Owner-approved copy.",
        files=edited_files,
        expected_hashes=prepared["hashes"],
    )

    assert saved["drafted_description"] == "Owner-approved copy."
    assert saved["hashes"]["content"] != prepared["hashes"]["content"]
    assert saved["hashes"]["author_description"] != prepared["hashes"]["author_description"]
    assert saved["hashes"]["package_manifest"] != prepared["hashes"]["package_manifest"]
    assert saved["professionalism_check"]["status"] == "unavailable"
    assert "# Owner edited incident review" in next(
        item["content_utf8"]
        for item in saved["files"]
        if item["path"] == "SKILL.md"
    )
    assert (source / "SKILL.md").read_bytes() == original
    assert Path(saved["overlay_path"]) != Path(prepared["overlay_path"])


def test_inspect_exports_a_complete_save_ready_exact_payload(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")

    payload = value.inspect_prepared(prepared["draft_id"])

    assert set(payload) == {
        "draft_id", "author_description", "files", "expected_hashes"
    }
    assert payload["draft_id"] == prepared["draft_id"]
    assert payload["author_description"] == prepared["drafted_description"]
    assert payload["expected_hashes"] == prepared["hashes"]
    assert payload["files"] == [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ]


def test_disclosed_hashes_reject_concurrent_draft_before_model_call(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, _binding = reviewer(tmp_path, llm=llm)
    prepared = value.prepare("reviewed-skill")
    disclosed = value.inspect_prepared(prepared["draft_id"])
    value.save_prepared(
        prepared["draft_id"], author_description="Concurrent private edit.",
        files=disclosed["files"], expected_hashes=disclosed["expected_hashes"],
    )

    with pytest.raises(WisdomConflict, match="changed.*displayed|displayed.*changed"):
        value.reopen(
            prepared["draft_id"], allow_model_review=True,
            reviewed_hashes=disclosed["expected_hashes"], may_apply=lambda: True,
        )

    assert not llm.calls


def test_revocation_during_real_save_validation_prevents_durable_replacement(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    admitted = [True]
    original_scan = value.host.scan

    def revoke_during_scan(overlay):
        result = original_scan(overlay)
        admitted[0] = False
        return result

    value.host.scan = revoke_during_scan
    before = value.store.draft(prepared["draft_id"])
    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.save_prepared(
            prepared["draft_id"], author_description="Revoked edit.",
            files=[
                {"path": item["path"], "content_utf8": item["content_utf8"]}
                for item in prepared["files"]
            ], expected_hashes=prepared["hashes"], may_apply=lambda: admitted[0],
        )

    assert value.store.draft(prepared["draft_id"]) == before


def test_revocation_after_model_queue_claim_prevents_provider_send(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, _binding = reviewer(tmp_path, llm=llm)
    prepared = value.prepare("reviewed-skill")
    disclosed = value.inspect_prepared(prepared["draft_id"])
    admitted = [True]
    original_claim = value.store.claim_professionalism_review

    def revoke_after_claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        admitted[0] = False
        return result

    value.store.claim_professionalism_review = revoke_after_claim
    with pytest.raises(WisdomAuthError, match="no longer current"):
        value.reopen(
            prepared["draft_id"], allow_model_review=True,
            reviewed_hashes=disclosed["expected_hashes"], may_apply=lambda: admitted[0],
        )

    assert not llm.calls


def test_stale_sequential_save_cannot_replace_new_file_set(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    first_files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ] + [{"path": "refs/first.md", "content_utf8": "winner\n"}]
    winner = value.save_prepared(
        prepared["draft_id"], author_description="Winner copy.",
        files=first_files, expected_hashes=prepared["hashes"],
    )
    stale_files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ] + [{"path": "refs/stale.md", "content_utf8": "loser\n"}]

    with pytest.raises(WisdomConflict, match="changed"):
        value.save_prepared(
            prepared["draft_id"], author_description="Stale copy.",
            files=stale_files, expected_hashes=prepared["hashes"],
        )

    current = value.reopen(prepared["draft_id"])
    assert current["hashes"] == winner["hashes"]
    assert {item["path"] for item in current["files"]} == {
        "SKILL.md", "skill.manifest.json", "refs/first.md"
    }


def test_independent_store_saves_cas_one_winner_and_clean_loser_stage(tmp_path: Path):
    first_reviewer, _source, binding = reviewer(tmp_path)
    prepared = first_reviewer.prepare("reviewed-skill")
    second_reviewer = ContributionReviewer(
        WisdomStore(tmp_path / "state"),
        Host(tmp_path / "profile" / "skills"),
        lambda: dict(binding),
    )
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def run(value: ContributionReviewer, suffix: str) -> None:
        original_cas = value.store.replace_prepared_draft

        def synchronized_cas(*args, **kwargs):
            barrier.wait(timeout=5)
            return original_cas(*args, **kwargs)

        value.store.replace_prepared_draft = synchronized_cas
        files = [
            {
                "path": item["path"],
                "content_utf8": (
                    item["content_utf8"].replace("Use the checklist.", f"Use {suffix}.")
                    if item["path"] == "SKILL.md"
                    else item["content_utf8"]
                ),
            }
            for item in prepared["files"]
        ]
        try:
            outcomes.append(value.save_prepared(
                prepared["draft_id"], author_description=f"Copy {suffix}.",
                files=files, expected_hashes=prepared["hashes"],
            ))
        except WisdomConflict as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=run, args=(first_reviewer, "alpha")),
        threading.Thread(target=run, args=(second_reviewer, "beta")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(outcomes) == 2
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, WisdomConflict) for item in outcomes) == 1
    current = first_reviewer.store.draft(prepared["draft_id"])
    assert current is not None
    assert current["description"] in {"Copy alpha.", "Copy beta."}
    edit_roots = list((tmp_path / "state" / "contribution-edits").glob("*"))
    assert len(edit_roots) == 1


def test_failed_edited_manifest_preserves_prior_draft_and_removes_new_stage(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    before = value.store.draft(prepared["draft_id"])
    original_source = (source / "SKILL.md").read_bytes()
    edited_files = [
        {
            "path": item["path"],
            "content_utf8": "{}" if item["path"] == "skill.manifest.json" else item["content_utf8"],
        }
        for item in prepared["files"]
    ]

    with pytest.raises(PackagePolicyError, match="manifest"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=edited_files, expected_hashes=prepared["hashes"],
        )

    after = value.store.draft(prepared["draft_id"])
    assert before == after
    assert (source / "SKILL.md").read_bytes() == original_source
    edits = tmp_path / "state" / "contribution-edits"
    assert not edits.exists() or not list(edits.iterdir())


def test_database_failure_preserves_prior_draft_and_removes_new_stage(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    before = value.store.draft(prepared["draft_id"])

    def fail_cas(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected write failure")

    value.store.replace_prepared_draft = fail_cas
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=prepared["files"], expected_hashes=prepared["hashes"],
        )

    assert value.store.draft(prepared["draft_id"]) == before
    edits = tmp_path / "state" / "contribution-edits"
    assert not edits.exists() or not list(edits.iterdir())


def test_precommit_store_failure_preserves_prior_draft_and_removes_unreferenced_stage(
    tmp_path: Path,
):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    before = value.store.draft(prepared["draft_id"])
    original_replace = value.store.replace_prepared_draft

    def lose_compare_before_commit(*args, **kwargs):
        kwargs["expected_hashes"] = {
            **kwargs["expected_hashes"],
            "content": "sha256:simulated-concurrent-winner",
        }
        return original_replace(*args, **kwargs)

    value.store.replace_prepared_draft = lose_compare_before_commit
    with pytest.raises(WisdomConflict, match="changed"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=prepared["files"], expected_hashes=prepared["hashes"],
        )

    assert WisdomStore(tmp_path / "state").draft(prepared["draft_id"]) == before
    edits = tmp_path / "state" / "contribution-edits"
    assert not edits.exists() or not list(edits.iterdir())


def test_postcommit_store_response_loss_retains_committed_overlay_for_reopen(
    tmp_path: Path,
):
    value, _source, binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    original_replace = value.store.replace_prepared_draft

    def lose_response_after_commit(*args, **kwargs):
        assert original_replace(*args, **kwargs) is True
        raise OSError("simulated response loss after commit")

    value.store.replace_prepared_draft = lose_response_after_commit
    with pytest.raises(OSError, match="response loss"):
        value.save_prepared(
            prepared["draft_id"], author_description="Committed owner copy.",
            files=prepared["files"], expected_hashes=prepared["hashes"],
        )

    restarted = ContributionReviewer(
        WisdomStore(tmp_path / "state"),
        Host(tmp_path / "profile" / "skills"),
        lambda: dict(binding),
    )
    reopened = restarted.reopen(prepared["draft_id"])
    assert reopened["drafted_description"] == "Committed owner copy."
    assert Path(reopened["overlay_path"]).is_dir()


def test_uncertain_store_reread_retains_failed_stage(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")

    def fail_before_commit_then_make_reread_uncertain(*_args, **_kwargs):
        def fail_read(_draft_id):
            raise sqlite3.OperationalError("injected uncertain reread")

        value.store.draft = fail_read
        raise sqlite3.OperationalError("injected failure before commit")

    value.store.replace_prepared_draft = fail_before_commit_then_make_reread_uncertain
    with pytest.raises(sqlite3.OperationalError, match="before commit"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=prepared["files"], expected_hashes=prepared["hashes"],
        )

    edits = tmp_path / "state" / "contribution-edits"
    assert len(list(edits.iterdir())) == 1


def test_cli_inspect_save_roundtrip_applies_complete_file_set_changes(
    tmp_path: Path, capsys
):
    value, source, _binding = reviewer(tmp_path)
    refs = source / "refs"
    refs.mkdir()
    (refs / "old.md").write_text("rename through CLI\n", encoding="utf-8")
    prepared = value.prepare("reviewed-skill")

    class PreparedService:
        """Bind the real reviewer to the narrow prepared CLI adapter."""

        prepared_inspect = staticmethod(value.inspect_prepared)
        prepared_save = staticmethod(value.save_prepared)

    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert run_cli(
        parser.parse_args(["prepared", "inspect", prepared["draft_id"], "--json"]),
        PreparedService(),
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    for item in payload["files"]:
        if item["path"] == "refs/old.md":
            item["path"] = "refs/renamed.md"
    payload["files"].append(
        {"path": "assets/added.txt", "content_utf8": "added through CLI\n"}
    )
    payload_path = tmp_path / "edited-draft.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    assert run_cli(
        parser.parse_args(["prepared", "save", str(payload_path), "--json"]),
        PreparedService(),
    ) == 0
    saved = json.loads(capsys.readouterr().out)
    assert {
        item["path"]: item["content_utf8"] for item in saved["files"]
    }["refs/renamed.md"] == "rename through CLI\n"
    assert {
        item["path"]: item["content_utf8"] for item in saved["files"]
    }["assets/added.txt"] == "added through CLI\n"
    assert "refs/old.md" not in {item["path"] for item in saved["files"]}
    assert (source / "refs" / "old.md").read_text(encoding="utf-8") == "rename through CLI\n"


def test_maximum_supported_escaped_inspect_output_can_be_saved(  # pylint: disable=too-many-locals
    tmp_path: Path, capsys
):
    value, source, _binding = reviewer(tmp_path)
    initial = value.prepare("reviewed-skill")
    initial_overlay = Path(initial["overlay_path"])
    for name in ("SKILL.md", "skill.manifest.json"):
        (source / name).write_bytes((initial_overlay / name).read_bytes())
    fixed_size = sum(
        (source / name).stat().st_size
        for name in ("SKILL.md", "skill.manifest.json")
    )
    remaining = MAX_TREE_BYTES - fixed_size
    assert remaining > MAX_FILE_BYTES
    refs = source / "refs"
    refs.mkdir()
    (refs / "a.txt").write_text("\x01" * MAX_FILE_BYTES, encoding="utf-8")
    (refs / "b.txt").write_text(
        "\x01" * (remaining - MAX_FILE_BYTES), encoding="utf-8"
    )
    maximum = value.prepare("reviewed-skill")

    class PreparedService:
        """Bind the real reviewer to the narrow prepared CLI adapter."""

        prepared_inspect = staticmethod(value.inspect_prepared)
        prepared_save = staticmethod(value.save_prepared)

    parser = argparse.ArgumentParser(prog="hermes wisdom")
    build_parser(parser)
    assert run_cli(
        parser.parse_args(["prepared", "inspect", maximum["draft_id"], "--json"]),
        PreparedService(),
    ) == 0
    exported = capsys.readouterr().out
    assert len(exported.encode("utf-8")) > 1024 * 1024
    payload_path = tmp_path / "maximum-draft.json"
    payload_path.write_text(exported, encoding="utf-8")

    assert run_cli(
        parser.parse_args(["prepared", "save", str(payload_path), "--json"]),
        PreparedService(),
    ) == 0
    assert json.loads(capsys.readouterr().out)["drafted_description"] == (
        maximum["drafted_description"]
    )


def test_save_complete_file_set_can_remove_a_supported_file(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    refs = source / "refs"
    refs.mkdir()
    (refs / "old.md").write_text("old\n", encoding="utf-8")
    original_skill = (source / "SKILL.md").read_bytes()
    prepared = value.prepare("reviewed-skill")
    files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
        if item["path"] != "refs/old.md"
    ]

    saved = value.save_prepared(
        prepared["draft_id"], author_description="Owner copy.",
        files=files, expected_hashes=prepared["hashes"],
    )

    assert {item["path"] for item in saved["files"]} == {
        "SKILL.md", "skill.manifest.json"
    }
    assert not (Path(saved["overlay_path"]) / "refs" / "old.md").exists()
    assert (source / "refs" / "old.md").read_text(encoding="utf-8") == "old\n"
    assert (source / "SKILL.md").read_bytes() == original_skill


def test_save_complete_file_set_can_add_a_supported_file(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    original = (source / "SKILL.md").read_bytes()
    prepared = value.prepare("reviewed-skill")
    files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ]
    files.append({"path": "refs/new.md", "content_utf8": "new\n"})

    saved = value.save_prepared(
        prepared["draft_id"], author_description="Owner copy.",
        files=files, expected_hashes=prepared["hashes"],
    )

    assert {item["path"] for item in saved["files"]} == {
        "SKILL.md", "skill.manifest.json", "refs/new.md"
    }
    assert next(
        item["content_utf8"] for item in saved["files"]
        if item["path"] == "refs/new.md"
    ) == "new\n"
    assert saved["hashes"]["content"] != prepared["hashes"]["content"]
    assert (source / "SKILL.md").read_bytes() == original


def test_save_complete_file_set_can_rename_a_supported_file(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    refs = source / "refs"
    refs.mkdir()
    (refs / "old.md").write_text("same bytes\n", encoding="utf-8")
    prepared = value.prepare("reviewed-skill")
    old_commit = value.store.draft(prepared["draft_id"])["draft_commit"]
    files = [
        {
            "path": "refs/new.md" if item["path"] == "refs/old.md" else item["path"],
            "content_utf8": item["content_utf8"],
        }
        for item in prepared["files"]
    ]

    saved = value.save_prepared(
        prepared["draft_id"], author_description=prepared["drafted_description"],
        files=files, expected_hashes=prepared["hashes"],
    )

    by_path = {item["path"]: item["content_utf8"] for item in saved["files"]}
    assert "refs/old.md" not in by_path
    assert by_path["refs/new.md"] == "same bytes\n"
    assert saved["hashes"]["content"] != prepared["hashes"]["content"]
    assert saved["hashes"]["package_manifest"] == prepared["hashes"]["package_manifest"]
    assert value.store.draft(prepared["draft_id"])["draft_commit"] != old_commit
    assert (source / "refs" / "old.md").read_text(encoding="utf-8") == "same bytes\n"


def test_save_mixed_complete_file_set_is_the_exact_new_manifest(tmp_path: Path):
    value, source, _binding = reviewer(tmp_path)
    refs = source / "refs"
    refs.mkdir()
    (refs / "remove.md").write_text("remove\n", encoding="utf-8")
    (refs / "rename.md").write_text("rename\n", encoding="utf-8")
    prepared = value.prepare("reviewed-skill")
    requested = []
    for item in prepared["files"]:
        if item["path"] == "refs/remove.md":
            continue
        path = "assets/renamed.txt" if item["path"] == "refs/rename.md" else item["path"]
        content = item["content_utf8"]
        if path == "SKILL.md":
            content = content.replace("Use the checklist.", "Use the mixed checklist.")
        requested.append({"path": path, "content_utf8": content})
    requested.append({"path": "refs/added.md", "content_utf8": "added\n"})

    saved = value.save_prepared(
        prepared["draft_id"], author_description="Mixed owner copy.",
        files=requested, expected_hashes=prepared["hashes"],
    )

    expected = {item["path"]: item["content_utf8"] for item in requested}
    actual = {item["path"]: item["content_utf8"] for item in saved["files"]}
    assert actual == expected
    assert saved["drafted_description"] == "Mixed owner copy."
    assert saved["hashes"]["content"] != prepared["hashes"]["content"]
    assert saved["hashes"]["author_description"] != prepared["hashes"]["author_description"]
    assert source.joinpath("refs/remove.md").read_text(encoding="utf-8") == "remove\n"
    assert source.joinpath("refs/rename.md").read_text(encoding="utf-8") == "rename\n"
    assert not source.joinpath("refs/added.md").exists()


@pytest.mark.parametrize("required", ["SKILL.md", "skill.manifest.json"])
def test_save_cannot_remove_required_root_file(tmp_path: Path, required: str):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    before = value.store.draft(prepared["draft_id"])
    files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"] if item["path"] != required
    ]

    with pytest.raises(PackagePolicyError, match="complete"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=files, expected_hashes=prepared["hashes"],
        )

    assert value.store.draft(prepared["draft_id"]) == before
    edits = tmp_path / "state" / "contribution-edits"
    assert not edits.exists() or not list(edits.iterdir())


@pytest.mark.parametrize("mutation", ["duplicate", "traversal", "collision"])
def test_save_rejects_unsafe_complete_file_set(tmp_path: Path, mutation: str):
    value, _source, _binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    files = [
        {"path": item["path"], "content_utf8": item["content_utf8"]}
        for item in prepared["files"]
    ]
    if mutation == "duplicate":
        files.append(dict(files[0]))
    elif mutation == "traversal":
        files[0]["path"] = "../SKILL.md"
    else:
        files.extend([
            {"path": "refs/Name.md", "content_utf8": "first\n"},
            {"path": "refs/name.md", "content_utf8": "second\n"},
        ])

    with pytest.raises(PackagePolicyError, match="duplicate|unsafe|collision"):
        value.save_prepared(
            prepared["draft_id"], author_description="Owner copy.",
            files=files, expected_hashes=prepared["hashes"],
        )


def test_reopen_denies_foreign_owner_before_bytes_or_model_disclosure(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, binding = reviewer(tmp_path, llm=llm)
    prepared = value.prepare("reviewed-skill")
    llm.calls.clear()
    (Path(prepared["overlay_path"]) / "SKILL.md").unlink()
    binding["account_id"] = "foreign-member"

    with pytest.raises(WisdomAuthError, match="owner"):
        value.reopen(prepared["draft_id"], allow_model_review=True)
    assert not llm.calls


def test_saved_edit_gets_fresh_exact_model_review_only_when_authorized(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, _binding = reviewer(tmp_path, llm=llm)
    prepared = value.prepare("reviewed-skill")
    assert not llm.calls
    files = [
        {
            "path": item["path"],
            "content_utf8": item["content_utf8"].replace(
                "Use the checklist.", "Use the owner checklist."
            ),
        }
        for item in prepared["files"]
    ]
    saved = value.save_prepared(
        prepared["draft_id"], author_description="Owner copy.",
        files=files, expected_hashes=prepared["hashes"],
    )
    assert saved["professionalism_check"]["status"] == "unavailable"
    assert not llm.calls

    reviewed = value.reopen(prepared["draft_id"], allow_model_review=True)
    assert [call["schema_name"] for call in llm.calls] == [
        "wisdom_professionalism_review"
    ]
    assert reviewed["professionalism_check"]["content_hash"] == saved["hashes"]["content"]
    assert reviewed["professionalism_check"]["author_description_hash"] == (
        saved["hashes"]["author_description"]
    )


def test_saved_edit_reopens_from_a_fresh_store_instance(tmp_path: Path):
    value, _source, binding = reviewer(tmp_path)
    prepared = value.prepare("reviewed-skill")
    files = [
        {
            "path": item["path"],
            "content_utf8": item["content_utf8"].replace(
                "Use the checklist.", "Use the persisted checklist."
            ),
        }
        for item in prepared["files"]
    ]
    saved = value.save_prepared(
        prepared["draft_id"], author_description="Persisted owner copy.",
        files=files, expected_hashes=prepared["hashes"],
    )

    restarted = ContributionReviewer(
        WisdomStore(tmp_path / "state"),
        Host(tmp_path / "profile" / "skills"),
        lambda: dict(binding),
    )
    reopened = restarted.reopen(prepared["draft_id"])

    assert reopened["hashes"] == saved["hashes"]
    assert reopened["drafted_description"] == "Persisted owner copy."
    assert "Use the persisted checklist." in next(
        item["content_utf8"]
        for item in reopened["files"]
        if item["path"] == "SKILL.md"
    )


def test_repeated_preparation_preserves_an_edited_private_overlay(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path)
    first = value.prepare("reviewed-skill")
    overlay_skill = Path(first["overlay_path"]) / "SKILL.md"
    overlay_skill.write_text("owner edited private draft\n", encoding="utf-8")

    with pytest.raises(WisdomConflict, match="edited draft"):
        value.prepare("reviewed-skill")

    assert overlay_skill.read_text(encoding="utf-8") == "owner edited private draft\n"


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo"])
def test_reopened_overlay_rejects_unsafe_files(tmp_path: Path, entry_kind: str):
    value, _source, _binding = reviewer(tmp_path)
    first = value.prepare("reviewed-skill")
    unsafe = Path(first["overlay_path"]) / "refs" / "unsafe.md"
    unsafe.parent.mkdir()
    if entry_kind == "symlink":
        target = tmp_path / "outside.md"
        target.write_text("outside\n", encoding="utf-8")
        unsafe.symlink_to(target)
    else:
        os.mkfifo(unsafe)

    with pytest.raises(PackagePolicyError, match="symlink|special filesystem"):
        value.prepare("reviewed-skill")


def test_owner_change_is_denied_before_persisting_or_reviewing(tmp_path: Path):
    value, _source, binding = reviewer(tmp_path)
    calls = 0

    def changed_owner():
        nonlocal calls
        calls += 1
        current = dict(binding)
        if calls > 1:
            current["account_id"] = "other-member"
        return current

    value.owner_provider = changed_owner
    with pytest.raises(WisdomAuthError, match="owner changed"):
        value.prepare("reviewed-skill")
    assert value.store.prepared_draft("missing", "missing") is None


def test_reopening_draft_requires_its_persisted_full_owner_before_model_disclosure(
    tmp_path: Path,
):
    llm = RecordingLlm()
    value, _source, binding = reviewer(tmp_path, llm=llm)
    first = value.prepare("reviewed-skill")
    persisted = value.store.draft(first["draft_id"])
    assert persisted is not None
    assert persisted["owner"] == binding

    binding["account_id"] = "other-member"
    with pytest.raises(WisdomAuthError, match="owner"):
        value.prepare("reviewed-skill", allow_model_review=True)

    assert not llm.calls


@pytest.mark.parametrize("name", ["../reviewed-skill", "missing", "reviewed-skill/child"])
def test_missing_or_unsafe_source_is_rejected(tmp_path: Path, name: str):
    value, _source, _binding = reviewer(tmp_path)
    with pytest.raises(Exception, match="local skill"):
        value.prepare(name)


def test_explicit_model_review_is_tool_free_hash_bound_and_route_attributed(tmp_path: Path):
    llm = RecordingLlm()
    value, source, _binding = reviewer(tmp_path, llm=llm)
    original = (source / "SKILL.md").read_bytes()

    result = value.prepare("reviewed-skill", allow_model_review=True)

    assert [call["schema_name"] for call in llm.calls] == [
        "wisdom_editorial_metadata", "wisdom_professionalism_review"
    ]
    assert all(call.get("input") and "tools" not in call for call in llm.calls)
    assert result["professionalism_check"]["status"] == "pass"
    assert result["professionalism_check"]["content_hash"] == result["hashes"]["content"]
    assert result["professionalism_check"]["author_description_hash"] == (
        result["hashes"]["author_description"]
    )
    assert result["professionalism_check"]["provenance"] == {
        "kind": "agent_assessed", "provider": "recording", "model": "recording-model"
    }
    persisted = value.store.professionalism_review(
        skill_id=result["skill_id"],
        content_hash=result["hashes"]["content"],
        author_description_hash=result["hashes"]["author_description"],
    )
    assert persisted is not None and persisted["state"] == "complete"
    assert persisted["result"] == result["professionalism_check"]
    overlay_text = (Path(result["overlay_path"]) / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "editorial_name:" in overlay_text and "Incident Review" in overlay_text
    assert (source / "SKILL.md").read_bytes() == original


def test_unavailable_model_review_never_becomes_pass(tmp_path: Path):
    value, _source, _binding = reviewer(tmp_path, llm=RecordingLlm(fail=True))
    result = value.prepare("reviewed-skill", allow_model_review=True)
    assert result["professionalism_check"]["status"] == "unavailable"


def test_existing_editorial_description_precedes_canonical_author_copy(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, _binding = reviewer(tmp_path, llm=llm)
    value.host.frontmatter = {
        "name": "reviewed-skill",
        "description": "Agent-facing routing copy.",
        "metadata": {
            "hermes": {
                "editorial_name": "Incident Review",
                "editorial_description": "Owner-facing incident review copy.",
            }
        },
    }

    result = value.prepare("reviewed-skill")

    assert result["drafted_description"] == "Owner-facing incident review copy."
    assert not llm.calls


def test_missing_author_copy_uses_authorized_model_fallback(tmp_path: Path):
    llm = RecordingLlm()
    value, _source, _binding = reviewer(tmp_path, llm=llm)
    value.host.frontmatter = {"name": "reviewed-skill"}

    result = value.prepare("reviewed-skill", allow_model_review=True)

    assert result["drafted_description"] == (
        "Draft incident reviews with a repeatable checklist."
    )
    assert [call["schema_name"] for call in llm.calls] == [
        "author_description",
        "wisdom_editorial_metadata",
        "wisdom_professionalism_review",
    ]


def test_editorial_overlay_round_trips_flow_metadata_comments_quotes_and_crlf(
    tmp_path: Path,
):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    skill_md = overlay / "SKILL.md"
    original = (
        '\ufeff---\r\nname: "reviewed-skill" # keep name comment\r\n'
        'description: "Review: incident notes."\r\n'
        'metadata: {hermes: {custom: "keep"}, owner: "Shannon"}\r\n'
        'unknown: "quoted value"\r\n---\r\n# Body\r\n'
    )
    skill_md.write_text(original, encoding="utf-8", newline="")

    changed = apply_editorial_metadata_to_overlay(
        overlay,
        editorial_name="Incident Review",
        editorial_description="Review incident notes with a checklist.",
        frontmatter={"metadata": {"hermes": {"custom": "keep"}}},
    )

    with skill_md.open("r", encoding="utf-8", newline="") as stream:
        updated = stream.read()
    assert changed is True
    assert updated.startswith("\ufeff---\r\n")
    assert "# keep name comment\r\n" in updated
    assert 'name: "reviewed-skill"' in updated
    assert 'unknown: "quoted value"' in updated
    assert updated.count("metadata:") == 1
    assert updated.count("hermes:") == 1
    assert 'custom: "keep"' in updated
    assert 'owner: "Shannon"' in updated
    assert "editorial_name: Incident Review" in updated
    assert "editorial_description: Review incident notes with a checklist." in updated
    assert "\n" not in updated.replace("\r\n", "")
