"""Complete, bounded catalog file consent from native inventory facts."""
# pylint: disable=missing-function-docstring,redefined-outer-name

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.catalog_reviews import project_review


def _file(path, *, size=7, identity="new", mode="file"):
    return {"path": path, "size": size, "hash": "sha256:" + identity, "mode": mode}


@pytest.fixture
def plan(tmp_path):
    return {
        "owner": {"organization_id": "org"}, "skill_id": "skill", "slug": "skill",
        "version": 2, "content_hash": "sha256:package", "allowed": True,
        "target_path": str(tmp_path / "skill"),
        "previous_installation": {"version": 1},
        "local_snapshot": {"SKILL.md": "sha256:old"},
        "baseline": {"SKILL.md": "sha256:new"}, "files": [_file("SKILL.md")],
    }


def _review(plan, action="update"):
    host = SimpleNamespace(skills_root=Path(plan["target_path"]).parent)
    return project_review(plan, action, "source", host, review_id="review", token="token", now=1)


def _incomplete(review):
    assert review["preview_complete"] is False
    assert review["confirmable"] is False
    assert review["review_token"] is None
    assert review["expires_at_ms"] is None


def test_update_projects_the_union_with_all_four_changes_without_mutating_plan(plan):
    plan["local_snapshot"] = {
        "SKILL.md": "sha256:old", "gone.txt": "sha256:gone", "same.txt": "sha256:same",
    }
    plan["files"] += [_file("added.txt", size=0), _file("same.txt", identity="same")]
    plan["baseline"].update({"added.txt": "sha256:new", "same.txt": "sha256:same"})
    original = copy.deepcopy(plan)

    review = _review(plan)

    assert sorted(review["files"], key=lambda row: row["path"]) == [
        {"path": "SKILL.md", "size": 7, "change": "replace"},
        {"path": "added.txt", "size": 0, "change": "add"},
        {"path": "gone.txt", "size": 0, "change": "remove"},
        {"path": "same.txt", "size": 7, "change": "unchanged"},
    ]
    assert review["preview_complete"] and review["confirmable"]
    assert plan == original


@pytest.mark.parametrize(("old_mode", "new_mode", "change"), [
    ("file", "file", "unchanged"), ("exec", "exec", "unchanged"),
    ("exec", "file", "replace"), ("file", "exec", "replace"),
])
def test_update_compares_executable_intent_as_well_as_content(plan, old_mode, new_mode, change):
    plan["local_snapshot"] = {"SKILL.md": ("exec:" if old_mode == "exec" else "") + "sha256:new"}
    plan["files"] = [_file("SKILL.md", mode=new_mode)]
    plan["baseline"] = {"SKILL.md": ("exec:" if new_mode == "exec" else "") + "sha256:new"}

    assert _review(plan)["files"] == [{"path": "SKILL.md", "size": 7, "change": change}]


@pytest.mark.parametrize("count", [1999, 2000, 2001])
def test_update_limits_the_union_not_the_incoming_inventory(plan, count):
    plan["local_snapshot"] = {f"old-{index}.txt": "sha256:old" for index in range(1000)}
    plan["files"] = [_file(f"new-{index}.txt") for index in range(count - 1000)]
    plan["baseline"] = {row["path"]: row["hash"] for row in plan["files"]}

    review = _review(plan)

    assert len(review["files"]) <= 2000
    if count > 2000:
        _incomplete(review)
    else:
        assert len(review["files"]) == count
        assert review["confirmable"] and review["preview_complete"]


def test_overlapping_inventories_count_each_path_once(plan):
    plan["files"] = [_file(f"same-{index}.txt") for index in range(2000)]
    plan["local_snapshot"] = {row["path"]: row["hash"] for row in plan["files"]}
    plan["baseline"] = dict(plan["local_snapshot"])

    review = _review(plan)

    assert len(review["files"]) == 2000 and review["confirmable"]
    assert all(row["change"] == "unchanged" for row in review["files"])


@pytest.mark.parametrize("inventory", [None, {}, "unknown"])
def test_update_refuses_untrustworthy_incoming_inventory(plan, inventory):
    plan["files"] = inventory
    _incomplete(_review(plan))


def test_update_refuses_omitted_incoming_inventory(plan):
    del plan["files"]
    plan["baseline"] = {}
    _incomplete(_review(plan))


@pytest.mark.parametrize("snapshot", [None, [], "unknown", {"SKILL.md": None}])
def test_update_refuses_untrustworthy_old_inventory(plan, snapshot):
    plan["local_snapshot"] = snapshot
    _incomplete(_review(plan))


def test_update_does_not_substitute_recorded_baseline_for_missing_actual_snapshot(plan):
    del plan["local_snapshot"]
    plan["previous_baseline"] = {"lost-local.txt": "sha256:old"}
    _incomplete(_review(plan))


@pytest.mark.parametrize("size", [None, True, -1, 1.5, "7", 9007199254740992])
def test_update_refuses_untrustworthy_incoming_size(plan, size):
    plan["files"][0]["size"] = size
    _incomplete(_review(plan))


def test_update_refuses_missing_size_instead_of_inventing_zero(plan):
    del plan["files"][0]["size"]
    _incomplete(_review(plan))


@pytest.mark.parametrize("field", ["hash", "mode"])
def test_update_refuses_missing_comparison_identity(plan, field):
    del plan["files"][0][field]
    _incomplete(_review(plan))


def test_update_refuses_duplicate_incoming_paths(plan):
    plan["files"].append(dict(plan["files"][0]))
    _incomplete(_review(plan))


@pytest.mark.parametrize("inventory", [[None], [{}], [{"path": "SKILL.md"}]])
def test_update_refuses_malformed_rows_without_crashing(plan, inventory):
    plan["files"] = inventory
    _incomplete(_review(plan))


def test_update_refuses_inventory_that_omits_a_known_incoming_path(plan):
    plan["baseline"]["hidden.txt"] = "sha256:hidden"
    _incomplete(_review(plan))


def test_update_refuses_inventory_that_disagrees_with_incoming_identity(plan):
    plan["baseline"]["SKILL.md"] = "sha256:different"
    _incomplete(_review(plan))


def test_explicit_empty_incoming_inventory_can_review_removing_the_old_tree(plan):
    plan["files"], plan["baseline"] = [], {}
    review = _review(plan)
    assert review["files"] == [{"path": "SKILL.md", "size": 0, "change": "remove"}]
    assert review["confirmable"]


def test_install_still_adds_every_incoming_file_without_old_snapshot(plan):
    del plan["local_snapshot"]
    review = _review(plan, "install")
    assert review["files"] == [{"path": "SKILL.md", "size": 7, "change": "add"}]
    assert review["confirmable"]


def test_uninstall_preserves_its_actual_file_sizes(plan):
    target = Path(plan["target_path"])
    target.mkdir()
    (target / "SKILL.md").write_bytes(b"old bytes")
    del plan["files"]
    review = _review(plan, "uninstall")
    assert review["files"] == [{"path": "SKILL.md", "size": 9, "change": "remove"}]
    assert review["confirmable"]


def test_blocked_native_plan_never_receives_generic_consent(plan):
    plan["allowed"] = False
    review = _review(plan)
    assert review["preview_complete"]
    assert not review["confirmable"] and review["review_token"] is None


def test_real_native_update_inventory_remains_confirmable(tmp_path):
    from test_update_review import _reviewer  # pylint: disable=import-outside-toplevel

    reviewer, _store, target = _reviewer(tmp_path)
    native = reviewer.plan("skill-1")
    before = {path: (target / path).read_bytes() for path in native["local_snapshot"]}

    review = _review(native)

    assert review["confirmable"] and review["preview_complete"]
    assert len(review["files"]) == len(native["files"])
    assert next(row for row in review["files"] if row["path"] == "SKILL.md")[
        "change"] == "replace"
    assert {row["change"] for row in review["files"]} == {"unchanged", "replace"}
    assert before == {path: (target / path).read_bytes() for path in native["local_snapshot"]}


def test_review_can_use_complete_native_inventory_without_a_redundant_baseline(plan):
    del plan["baseline"]
    assert _review(plan)["confirmable"]
