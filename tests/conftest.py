"""Shared fixtures for Collective Wisdom tests."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def full_release_scope(monkeypatch):
    """Register every built surface, for tests of code the release keeps dormant.

    The returned callable widens a synthetic native-loader package as well, for
    tests that load the root entry point under another module name.
    """

    def widen(package: str = "hermes_wisdom_plugin") -> None:
        module = importlib.import_module(f"{package}.release_scope")
        monkeypatch.setattr(module, "ACTIVE", module.FULL)

    widen()
    return widen


@pytest.fixture
def enrolled_profile(monkeypatch):
    """Treat the profile as set up, for routing tests that stub enrolled services.

    Without it, an empty fixture state is a never-enrolled profile and every
    ``/wisdom`` command answers with the setup step before routing.
    """

    from hermes_wisdom_plugin.runtime import (  # pylint: disable=import-outside-toplevel
        InvocationService,
    )

    monkeypatch.setattr(InvocationService, "setup_pending", lambda _self: False)
