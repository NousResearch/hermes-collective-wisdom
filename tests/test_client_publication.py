"""Pinned Gateway publication wire tests."""
# pylint: disable=cyclic-import,duplicate-code,missing-function-docstring

from test_client import Response, Session

from hermes_wisdom_plugin.client import WisdomClient, WisdomGatewayCredentials
from hermes_wisdom_plugin.wire import ObjectSet


def credentials(scope_calls, scope):
    def provider():
        scope_calls.append(scope)
        return WisdomGatewayCredentials("https://gateway.example", "token", "org-1")
    return provider


def policy_body(mode="open"):
    return {
        "org_id": "org-1",
        "usage_evidence_window_days": 30,
        "min_aggregate_invocations": 5,
        "consecutive_day_usage_counts": True,
        "repeated_edits_count": True,
        "max_recommendations_per_user_per_week": 1,
        "publication_mode": mode,
        "install_popularity_threshold": 2,
        "notification_defaults": {
            "skill_ready_to_share": True,
            "teammate_published": True,
            "update_available": True,
        },
        "manager_review_email_cadence": "immediate",
        "not_now_suppression_days": 7,
        "version": 3,
        "updated_by_user_id": "manager",
    }


def draft_body(state="ready"):
    return {
        "id": "draft-1",
        "supersedesDraftId": None,
        "orgId": "org-1",
        "ownerUserId": "user-1",
        "slug": "reviewed-skill",
        "draftCommit": "sha256:" + "1" * 64,
        "contentHash": "sha256:" + "2" * 64,
        "authorDescription": "Description.",
        "authorDescriptionHash": "sha256:" + "3" * 64,
        "state": state,
        "packageManifestHash": "sha256:" + "4" * 64,
        "packageManifestSchemaVersion": 1,
        "systemSpec": None,
        "scan": {"verdict": "pass"},
        "scanVerdict": "pass",
        "security_check": {"status": "pass"},
        "professionalism_check": None,
        "explanation": "Does a thing.",
        "updatedAt": "revision-1",
    }


def test_publication_calls_use_pinned_paths_shapes_and_publish_scope():
    calls = []
    session = Session(Response(200, policy_body()))
    value = WisdomClient(
        credentials(calls, "read"),
        "org-1",
        session=session,
        publish_credential_provider=credentials(calls, "publish"),
    )

    assert value.agent_led_policy().publication_mode == "open"
    session.response = Response(201, {"draft": draft_body()})
    draft = value.submit_draft(
        slug="reviewed-skill",
        commit="sha256:" + "1" * 64,
        content_hash="sha256:" + "2" * 64,
        description="Description.",
        description_hash="sha256:" + "3" * 64,
        manifest_hash="sha256:" + "4" * 64,
    )
    assert draft.id == "draft-1"
    session.response = Response(200, {"drafts": [draft_body()]})
    assert value.list_drafts()[0].id == "draft-1"
    session.response = Response(200, {"draft": draft_body(), "effective_policy": {}})
    assert value.draft("draft-1").draft.orgId == "org-1"
    session.response = Response(200, {"draft": draft_body("owner_approved")})
    value.approve(
        "draft-1",
        content_hash="sha256:" + "2" * 64,
        description_hash="sha256:" + "3" * 64,
        manifest_hash="sha256:" + "4" * 64,
    )
    session.response = Response(200, {"state": "published", "proposal_id": 1})
    value.publish("draft-1", content_hash="sha256:" + "2" * 64)
    session.response = Response(200, {"state": "declined"})
    value.decline("draft-1")

    assert calls == ["read", "publish", "publish", "publish", "publish", "publish", "publish"]
    assert [call[1] for call in session.calls] == [
        "https://gateway.example/v1/sync/wisdom/agent-led/policy",
        "https://gateway.example/v1/sync/wisdom/drafts",
        "https://gateway.example/v1/sync/wisdom/drafts",
        "https://gateway.example/v1/sync/wisdom/drafts/draft-1",
        "https://gateway.example/v1/sync/wisdom/drafts/draft-1/approve",
        "https://gateway.example/v1/sync/wisdom/drafts/draft-1/publish",
        "https://gateway.example/v1/sync/wisdom/drafts/draft-1/decline",
    ]
    assert "publication_mode" not in session.calls[-2][2]["json"]


def test_private_object_upload_uses_owner_scoped_raw_multipart_and_is_idempotent_shape():
    calls = []
    session = Session(Response(201, {"objects": []}))
    value = WisdomClient(
        credentials(calls, "read"),
        "org-1",
        session=session,
        publish_credential_provider=credentials(calls, "publish"),
    )
    objects = ObjectSet()
    digest = objects.add("blob", b"exact bytes")

    value.upload_private_objects(objects.objects)

    method, url, options = session.calls[0]
    assert (method, url) == ("POST", "https://gateway.example/v1/sync/objects")
    assert options["files"] == [
        (digest, ("blob", b"exact bytes", "application/octet-stream"))
    ]
    assert options["params"] is None
    assert calls == ["publish"]
