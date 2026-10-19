#!/usr/bin/env python3
"""Print a Hermes dependency venv the dev launcher can run Hermes with.

Looks only at Hermes's own install records (~/.hermes/installs/*/facts.json),
newest first, and picks the first venv that can import Hermes's core
dependencies. Override with HERMES_PYTHON in the launcher to skip this.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

src = Path(sys.argv[1]).resolve()


def site_packages(env):
    return next(iter(sorted(env.glob("lib/python3.*/site-packages"))), None)


def usable(env):
    py, site = env / "bin" / "python", site_packages(env)
    if not py.exists() or site is None:
        return False
    probe = subprocess.run(
        [str(py), "-c", "import ruamel.yaml, fastapi, pydantic"],
        env={**os.environ, "PYTHONPATH": f"{src}:{site}"},
        capture_output=True, timeout=30)
    return probe.returncode == 0


candidates = []
for facts in Path.home().glob(".hermes/installs/*/facts.json"):
    try:
        env = Path(json.loads(facts.read_text())["packages"]["venv"]["environment"])
    except Exception:
        continue
    candidates.append((facts.stat().st_mtime, env))
for _, env in sorted(candidates, reverse=True):
    if usable(env):
        print(env)
        sys.exit(0)
sys.exit(1)
