"""Provider wire reads against a fake host, with no core feature checkout."""
# pylint: disable=missing-function-docstring,too-few-public-methods,protected-access
# pylint: disable=redefined-outer-name,import-outside-toplevel,duplicate-code

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_uninstall_review import _installed

from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomUnavailable


CONTEXT = {"profile_key": "profile_fixture_01", "plugin_key": "collective-wisdom",
           "generation": "generation_fixture_01", "request_id": "request_fixture_01",
           "deadline_ms": 9999999999999}


def request(**fields):
    return {"context": dict(CONTEXT), **fields}


@pytest.fixture
def catalog(tmp_path):
    from hermes_wisdom_plugin.skill_catalog import WisdomCatalog

    executor, store, _client, owner, target = _installed(tmp_path)
    rows = [{"id": f"skill-{index}", "slug": f"skill-{index}",
             "author_description": f"Description {index}", "latest_version": 2,
             "created_by_display_name": "A teammate", "tags": ["team"],
             "installation_status": {"state": "not_installed"}}
            for index in range(45)]
    closed = []
    service = SimpleNamespace(
        profile=("default", tmp_path), native_owner=lambda: dict(owner), installed=store,
        search_skills=lambda query: [row for row in rows if query in row["slug"]],
        check_owner=lambda: None, list_skills=lambda: {"skills": rows},
    )

    @contextmanager
    def services():
        try:
            yield service
        finally:
            closed.append(True)

    provider = WisdomCatalog(services, "collective-wisdom", executor.host)
    return provider, service, rows, target, closed


def test_search_maps_existing_catalog_and_pages_past_twenty(catalog):
    provider, _service, _rows, _target, closed = catalog
    first = provider.search(request(query="", limit=20, cursor=None))
    assert first["ok"]
    page = first["data"]
    assert len(page["items"]) == 20 and page["next_cursor"]
    second = provider.search(request(query="", limit=20, cursor=page["next_cursor"]))["data"]
    third = provider.search(request(query="", limit=20, cursor=second["next_cursor"]))["data"]
    all_rows = page["items"] + second["items"] + third["items"]
    assert len({row["identifier"] for row in all_rows}) == 45
    assert third["next_cursor"] is None
    row = all_rows[0]
    assert row["provider"] == {
        "id": "team", "plugin_id": "collective-wisdom", "label": "Team skills",
        "version": "2", "author": "A teammate", "managed_state": "not_installed",
        "scan_verdict": "unknown", "actions": ["install"],
    }
    assert row["name"] == "skill-0" and row["description"] == "Description 0"
    assert row["identifier"] == provider.source + ":skill-0"
    assert row["repo"] is row["path"] is None and row["trust_level"] == "community"
    assert len(closed) == 3


def test_search_cursor_is_bound_to_query_and_owner(catalog):
    provider, service, _rows, _target, _closed = catalog
    cursor = provider.search(request(query="", limit=20))["data"]["next_cursor"]
    result = provider.search(request(query="skill", cursor=cursor))
    assert result["error"]["code"] == "invalid_request"
    original = service.native_owner()
    service.native_owner = lambda: {**original, "account_id": "other-account"}
    assert provider.search(request(query="", cursor=cursor))["error"]["code"] == "invalid_request"


def test_gateway_failure_is_unavailable_not_an_empty_library(catalog):
    provider, service, _rows, _target, _closed = catalog

    def unavailable(_query):
        raise WisdomUnavailable()

    service.search_skills = unavailable
    result = provider.search(request(query=""))
    assert result["ok"] is False and result["error"]["code"] == "provider_unavailable"


def test_known_ineligible_accounts_hide_source_but_network_failure_does_not(catalog):
    provider, service, _rows, _target, _closed = catalog

    def no_access():
        raise WisdomAuthError("No wisdom scope")

    service.native_owner = no_access
    assert provider.availability(request())["data"]["state"] == "hidden"

    def network_failure():
        raise WisdomUnavailable()

    service.native_owner = network_failure
    assert provider.availability(request())["data"]["state"] == "unavailable"


def test_availability_probes_gateway_and_hides_setup_pending(catalog):
    from hermes_wisdom_plugin.errors import WisdomValidationError

    provider, service, _rows, _target, _closed = catalog

    def lost():
        raise WisdomUnavailable()

    service.list_skills = lost
    assert provider.availability(request())["data"]["state"] == "unavailable"

    def pending():
        raise WisdomValidationError("setup is missing", code="setup_required")

    service.native_owner = pending
    assert provider.availability(request())["data"]["state"] == "hidden"


def test_installed_is_local_only_and_reenable_reclaims_only_matching_trees(catalog):
    provider, service, _rows, target, _closed = catalog
    original = service.installed._read()
    first = provider.installed(request(limit=20, cursor=None))["data"]["items"][0]
    assert first["managed_state"] == "installed"
    assert (first["target_relative_path"]
        == target.relative_to(service.profile[1] / "skills").as_posix())
    assert first["installed_version"] == "2" and first["installation_revision"]
    (target / "SKILL.md").write_text("replaced while the provider was disabled")
    changed = provider.installed(request())["data"]["items"][0]
    assert changed["managed_state"] == "unmanaged_local" and changed["actions"] == []
    backup = target.with_name("backup")
    target.rename(backup)
    removed = provider.installed(request())["data"]["items"][0]
    assert removed["managed_state"] == "removed" and removed["actions"] == ["install"]
    target.symlink_to(backup, target_is_directory=True)
    refused = provider.installed(request())["data"]["items"][0]
    assert refused["managed_state"] == "unavailable" and refused["actions"] == []
    assert service.installed._read() == original
    assert (backup / "SKILL.md").read_text() == "replaced while the provider was disabled"


def test_installed_refuses_inner_symlinks_and_file_replacement_instead_of_removed(catalog):
    provider, _service, _rows, target, _closed = catalog
    original = target / "SKILL.md"
    original.unlink()
    original.symlink_to(target.parent / "outside")
    row = provider.installed(request())["data"]["items"][0]
    assert row["managed_state"] == "unavailable" and row["actions"] == []
    backup = target.with_name("backup")
    target.rename(backup)
    target.write_text("a regular file is not absence")
    row = provider.installed(request())["data"]["items"][0]
    assert row["managed_state"] == "unavailable" and row["actions"] == []


def test_inspect_uses_exact_downloaded_bytes_but_never_creates_install_receipt(catalog):
    from test_install_review import Client

    provider, service, _rows, target, _closed = catalog
    client = Client()
    service.client = client
    service.resolve_skill = lambda _id: {
        "skill": {"id": "skill-1", "slug": "managed-skill"},
        "latest_version_detail": {"version": {"version": 2}},
    }
    original = service.installed._read()
    preview = provider.inspect(request(id="skill-1", version="1"))
    assert preview["ok"]
    data = preview["data"]
    assert data["entry"]["provider"]["version"] == "1"
    expected = next(body.decode() for path, _mode, body in client.files if path == "SKILL.md")
    assert data["skill_md"] == expected
    assert data["preview_complete"] is True
    assert {row["path"]: row["size"] for row in data["files"]} == {
        path: len(body) for path, _mode, body in client.files
    }
    assert ("version", "skill-1", 1) in client.calls
    assert service.installed._read() == original and target.is_dir()


@pytest.mark.parametrize("changes", [
    {"limit": True}, {"limit": 101}, {"query": None}, {"cursor": "unknown"},
    {"extra": "unknown"}, {"context": {**CONTEXT, "plugin_key": "another-plugin"}},
])
def test_malformed_requests_refuse_without_discovery(catalog, changes):
    provider, service, _rows, _target, _closed = catalog
    service.search_skills = lambda _query: pytest.fail("malformed request reached discovery")
    assert provider.search({**request(), **changes})["error"]["code"] == "invalid_request"
