"""Offline native transient catalog failures never create a replayable Slack card."""
# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio

import pytest

from installed_test_support import JsonState
from native_test_support import owner
from restoration_support import SlackHarness
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomUnavailable
from hermes_wisdom_plugin.runtime import _native_review_reply


class Service:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.reads = 0

    def native_owner(self):
        return owner()

    def close(self):
        return None


@pytest.mark.parametrize("command,target", [
    ("browse", "incident"), ("show", "skill-1"), ("versions", "skill-1"),
])
def test_slack_catalog_transient_failure_requires_new_read_command(tmp_path, command, target):
    class UnavailableService(Service):
        def search_skills(self, _query):
            self.reads += 1
            raise WisdomUnavailable()

        def show(self, _target):
            self.reads += 1
            raise WisdomUnavailable()

        def versions(self, _target):
            self.reads += 1
            raise WisdomUnavailable()

    async def scenario():
        service = UnavailableService()
        ui = SlackHarness(tmp_path, service)
        before = JsonState(tmp_path).get("wisdom-reviews")
        result = ui.runtime.begin_catalog(ui.caller, command, target)
        assert _native_review_reply(result) == (
            "Collective Wisdom is temporarily unavailable. "
            f"Resend your /wisdom {command} command to retry."
        )
        assert service.reads == 1
        assert JsonState(tmp_path).get("wisdom-reviews") == before
        await ui.flush()
        assert not ui.posts
        ui.runtime.close()

    asyncio.run(scenario())


def test_slack_catalog_auth_failure_is_not_presented_as_transient(tmp_path):
    class UnauthenticatedService(Service):
        def search_skills(self, _query):
            raise WisdomAuthError("Sign in required")

    async def scenario():
        ui = SlackHarness(tmp_path, UnauthenticatedService())
        with pytest.raises(WisdomAuthError):
            ui.runtime.begin_catalog(ui.caller, "browse")
        assert not ui.posts
        ui.runtime.close()

    asyncio.run(scenario())
