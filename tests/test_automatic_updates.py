"""Automatic updates run directly without a notice or delivery queue."""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

from hermes_wisdom_plugin.automatic_updates import AutomaticUpdateMaintenance
from hermes_wisdom_plugin.package import PackagePolicyError


class Installed:
    def __init__(self):
        self.cursor = None

    def installations(self, *, owner):
        del owner
        return [
            {"skill_id": "skill-a", "state": "active"},
            {"skill_id": "skill-b", "state": "disabled"},
        ]

    def automatic_cursor(self, owner):
        del owner
        return self.cursor

    def set_automatic_cursor(self, owner, skill_id):
        del owner
        self.cursor = skill_id


class Reviewer:
    def check(self, _skill_id):
        # cw/v1: auto-apply needs a readable Gateway scan pass for the new version.
        return {
            "state": "update_available", "effective_update_mode": "AUTO_WITH_NOTICE",
            "latest_version": 2,
            "latest_access_review": {
                "access_expanded": False, "new_capabilities": [], "scan_status": "pass",
            },
        }

    def plan_automatic(self, skill_id, **_kwargs):
        return {"receipt": f"receipt:{skill_id}"}


class Executor:
    def apply_automatic(self, receipt):
        return {"updated": True, "receipt": receipt}


def test_automatic_updates_need_no_notice_store_or_delivery_queue():
    installed = Installed()
    runner = AutomaticUpdateMaintenance(
        installed, Reviewer(), Executor(), lambda: {"account_id": "acct"}
    )

    result = runner.run()

    assert result["processed"] == 1
    assert result["results"] == [{
        "skill_id": "skill-a",
        "state": "updated",
        "result": {"updated": True, "receipt": "receipt:skill-a"},
    }]
    assert installed.cursor == "skill-a"


def test_manual_update_policy_never_plans_or_applies_maintenance():
    """U09/X09: maintenance checks MANUAL installs but cannot mutate them."""
    events = []

    class ManualReviewer:
        def check(self, skill_id):
            events.append(("check", skill_id))
            return {"state": "update_available", "effective_update_mode": "MANUAL"}

        def plan_automatic(self, skill_id):
            events.append(("plan", skill_id))
            raise AssertionError("manual update was planned")

    class RejectingExecutor:
        def apply_automatic(self, receipt):
            events.append(("apply", receipt))
            raise AssertionError("manual update was applied")

    result = AutomaticUpdateMaintenance(
        Installed(), ManualReviewer(), RejectingExecutor(), lambda: {"account_id": "acct"}
    ).run()

    assert events == [("check", "skill-a")]
    assert result["results"] == [{
        "skill_id": "skill-a",
        "state": "needs_review",
        "reason": "manual update policy requires explicit review",
    }]


def test_modified_automatic_update_is_review_only_and_does_not_block_neighbor():
    """U07/X07: a protected local tree stays blocked while the batch remains bounded."""

    class TwoInstalled(Installed):
        def installations(self, *, owner):
            del owner
            return [
                {"skill_id": "skill-a", "state": "active"},
                {"skill_id": "skill-b", "state": "active"},
            ]

    class ModifiedReviewer(Reviewer):
        def plan_automatic(self, skill_id, **kwargs):
            if skill_id == "skill-a":
                raise PackagePolicyError("installed package has local modifications")
            return super().plan_automatic(skill_id, **kwargs)

    executor = Executor()
    result = AutomaticUpdateMaintenance(
        TwoInstalled(), ModifiedReviewer(), executor, lambda: {"account_id": "acct"}
    ).run()

    assert result == {
        "results": [
            {
                # cw/v1: a refused plan pauses as "Needs a look" (single Update).
                "skill_id": "skill-a",
                "state": "needs_look",
                "reason": "flagged",
                "detail": "installed package has local modifications",
            },
            {
                "skill_id": "skill-b",
                "state": "updated",
                "result": {"updated": True, "receipt": "receipt:skill-b"},
            },
        ],
        "processed": 2,
        "more": False,
    }
