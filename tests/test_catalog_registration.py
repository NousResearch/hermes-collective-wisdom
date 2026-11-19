"""Optional catalog registration and request guards on fake current/older hosts."""
# pylint: disable=redefined-outer-name,missing-class-docstring,too-few-public-methods
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
from types import SimpleNamespace

import pytest
from test_catalog_reviews import reviews  # pylint: disable=unused-import


@pytest.mark.parametrize("field,value", [("review_token", ""), ("review_token", 3),
                                           ("operation_id", "bad/identifier")])
def test_bad_authority_is_rejected_before_service(reviews, field, value):
    from test_skill_catalog import request

    adapter, _service, _reviewer, _host, _client, _clock = reviews
    adapter.services = lambda: pytest.fail("invalid authority opened credentials")
    method = adapter.operation if field == "operation_id" else adapter.reviewed
    assert method(request(**{field: value}))["error"]["code"] == "invalid_request"


def test_registration_is_optional_and_requires_grant(tmp_path):
    from hermes_wisdom_plugin.catalog_registration import register_catalog

    runtime = SimpleNamespace(profile=("default", tmp_path), registrations=[],
        plugin_id="collective-wisdom")
    assert register_catalog(SimpleNamespace(), runtime) is None
    calls = []
    ctx = SimpleNamespace(register_skill_catalog=calls.append, has_capability=lambda _name: False)
    assert register_catalog(ctx, runtime) is None and not calls


def test_register_entrypoint_exposes_catalog_only_after_grant(tmp_path, monkeypatch):
    import importlib
    import sys
    from test_release_scope import _context, _load_root_plugin, _unload

    class Provider:
        pass

    monkeypatch.setitem(sys.modules, "hermes_cli.skill_catalog",
        SimpleNamespace(SkillCatalogProvider=Provider))
    name, plugin = _load_root_plugin()
    module = importlib.import_module(name + ".hermes_wisdom_plugin.runtime")
    monkeypatch.setattr(module, "current_profile", lambda: ("default", tmp_path))
    calls = []
    entries, ctx = _context(tmp_path, register_skill_catalog=calls.append,
                            has_capability=lambda value: value == "skills.catalog",
                            skill_catalog_mutations=object())
    try:
        plugin.register(ctx)
        assert len(calls) == 1 and isinstance(calls[0], Provider)
        entries["unload"]()
    finally:
        _unload(name)


def test_current_host_registers_without_opening_account(tmp_path, monkeypatch):
    import sys
    from hermes_wisdom_plugin.catalog_registration import register_catalog

    class Provider:
        pass

    monkeypatch.setitem(sys.modules, "hermes_cli.skill_catalog",
        SimpleNamespace(SkillCatalogProvider=Provider))
    calls = []
    runtime = SimpleNamespace(profile=("default", tmp_path), registrations=[],
        plugin_id="collective-wisdom",
                              _service=lambda: pytest.fail("registration must not open account"))
    ctx = SimpleNamespace(register_skill_catalog=lambda row: calls.append(row) or "handle",
                          has_capability=lambda name: name == "skills.catalog",
                          skill_catalog_mutations=object())
    register_catalog(ctx, runtime)
    assert len(calls) == 1 and isinstance(calls[0], Provider)
    assert runtime.registrations == ["handle"]
