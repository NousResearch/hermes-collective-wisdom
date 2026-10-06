"""Top skills against the real Hermes ``tools.skill_usage`` (Salt R1 SF-1, SF-6 R3).

Runs only when ``WISDOM_AGENT_DIR`` names a Hermes Agent source tree. The probe
runs in a subprocess with a launch home that differs from the plugin profile, so
it also proves the profile override binds and resets.
"""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from host_probe_support import isolated_host_environment, run_host_python

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from hermes_wisdom_plugin.top_skills import collect_top_skills
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    from tools import skill_usage

    from hermes_wisdom_plugin.top_skills import _description

    profile = Path(sys.argv[1])
    result = collect_top_skills(profile)
    names = [item.name for item in result.items]
    token = set_hermes_home_override(str(profile))
    try:
        host_dirs = {name: skill_usage._find_skill_dir(name) for name in names}
    finally:
        reset_hermes_home_override(token)
    print(json.dumps({"state": result.state, "names": names,
                      "uses": {item.name: item.uses for item in result.items},
                      "resolvable": {name: path is not None for name, path in host_dirs.items()},
                      "same_dir": {item.name: host_dirs[item.name] is not None and item.description
                                   == _description(host_dirs[item.name] / "SKILL.md")
                                   for item in result.items},
                      "descriptions": {item.name: item.description for item in result.items},
                      "home_after": str(get_hermes_home())}))
""")


def _skill(root: Path, relative: str, text: str) -> None:
    path = root / "skills" / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")


def _front(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} helper\n---\n# {name}\n"


def test_ranked_skills_match_the_host_resolver_and_profile(tmp_path) -> None:
    agent_dir = os.environ.get("WISDOM_AGENT_DIR")
    if not agent_dir:
        pytest.skip("Set WISDOM_AGENT_DIR to a Hermes Agent source tree")
    profile, launch = tmp_path / "profile", tmp_path / "launch"
    for relative, text in {
        "mine": _front("mine"),
        "dir-name": _front("front-name"),
        "lead": "\n---\nname: lead-name\n---\n",
        "bundled-one": _front("bundled-one"),
        "suppressed-one": _front("suppressed-one"),
        "hubdir/installed": _front("hub-front"),
        "vendor/ext-skill": _front("ext-skill"),
        "parent": _front("parent"),
        "parent/references/inner": _front("support-inner"),
        "pkg/node_modules/dep": _front("nm-dep"),
        "a-first/twin": "---\nname: twin\ndescription: first twin\n---\n",
        "b-second/twin": "---\nname: twin\ndescription: second twin\n---\n",
        "aaa-vendor/parent-copy": "---\nname: parent\ndescription: external copy\n---\n",
    }.items():
        _skill(profile, relative, text)
    skills = profile / "skills"
    (skills / ".bundled_manifest").write_text("bundled-one:abc\n", encoding="utf-8")
    (skills / ".curator_suppressed").write_text("suppressed-one\n", encoding="utf-8")
    (skills / ".hub").mkdir()
    (skills / ".hub" / "lock.json").write_text(json.dumps(
        {"installed": {"hub-slug": {"install_path": "hubdir/installed"}}}), encoding="utf-8")
    (profile / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {skills / 'vendor'}\n    - {skills / 'aaa-vendor'}\n",
        encoding="utf-8")
    names = ("mine", "front-name", "lead-name", "bundled-one", "suppressed-one", "hub-front",
             "ext-skill", "parent", "support-inner", "nm-dep", "twin")
    (skills / ".usage.json").write_text(json.dumps(
        {name: {"use_count": 3, "patch_count": 1} for name in names}), encoding="utf-8")
    (launch / "skills").mkdir(parents=True)
    (launch / "skills" / ".usage.json").write_text(
        json.dumps({"mine": {"use_count": 999}}), encoding="utf-8")
    host = Path(agent_dir).resolve()
    environment = isolated_host_environment(
        home=tmp_path, hermes_home=launch, host_path=host, python_paths=(host, PROJECT_ROOT),
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = run_host_python(["-c", _PROBE, str(profile)], cwd=tmp_path, timeout=120,
                             environment=environment)
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["state"] == "ok"
    assert set(observed["names"]) == {"mine", "front-name", "lead-name", "parent", "twin"}
    assert all(observed["resolvable"].values())
    assert all(observed["same_dir"].values())
    assert observed["descriptions"]["twin"] == "first twin"
    assert observed["descriptions"]["parent"] == "parent helper"
    assert observed["uses"]["mine"] == 3
    assert Path(observed["home_after"]) == launch
