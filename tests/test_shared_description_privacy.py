"""Local usage never leaves the profile through the shared description."""
# pylint: disable=missing-function-docstring

import importlib
import importlib.util
import sys
from datetime import datetime, timezone

from test_contribution_review import reviewer
from hermes_wisdom_plugin.contract import author_description_hash
from hermes_wisdom_plugin.package import source_fingerprint


def test_recorded_usage_never_enters_the_shared_description(tmp_path, monkeypatch):
    # Before the fix, preparation read the host timezone to window usage days; pin it
    # to UTC so the seeded day is inside that window on the pre-fix code as well.
    legacy = sys.modules.get("hermes_wisdom_plugin.publisher_evidence")
    if legacy is None and importlib.util.find_spec("hermes_wisdom_plugin.publisher_evidence"):
        legacy = importlib.import_module("hermes_wisdom_plugin.publisher_evidence")
    if legacy is not None:
        monkeypatch.setattr(legacy, "_host_timezone", lambda: (timezone.utc, "UTC"))
    value, source, binding = reviewer(tmp_path)
    store = value.source_store
    assert store is not None
    store.bind_owner(binding)
    skill = store.register_skill(
        source, content_hash=source_fingerprint(source), source_kind="local",
    )
    day = datetime.now(timezone.utc).date().isoformat()
    for _ in range(3):
        store.record_usage_day(skill, day, timezone_name="UTC", retain_after="2026-01-01")

    prepared = value.prepare("reviewed-skill")

    description = prepared["drafted_description"]
    assert "Publisher usage" not in description
    assert "invocation" not in description.casefold()
    assert "\n\n" not in description
    assert prepared["hashes"]["author_description"] == author_description_hash(description)
    assert "publisher_evidence" not in prepared
