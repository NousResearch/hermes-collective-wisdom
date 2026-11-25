"""Real config and usage readers agree with core on overlapping skill roots."""
# pylint: disable=missing-function-docstring
import os
from pathlib import Path
import subprocess
import sys

import pytest


OVERLAP_PROBE = r'''
import json
import os
from pathlib import Path
from tools.skills_tool import locate_skill
from hermes_wisdom_plugin import share_sources
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.install_host import InstallationHost
from hermes_wisdom_plugin.package import SkillRefusal
from hermes_wisdom_plugin.top_skills import collect_top_skills, owner_skill

home = Path(os.environ["HERMES_HOME"])
skills = home / "skills"
mode = os.environ["PROBE_MODE"]
team = skills / "mounted" if mode == "nested-local" else home.parent / "team"


def put(root, rel, name, body):
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Review incidents.\n---\n# {body}\n")
    return path


skills.mkdir(parents=True)
if mode == "nested-local":
    expected = put(skills, "same", "same", "local")
    put(team, "same", "same", "external")
    external, name = [team], "same"
elif mode == "distinct":
    left = put(team, "same", "same", "outer")
    right = put(team, "ops/same", "same", "inner")
    external, name = [team, team / "ops"], "same"
else:
    expected = put(team, "ops/inner", "inner", "one source")
    external, name = [team, team / "ops"], "inner"
if os.environ["PROBE_REVERSE"] == "1":
    external.reverse()
(home / "config.yaml").write_text(json.dumps({"skills": {"external_dirs": list(map(str, external))}}))
(skills / ".usage.json").write_text(json.dumps({name: {"use_count": 4}}))
host = InstallationHost(home)
roots = host.skill_search_roots()
assert share_sources._Core.load().pick.__module__ == "agent.skill_utils"
error, directory, _ = locate_skill(name, None, roots)
review = ContributionReviewer(None, host, lambda: {})
if mode == "distinct":
    assert "Ambiguous skill name" in error, error
    try:
        review.source(name)
    except SkillRefusal as refused:
        assert str(left) in str(refused) and str(right) in str(refused), refused
    else:
        raise AssertionError("distinct same-tier skills must be refused")
    assert collect_top_skills(home).items == ()
    assert owner_skill(home, name) is None
else:
    assert error is None and directory == expected, (error, directory)
    assert review.source(name) == expected
    ranked = collect_top_skills(home)
    assert [row.name for row in ranked.items] == [name], ranked
    found = owner_skill(home, name)
    assert found is not None and found[1] == expected, found
assert host.skills_root == skills
print(json.dumps({"mode": mode, "roots": [str(root) for _, root in roots]}))
'''


@pytest.mark.parametrize("mode", ["nested-external", "nested-local", "distinct"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_host_overlapping_roots_match_core(tmp_path, mode, reverse):
    roots = os.environ.get("WISDOM_HOST_AGENT_DIRS", "").split(os.pathsep)
    host = next((Path(root) for root in roots if root and
                 (Path(root) / "tools/skills_tool.py").is_file()), None)
    if host is None:
        pytest.skip("set WISDOM_HOST_AGENT_DIRS for the real-host overlap control")
    project = Path(__file__).resolve().parents[1]
    script = tmp_path / "overlap.py"
    script.write_text(OVERLAP_PROBE)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "HERMES_HOME": str(tmp_path / "profile"), "PROBE_MODE": mode,
           "PROBE_REVERSE": "1" if reverse else "0",
           "PYTHONPATH": os.pathsep.join(map(str, [project, project / "tests", host])),
           "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"mode": "' + mode + '"' in result.stdout
