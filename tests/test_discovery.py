"""Tests adapted from WisdomService discovery coverage at source revision 0924f7946b."""

# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.discovery import MAX_DISCOVERY_PAGES, Discovery
from hermes_wisdom_plugin.errors import WisdomNotFound, WisdomValidationError


class _Model(SimpleNamespace):
    def model_dump(self, *, mode):
        assert mode == "json"
        return deepcopy(self.value)


def _item(**overrides):
    value = {
        "id": "skill-1",
        "slug": "incident-handoff",
        "author_description": "Summarize an incident",
    }
    value.update(overrides)
    return _Model(value=value)


def _page(items, cursor=None):
    value = {
        "skills": [item.model_dump(mode="json") for item in items],
        "next_cursor": cursor,
    }
    return _Model(value=value, skills=items, next_cursor=cursor)


def _detail(skill_id="skill-1", versions=None):
    value = {
        "skill": {"id": skill_id, "slug": "incident-handoff"},
        "versions": deepcopy(versions or [{"version": 2}, {"version": 1}]),
    }
    return _Model(value=value, versions=value["versions"])


class _Client:
    def __init__(self):
        self.pages = [_page([_item()])]
        self.skill_calls = []
        self.version_calls = []

    def list_skills(self, *, cursor=None):
        index = 0 if cursor is None else int(cursor)
        return self.pages[index]

    def skill(self, skill_id):
        self.skill_calls.append(skill_id)
        return _detail(skill_id)

    def version(self, skill_id, version):
        self.version_calls.append((skill_id, version))
        value = {
            "skill": {"id": skill_id, "slug": "incident-handoff"},
            "version": {"version": version, "author_description": "Latest"},
        }
        return _Model(value=value)


def _discovery(client=None, checks=None):
    checks = checks if checks is not None else []
    return Discovery(
        client or _Client(),
        lambda: checks.append("owner"),
        "https://portal.example/",
        "nas_organisation:team one",
    )


def test_list_skills_preserves_wire_shape_and_checks_owner_before_and_after():
    checks = []
    result = _discovery(checks=checks).list_skills()

    assert result == {
        "skills": [
            {
                "id": "skill-1",
                "slug": "incident-handoff",
                "author_description": "Summarize an incident",
            }
        ],
        "next_cursor": None,
    }
    assert checks == ["owner", "owner"]


def test_local_status_overwrites_untrusted_remote_status():
    client = _Client()
    client.pages = [_page([_item(
        latest_version=2,
        installation_status={"state": "installed", "text": "remote claim"},
    )])]
    discovery = Discovery(
        client, lambda: None, "https://portal.example", "org-1",
        installation_status=lambda _skill_id, _slug, latest: {
            "state": "not_installed", "text": "Not installed",
            "installed_version": None, "latest_version": latest,
        },
    )

    result = discovery.search_skills()

    assert result[0]["installation_status"]["text"] == "Not installed"


def test_owner_is_rechecked_when_client_raises():
    checks = []
    client = _Client()
    client.list_skills = lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("late"))

    with pytest.raises(TimeoutError, match="late"):
        _discovery(client, checks).search_skills()

    assert checks == ["owner", "owner"]


def test_search_pages_and_casefolds_every_reference_author_field():
    client = _Client()
    client.pages = [
        _page([_item(id="one", slug="Deploy")], "1"),
        _page(
            [
                _item(id="two", slug="other", created_by_display_name="DEPLOYER"),
                _item(id="three", slug="third", author="deploy team"),
            ]
        ),
    ]
    checks = []

    result = _discovery(client, checks).search_skills("  DEPLOY  ")

    assert [item["id"] for item in result] == ["one", "two", "three"]
    assert len(checks) == 4


def test_search_rejects_repeated_cursor():
    client = _Client()
    client.pages = [_page([], "1"), _page([], "1")]

    with pytest.raises(WisdomValidationError, match="repeated"):
        _discovery(client).search_skills()


def test_search_enforces_one_hundred_page_limit():
    class EndlessClient(_Client):
        def list_skills(self, *, cursor=None):
            number = int(cursor or 0)
            return _page([], str(number + 1))

    checks = []
    with pytest.raises(WisdomValidationError, match="page limit"):
        _discovery(EndlessClient(), checks).search_skills()

    assert len(checks) == MAX_DISCOVERY_PAGES * 2


def test_resolve_prefers_id_then_uses_one_exact_case_sensitive_slug():
    client = _Client()

    def skill(skill_id):
        client.skill_calls.append(skill_id)
        if skill_id in {"incident-handoff", "Incident-Handoff"}:
            raise WisdomNotFound("opaque miss")
        return _detail(skill_id)

    client.skill = skill
    assert _discovery(client).resolve_skill("skill-1")["skill"]["id"] == "skill-1"
    assert _discovery(client).resolve_skill("incident-handoff")["skill"]["id"] == "skill-1"
    with pytest.raises(WisdomNotFound, match="Wisdom skill not found"):
        _discovery(client).resolve_skill("Incident-Handoff")


def test_resolve_prefers_exact_name_when_another_skill_has_matching_id():
    class CollisionClient(_Client):
        def __init__(self):
            super().__init__()
            self.pages = [_page([_item(id="skill-1", slug="incident-handoff")])]

        def skill(self, skill_id):
            self.skill_calls.append(skill_id)
            if skill_id == "incident-handoff":
                return _Model(value={"skill": {"id": skill_id, "slug": "other-skill"},
                                     "versions": []}, versions=[])
            return _detail(skill_id)

    client = CollisionClient()
    assert _discovery(client).resolve_skill("incident-handoff")["skill"]["id"] == "skill-1"
    assert client.skill_calls == ["incident-handoff", "skill-1"]


def test_resolve_keeps_missing_and_ambiguous_slug_opaque():
    client = _Client()
    client.skill = lambda _skill_id: (_ for _ in ()).throw(WisdomNotFound("secret"))
    client.pages = [
        _page(
            [
                _item(id="one", slug="duplicate"),
                _item(id="two", slug="duplicate"),
            ]
        )
    ]

    with pytest.raises(WisdomNotFound, match="Wisdom skill not found"):
        _discovery(client).resolve_skill("duplicate")


def test_show_fetches_only_latest_positive_published_version_and_portal_url():
    client = _Client()
    versions = [
        {"version": True},
        {"version": -1},
        {"version": 0},
        {"version": 9, "state": "draft"},
        {"version": 3, "state": "published"},
        {"version": 2},
    ]
    client.skill = lambda skill_id: _detail(skill_id, versions)

    result = _discovery(client).show("skill/a")

    assert client.version_calls == [("skill/a", 3)]
    assert result["latest_version_detail"]["version"]["version"] == 3
    assert result["portal_url"] == (
        "https://portal.example/wisdom/skills/skill%2Fa?"
        "org_id=nas_organisation%3Ateam+one"
    )
    assert "local_compatibility" not in result
    assert "local_installation" not in result


def test_versions_returns_authoritative_index_with_owner_checks():
    checks = []
    result = _discovery(checks=checks).versions("skill-1")

    assert result == [{"version": 2}, {"version": 1}]
    assert checks == ["owner", "owner"]


@pytest.mark.parametrize(
    "arguments",
    [
        (object(), lambda: None, "https://portal.example", "org"),
        (_Client(), object(), "https://portal.example", "org"),
        (_Client(), lambda: None, "", "org"),
        (_Client(), lambda: None, "https://portal.example", ""),
    ],
)
def test_constructor_rejects_malformed_dependencies(arguments):
    with pytest.raises((TypeError, ValueError)):
        Discovery(*arguments)


@pytest.mark.parametrize("value", [None, "", "\x00bad"])
def test_public_reads_reject_malformed_references(value):
    with pytest.raises(WisdomValidationError, match="reference"):
        _discovery().show(value)


def test_show_uses_canonical_org_route_when_slug_is_verified():
    discovery = Discovery(
        _Client(), lambda: None, "https://portal.example/", "org-1",
        org_slug=lambda: "incident-team",
    )
    assert discovery.show("skill/a")["portal_url"] == (
        "https://portal.example/orgs/incident-team/wisdom/skills/skill%2Fa"
    )


@pytest.mark.parametrize("slug", [lambda: None, lambda: (_ for _ in ()).throw(OSError())])
def test_show_falls_back_to_slug_resolving_redirect_route(slug):
    discovery = Discovery(
        _Client(), lambda: None, "https://portal.example", "org-1", org_slug=slug,
    )
    assert discovery.show("skill-1")["portal_url"] == (
        "https://portal.example/wisdom/skills/skill-1?org_id=org-1"
    )


def test_show_reads_the_org_slug_inside_the_owner_checked_path():  # (Salt R1 #1)
    """A switch observed around the slug read fails closed instead of linking."""
    state = {"switched": False}
    def check_owner():
        if state["switched"]:
            raise WisdomValidationError("owner changed")
    def org_slug():
        state["switched"] = True
        return "team-one"
    discovery = Discovery(
        _Client(), check_owner, "https://portal.example", "org-1", org_slug=org_slug,
    )
    with pytest.raises(WisdomValidationError, match="owner changed"):
        discovery.show("skill-1")


def test_portal_skill_url_carries_version_on_both_routes():
    from hermes_wisdom_plugin.portal_urls import portal_skill_url  # pylint: disable=import-outside-toplevel
    assert portal_skill_url("https://p.example/", "s 1", org_slug="team",
                            organization_id="org-1", version=3) == (
        "https://p.example/orgs/team/wisdom/skills/s%201?version=3")
    assert portal_skill_url("https://p.example", "s1", org_slug=None,
                            organization_id="org 1", version=2) == (
        "https://p.example/wisdom/skills/s1?org_id=org+1&version=2")


def test_search_lists_newest_publication_first_across_pages():
    client = _Client()
    client.pages = [
        _page([_item(id="old", latest_published_at="2026-09-17T00:32:42.877Z"),
               _item(id="undated")], "1"),
        _page([_item(id="newest", latest_published_at="2026-10-22T17:21:16.842Z"),
               _item(id="first-only", first_published_at="2026-10-01T08:00:00.000Z")]),
    ]

    result = _discovery(client).search_skills()

    assert [item["id"] for item in result] == ["newest", "first-only", "old", "undated"]
