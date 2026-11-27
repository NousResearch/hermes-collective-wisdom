"""The share catalog keeps Hermes core candidate semantics on any host.

Core policy (agent/skill_utils.py, tools/skills_tool.py): recursive discovery
that prunes dependency, cache and package-support folders, declared names read
with the malformed-YAML fallback, and identical same-root copies resolving to
the strictly shallower copy. Sharing keeps its own symlink and containment
rules on top. These cases run without a Hermes host; the real-host control is
tests/test_share_sources_host.py.
"""
# pylint: disable=missing-function-docstring,protected-access
from types import SimpleNamespace

import pytest

from test_contribution_review import reviewer
from test_share_source_listing import _set_roots
from test_top_skills import BOUND, _api, _record
from hermes_wisdom_plugin import top_skills
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.package import SkillRefusal
from hermes_wisdom_plugin.share_sources import ShareSources

MALFORMED = "---\nname: reviewed-skill\ndescription: [\n---\n# malformed\n"


@pytest.fixture(autouse=True)
def _scoped_home(monkeypatch):
    BOUND.clear()
    monkeypatch.setattr(top_skills, "_home_override", lambda: (
        lambda home: BOUND.append(("set", home)) or len(BOUND),
        lambda token: BOUND.append(("reset", token)),
    ))


def _skill(root, relative, name, body=None):
    path = root / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        body or f"---\nname: {name}\ndescription: Review incident notes.\n---\n# {name}\n",
        encoding="utf-8",
    )
    return path


def _sources(*roots):
    return ShareSources(SimpleNamespace(
        skills_root=roots[0], skill_search_roots=lambda: list(zip((1, 3, 3), roots))))


# B1: a malformed-YAML alias is still a collision contender.

def test_malformed_frontmatter_alias_cannot_hide_a_same_tier_collision(tmp_path):
    value, local, _binding = reviewer(tmp_path)
    other = _skill(value.host.skills_root, "other", "reviewed-skill", MALFORMED)
    with pytest.raises(SkillRefusal, match="Ambiguous skill name") as refused:
        value.prepare("reviewed-skill", allow_model_review=False)
    assert str(local) in str(refused.value) and str(other) in str(refused.value)
    with pytest.raises(SkillRefusal, match="Ambiguous skill name"):
        value.source("reviewed-skill")


def test_malformed_frontmatter_alias_resolves_alone(tmp_path):
    root = tmp_path / "skills"
    source = _skill(root, "folder", "declared", "---\nname: declared\ndescription: [\n---\n")
    sources = _sources(root)
    assert sources.resolve("declared") == source
    assert sources.resolve("folder") == source
    assert sources.names() == ["declared"]


# B2: identical same-root copies choose the strictly shallower copy.

def test_identical_same_root_copies_resolve_to_the_shallower_copy(tmp_path):
    root = tmp_path / "skills"
    # Alphabetically the deeper copy sorts first; depth, not name order, decides.
    deeper = _skill(root, "a-category/same", "same")
    shallow = _skill(root, "same", "same")
    assert (deeper / "SKILL.md").read_bytes() == (shallow / "SKILL.md").read_bytes()
    sources = _sources(root)
    assert sources.resolve("same") == shallow
    assert sources.names() == ["same"]


@pytest.mark.parametrize("case", ["different-contents", "equal-depth", "separate-roots"])
def test_other_same_tier_duplicates_stay_ambiguous(tmp_path, case):
    first, second = tmp_path / "first", tmp_path / "second"
    if case == "different-contents":
        left = _skill(first, "same", "same")
        right = _skill(first, "category/same", "same", "---\nname: same\n---\n# other body\n")
    elif case == "equal-depth":
        left = _skill(first, "one/same", "same")
        right = _skill(first, "two/same", "same")
    else:
        left = _skill(first, "same", "same")
        right = _skill(second, "category/same", "same")
    sources = ShareSources(SimpleNamespace(skills_root=first, skill_search_roots=lambda: [
        (3, first), (3, second)]))
    with pytest.raises(SkillRefusal, match="Ambiguous skill name") as refused:
        sources.resolve("same")
    assert str(left) in str(refused.value) and str(right) in str(refused.value)


def test_identical_copy_resolves_through_review_and_rankings(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    deeper = _skill(home / "skills", "a-category/same", "same")
    shallow = _skill(home / "skills", "same", "same")
    assert (deeper / "SKILL.md").read_bytes() == (shallow / "SKILL.md").read_bytes()
    _set_roots(monkeypatch, [(1, home / "skills")])
    api = _api({"same": _record(uses=4)})
    ranked = top_skills.collect_top_skills(home, usage_api=lambda: api)
    assert [row.name for row in ranked.items] == ["same"]
    assert top_skills.owner_skill(home, "same", usage_api=lambda: api)[1] == shallow
    host = SimpleNamespace(skills_root=home / "skills")
    assert ContributionReviewer(None, host, lambda: {}).source("same") == shallow


# B3: dependency, cache and package-support documents are not skills.

@pytest.mark.parametrize("relative, name", [
    ("venv/cached", "cached"),
    ("site-packages/dist", "dist"),
    ("tool/__pycache__/compiled", "compiled"),
    (".hermes-dist/staged", "staged"),
    ("parent/scripts", "support-child"),
    ("parent/scripts/support-child", "support-child"),
    ("parent/references/support-note", "support-note"),
])
def test_excluded_and_package_support_folders_are_not_share_sources(tmp_path, relative, name):
    root = tmp_path / "skills"
    _skill(root, "parent", "parent")
    _skill(root, relative, name)
    sources = _sources(root)
    assert sources.names() == ["parent"]
    with pytest.raises(WisdomNotFound):
        sources.resolve(name)


def test_support_folder_without_an_owning_skill_stays_discoverable(tmp_path):
    root = tmp_path / "skills"
    source = _skill(root, "scripts/runner", "runner")
    sources = _sources(root)
    assert sources.names() == ["runner"]
    assert sources.resolve("runner") == source


@pytest.mark.parametrize("managed", ["_wisdom", "_org"])
def test_managed_team_install_roots_are_not_share_sources(tmp_path, managed):
    root = tmp_path / "skills"
    _skill(root, f"{managed}/org-1/installed", "installed")
    nested = _skill(root, f"category/{managed}/own", "own")
    sources = _sources(root)
    assert sources.names() == ["own"]
    assert sources.resolve("own") == nested
    with pytest.raises(WisdomNotFound):
        sources.resolve("installed")


# B4: recursive discovery keeps deeper local and external skills.

def test_deeper_local_and_external_skills_resolve_and_list(tmp_path):
    local, external = tmp_path / "skills", tmp_path / "shared"
    deep = _skill(local, "category/subcategory/deep", "deep")
    far = _skill(external, "team/area/topic/far", "far")
    sources = _sources(local, external)
    assert sources.names() == ["deep", "far"]
    assert sources.resolve("deep") == deep
    assert sources.resolve("far") == far


def test_unconfigured_ranking_keeps_core_discovery(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    deep = _skill(home / "skills", "category/subcategory/deep", "deep")
    _skill(home / "skills", "parent", "parent")
    _skill(home / "skills", "parent/scripts", "support-child")
    _skill(home / "skills", "venv/cached", "cached")
    _set_roots(monkeypatch, [(1, home / "skills")])
    api = _api({name: _record(uses=5) for name in ("deep", "parent", "support-child", "cached")})
    result = top_skills.collect_top_skills(home, usage_api=lambda: api)
    assert [row.name for row in result.items] == ["deep", "parent"]
    assert top_skills.owner_skill(home, "deep", usage_api=lambda: api)[1] == deep
    assert top_skills.owner_skill(home, "support-child", usage_api=lambda: api) is None
    assert top_skills.owner_skill(home, "cached", usage_api=lambda: api) is None


def test_deeper_symlinked_components_are_still_refused(tmp_path):
    root = tmp_path / "skills"
    outside = _skill(tmp_path / "outside", "deep", "deep")
    (root / "category" / "subcategory").mkdir(parents=True)
    (root / "category" / "subcategory" / "deep").symlink_to(outside, target_is_directory=True)
    sources = _sources(root)
    assert sources.names() == []
    with pytest.raises(WisdomNotFound):
        sources.resolve("deep")
