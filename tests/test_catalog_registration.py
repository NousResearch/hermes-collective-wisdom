"""Optional catalog registration and request guards on fake current/older hosts."""
# pylint: disable=redefined-outer-name,missing-class-docstring,too-few-public-methods
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
from inspect import getattr_static
from types import SimpleNamespace

import pytest
from test_skill_catalog import catalog, request  # pylint: disable=unused-import
from test_catalog_reviews import reviews  # pylint: disable=unused-import


@pytest.mark.parametrize("field,value", [("review_token", ""), ("review_token", 3),
                                           ("operation_id", "bad/identifier")])
def test_bad_authority_is_rejected_before_service(reviews, field, value):
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


@pytest.mark.parametrize("team_name,expected", [
    ("Nous", "Team skills · Nous"),
    ("  Nous  ", "Team skills · Nous"),
    ("N" * 100, ("Team skills · " + "N" * 100)[:80]),
    (None, "Team skills"),
    ("   ", "Team skills"),
    (42, "Team skills"),
    ("org-1", "Team skills"),
    ("Bad\nname", "Team skills"),
    ("Bad\x7fname", "Team skills"),
    ("Bad\x85name", "Team skills"),
    ("Bad\ud800name", "Team skills"),
])
def test_registered_descriptor_and_read_rows_share_cached_team_label(
    catalog, monkeypatch, team_name, expected,
):
    import sys
    from test_install_review import Client
    from hermes_wisdom_plugin.catalog_registration import register_catalog

    class Provider:
        label = ""

    service = catalog[1]
    info = SimpleNamespace(logged_in=True, org_id="org-1", org_name=team_name)

    monkeypatch.setitem(sys.modules, "hermes_cli.skill_catalog",
                        SimpleNamespace(SkillCatalogProvider=Provider))
    monkeypatch.setitem(sys.modules, "hermes_cli.nous_account", SimpleNamespace(
        cached_nous_portal_account_info=lambda: info,
        get_nous_portal_account_info=lambda: pytest.fail("label fetched account"),
    ))
    runtime = SimpleNamespace(profile=service.profile, registrations=[],
        plugin_id="collective-wisdom",
        _service=lambda: pytest.fail("registration opened account"))
    descriptors = []
    ctx = SimpleNamespace(register_skill_catalog=lambda provider: descriptors.append({
        key: getattr_static(provider, key) for key in ("name", "label", "implementation_revision")
    }), has_capability=lambda _name: True)
    provider = register_catalog(ctx, runtime)
    assert descriptors == [{"name": "team", "label": expected, "implementation_revision": "1"}]
    assert provider is not None
    assert provider.source == catalog[0].source
    provider.services = catalog[0].services
    assert provider.search(request(query="skill-0"))["data"]["items"][0]["provider"]["label"] \
        == expected
    service.client = Client()
    service.resolve_skill = lambda _id: {"skill": {"id": "skill-1", "slug": "skill-1"}}
    assert provider.inspect(request(id="skill-1", version="1"))["data"]["entry"]["provider"] \
        ["label"] == expected
    info.org_name = "Different team after registration"
    assert getattr_static(provider, "label") == expected
    assert provider.search(request(query="skill-0"))["data"]["items"][0]["provider"]["label"] \
        == expected


@pytest.mark.parametrize("cache_state", ["missing", "empty", "error", "signed_out"])
def test_optional_account_cache_keeps_fallback_descriptor(tmp_path, monkeypatch, cache_state):
    import sys
    from hermes_wisdom_plugin.catalog_registration import register_catalog

    class Provider:
        pass

    def cached():
        if cache_state == "error":
            raise RuntimeError("account cache unavailable")
        if cache_state == "signed_out":
            return SimpleNamespace(logged_in=False, org_name="Old team")
        return None

    account = SimpleNamespace()
    if cache_state != "missing":
        account.cached_nous_portal_account_info = cached
    monkeypatch.setitem(sys.modules, "hermes_cli.nous_account", account)
    monkeypatch.setitem(sys.modules, "hermes_cli.skill_catalog",
                        SimpleNamespace(SkillCatalogProvider=Provider))
    labels = []
    runtime = SimpleNamespace(profile=("default", tmp_path), registrations=[],
                              plugin_id="collective-wisdom")
    register_catalog(SimpleNamespace(register_skill_catalog=lambda row: labels.append(row.label),
                                    has_capability=lambda _name: True), runtime)
    assert labels == ["Team skills"]
