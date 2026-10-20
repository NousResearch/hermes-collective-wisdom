"""Failure-log redaction over host-redactor and fallback paths (Salt R1 #2)."""
# pylint: disable=missing-function-docstring
from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

import pytest

from hermes_wisdom_plugin.errors import describe_exception

LONG_RECEIPT = "r" * 64

# (case name, raw exception text, substrings that must not survive)
CASES = [
    ("short receipt", "receipt=short-receipt-123", ["short-receipt-123"]),
    ("receipt colon", "receipt: short-receipt-123", ["short-receipt-123"]),
    ("long receipt", f"receipt={LONG_RECEIPT}", [LONG_RECEIPT, "rrrrrrrr"]),
    ("json receipt", '{"receipt": "abc123"}', ["abc123"]),
    ("url userinfo with token",
     "https://user:pass@example.com/path?token=abc123", ["user:pass", "abc123"]),
    ("url api_key", "https://example.com/path?api_key=abc123", ["abc123"]),
    ("url key", "https://example.com/p?x=1&key=abc123", ["abc123"]),
    ("url secret", "https://example.com/p?secret=abc123&x=1", ["abc123"]),
    ("url password", "https://example.com/p?password=hunter2", ["hunter2"]),
    ("url signature", "https://example.com/p?signature=sig123", ["sig123"]),
    ("url receipt", "https://example.com/p?receipt=rcpt123", ["rcpt123"]),
    ("url code", "https://example.com/cb?code=oauth123&state=s", ["oauth123"]),
    ("url access_token", "https://example.com/cb#access_token=at123", ["at123"]),
    ("short bearer", "Bearer abc123", ["abc123"]),
    ("authorization header", "Authorization: Bearer abc123", ["abc123"]),
    ("prefixed key", "bad token=sk-" + "a" * 40, ["sk-" + "a" * 40]),
]


def _install_host(monkeypatch, redact):
    module = ModuleType("agent.redact")
    module.redact_sensitive_text = redact
    package = ModuleType("agent")
    package.redact = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.redact", module)


def _no_host(monkeypatch):
    real_import = importlib.import_module

    def fail(name, *args, **kwargs):
        if name.startswith("agent"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "agent.redact", raising=False)
    monkeypatch.setattr(importlib, "import_module", fail)


def _identity_host(monkeypatch):
    calls = []

    def redact(text, *, force=False, redact_url_credentials=False):
        calls.append((force, redact_url_credentials))
        return text

    _install_host(monkeypatch, redact)
    return calls


def _legacy_host(monkeypatch):
    calls = []

    def redact(text, *, force=False):
        calls.append(force)
        return text

    _install_host(monkeypatch, redact)
    return calls


MODES = {"fallback": _no_host, "strict-host": _identity_host, "legacy-host": _legacy_host}


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("name,raw,secrets", CASES, ids=[case[0] for case in CASES])
def test_describe_exception_masks_every_credential_shape(  # (Salt R1 #2)
    monkeypatch, mode, name, raw, secrets,
):
    MODES[mode](monkeypatch)
    text = describe_exception(ValueError(raw + "\nnext"))
    assert text.startswith("ValueError: "), name
    assert "\n" not in text
    for secret in secrets:
        assert secret not in text, (mode, name, text)


def test_strict_host_redactor_is_called_with_url_credential_mode(monkeypatch):
    calls = _identity_host(monkeypatch)
    describe_exception(ValueError("https://example.com/?token=abc"))
    assert calls == [(True, True)]


def test_legacy_host_signature_is_retried_without_the_strict_flag(monkeypatch):
    calls = _legacy_host(monkeypatch)
    assert "abc" not in describe_exception(ValueError("https://example.com/?token=abc"))
    assert calls == [True]


def test_public_url_and_ordinary_words_survive(monkeypatch):
    _no_host(monkeypatch)
    text = describe_exception(ValueError(
        "draft cannot be edited at https://portal.example/wisdom/skills/s1?org_id=o1"))
    assert "https://portal.example/wisdom/skills/s1?org_id=o1" in text
    assert "draft cannot be edited" in text


def _host_dirs():
    raw = os.environ.get("WISDOM_HOST_AGENT_DIRS", "")
    return [path for path in raw.split(os.pathsep) if path]


@pytest.mark.parametrize("host_dir", _host_dirs() or [None])
@pytest.mark.parametrize("name,raw,secrets", CASES, ids=[case[0] for case in CASES])
def test_real_host_redactor_with_plugin_masking(  # (Salt R1 #2)
    monkeypatch, host_dir, name, raw, secrets,
):
    """Runs against real Hermes checkouts named in WISDOM_HOST_AGENT_DIRS."""
    if host_dir is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS to Hermes checkouts to run this cell")
    for module_name in [key for key in sys.modules if key == "agent" or key.startswith("agent.")]:
        monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.syspath_prepend(host_dir)
    redact = importlib.import_module("agent.redact")
    assert os.path.dirname(os.path.dirname(redact.__file__)) == os.path.abspath(host_dir)
    text = describe_exception(ValueError(raw))
    for secret in secrets:
        assert secret not in text, (host_dir, name, text)
    assert text.startswith("ValueError: ")
