"""Share sources follow Hermes roots without widening install destinations."""
# pylint: disable=missing-function-docstring,protected-access
from types import SimpleNamespace

import pytest

from test_contribution_review import reviewer
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.package import PackagePolicyError, SkillRefusal


def _skill(root, relative, name="reviewed-skill"):
    path = root / relative
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review incident notes.\n---\n# {relative}\n",
        encoding="utf-8",
    )
    return path


def _roots(monkeypatch, host, roots):
    monkeypatch.setattr(host, "skill_search_roots", lambda: roots, raising=False)


@pytest.mark.parametrize("relative", ["reviewed-skill", "category/reviewed-skill"])
def test_external_source_prepares_exact_private_draft(tmp_path, monkeypatch, relative):
    value, local, _binding = reviewer(tmp_path)
    local.rename(local.with_name("other-local"))
    external = tmp_path / "shared"
    source = _skill(external, relative)
    original = (source / "SKILL.md").read_bytes()
    _roots(monkeypatch, value.host, [(1, value.host.skills_root), (3, external)])
    # Remove the frontmatter alias left by renaming the fixture.
    (local.with_name("other-local") / "SKILL.md").write_text("# Other local skill\n")
    result = value.prepare("reviewed-skill", allow_model_review=False)
    assert value.store.draft(result["draft_id"])["source_path"] == str(source)
    assert (source / "SKILL.md").read_bytes() == original
    assert value.host.skills_root == tmp_path / "profile" / "skills"


def test_local_source_wins_over_external_copy(tmp_path, monkeypatch):
    value, local, _binding = reviewer(tmp_path)
    external = tmp_path / "shared"
    _skill(external, "reviewed-skill")
    _roots(monkeypatch, value.host, [(1, value.host.skills_root), (3, external)])
    assert value.source("reviewed-skill") == local


def test_create_root_wins_over_external_when_profile_root_is_missing(tmp_path, monkeypatch):
    value, _local, _binding = reviewer(tmp_path)
    create, external = tmp_path / "create", tmp_path / "shared"
    source = _skill(create, "reviewed-skill")
    _skill(external, "reviewed-skill")
    _roots(monkeypatch, value.host, [(1, tmp_path / "missing"), (2, create), (3, external)])
    assert value.source("reviewed-skill") == source


@pytest.mark.parametrize("separate_roots", [False, True])
def test_same_tier_ambiguity_names_both_paths(tmp_path, monkeypatch, separate_roots):
    value, _local, _binding = reviewer(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    left = _skill(first, "one/reviewed-skill")
    right = _skill(second if separate_roots else first, "two/reviewed-skill")
    _roots(monkeypatch, value.host, [(3, first), (3, second)])
    with pytest.raises(SkillRefusal) as refused:
        value.source("reviewed-skill")
    assert str(left) in str(refused.value) and str(right) in str(refused.value)


@pytest.mark.parametrize("kind", ["folder", "category", "markdown", "root"])
def test_external_symlinks_are_never_share_sources(tmp_path, monkeypatch, kind):
    value, _local, _binding = reviewer(tmp_path)
    external = tmp_path / "external"
    source = _skill(tmp_path / "outside", "category/reviewed-skill")
    external.mkdir()
    if kind == "folder":
        (external / "reviewed-skill").symlink_to(source, target_is_directory=True)
    elif kind == "category":
        (external / "category").symlink_to(source.parent, target_is_directory=True)
    elif kind == "markdown":
        target = external / "reviewed-skill"
        target.mkdir()
        (target / "SKILL.md").symlink_to(source / "SKILL.md")
    else:
        external = tmp_path / "root-link"
        external.symlink_to(source.parent, target_is_directory=True)
    # No local copy may mask the external safety boundary.
    _local.rename(_local.with_name("other-local"))
    (_local.with_name("other-local") / "SKILL.md").write_text("# Other local skill\n")
    _roots(monkeypatch, value.host, [(1, value.host.skills_root), (3, external)])
    with pytest.raises(WisdomNotFound):
        value.source("reviewed-skill")


def test_declared_name_and_folder_alias_resolve_same_source(tmp_path, monkeypatch):
    value, _local, _binding = reviewer(tmp_path)
    external = tmp_path / "shared"
    source = _skill(external, "category/folder-name", "declared-name")
    _roots(monkeypatch, value.host, [(3, external)])
    assert value.source("declared-name") == source
    assert value.source("folder-name") == source


def test_external_source_runs_one_shot_share_without_copying(tmp_path, monkeypatch, capsys):
    import argparse  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel
    from test_share_command import ShareCli  # pylint: disable=import-outside-toplevel
    from hermes_wisdom_plugin.commands import build_parser, run_cli  # pylint: disable=import-outside-toplevel

    service = ShareCli(tmp_path)
    source = tmp_path / "external" / "category" / service.source.name
    source.parent.mkdir(parents=True)
    service.source.rename(source)
    _roots(monkeypatch, service.reviewer.host, [
        (1, service.reviewer.host.skills_root), (3, tmp_path / "external")])
    parser = argparse.ArgumentParser()
    build_parser(parser)
    assert run_cli(parser.parse_args(["share", source.name, "--yes", "--json"]), service) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "shared"
    assert service.client.state.calls["publish"] == 1
    assert (source / "SKILL.md").is_file()
    assert not service.source.exists()


def test_sharing_external_source_keeps_install_review_profile_local(tmp_path, monkeypatch):
    from test_install_review import Host, _reviewer  # pylint: disable=import-outside-toplevel

    value, local, _binding = reviewer(tmp_path)
    local.rename(local.with_name("other-local"))
    (local.with_name("other-local") / "SKILL.md").write_text("# Other local skill\n")
    external = tmp_path / "shared"
    source = _skill(external, "category/reviewed-skill")
    _roots(monkeypatch, value.host, [(1, value.host.skills_root), (3, external)])
    value.prepare("reviewed-skill", allow_model_review=False)
    install_host = Host(tmp_path / "profile")
    _reviewer(tmp_path, host=install_host).plan("skill-1")
    assert install_host.targets
    assert all(path.is_relative_to(value.host.skills_root) for path in install_host.targets)
    assert all(not path.is_relative_to(external) for path in install_host.targets)
    assert (source / "SKILL.md").is_file()


def test_unrelated_malformed_frontmatter_does_not_block_sharing(tmp_path):
    value, source, _binding = reviewer(tmp_path)
    broken = _skill(value.host.skills_root, "broken", "broken")
    (broken / "SKILL.md").write_text("---\nname: [\n---\n# Broken\n")
    assert value.source("reviewed-skill") == source


def test_host_uses_profile_root_and_excludes_project_roots(tmp_path, monkeypatch):
    host = InstallationHost(tmp_path)
    calls = []

    def roots(local, *, include_project):
        calls.append((local, include_project))
        return [(1, local), (2, tmp_path / "create"), (3, tmp_path / "external")]

    modules = {
        "hermes_constants": SimpleNamespace(get_hermes_home=lambda: tmp_path),
        "agent.skill_utils": SimpleNamespace(get_skill_search_roots=roots),
    }
    monkeypatch.setattr(host, "_module", modules.__getitem__)
    monkeypatch.setattr("hermes_wisdom_plugin.share_sources.import_module", modules.__getitem__)
    assert host.skill_search_roots() == roots(host.skills_root, include_project=False)
    assert calls == [(tmp_path / "skills", False)] * 2
    assert host.skills_root == tmp_path / "skills"


def test_old_host_falls_back_to_profile_root(tmp_path, monkeypatch):
    host = InstallationHost(tmp_path)
    modules = {
        "hermes_constants": SimpleNamespace(get_hermes_home=lambda: tmp_path),
        "agent.skill_utils": SimpleNamespace(),
    }
    monkeypatch.setattr(host, "_module", modules.__getitem__)
    monkeypatch.setattr("hermes_wisdom_plugin.share_sources.import_module", modules.__getitem__)
    assert host.skill_search_roots() == [(1, host.skills_root)]


def test_host_root_discovery_refuses_profile_change(tmp_path, monkeypatch):
    host = InstallationHost(tmp_path)
    monkeypatch.setattr(host, "_module", lambda _name: SimpleNamespace(
        get_hermes_home=lambda: tmp_path / "other"))
    with pytest.raises(PackagePolicyError, match="profile changed"):
        host.skill_search_roots()
