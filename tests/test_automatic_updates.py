"""Automatic updates run directly without a notice or delivery queue."""
# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

from hermes_wisdom_plugin.automatic_updates import AutomaticUpdateMaintenance


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
        return {"state": "update_available", "effective_update_mode": "AUTO_WITH_NOTICE"}

    def plan_automatic(self, skill_id):
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
