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
