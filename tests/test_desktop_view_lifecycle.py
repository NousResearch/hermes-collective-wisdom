"""A slow owner read cannot revive revoked Desktop review authority."""
# pylint: disable=missing-function-docstring,missing-class-docstring,too-few-public-methods

from pathlib import Path

import pytest

from hermes_wisdom_plugin.desktop_api import DesktopWorkflowApi


class _Owner:
    def __init__(self, home: Path):
        self.profile = ("disposable", home)
        self.on_read = lambda: None

    def native_owner(self):
        self.on_read()
        return {"account_id": "fixture", "installation_id": "fixture-installation"}


@pytest.mark.parametrize("invalidation", ["close", "expire"])
def test_owner_read_does_not_revive_invalidated_view(tmp_path, invalidation):
    now = [100.0]
    workflow = DesktopWorkflowApi(clock=lambda: now[0], view_ttl_seconds=60)
    service = _Owner(tmp_path)
    view_id = workflow.open_view(service)["view_id"]

    def invalidate():
        # Use the real close path, suppressing only recursive owner callbacks.
        service.on_read = lambda: None
        if invalidation == "close":
            workflow.close_view(service, view_id)
        else:
            now[0] = 161.0

    service.on_read = invalidate
    assert workflow.current(service, view_id) is False


def test_disable_during_owner_read_revokes_inflight_authority(tmp_path):
    enabled = [True]
    workflow = DesktopWorkflowApi(enabled=lambda: enabled[0])
    service = _Owner(tmp_path)
    view_id = workflow.open_view(service)["view_id"]
    service.on_read = lambda: enabled.__setitem__(0, False)
    assert workflow.current(service, view_id) is False


def test_owner_rotation_immediately_after_first_snapshot_revokes_view(tmp_path):
    class RotatingOwner:
        def __init__(self, home):
            self.profile = ("disposable", home)
            self.account = "account-a"
            self.rotate_after_read = False

        def native_owner(self):
            owner = {
                "account_id": self.account,
                "installation_id": f"installation-{self.account}",
            }
            if self.rotate_after_read:
                self.rotate_after_read = False
                self.account = "account-b"
            return owner

    workflow = DesktopWorkflowApi()
    service = RotatingOwner(tmp_path)
    view_id = workflow.open_view(service)["view_id"]
    service.rotate_after_read = True

    assert workflow.current(service, view_id) is False
