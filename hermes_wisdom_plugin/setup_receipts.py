"""Owner-bound setup receipts using the hardened install receipt storage."""

import json

from .errors import WisdomAuthError, WisdomConflict

from .install_receipts import InstallReceipts
from .store import utc_now

_IDENTITY_FIELDS = (
    "owner", "version", "content_hash", "target_path", "setup_key", "step"
)


def _completed_result(row: dict, record: dict) -> dict | None:
    if row["state"] != "done":
        return None
    payload = json.loads(row["payload_json"])
    result = payload.get("result")
    if not isinstance(result, dict):
        raise WisdomConflict("Setup completion journal has no durable result")
    if record.get("result") != result:
        raise WisdomConflict("Setup completion records disagree")
    return result


class SetupReceipts(InstallReceipts):
    """Receipts that can authorize only one setup/prerequisite/verify step."""

    OPERATION = "installed_setup"
    JOURNAL_KIND = "wisdom_setup"
    KEY_PREFIX = "setup_receipt:"
    LABEL = "Setup"

    def finalize(
        self,
        receipt: str,
        expected_row: dict,
        payload: dict,
        result: dict,
        phase: str,
    ) -> dict:
        """Atomically bind one expected journal state to its durable outcome."""

        plan, checked_record = self._read(receipt)
        if plan.get("skill_id") != expected_row.get("entity_id") or any(
            plan.get(field) != payload.get(field) for field in _IDENTITY_FIELDS
        ):
            raise WisdomConflict("Setup completion no longer matches its reviewed content")
        expected_payload = expected_row.get("payload_json")
        if not isinstance(expected_payload, str):
            raise WisdomConflict("Setup completion journal is invalid")
        with self.store.transaction() as db:
            metadata_row = db.execute(
                "SELECT value FROM schema_meta WHERE key=?", (self._key(receipt),)
            ).fetchone()
            if metadata_row is None:
                raise WisdomConflict("Setup completion receipt disappeared")
            record = json.loads(metadata_row["value"])
            if record.get("owner") != checked_record.get("owner") or record.get(
                "operation_id"
            ) != expected_row["id"]:
                raise WisdomAuthError("Setup completion owner or operation changed")
            current_row = db.execute(
                "SELECT * FROM operation_journal WHERE id=?", (expected_row["id"],)
            ).fetchone()
            if current_row is None:
                raise WisdomConflict("Setup completion journal disappeared")
            current_result = _completed_result(current_row, record)
            if current_result is not None:
                return current_result
            if record.get("result") is not None:
                raise WisdomConflict("Setup receipt completed without its journal")
            cursor = db.execute(
                "UPDATE operation_journal SET phase=?,state='done',payload_json=?,updated_at=? "
                "WHERE id=? AND kind=? AND entity_id=? AND phase=? AND state=? "
                "AND payload_json=?",
                (
                    phase,
                    json.dumps({**payload, "result": result}, sort_keys=True),
                    utc_now(),
                    expected_row["id"],
                    self.JOURNAL_KIND,
                    expected_row["entity_id"],
                    expected_row["phase"],
                    expected_row["state"],
                    expected_payload,
                ),
            )
            if cursor.rowcount != 1:
                raise WisdomConflict("Setup operation changed before completion")
            record["result"] = result
            self.store.set_metadata(self._key(receipt), record, _db=db)
        return result
