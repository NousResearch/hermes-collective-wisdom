"""Every listed or suggested skill resolves through the share-source catalog."""
# pylint: disable=missing-function-docstring,protected-access
from types import SimpleNamespace

import pytest

from test_top_skills import BOUND, _api, _record, _skill
from hermes_wisdom_plugin import top_skills
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.package import SkillRefusal


@pytest.fixture(autouse=True)
def _scoped_roots(monkeypatch):
    BOUND.clear()
    monkeypatch.setattr(top_skills, "_home_override", lambda: (
        lambda home: BOUND.append(("set", home)) or len(BOUND),
        lambda token: BOUND.append(("reset", token)),
    ))


def _set_roots(monkeypatch, roots):
    monkeypatch.setattr("hermes_wisdom_plugin.share_sources.import_module", lambda _name:
                        SimpleNamespace(get_skill_search_roots=lambda _local, **_kw: roots))


def test_external_top_and_automatic_candidate_are_shareable(tmp_path, monkeypatch):
    home, outside = tmp_path / "profile", tmp_path / "shared"
    home.mkdir()
    source = _skill(outside, "category/external", "external")
    roots = [(1, home / "skills"), (3, outside / "skills")]
    _set_roots(monkeypatch, roots)
    api = _api({"external": _record(uses=7)})
    result = top_skills.collect_top_skills(home, usage_api=lambda: api)
    assert [row.name for row in result.items] == ["external"]
    found = top_skills.owner_skill(home, "external", usage_api=lambda: api)
    assert found is not None and found[1] == source
    host = SimpleNamespace(skills_root=home / "skills", skill_search_roots=lambda: roots)
    reviewer = ContributionReviewer(None, host, lambda: {})
    assert reviewer.source(result.items[0].name) == source


def test_rankings_never_offer_ambiguous_or_symlinked_sources(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    left = _skill(home, "one/duplicate", "duplicate")
    _skill(home, "two/duplicate", "duplicate", "different content")
    linked = _skill(tmp_path / "outside", "linked", "linked")
    (home / "skills" / "linked").symlink_to(linked, target_is_directory=True)
    _set_roots(monkeypatch, [(1, home / "skills")])
    api = _api({"duplicate": _record(uses=9), "linked": _record(uses=7)})
    assert not top_skills.collect_top_skills(home, usage_api=lambda: api).items
    assert top_skills.owner_skill(home, "duplicate", usage_api=lambda: api) is None
    assert top_skills.owner_skill(home, "linked", usage_api=lambda: api) is None
    reviewer = ContributionReviewer(None, SimpleNamespace(skills_root=home / "skills"), lambda: {})
    with pytest.raises(SkillRefusal, match=str(left)):
        reviewer.source("duplicate")
    with pytest.raises(WisdomNotFound):
        reviewer.source("linked")


def test_declared_name_is_offered_once_with_local_precedence(tmp_path, monkeypatch):
    home, external = tmp_path / "profile", tmp_path / "external"
    local = _skill(home, "folder-name", "declared-name", "local description")
    _skill(external, "folder-name", "declared-name", "external description")
    _set_roots(monkeypatch, [(1, home / "skills"), (3, external / "skills")])
    api = _api({"declared-name": _record(uses=3), "folder-name": _record(uses=3)})
    result = top_skills.collect_top_skills(home, usage_api=lambda: api)
    assert [(row.name, row.description) for row in result.items] == [
        ("declared-name", "local description")]
    assert top_skills.owner_skill(home, "declared-name", usage_api=lambda: api)[1] == local
