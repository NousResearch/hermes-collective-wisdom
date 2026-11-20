"""Author publication metadata never claims the author's ordinary local tree."""
# pylint: disable=missing-function-docstring,protected-access
from contextlib import contextmanager
from types import SimpleNamespace

from test_publisher_enrollment import _enrollment_fixture
from test_skill_catalog import request

from hermes_wisdom_plugin.package_application import tree_hashes
from hermes_wisdom_plugin.skill_catalog import WisdomCatalog


def test_author_enrollment_is_metadata_only_without_a_catalog_claim(tmp_path):
    enrollment, operation, result, store, _client, source, owner = _enrollment_fixture(tmp_path)
    before = tree_hashes(source)
    enrollment.link(operation, result)
    service = SimpleNamespace(installed=store, native_owner=lambda: dict(owner))

    @contextmanager
    def services():
        yield service

    catalog = WisdomCatalog(services, "collective-wisdom", enrollment.host)
    response = catalog.installed(request())
    assert response == {"ok": True, "data": {"items": [], "next_cursor": None}}
    assert tree_hashes(source) == before
    assert not store.pending_operations()
    assert (store.installation("published-skill-1")["target_provenance"]["kind"]
        == "publisher_source")
