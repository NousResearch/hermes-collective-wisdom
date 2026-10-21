"""A Gateway package rejection is logged, explained in plugin words and recorded."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_publication import publication
from test_reshare_pending import Kernel

from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.errors import WisdomValidationError
from hermes_wisdom_plugin.publication_copy import publication_copy

_COPY = "Gateway's package rules refused a file in this skill. Nothing was published."
_SECRET = "sk-live-" + "S" * 40


def _gateway_422(detail: str, code: str = "instruction_only_violation") -> WisdomValidationError:
    """The exact exception the client raises for Gateway's 422 body."""
    try:
        WisdomClient._raise_for_status(  # pylint: disable=protected-access
            SimpleNamespace(status_code=422), {"error": code, "detail": detail})
    except WisdomValidationError as exc:
        return exc
    raise AssertionError("a 422 must raise WisdomValidationError")


def test_client_keeps_gateway_422_detail_off_the_message():
    exc = _gateway_422('"a.md" is bad')
    assert (str(exc), exc.code, exc.status, exc.detail) == (
        "instruction_only_violation", "instruction_only_violation", 422, '"a.md" is bad',
    )


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_share_rejected_by_gateway_package_rules_names_the_file_and_logs(
    tmp_path, surface, caplog,
):
    detail = f'"scripts/hello.py" is in a prohibited active package directory token={_SECRET}'

    async def scenario():
        kernel = Kernel(tmp_path)
        (kernel.source / "scripts").mkdir()
        (kernel.source / "scripts" / "hello.py").write_text("print(1)\n", encoding="utf-8")
        kernel.client.state.submit_error = _gateway_422(detail)
        ui = surface(tmp_path, kernel)
        identity = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")["review_id"]
        await ui.flush()
        with caplog.at_level(logging.WARNING):
            await ui.click(identity, "confirm")
        view = ui.view(identity)
        assert view.title == "❌ Not shared"
        assert view.summary == f"{_COPY} The refused file is 'scripts/hello.py'."
        posted = str(ui.posts) + str(ui.edits)
        assert "prohibited" not in posted and _SECRET not in posted
        operation_id = ui.record(identity)["result"]["operation_id"]
        stored = kernel.publication.state.publication(operation_id)
        assert stored["rejection_code"] == "instruction_only_violation"
        assert stored["rejection_gateway_detail"].startswith(
            '"scripts/hello.py" is in a prohibited active package directory token='
        )
        assert _SECRET not in str(stored)
        status = kernel.publication.status(operation_id)
        assert status["gateway_detail"] == stored["rejection_gateway_detail"]
        assert status["reason"] == "The refused file is 'scripts/hello.py'."
        assert kernel.client.state.calls["submit"] == 1
        assert not kernel.client.state.calls["publish"]
        ui.runtime.close()
        return operation_id

    operation_id = asyncio.run(scenario())
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING
                and "rejected publication" in record.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message.startswith(
        f"Collective Wisdom Gateway rejected publication {operation_id} at draft submission "
        "(HTTP 422, code=instruction_only_violation): \"scripts/hello.py\" is in a "
        "prohibited active package directory token="
    )
    assert _SECRET not in caplog.text


def test_local_rules_name_the_refused_file_when_the_submitted_package_breaks_them(
    tmp_path: Path, caplog,
):
    """An overlay carrying a Gateway-refused folder gets the local rule's exact words."""
    service, prepared, client, _ = publication(tmp_path)
    client.state.submit_error = _gateway_422('"hooks/notes.md" is in a prohibited active '
                                             "package directory")
    receipt = service.review(prepared["draft_id"])["receipt"]
    upload = client.upload_private_objects
    overlay = Path(service.reviewer.store.draft(prepared["draft_id"])["overlay_path"])

    def upload_then_stale_overlay(objects):
        # The package Gateway refuses: what an older plugin prepared.
        result = upload(objects)
        (overlay / "hooks").mkdir()
        (overlay / "hooks" / "notes.md").write_text("notes\n", encoding="utf-8")
        return result

    client.upload_private_objects = upload_then_stale_overlay
    with caplog.at_level(logging.WARNING):
        rejected = service.submit(receipt)
    reason = ("package path 'hooks' is refused: folders named 'hooks' can't be shared; "
              "Gateway treats them as active package directories (hook scripts). Rename it")
    assert rejected["reason"] == reason
    assert rejected["gateway_detail"] == (
        '"hooks/notes.md" is in a prohibited active package directory'
    )
    assert publication_copy(rejected) == ("Failed", f"{_COPY} {reason}")
    assert client.state.calls == Counter(upload=1, submit=1)
    assert "code=instruction_only_violation" in caplog.text


def test_unknown_gateway_rejection_code_keeps_the_generic_copy(tmp_path: Path, caplog):
    service, prepared, client, _ = publication(tmp_path)
    client.state.submit_error = _gateway_422("internal db-7 constraint failed",
                                             code="author_mismatch")
    with caplog.at_level(logging.WARNING):
        rejected = service.submit(service.review(prepared["draft_id"])["receipt"])
    assert "reason" not in rejected
    assert rejected["gateway_detail"] == "internal db-7 constraint failed"
    title, text = publication_copy(rejected)
    assert (title, text) == ("Failed", "Gateway rejected the submission before creating a "
                             "draft. Edit and re-review the local draft before a fresh "
                             "submission. Nothing was published.")
    assert "db-7" not in text
    assert "code=author_mismatch): internal db-7 constraint failed" in caplog.text
