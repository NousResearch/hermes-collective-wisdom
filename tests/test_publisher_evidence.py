"""Reference-derived publisher evidence tests."""
# pylint: disable=line-too-long,missing-function-docstring

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from test_contribution_review import reviewer

from hermes_wisdom_plugin.contract import author_description_hash
from hermes_wisdom_plugin.publisher_evidence import publication_description
from hermes_wisdom_plugin.store import WisdomStore


def test_only_own_recent_profile_usage_enters_reviewable_description(tmp_path):
    store = WisdomStore(tmp_path / "state")
    source = tmp_path / "skills" / "one"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("one", encoding="utf-8")
    skill = store.register_skill(source, content_hash="one", source_kind="local")
    other_path = tmp_path / "skills" / "other"
    other_path.mkdir()
    (other_path / "SKILL.md").write_text("other", encoding="utf-8")
    other = store.register_skill(other_path, content_hash="other", source_kind="local")
    at = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO usage_day VALUES(?,?,?,?)",
            [
                (skill, "2026-09-04", "Australia/Brisbane", 2),
                (skill, "2026-09-10", "Australia/Brisbane", 1),
                (skill, "2026-09-03", "Australia/Brisbane", 50),
                (skill, "2026-09-11", "Australia/Brisbane", 60),
                (skill, "2026-09-05", "UTC", 70),
                (other, "2026-09-05", "Australia/Brisbane", 80),
            ],
        )
    result = publication_description(
        store, skill, "Owner copy", at=at,
        timezone_info=(ZoneInfo("Australia/Brisbane"), "Australia/Brisbane"),
    )
    assert result.startswith("Owner copy\n\n")
    assert "Publisher usage (client-reported)" in result
    assert "3 invocations on 2 days" in result
    assert "2026-09-04 through 2026-09-10" in result
    assert "may span local revisions" in result
    assert "do not verify successful outcomes" in result
    assert str(source) not in result and "Australia/Brisbane" not in result


def test_missing_or_managed_evidence_does_not_invent_zero_use(tmp_path):
    store = WisdomStore(tmp_path / "state")
    source = tmp_path / "skills" / "unused"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("unused", encoding="utf-8")
    skill = store.register_skill(source, content_hash="unused", source_kind="local")
    assert publication_description(store, skill, "Owner copy", timezone_info=(timezone.utc, "UTC")) == "Owner copy"
    with store.transaction() as db:
        db.execute("UPDATE local_skill SET source_kind='managed' WHERE id=?", (skill,))
        db.execute("INSERT INTO usage_day VALUES(?,?,?,?)", (skill, datetime.now(timezone.utc).date().isoformat(), "UTC", 4))
    assert publication_description(store, skill, "Owner copy", timezone_info=(timezone.utc, "UTC")) == "Owner copy"


def test_publisher_evidence_is_frozen_reviewed_and_hash_bound_before_retry(tmp_path, monkeypatch):
    value, source, _binding = reviewer(tmp_path)
    day = datetime.now(timezone.utc).date().isoformat()
    source_hash = __import__("hermes_wisdom_plugin.package", fromlist=["source_fingerprint"]).source_fingerprint(source)
    skill = value.store.register_skill(source, content_hash=source_hash, source_kind="local")
    value.store.record_usage_day(
        skill, day, timezone_name="UTC",
        retain_after=(datetime.now(timezone.utc).date() - timedelta(days=35)).isoformat(),
    )
    monkeypatch.setattr(
        "hermes_wisdom_plugin.publisher_evidence._host_timezone",
        lambda: (timezone.utc, "UTC"),
    )
    first = value.prepare("reviewed-skill")
    description = first["drafted_description"]
    assert "Publisher usage (client-reported)" in description
    assert "1 invocation on 1 day" in description
    assert first["publisher_evidence"] == description.split("\n\n", 1)[1]
    assert first["hashes"]["author_description"] == author_description_hash(description)
    value.store.record_usage_day(
        skill, day, timezone_name="UTC",
        retain_after=(datetime.now(timezone.utc).date() - timedelta(days=35)).isoformat(),
    )
    retried = value.prepare("reviewed-skill")
    assert retried["drafted_description"] == description
    assert retried["hashes"] == first["hashes"]
