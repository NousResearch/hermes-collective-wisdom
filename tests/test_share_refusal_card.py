"""A definite share refusal names its exact reason on the native card."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import asyncio
import logging

import pytest

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_commands import _native_invocation
from test_reshare_pending import Kernel

from hermes_wisdom_plugin.errors import WisdomValidationError
from hermes_wisdom_plugin.native_review import NativeReviewController
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.review_state import ReviewState
from hermes_wisdom_plugin.runtime import CommandRuntime

_SYMLINK = "symlinks are not supported: scripts/link"
_GENERIC = "Couldn't open Collective Wisdom right now. Please try again."


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
        assert f"PackagePolicyError: {_SYMLINK}" in caplog.text
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


@pytest.mark.usefixtures("enrolled_profile")
@pytest.mark.parametrize("error", [
    PackagePolicyError("package path 'a\\x01' is refused: control character U+0001"),
    WisdomValidationError("Gateway returned invalid version metadata"),
])
def test_other_native_reviews_report_a_definite_refusal_exactly(tmp_path, monkeypatch, error):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def refuse(*_args):
        raise error

    monkeypatch.setattr(runtime.native, "begin_update", refuse)
    try:
        assert runtime.slash("update managed-skill") == (
            f"Couldn't open Collective Wisdom: {error}"
        )
    finally:
        runtime.close()


@pytest.mark.usefixtures("enrolled_profile")
def test_a_refusal_shown_in_chat_is_masked(tmp_path, monkeypatch):
    secret = "sk-" + "a" * 40
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def refuse(*_args):
        raise PackagePolicyError(f"invalid package entry: token={secret}")

    monkeypatch.setattr(runtime.native, "begin_update", refuse)
    try:
        reply = runtime.slash("update managed-skill")
    finally:
        runtime.close()
    assert reply.startswith("Couldn't open Collective Wisdom: invalid package entry: token=")
    assert secret not in reply
