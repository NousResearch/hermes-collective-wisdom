"""Real Hermes config reader to share preparation and ranking, offline."""
# pylint: disable=missing-function-docstring
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = r'''
import json
import os
from pathlib import Path
from types import SimpleNamespace
from agent.skill_utils import get_skill_search_roots
from test_contribution_review import reviewer
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.top_skills import collect_top_skills, owner_skill

home = Path(os.environ["HERMES_HOME"])
value, local, binding = reviewer(home.parent)
assert value.host.skills_root.parent == home
host = InstallationHost(home)
value.host.skill_search_roots = host.skill_search_roots
value.host.parse_skill_frontmatter = host.parse_skill_frontmatter
external = home.parent / "shared"
create = home.parent / "created"
for root, name in ((external, "external-skill"), (create, "created-skill")):
    source = root / "category" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review incident notes.\n---\n# {name}\n")
mode = os.environ["PROBE_MODE"]
config = {"skills": {"external_dirs": [str(external)], "create_dir": str(create)}}
if mode == "stock":
    config = {}
(home / "config.yaml").write_text(json.dumps(config))
roots = host.skill_search_roots()
assert roots == get_skill_search_roots(home / "skills", include_project=False)
if mode == "configured":
    assert [str(path) for _, path in roots] == [str(home / "skills"), str(create), str(external)]
    for name, root in (("external-skill", external), ("created-skill", create)):
        plan = value.prepare(name, allow_model_review=False)
        assert value.store.draft(plan["draft_id"])["source_path"] == str(root / "category" / name)
    api = SimpleNamespace(load_usage=lambda: {
        "external-skill": {"use_count": 3}, "created-skill": {"use_count": 2}},
        is_bundled=lambda _: False, is_hub_installed=lambda _: False)
    ranked = collect_top_skills(home, usage_api=lambda: api)
    assert [row.name for row in ranked.items] == ["external-skill", "created-skill"]
    assert owner_skill(home, "external-skill", usage_api=lambda: api)[1] == external / "category/external-skill"
else:
    assert roots == [(1, home / "skills")]
    assert value.source("reviewed-skill") == local
assert host.skills_root == home / "skills"
print(json.dumps({"mode": mode, "roots": [str(path) for _, path in roots],
                  "host_module": __import__("agent.skill_utils", fromlist=["x"]).__file__}))
'''


@pytest.mark.parametrize("mode", ["configured", "stock"])
def test_real_host_config_reaches_share_and_ranking(tmp_path, mode):
    roots = os.environ.get("WISDOM_HOST_AGENT_DIRS", "").split(os.pathsep)
    host = next((Path(root) for root in roots if root and
                 (Path(root) / "agent/skill_utils.py").is_file()), None)
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS for the real-host reader control")
    project = Path(__file__).resolve().parents[1]
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "HERMES_HOME": str(tmp_path / "profile"), "PROBE_MODE": mode,
           "PYTHONPATH": os.pathsep.join(map(str, [project, project / "tests", host])),
           "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"mode": "' + mode + '"' in result.stdout


# Core parity control: the real locate_skill, parser, recursive index and usage
# reader decide each case first; the plugin must agree on the same tree.
PARITY_PROBE = r'''
import json
import os
from pathlib import Path
from tools.skills_tool import locate_skill
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.errors import WisdomNotFound
from hermes_wisdom_plugin.package import SkillRefusal
from hermes_wisdom_plugin.share_sources import ShareSources
from hermes_wisdom_plugin.top_skills import collect_top_skills

home = Path(os.environ["HERMES_HOME"])
skills = home / "skills"
external = home.parent / "shared"
home.mkdir(parents=True, exist_ok=True)
(home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": [str(external)]}}))


def put(root, rel, name, body=None):
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        body or f"---\nname: {name}\ndescription: Review incidents.\n---\n# {name}\n")
    return path


put(skills, "reviewed-skill", "reviewed-skill")
put(skills, "other", "reviewed-skill", "---\nname: reviewed-skill\ndescription: [\n---\n# malformed\n")
put(skills, "same", "same")
put(skills, "a-category/same", "same")
put(skills, "differs", "differs")
put(skills, "category/differs", "differs", "---\nname: differs\n---\n# other body\n")
put(skills, "category/subcategory/deep", "deep")
put(skills, "parent", "parent")
put(skills, "parent/scripts", "support-child")
put(skills, "venv/cached", "cached")
put(external, "team/area/topic/far", "far")
(skills / ".usage.json").write_text(json.dumps({name: {"use_count": 5} for name in (
    "deep", "parent", "support-child", "cached", "same", "far", "reviewed-skill")}))

host = InstallationHost(home)
roots = host.skill_search_roots()
assert [path for _, path in roots] == [skills, external], roots
sources = ShareSources(host)


def plugin(name):
    try:
        return {"dir": str(sources.resolve(name))}
    except SkillRefusal as exc:
        return {"ambiguous": "Ambiguous skill name" in str(exc)}
    except WisdomNotFound:
        return {"missing": True}


def core(name):
    error, directory, _ = locate_skill(name, None, roots)
    if error is None:
        return {"dir": str(directory)}
    if "Ambiguous skill name" in error:
        return {"ambiguous": True}
    return {"missing": True}


cases = {name: (core(name), plugin(name)) for name in (
    "reviewed-skill", "other", "same", "differs", "deep", "parent", "support-child", "cached", "far")}
disagreements = {name: pair for name, pair in cases.items() if pair[0] != pair[1]}
assert not disagreements, disagreements
assert cases["reviewed-skill"][0] == {"ambiguous": True}
assert cases["same"][0] == {"dir": str(skills / "same")}
assert cases["differs"][0] == {"ambiguous": True}
assert cases["deep"][0] == {"dir": str(skills / "category/subcategory/deep")}
assert cases["far"][0] == {"dir": str(external / "team/area/topic/far")}
assert cases["support-child"][0] == {"missing": True} and cases["cached"][0] == {"missing": True}
token = set_hermes_home_override(str(home))
try:
    ranked = collect_top_skills(home)
finally:
    reset_hermes_home_override(token)
assert ranked.state == "ok", ranked
assert [row.name for row in ranked.items] == ["deep", "far", "parent", "same"], ranked
print(json.dumps({"cases": {name: pair[0] for name, pair in cases.items()},
                  "ranked": [row.name for row in ranked.items]}))
'''


def test_real_host_catalog_matches_core_lookup(tmp_path):
    roots = os.environ.get("WISDOM_HOST_AGENT_DIRS", "").split(os.pathsep)
    host = next((Path(root) for root in roots if root and
                 (Path(root) / "tools/skills_tool.py").is_file()), None)
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS for the real-host parity control")
    project = Path(__file__).resolve().parents[1]
    script = tmp_path / "parity.py"
    script.write_text(PARITY_PROBE)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "HERMES_HOME": str(tmp_path / "profile"),
           "PYTHONPATH": os.pathsep.join(map(str, [project, project / "tests", host])),
           "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"ranked": ["deep", "far", "parent", "same"]' in result.stdout
