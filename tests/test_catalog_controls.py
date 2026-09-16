"""Profile-bound catalog controls for managed and locally published skills."""

# Small service fixtures implement the controller protocol rather than public API.
# pylint: disable=missing-class-docstring,missing-function-docstring

from __future__ import annotations

from pathlib import Path

from native_test_support import native_invocation, owner
from test_publication import publication as create_publication

from hermes_wisdom_plugin.native_review import NativeReviewController, project_view
from hermes_wisdom_plugin.store import WisdomStore


CONTENT_A = "sha256:" + "a" * 64
CONTENT_B = "sha256:" + "b" * 64


class CatalogService:
    def __init__(self, store, rows, bound_owner, *, detail_hash=CONTENT_A):
        self.store = store
        self.rows = rows
        self.bound_owner = bound_owner
        self.detail_hash = detail_hash
        self.update_plans = []

    def native_owner(self):
        return dict(self.bound_owner)

    def search_skills(self, _query=None):
        return [dict(row) for row in self.rows]

    def show(self, skill_id):
        versions = [
            {
                **row,
                "version": row.get("version", row.get("latest_version")),
            }
            for row in self.rows
        ]
        latest = max(versions, key=lambda row: row["version"])
        return {
            "skill": {
                "id": skill_id,
                "slug": "shared-skill",
                "created_by_user_id": latest.get("created_by_user_id"),
            },
            "versions": versions,
            "latest_version_detail": {
                "skill": {"id": skill_id},
                "version": {**latest, "content_hash": self.detail_hash},
            },
            "portal_url": "https://example.invalid/skill",
        }

    def resolve_skill(self, reference):
        return self.show(reference)

    def versions(self, _skill_id):
        return [dict(row) for row in self.rows]

    def update_plan(self, skill_id, **choices):
        self.update_plans.append((skill_id, choices))
        return {
            "operation": "update", "owner": dict(self.bound_owner), "allowed": True,
            "skill_id": skill_id, "slug": "shared-skill", "from_version": 1,
            "version": 2, "receipt": "update-receipt", "files": [], "policy": {},
        }


def install(store, bound_owner, *, version):
    store.record_install({
        "skill_id": "skill-1", "org_id": bound_owner["organization_id"],
        "slug": "shared-skill", "version": version, "content_hash": CONTENT_A,
        "baseline": {"SKILL.md": CONTENT_A}, "target_path": "/tmp/skills/shared-skill",
        "update_mode": "MANUAL", "owner": bound_owner,
    })


def browse_view(store, rows, bound_owner, *, detail_hash=CONTENT_A):
    controller = NativeReviewController(store)
    record = controller.request_catalog(
        CatalogService(store, rows, bound_owner, detail_hash=detail_hash),
        native_invocation(), "browse",
    )
    return project_view(record)


def test_current_managed_version_suppresses_install_and_newer_version_offers_review_update(
    tmp_path: Path,
):
    store = WisdomStore(tmp_path / "state")
    bound_owner = owner()
    install(store, bound_owner, version=1)

    current = browse_view(store, [{
        "id": "skill-1", "slug": "shared-skill", "latest_version": 1,
        "content_hash": CONTENT_A,
    }], bound_owner)
    newer = browse_view(store, [{
        "id": "skill-1", "slug": "shared-skill", "latest_version": 2,
        "content_hash": CONTENT_B,
    }], bound_owner)

    assert [action.label for action in current.items[0].actions] == ["View"]
    assert [action.label for action in newer.items[0].actions] == ["View", "Review update"]
    assert newer.items[0].actions[-1].action == "catalog.install.0"


def test_other_profile_install_does_not_hide_current_profiles_install(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    bound_owner = owner()
    install(store, owner(profile_name="other", profile_home="/profiles/other"), version=1)

    view = browse_view(store, [{
        "id": "skill-1", "slug": "shared-skill", "latest_version": 1,
        "content_hash": CONTENT_A,
    }], bound_owner)

    assert [action.label for action in view.items[0].actions] == ["View", "Install"]


def test_detail_and_versions_use_the_same_version_aware_controls(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    bound_owner = owner()
    install(store, bound_owner, version=1)
    rows = [
        {"id": "version-row-1", "version": 1, "content_hash": CONTENT_A},
        {"id": "version-row-2", "version": 2, "content_hash": CONTENT_B},
    ]
    controller = NativeReviewController(store)
    service = CatalogService(store, rows, bound_owner)

    detail = project_view(controller.request_catalog(
        service, native_invocation(), "show", "skill-1",
    ))
    versions = project_view(controller.request_catalog(
        service, native_invocation(), "versions", "skill-1",
    ))

    assert [action.label for action in detail.actions] == [
        "Versions", "View in Portal", "Review update",
    ]
    assert [action.label for action in versions.items[0].actions] == [
        "View in Portal", "Review update",
    ]
    assert [action.label for action in versions.items[1].actions] == ["View in Portal"]


def test_exact_published_source_from_this_profile_suppresses_redundant_install(tmp_path: Path):
    publication, prepared, _client, bound_owner = create_publication(tmp_path)
    result = publication.submit(publication.review(prepared["draft_id"])["receipt"])
    store = publication.store
    content_hash = result["submitted_request"]["content_hash"]

    exact = browse_view(store, [{
        "id": "remote-skill", "slug": "shared-skill", "latest_version": 1,
        "created_by_user_id": bound_owner["account_id"],
    }], bound_owner, detail_hash=content_hash)
    same_slug_other_bytes = browse_view(store, [{
        "id": "remote-skill", "slug": "shared-skill", "latest_version": 2,
        "created_by_user_id": bound_owner["account_id"],
    }], bound_owner, detail_hash=CONTENT_B)
    same_bytes_other_creator = browse_view(store, [{
        "id": "remote-skill", "slug": "shared-skill", "latest_version": 1,
        "created_by_user_id": "teammate",
    }], bound_owner, detail_hash=content_hash)

    assert [action.label for action in exact.items[0].actions] == ["View"]
    assert "Published from this profile" in exact.items[0].detail
    assert [action.label for action in same_slug_other_bytes.items[0].actions] == [
        "View", "Install",
    ]
    assert [action.label for action in same_bytes_other_creator.items[0].actions] == [
        "View", "Install",
    ]


def test_publication_from_another_profile_never_suppresses_install(tmp_path: Path):
    publication, prepared, _client, published_owner = create_publication(tmp_path)
    result = publication.submit(publication.review(prepared["draft_id"])["receipt"])
    store = publication.store
    bound_owner = {
        **published_owner, "profile_name": "other", "profile_home": "/profiles/other",
    }

    controller = NativeReviewController(store)
    service = CatalogService(store, [{
        "id": "remote-skill", "slug": "shared-skill", "latest_version": 1,
        "created_by_user_id": published_owner["account_id"],
    }], bound_owner, detail_hash=result["submitted_request"]["content_hash"])
    view = project_view(controller.request_catalog(
        service, native_invocation(profile="other"), "browse",
    ))
    other_org = {**published_owner, "organization_id": "other-org"}
    org_view = browse_view(store, [{
        "id": "remote-skill", "slug": "shared-skill", "latest_version": 1,
        "created_by_user_id": published_owner["account_id"],
    }], other_org, detail_hash=result["submitted_request"]["content_hash"])

    assert [action.label for action in view.items[0].actions] == ["View", "Install"]
    assert [action.label for action in org_view.items[0].actions] == ["View", "Install"]


def test_deleted_local_source_does_not_suppress_its_published_catalog_version(tmp_path: Path):
    publication, prepared, _client, bound_owner = create_publication(tmp_path)
    result = publication.submit(publication.review(prepared["draft_id"])["receipt"])
    source = Path(publication.store.local_skill(prepared["skill_id"])["canonical_path"])
    for child in source.iterdir():
        child.unlink()
    source.rmdir()
    service = CatalogService(publication.store, [{
        "id": "remote-skill", "slug": "reviewed-skill", "latest_version": 1,
        "created_by_user_id": bound_owner["account_id"],
    }], bound_owner, detail_hash=result["submitted_request"]["content_hash"])

    view = project_view(NativeReviewController(publication.store).request_catalog(
        service, native_invocation(), "browse",
    ))

    assert [action.label for action in view.items[0].actions] == ["View", "Install"]


def test_review_update_uses_update_review_and_rechecks_stale_install_state(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    bound_owner = owner()
    install(store, bound_owner, version=1)
    controller = NativeReviewController(store)
    invocation = native_invocation(message_id="card")
    service = CatalogService(store, [{
        "id": "skill-1", "slug": "shared-skill", "latest_version": 2,
        "created_by_user_id": "teammate",
    }], bound_owner, detail_hash=CONTENT_B)
    record = controller.request_catalog(service, invocation, "browse")
    controller.bind_delivery(record["id"], invocation, "card")

    updated = controller.act(service, record["id"], invocation, "catalog.install.0")

    assert updated["operation"] == "update"
    assert updated["plan"]["receipt"] == "update-receipt"
    assert service.update_plans == [("skill-1", {})]

    stale_store = WisdomStore(tmp_path / "stale")
    stale_controller = NativeReviewController(stale_store)
    stale_service = CatalogService(stale_store, service.rows, bound_owner, detail_hash=CONTENT_B)
    stale_invocation = native_invocation(message_id="stale-card")
    stale_record = stale_controller.request_catalog(stale_service, stale_invocation, "browse")
    stale_controller.bind_delivery(stale_record["id"], stale_invocation, "stale-card")
    install(stale_store, bound_owner, version=2)

    refreshed = stale_controller.act(
        stale_service, stale_record["id"], stale_invocation, "catalog.install.0",
    )

    assert refreshed["operation"] == "catalog"
    assert [action.label for action in project_view(refreshed).items[0].actions] == ["View"]
    assert "Installed: current v2" in project_view(refreshed).items[0].detail


def test_catalog_pagination_refreshes_rows_before_navigation(tmp_path: Path):
    store = WisdomStore(tmp_path / "state")
    bound_owner = owner()
    rows = [
        {"id": f"skill-{index}", "slug": f"skill-{index}", "latest_version": 1,
         "created_by_user_id": "teammate"}
        for index in range(6)
    ]
    service = CatalogService(store, rows, bound_owner)
    controller = NativeReviewController(store)
    invocation = native_invocation(message_id="card")
    record = controller.request_catalog(service, invocation, "browse")
    controller.bind_delivery(record["id"], invocation, "card")
    service.rows[-1] = {
        "id": "fresh-skill", "slug": "fresh-skill", "latest_version": 1,
        "created_by_user_id": "teammate",
    }

    refreshed = controller.act(service, record["id"], invocation, "catalog.page.1")

    assert project_view(refreshed).items[0].title == "fresh-skill"
