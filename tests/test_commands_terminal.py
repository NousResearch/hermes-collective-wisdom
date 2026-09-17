"""Real subprocess/PTY coverage for the human and JSON command paths."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time

from hermes_wisdom_plugin.terminal_ui import review_identity


_COMMAND = r'''
import argparse
import json
import os
import sys
from hermes_wisdom_plugin.commands import build_parser, run_cli

MODE = sys.argv[1]

def record(value):
    path = os.environ.get("CALL_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(value + "\n")

class Service:
    def suggest(self, skill, *, allow_model_review=False):
        result = {
            "state": "prepared",
            "draft_id": "local:pty-draft",
            "skill_name": skill,
            "description": "Synthetic PTY fixture",
            "hashes": {
                "content": "sha256:content", "author_description": "sha256:description",
                "package_manifest": "sha256:manifest",
            },
            "files": [{
                "path": "bad\\x1b[2J.md", "mode": "file", "size": 1,
                "hash": "sha256:file", "content_utf8": "safe fixture",
            }],
            "security_check": {"local_status": "pass", "summary": "No warnings."},
            "professionalism_check": {"status": "unavailable"},
            "network_submission": False,
        }
        if MODE == "share":
            result["security_check"] = {
                "local_status": "warning_required", "summary": "Review warning."
            }
            result["local_scan"] = {"allowed": False, "findings": [{
                "severity": "warning", "category": "instructions",
                "pattern_id": "pty-warning", "file": "SKILL.md", "line": 4,
                "description": "Synthetic warning detail.", "match": "RAW_PTY_SECRET",
            }]}
        return result
    def prepared_review(self, draft_id, *, allow_model_review=False):
        result = self.suggest("fixture", allow_model_review=allow_model_review)
        result["draft_id"] = draft_id
        return result
    def publication_review(self, draft_id, *, accept_warnings=False):
        if MODE != "share":
            raise AssertionError("cancel and JSON paths must not review publication")
        record("review:" + str(accept_warnings))
        return {
            **self.prepared_review(draft_id), "receipt": "receipt-1",
            "publication_mode": "moderated",
            "warning_acceptance": {"accepted": accept_warnings},
        }
    def publication_submit(self, receipt):
        if MODE != "share":
            raise AssertionError("cancel and JSON paths must not submit publication")
        record("submit:" + receipt)
        return {"publication_state": "pending_moderation", "operation_id": "op-1"}
    def binding(self):
        return {"organization_id": "team-pty", "account_id": "member-pty"}

parser = argparse.ArgumentParser(prog="hermes wisdom")
build_parser(parser)
argv = ["suggest", "fixture"] + (["--json"] if MODE == "json" else [])
raise SystemExit(run_cli(parser.parse_args(argv), Service()))
'''


def test_real_pty_cancel_keeps_draft_without_publication_effect():
    """A real TTY cancel keeps the local draft and performs no review/upload."""
    master, slave = os.openpty()
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", _COMMAND, "human"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10
    sent = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if b"Choose 1-4:" in output and not sent:
                    os.write(master, b"4\n")
                    sent = True
            if process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    text = output.decode("utf-8", errors="replace")
    assert sent
    assert "Draft kept" in text
    assert "\x1b" not in text


def test_real_subprocess_json_is_noninteractive_and_machine_parseable():
    """Explicit JSON remains prompt-free even when standard input is available."""
    completed = subprocess.run(
        [sys.executable, "-c", _COMMAND, "json"],
        input="",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["draft_id"] == "local:pty-draft"
    assert "Choose 1-4" not in completed.stdout
    assert completed.stderr == ""


def test_real_pty_warning_review_acknowledgement_and_final_consent(tmp_path):
    """The real parser/PTY path shows safe facts and submits only after final yes."""

    call_log = tmp_path / "calls.log"
    master, slave = os.openpty()
    environment = {**os.environ, "CALL_LOG": str(call_log)}
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", _COMMAND, "share"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10
    sent_menu = sent_ack = sent_final = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            if b"Choose 1-4:" in output and not sent_menu:
                os.write(master, b"3\n")
                sent_menu = True
            if b"Acknowledge the displayed scanner warnings" in output and not sent_ack:
                assert not call_log.exists()
                os.write(master, b"yes\n")
                sent_ack = True
            if b"Share this exact reviewed package?" in output and not sent_final:
                assert call_log.read_text(encoding="utf-8") == "review:True\n"
                os.write(master, b"yes\n")
                sent_final = True
            if process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    rendered = output.decode("utf-8", errors="replace")
    assert sent_menu and sent_ack and sent_final
    assert "pty-warning" in rendered
    assert "Synthetic warning detail." in rendered
    assert "Organization: team-pty" in rendered
    assert "RAW_PTY_SECRET" not in rendered
    assert "\x1b[2J" not in rendered
    assert call_log.read_text(encoding="utf-8") == (
        "review:True\nsubmit:receipt-1\n"
    )


def test_review_identity_uses_opaque_complete_scan_evidence_fingerprint():
    """Hidden raw scan changes invalidate confirmation without leaking evidence."""

    original = {
        "hashes": {"content": "sha256:content"},
        "local_scan": {"guard": {
            "available": True,
            "allowed": True,
            "verdict": "allow",
            "summary": "clean",
            "findings": [{
                "kind": "advisory",
                "file": "SKILL.md",
                "match": "RAW_HIDDEN_MATCH_ONE",
            }],
        }},
    }
    changed = json.loads(json.dumps(original))
    changed["local_scan"]["guard"]["match_context"] = "RAW_HIDDEN_MATCH_TWO"

    original_identity = review_identity(original)
    changed_identity = review_identity(changed)

    assert original_identity != changed_identity
    assert "RAW_HIDDEN_MATCH_ONE" not in original_identity
    assert "RAW_HIDDEN_MATCH_TWO" not in changed_identity
