"""Publisher evidence over current PluginState, not the retired database."""
# pylint: disable=missing-function-docstring

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from installed_test_support import JsonState
from test_contribution_review import reviewer
from hermes_wisdom_plugin.contract import author_description_hash
from hermes_wisdom_plugin.local_state import QualificationState
from hermes_wisdom_plugin.package import source_fingerprint
from hermes_wisdom_plugin.publisher_evidence import publication_description
from hermes_wisdom_plugin.runtime import InvocationService


def record(store, skill, day, count=1, zone="UTC"):
    for _ in range(count):
        store.record_usage_day(skill, day, timezone_name=zone, retain_after="2026-01-01")


def test_only_recent_profile_calendar_usage_enters_description(tmp_path):
    store = QualificationState(JsonState(tmp_path), tmp_path)
    skill = store.register_skill(tmp_path / "one", content_hash="one", source_kind="local")
    other = store.register_skill(tmp_path / "other", content_hash="other", source_kind="local")
    zone = "Australia/Brisbane"
    record(store, skill, "2026-09-04", 2, zone)
    record(store, skill, "2026-09-10", 1, zone)
    record(store, skill, "2026-09-03", 5, zone)
    record(store, skill, "2026-09-11", 6, zone)
    record(store, skill, "2026-09-05", 7, "UTC")
    record(store, other, "2026-09-05", 8, zone)
    result = publication_description(
        store, skill, "Owner copy", at=datetime(2026, 9, 9, 15, tzinfo=timezone.utc),
        timezone_info=(ZoneInfo(zone), zone),
    )
    assert result.startswith("Owner copy\n\nPublisher usage (client-reported)")
    assert "3 invocations on 2 days" in result
    assert "2026-09-04 through 2026-09-10" in result
    assert "may span local revisions" in result
    assert "do not verify successful outcomes" in result
    assert str(tmp_path) not in result and zone not in result


def test_missing_or_managed_evidence_is_not_invented(tmp_path):
    store = QualificationState(JsonState(tmp_path), tmp_path)
    source = tmp_path / "skill"
    skill = store.register_skill(source, content_hash="one", source_kind="local")
    options = {"at": datetime(2026, 9, 10, tzinfo=timezone.utc),
               "timezone_info": (timezone.utc, "UTC")}
    assert publication_description(store, skill, "Owner copy", **options) == "Owner copy"
    store.register_skill(source, content_hash="one", source_kind="managed")
    record(store, skill, "2026-09-10")
    assert publication_description(store, skill, "Owner copy", **options) == "Owner copy"


def test_evidence_is_frozen_and_hash_bound_across_prepare_retry(tmp_path, monkeypatch):
    value, source, binding = reviewer(tmp_path)
    store = value.source_store
    assert store is not None
    store.bind_owner(binding)
    skill = store.register_skill(
        source, content_hash=source_fingerprint(source), source_kind="local",
    )
    day = datetime.now(timezone.utc).date().isoformat()
    record(store, skill, day)
    monkeypatch.setattr(
        "hermes_wisdom_plugin.publisher_evidence._host_timezone", lambda: (timezone.utc, "UTC"),
    )
    first = value.prepare("reviewed-skill")
    description = first["drafted_description"]
    assert "1 invocation on 1 day" in description
    assert first["publisher_evidence"] == description.split("\n\n", 1)[1]
    assert first["hashes"]["author_description"] == author_description_hash(description)
    record(store, skill, day)
    retried = value.prepare("reviewed-skill")
    assert retried["drafted_description"] == description
    assert retried["hashes"] == first["hashes"]


def test_q11_notifications_and_inbox_read_current_candidate_without_external_effects(
    tmp_path, monkeypatch,
):
    host = JsonState(tmp_path)
    store = QualificationState(host, tmp_path)
    binding = {
        "profile_name": "fixture", "profile_home": str(tmp_path),
        "account_id": "user-1", "organization_id": "org-1",
        "base_url": "https://gateway.invalid", "installation_id": "install-1",
    }
    store.bind_owner(binding)
    source = tmp_path / "skills" / "one"
    source.mkdir(parents=True)
    skill = store.register_skill(source, content_hash="sha256:current", source_kind="local")
    event = store.emit_candidate(
        skill_id=skill, content_hash="sha256:current", qualification="high_usage",
        payload={"skill_name": "one", "local_reasons": {"consecutive_business_days": 7}},
    )
    service = InvocationService(tmp_path, ("fixture", tmp_path), state=host)
    service._qualification = store  # pylint: disable=protected-access
    service._installation_owner = lambda: binding  # pylint: disable=protected-access
    service.check_owner = lambda: None
    service.notification_mode = lambda: "agent"
    service.installed.installations = lambda _owner: []
    def forbidden(*_args, **_kwargs):
        raise AssertionError("external effect")
    service.client = SimpleNamespace(
        feed=forbidden, upload=forbidden, publish=forbidden, complete_structured=forbidden,
    )
    monkeypatch.setattr(
        "hermes_wisdom_plugin.native_review.NativeReviewController.pending",
        lambda _self, _owner, invocation=None, limit=100: [],
    )

    notifications = service.notifications(mark_seen=True)
    inbox = service.inbox()
    assert notifications["more"] is False
    assert [row["event_id"] for row in notifications["events"]] == [event]
    assert [row["event_id"] for row in inbox["assessments"]] == [event]
    assert store.local_event(event)["state"] == "unread"
    assert service.client.feed is forbidden
