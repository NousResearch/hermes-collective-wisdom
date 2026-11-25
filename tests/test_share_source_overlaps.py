"""Overlapping search roots expose each physical skill once, under its closest root."""
# pylint: disable=missing-function-docstring,protected-access
from types import SimpleNamespace

import pytest

from test_share_source_parity import _skill
from test_top_skills import _api, _record
from hermes_wisdom_plugin import share_sources, top_skills
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.package import SkillRefusal
from hermes_wisdom_plugin.share_sources import ShareSources


@pytest.fixture(autouse=True)
def _without_host(monkeypatch):
    monkeypatch.setattr(share_sources, "_host_utility", lambda: None)
    monkeypatch.setattr(top_skills, "_home_override", lambda: (lambda _home: None, lambda _t: None))


def _host(home, roots):
    return SimpleNamespace(skills_root=home / "skills", skill_search_roots=lambda: roots)


@pytest.mark.parametrize("reverse", [False, True])
def test_one_skill_in_overlapping_external_roots_resolves_and_ranks(tmp_path, monkeypatch, reverse):
    home, team = tmp_path / "profile", tmp_path / "team"
    inner = _skill(team, "ops/inner", "inner")
    external = [(3, team), (3, team / "ops")]
    roots = [(1, home / "skills"), *external[:: -1 if reverse else 1]]
    host = _host(home, roots)
    sources = ShareSources(host)
    assert sources.names() == ["inner"]
    assert sources.resolve("inner") == inner
    assert ContributionReviewer(None, host, lambda: {}).source("inner") == inner
    monkeypatch.setattr(top_skills, "hermes_search_roots", lambda _local: roots)
    api = _api({"inner": _record(uses=4)})
    ranked = top_skills.collect_top_skills(home, usage_api=lambda: api)
    assert [row.name for row in ranked.items] == ["inner"]
    assert top_skills.owner_skill(home, "inner", usage_api=lambda: api)[1] == inner


@pytest.mark.parametrize("reverse", [False, True])
def test_nested_external_root_does_not_inherit_local_tier(tmp_path, reverse):
    home = tmp_path / "profile"
    local = _skill(home / "skills", "same", "same", "---\nname: same\n---\n# local\n")
    _skill(home / "skills", "mounted/same", "same", "---\nname: same\n---\n# external\n")
    roots = [(1, home / "skills"), (3, home / "skills/mounted")]
    sources = ShareSources(_host(home, roots[:: -1 if reverse else 1]))
    assert sources.names() == ["same"]
    assert sources.resolve("same") == local


@pytest.mark.parametrize("reverse", [False, True])
def test_two_distinct_skills_in_overlapping_roots_stay_ambiguous(tmp_path, reverse):
    home, team = tmp_path / "profile", tmp_path / "team"
    left = _skill(team, "same", "same", "---\nname: same\n---\n# left\n")
    right = _skill(team, "ops/same", "same", "---\nname: same\n---\n# right\n")
    external = [(3, team), (3, team / "ops")]
    roots = [(1, home / "skills"), *external[:: -1 if reverse else 1]]
    with pytest.raises(SkillRefusal, match="Ambiguous skill name") as refused:
        ShareSources(_host(home, roots)).resolve("same")
    assert str(left) in str(refused.value) and str(right) in str(refused.value)
