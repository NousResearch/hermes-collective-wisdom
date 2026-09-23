"""Owner-bound listing of resumable local skill drafts."""
# pylint: disable=missing-function-docstring

from types import SimpleNamespace
from typing import Any, cast

from installed_test_support import JsonState

from hermes_wisdom_plugin.runtime import InvocationService

from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.contribution_review import ContributionReviewer
from hermes_wisdom_plugin.errors import WisdomAuthError


def _owner(account):
    return {"profile_name": "test", "profile_home": "/skills", "account_id": account,
            "organization_id": "team", "base_url": "https://gateway.invalid",
            "installation_id": "install-1"}


def test_prepared_drafts_list_only_current_owner_without_private_package(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    owner = {"profile_name": "alice", "account_id": "alice", "organization_id": "team"}
    other = {**owner, "account_id": "bob"}
    state.save_draft({"id": "draft-1", "slug": "incident-helper", "state": "prepared",
                      "owner": owner, "overlay_path": "/private/packages/one",
                      "content_hash": "secret"})
    state.save_draft({"id": "draft-2", "slug": "other-skill", "state": "prepared",
                      "owner": other})
    state.save_draft({"id": "draft-3", "slug": "published-skill", "state": "published",
                      "owner": owner})

    rows = state.prepared_drafts(owner)

    assert len(rows) == 1
    assert rows[0]["skill_name"] == "incident-helper"
    assert rows[0]["draft_id"] == "draft-1"
    assert "overlay_path" not in rows[0]
    assert "content_hash" not in rows[0]
    assert state.prepared_drafts(other)[0]["skill_name"] == "other-skill"


def test_prepared_drafts_preserve_same_name_candidates_for_explicit_selection(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    owner = {"profile_name": "alice"}
    for draft_id in ("older", "newer"):
        state.save_draft({"id": draft_id, "slug": "incident-helper", "state": "prepared",
                          "owner": owner})

    rows = state.prepared_drafts(owner)

    assert {row["draft_id"] for row in rows} == {"older", "newer"}
    assert all(row["skill_name"] == "incident-helper" for row in rows)


def test_invocation_service_exposes_owner_fenced_prepared_listing():
    service = cast(Any, InvocationService.__new__(InvocationService))
    calls = []
    service._installation_owner = lambda: calls.append("owner")  # pylint: disable=protected-access
    service._contribution_reviewer = lambda: SimpleNamespace(  # pylint: disable=protected-access
        prepared_drafts=lambda: [{"skill_name": "incident-helper", "draft_id": "draft-1"}]
    )
    service.check_owner = lambda: calls.append("check")
    assert service.prepared_drafts() == [
        {"skill_name": "incident-helper", "draft_id": "draft-1"}
    ]
    assert calls == ["owner", "check"]


def test_reviewer_lists_only_current_owner_and_rejects_owner_change(tmp_path):
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.save_draft({"id": "draft-1", "slug": "incident-helper", "state": "prepared",
                      "owner": _owner("alice")})
    current = [_owner("alice")]
    reviewer = ContributionReviewer(state, object(), lambda: current[0])

    assert reviewer.prepared_drafts()[0]["skill_name"] == "incident-helper"
    current[0] = _owner("bob")
    assert reviewer.prepared_drafts() == []

    values = iter([_owner("alice"), _owner("bob")])
    reviewer = ContributionReviewer(state, object(), lambda: next(values))
    try:
        reviewer.prepared_drafts()
    except WisdomAuthError:
        pass
    else:
        raise AssertionError("owner rotation must invalidate the listing")
