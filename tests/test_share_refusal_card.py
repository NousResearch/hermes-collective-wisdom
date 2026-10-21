"""A definite share refusal names its exact reason on the native card."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_commands import _native_invocation
from test_reshare_pending import Kernel

from hermes_wisdom_plugin.client import WisdomClient
from hermes_wisdom_plugin.errors import WisdomValidationError
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.package import PackagePolicyError, SkillRefusal
from hermes_wisdom_plugin.review_state import ReviewState
from hermes_wisdom_plugin.runtime import CommandRuntime

_SYMLINK = "symlinks are not supported: scripts/link"
_GENERIC = "Couldn't open Collective Wisdom right now. Please try again."
# Server-controlled 422 text that is not a credential: masking leaves it intact.
_SERVER_DETAIL = "constraint skills_owner_fkey failed on db-prod-7.internal for alice@corp.example"


def _server_422() -> WisdomValidationError:
    """The exact exception the client raises for a Gateway 422 with that text."""
    try:
        WisdomClient._raise_for_status(  # pylint: disable=protected-access
            SimpleNamespace(status_code=422), {"error": _SERVER_DETAIL})
    except WisdomValidationError as exc:
        return exc
    raise AssertionError("a 422 must raise WisdomValidationError")


def _with_symlink(kernel) -> None:
    (kernel.source / "scripts").mkdir(exist_ok=True)
    (kernel.source / "scripts" / "link").symlink_to(kernel.source / "SKILL.md")


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_native_share_of_a_symlinked_skill_names_the_exact_reason(tmp_path, surface, caplog):
    async def scenario():
        kernel = Kernel(tmp_path)
        _with_symlink(kernel)
        ui = surface(tmp_path, kernel)
        with caplog.at_level(logging.WARNING):
            queued = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")
        assert queued["state"] == "queued"
        await ui.flush()
        view = ui.view(queued["review_id"])
        assert view.title == "Skill needs attention"
        assert [(item.title, item.detail) for item in view.items] == [("Not shared", _SYMLINK)]
        assert [action.action for action in view.actions] == ["suggest.refresh", "suggest.keep"]
        assert "Fix this in the skill's folder" in view.notice
        assert _SYMLINK in str(ui.posts[-1]) and "Couldn't open" not in str(ui.posts[-1])
        assert f"SkillRefusal: {_SYMLINK}" in caplog.text
        # Removing the symlink and pressing Retry prepares the source again.
        (kernel.source / "scripts" / "link").unlink()
        await ui.click(queued["review_id"], "suggest.refresh")
        assert ui.view(queued["review_id"]).title == "📤 Share skill"
        assert not kernel.client.state.calls
        ui.runtime.close()
    asyncio.run(scenario())


def _runtime(tmp_path, monkeypatch, kernel):
    """The live slash entry with begin_suggest running the real review controller."""
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    controller = NativeReviewController(
        ReviewState(JsonState(tmp_path / "reviews"), tmp_path / "reviews"))
    records = []

    def begin_suggest(caller, skill):
        records.append(controller.request_suggest(kernel, caller, skill))
        return {"state": "queued", "review_id": records[-1]["id"]}

    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())
    monkeypatch.setattr(runtime.native, "begin_suggest", begin_suggest)
    return runtime, controller, records


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_share_of_a_refused_skill_posts_the_card_not_the_generic_text(
    tmp_path, monkeypatch,
):
    kernel = Kernel(tmp_path)
    _with_symlink(kernel)
    runtime, controller, records = _runtime(tmp_path, monkeypatch, kernel)
    try:
        assert runtime.slash("share reviewed-skill") == ""
    finally:
        runtime.close()
    view = controller.view(controller.load(records[-1]["id"]))
    assert view.title == "Skill needs attention"
    assert view.items[0].detail == _SYMLINK


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_share_of_a_refused_path_shows_the_exact_rule(tmp_path, monkeypatch):
    kernel = Kernel(tmp_path)
    (kernel.source / "scripts").mkdir(exist_ok=True)
    (kernel.source / "scripts" / "bad\x01.txt").write_text("x\n", encoding="utf-8")
    runtime, controller, records = _runtime(tmp_path, monkeypatch, kernel)
    try:
        assert runtime.slash("share reviewed-skill") == ""
    finally:
        runtime.close()
    view = controller.view(controller.load(records[-1]["id"]))
    assert view.items[0].detail == (
        "package path 'scripts/bad\\x01.txt' is refused: control character U+0001"
    )


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_share_with_an_unexpected_error_keeps_the_generic_text(
    tmp_path, monkeypatch, caplog,
):
    kernel = Kernel(tmp_path)

    def broken(_skill, **_kwargs):
        raise RuntimeError("internal preparation failure")

    monkeypatch.setattr(kernel, "suggest", broken)
    runtime, _controller, records = _runtime(tmp_path, monkeypatch, kernel)
    try:
        with caplog.at_level(logging.WARNING):
            assert runtime.slash("share reviewed-skill") == _GENERIC
    finally:
        runtime.close()
    assert not records
    assert "RuntimeError: internal preparation failure" in caplog.text


def _update_runtime(tmp_path, monkeypatch, error):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def refuse(*_args):
        raise error

    monkeypatch.setattr(runtime.native, "begin_update", refuse)
    return runtime


@pytest.mark.usefixtures("enrolled_profile")
def test_other_native_reviews_report_a_skill_refusal_exactly(tmp_path, monkeypatch):
    error = SkillRefusal("package path 'a\\x01' is refused: control character U+0001")
    runtime = _update_runtime(tmp_path, monkeypatch, error)
    try:
        assert runtime.slash("update managed-skill") == (
            f"Couldn't open Collective Wisdom: {error}"
        )
    finally:
        runtime.close()


@pytest.mark.usefixtures("enrolled_profile")
@pytest.mark.parametrize("error", [
    WisdomValidationError("Gateway returned invalid version metadata"),
    _server_422(),
    PackagePolicyError("update recovery found unrecognized managed bytes"),
], ids=["gateway-contract", "server-422", "internal-state"])
def test_other_native_reviews_keep_the_generic_text_for_anything_else(
    tmp_path, monkeypatch, caplog, error,
):
    """Only a skill refusal is shown; Gateway and internal failures stay generic."""
    runtime = _update_runtime(tmp_path, monkeypatch, error)
    try:
        with caplog.at_level(logging.WARNING):
            reply = runtime.slash("update managed-skill")
    finally:
        runtime.close()
    assert reply == _GENERIC
    assert str(error) not in reply
    assert f"{type(error).__name__}: {error}" in caplog.text


@pytest.mark.usefixtures("enrolled_profile")
def test_slash_share_with_server_422_text_never_shows_it(tmp_path, monkeypatch, caplog):
    kernel = Kernel(tmp_path)

    def rejected(_skill, **_kwargs):
        raise _server_422()

    monkeypatch.setattr(kernel, "suggest", rejected)
    runtime, _controller, records = _runtime(tmp_path, monkeypatch, kernel)
    with caplog.at_level(logging.WARNING):
        try:
            reply = runtime.slash("share reviewed-skill")
        finally:
            runtime.close()
    assert reply == _GENERIC and _SERVER_DETAIL not in reply
    assert not records
    assert f"WisdomValidationError: {_SERVER_DETAIL}" in caplog.text


@pytest.mark.parametrize("surface", [SlackHarness, TelegramHarness])
def test_retry_that_hits_server_422_text_never_puts_it_on_the_card(tmp_path, surface):
    async def scenario():
        kernel = Kernel(tmp_path)
        _with_symlink(kernel)
        ui = surface(tmp_path, kernel)
        queued = ui.runtime.begin_suggest(ui.caller, "reviewed-skill")
        await ui.flush()
        assert ui.view(queued["review_id"]).items[0].detail == _SYMLINK

        def rejected(_skill, **_kwargs):
            raise _server_422()

        kernel.suggest = rejected
        await ui.click(queued["review_id"], "suggest.refresh")
        await ui.flush()
        view = ui.view(queued["review_id"])
        rendered = repr(view) + repr(ui.posts) + repr(getattr(ui, "edits", ""))
        assert _SERVER_DETAIL not in rendered
        ui.runtime.close()
    asyncio.run(scenario())


@pytest.mark.usefixtures("enrolled_profile")
def test_a_refusal_shown_in_chat_is_masked(tmp_path, monkeypatch):
    secret = "sk-" + "a" * 40
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def refuse(*_args):
        raise SkillRefusal(f"invalid package entry: token={secret}")

    monkeypatch.setattr(runtime.native, "begin_update", refuse)
    try:
        reply = runtime.slash("update managed-skill")
    finally:
        runtime.close()
    assert reply.startswith("Couldn't open Collective Wisdom: invalid package entry: token=")
    assert secret not in reply
