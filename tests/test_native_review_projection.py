"""PR5 presentation must not weaken evidence or exact native consent."""
# pylint: disable=missing-function-docstring,protected-access
from copy import deepcopy

import pytest
from native_test_support import RecordingPublicationService, native_invocation

from hermes_wisdom_plugin.professionalism import CHECK_KEYS
from hermes_wisdom_plugin.native_review import (
    NativeReviewController, project_view,
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
