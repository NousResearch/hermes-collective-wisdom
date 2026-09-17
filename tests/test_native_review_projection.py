"""PR5 presentation must not weaken evidence or exact native consent."""
# pylint: disable=missing-function-docstring,protected-access
from copy import deepcopy
import json

import pytest
from native_test_support import (
    RecordingPublicationService, native_invocation, publication_review,
)

from hermes_wisdom_plugin.professionalism import CHECK_KEYS
from hermes_wisdom_plugin.native_review import (
    NativeReviewController, project_view,
)
from hermes_wisdom_plugin.native_telegram import (
    plain_html_is_lossless, rich_html_is_lossless,
)
from hermes_wisdom_plugin.store import WisdomStore


def passing_plan():
    return {
        "security_check": {
            "source": "local_preflight",
            "status": "pass", "local_status": "pass", "upload_allowed": True,
            "checks": [
                {"key": key, "status": "pass", "finding_count": 0}
                for key in (
                    "private_keys", "live_credentials", "secret_assignments", "skills_guard",
                )
            ],
        },
        "professionalism_check": {"status": "pass", "checks": [
            {"key": key, "status": "pass", "finding_count": 0} for key in CHECK_KEYS
        ]},
    }


@pytest.mark.parametrize("defect", ["missing", "partial", "count", "duplicate", "local", "upload"])
def test_compact_success_requires_complete_passing_evidence(defect):
    plan = passing_plan()
    record = {"state": "pending", "operation": "update", "plan": plan}
    assert "All sharing checks passed" in project_view(record).to_text()
    security = plan["security_check"]
    if defect == "missing":
        security.pop("checks")
    elif defect == "partial":
        security["checks"].pop()
    elif defect == "count":
        security["checks"][0]["finding_count"] = 1
    elif defect == "duplicate":
        security["checks"].append(deepcopy(security["checks"][0]))
    elif defect == "local":
        security["local_status"] = "unavailable"
    else:
        security["upload_allowed"] = False
    assert "All sharing checks passed" not in project_view(record).to_text()


def _publication_review(tmp_path, files, platform="slack"):
    service = RecordingPublicationService(files)
    controller = NativeReviewController(WisdomStore(tmp_path))
    actor = native_invocation(platform=platform)
    record = controller.request_publication(service, actor, "draft")
    controller.bind_delivery(record["id"], actor, "card")
    return controller, service, native_invocation(platform=platform, message_id="card"), record


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_publication_inspection_pages_preserve_all_bytes_without_approval(tmp_path, platform):
    content = "prefix\n" + "λ雪🙂" * 1500 + "\nlast-byte\n"

    controller, service, actor, record = _publication_review(
        tmp_path, [{"path": "SKILL.md", "content_utf8": content, "hash": "a" * 64}], platform,
    )
    first = controller.act(service, record["id"], actor, "inspect")
    pages = []
    for page in range(first["inspection"]["page_count"]):
        inspected = controller.act(service, record["id"], actor, f"inspect.{page}")
        pages.append(inspected["inspection"]["content"])
        view = controller.view(inspected)
        assert not any(action.action == "confirm" or action.primary for action in view.actions)
        assert view.actions[-1].label == "Looks good."
        assert not view.navigation_actions
        assert view.notice == "Reviewing does not upload, publish, or approve this package."
        assert len(inspected["inspection"]["content"]) <= 1000
    assert "".join(pages) == content
    returned = controller.act(service, record["id"], actor, "back")
    assert returned["state"] == "pending" and returned["inspection"] is None
    service.publication_submit.assert_not_called()


@pytest.mark.parametrize(
    ("files", "action", "message"),
    [
        ([], "inspect", "review page was not found"),
        ([{"path": "SKILL.md", "content_utf8": ""}], "inspect.1", "review page was not found"),
        ([{"path": "SKILL.md", "content_utf8": None}], "inspect", "requires the terminal"),
    ],
)
def test_publication_inspection_rejects_invalid_or_out_of_bounds_pages(
    tmp_path, files, action, message,
):
    controller, _service, actor, record = _publication_review(tmp_path, files)

    with pytest.raises(ValueError, match=message):
        controller.act(_service, record["id"], actor, action)


def test_blank_publication_file_is_one_exact_page(tmp_path):
    controller, service, actor, record = _publication_review(
        tmp_path, [{"path": "EMPTY", "content_utf8": ""}],
    )
    inspected = controller.act(service, record["id"], actor, "inspect")

    assert inspected["inspection"] == {
        "page": 0, "page_count": 1, "path": "EMPTY", "content": "",
    }


@pytest.mark.parametrize("platform", ["slack", "telegram"])
def test_compact_publication_details_round_trip_is_read_only_and_hash_stable(
    tmp_path, platform,
):  # pylint: disable=too-many-locals
    content_hash = "sha256:" + "b" * 64
    files = [{
        "path": "SKILL.md",
        "content_utf8": "---\nname: incident-review\ndescription: Review incidents.\n---\n",
        "hash": "sha256:" + "a" * 64,
    }]
    controller, service, actor, record = _publication_review(tmp_path, files, platform)
    plan = record["plan"]
    plan["editorial_name"] = "Incident Review"
    plan["editorial_description"] = "Turns incident notes into a repeatable review."
    plan["description"] = (
        "Exact owner description. " + "overflow " * 300
        + "\n\nPublisher usage (client-reported)\n"
        "Local ledger: 7 invocations on 5 days."
    )
    plan["hashes"] = {
        "content": content_hash,
        "author_description": "sha256:" + "c" * 64,
        "package_manifest": "sha256:" + "d" * 64,
    }
    with controller.store.transaction() as db:
        db.execute(
            "UPDATE wisdom_consent SET plan_json=?,result_json=? WHERE id=?",
            (
                json.dumps(plan, sort_keys=True),
                json.dumps({
                    "state": "pending", "publication_state": "ready",
                    "portal_url": "https://portal.example/wisdom/review/draft-1",
                }),
                record["id"],
            ),
        )
    record = controller.load(record["id"])
    controller.bind_delivery(record["id"], native_invocation(platform=platform), "card")
    record = controller.load(record["id"])

    collapsed = controller.view(record)
    collapsed_text = collapsed.to_text()
    assert collapsed.items[0].title == "Incident Review"
    assert "Turns incident notes into a repeatable review." in collapsed_text
    assert "Private draft ready" in collapsed_text
    assert "Nothing has been published" in collapsed_text
    assert "Publisher usage" not in collapsed_text
    assert content_hash not in collapsed_text
    assert "prepared inspect" not in collapsed_text
    assert [action.label for action in collapsed.actions][:2] == [
        "Show checks", "View More Details",
    ]

    expanded = controller.act(service, record["id"], actor, "details.0")
    expanded_view = controller.view(expanded)
    assert expanded["plan"]["hashes"] == record["plan"]["hashes"]
    assert expanded_view.summary.startswith("Publication details - 1/")
    assert expanded["inspection"]["page_count"] > 1
    assert len(expanded["inspection"]["content"]) <= 1000
    assert "Exact owner description." in expanded_view.to_text()
    assert any(action.label == "Next page" for action in expanded_view.actions)
    assert any(action.label == "Hide Details" for action in expanded_view.actions)
    assert service.publication_submit.call_count == 0

    collapsed_again = controller.act(service, record["id"], actor, "back")
    assert collapsed_again["inspection"] is None
    assert collapsed_again["plan"]["hashes"] == record["plan"]["hashes"]
    assert controller.view(collapsed_again).to_text() == collapsed_text

    checks = controller.act(service, record["id"], actor, "checks.show")
    assert checks["checks_expanded"] is True
    assert any(action.label == "Hide checks" for action in controller.view(checks).actions)
    hidden = controller.act(service, record["id"], actor, "checks.hide")
    assert hidden["checks_expanded"] is False
    assert hidden["plan"]["hashes"] == record["plan"]["hashes"]
    assert controller.view(hidden).to_text() == collapsed_text
    assert service.publication_submit.call_count == 0


def test_publication_title_falls_back_to_slug_not_internal_draft_id():
    plan = publication_review(
        "local:85bdcd79-d59e-42ae-b8de-1b273e273798:deadbeef",
        editorial_name="",
        slug="incident-review",
        editorial_description="Turns incidents into a repeatable review.",
    )
    view = project_view({
        "id": "publication-id", "state": "pending", "operation": "publish",
        "checks_expanded": False, "inspection": None, "result": None, "plan": plan,
    })

    assert view.items[0].title == "incident-review"
    assert "85bdcd79" not in view.to_text()


def test_publication_blocker_stays_visible_and_removes_share_action(tmp_path):
    service = RecordingPublicationService([])

    def blocked_review(draft_id):
        return publication_review(
            draft_id,
            allowed=False,
            security_check={
                "status": "blocked",
                "summary": "A live credential must be removed before sharing.",
            },
        )

    service.publication_review = blocked_review
    controller = NativeReviewController(WisdomStore(tmp_path))
    record = controller.request_publication(service, native_invocation(), "draft")
    view = controller.view(record)

    assert "live credential must be removed" in view.to_text()
    assert any(action.label == "Show checks" for action in view.actions)
    assert not any(action.action == "confirm" for action in view.actions)


def test_publication_detail_pages_survive_rich_and_legacy_html_escape_expansion(tmp_path):
    files = [{"path": "SKILL.md", "content_utf8": "# Skill\n", "hash": "a" * 64}]
    controller, service, actor, record = _publication_review(tmp_path, files, "telegram")
    plan = record["plan"]
    plan["description"] = "&<>\"'" * 1000
    with controller.store.transaction() as db:
        db.execute(
            "UPDATE wisdom_consent SET plan_json=? WHERE id=?",
            (json.dumps(plan, sort_keys=True), record["id"]),
        )
    controller.bind_delivery(record["id"], native_invocation(platform="telegram"), "card")

    first = controller.act(service, record["id"], actor, "details.0")
    for page in range(first["inspection"]["page_count"]):
        current = controller.act(service, record["id"], actor, f"details.{page}")
        view = controller.view(current)
        assert plain_html_is_lossless(view)
        assert rich_html_is_lossless(view, record["id"])
    service.publication_submit.assert_not_called()
