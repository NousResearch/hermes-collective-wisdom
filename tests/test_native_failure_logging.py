"""Native failure logs carry the exception class and a redacted message."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import logging

import pytest
from installed_test_support import JsonState
from test_commands import _native_invocation

from hermes_wisdom_plugin.errors import WisdomConflict, describe_exception
from hermes_wisdom_plugin.runtime import CommandRuntime


@pytest.mark.usefixtures("enrolled_profile")
def test_native_review_failure_logs_class_and_message(tmp_path, monkeypatch, caplog):
    runtime = CommandRuntime(
        tmp_path, ("default", tmp_path), state=JsonState(tmp_path), llm=None,
        spawn_task=lambda coroutine, **_kwargs: coroutine.close(),
    )
    monkeypatch.setattr(runtime.native, "consume_request", lambda _raw: _native_invocation())

    def fail(_caller, _skill):
        raise WisdomConflict("this local draft cannot be edited while it is submitted")

    monkeypatch.setattr(runtime.native, "begin_suggest", fail)
    try:
        with caplog.at_level(logging.WARNING):
            reply = runtime.slash("share reviewed-skill")
    finally:
        runtime.close()
    assert reply == "Couldn't open Collective Wisdom right now. Please try again."
    assert ("WisdomConflict: this local draft cannot be edited while it is submitted"
            in caplog.text)


def test_describe_exception_masks_credentials_and_controls():
    secret = "sk-" + "a" * 40
    text = describe_exception(ValueError(f"bad token={secret}\nnext"))
    assert text.startswith("ValueError: bad token=")
    assert secret not in text and "\n" not in text
