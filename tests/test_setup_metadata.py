"""Setup metadata shares the existing transaction and does not change schema."""

import pytest

from hermes_wisdom_plugin.store import WisdomStore


def test_metadata_survives_reopen_and_rolls_back_with_transaction(tmp_path):
    """A failed activation must not leave a successful setup record behind."""
    store = WisdomStore(tmp_path)
    assert store.get_metadata("terminal_setup") is None
    store.set_metadata("terminal_setup", {"state": "registering"})
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            store.set_metadata("terminal_setup", {"state": "active"}, _db=db)
            raise RuntimeError("interrupted")
    assert WisdomStore(tmp_path).get_metadata("terminal_setup") == {"state": "registering"}
    with pytest.raises(ValueError):
        store.set_metadata("schema_version", {})
