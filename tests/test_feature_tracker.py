"""Keep stable feature IDs and their dependency graph consistent."""

import csv
import re
from pathlib import Path

TRACKER = Path(__file__).resolve().parents[1] / "docs" / "FEATURES.md"
FEATURE_ID = re.compile(r"CW-M(\d+)-\d{3}")


def test_feature_ids_and_dependencies_are_consistent() -> None:
    """Every feature has a unique ID and resolvable, acyclic dependencies."""
    features = {}
    with TRACKER.open(encoding="utf-8", newline="") as handle:
        for cells in csv.reader(handle, delimiter="|"):
            if len(cells) != 11:
                continue
            feature_id = cells[1].strip()
            match = FEATURE_ID.fullmatch(feature_id)
            if match is None:
                continue
            assert feature_id not in features, f"Duplicate feature: {feature_id}"
            features[feature_id] = re.findall(r"CW-M\d+-\d{3}", cells[4])
    assert features, "No feature records found"

    resolved = set()
    while len(resolved) < len(features):
        ready = {
            feature_id
            for feature_id, dependencies in features.items()
            if feature_id not in resolved and set(dependencies) <= resolved
        }
        assert ready, "Feature dependencies contain a cycle or an unknown ID"
        resolved.update(ready)
