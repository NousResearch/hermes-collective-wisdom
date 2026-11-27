"""Facade presence never changes stock removal presentation on an unregistered host."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
# pylint: disable=too-few-public-methods,duplicate-code
from types import SimpleNamespace

import pytest


def test_no_registration_service_uses_stock_gate():
    from hermes_wisdom_plugin.runtime import InvocationService
    service = object.__new__(InvocationService)
    service.catalog_mutations = SimpleNamespace(admit_native=lambda *_a: None)
    service.plugin_id = "collective-wisdom"
    assert service.catalog_available() is False


def test_cli_no_registration_uses_stock_removal():
    from hermes_wisdom_plugin.commands import _uninstall_cli
    service = SimpleNamespace(
        catalog_mutations=object(), catalog_available=lambda: False,
        uninstall_plan=lambda *_a: pytest.fail("stock removal acquired new review"),
        uninstall=lambda value: {"uninstalled": value},
    )
    assert _uninstall_cli(SimpleNamespace(skill_id="skill-1", yes=True, json=False), service) == {
        "uninstalled": "skill-1"}


@pytest.mark.parametrize("active", [False, True])
def test_presence_probe_never_reserves_with_a_review(active):
    from hermes_wisdom_plugin.catalog_presence import catalog_live
    seen = []
    def admit(review, choices):
        seen.append((review, choices))
        assert "action" not in review and "review_token" not in review
        return {} if active else None
    assert catalog_live(SimpleNamespace(admit_native=admit), "collective-wisdom") == active
    assert len(seen) == 1


def test_presence_errors_fail_closed():
    from hermes_wisdom_plugin.catalog_presence import catalog_live
    def unavailable(*_args):
        raise RuntimeError("revoked")
    assert catalog_live(SimpleNamespace(admit_native=unavailable), "collective-wisdom")
