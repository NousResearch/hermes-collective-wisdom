"""Owner-bound install and update reviews stored in PluginState."""

# Public methods are the deliberately small receipt service API.
# pylint: disable=missing-function-docstring

from __future__ import annotations

import copy
import json
import re
import time
import uuid

from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from .package import MAX_METADATA_BYTES

REVIEW_SECONDS = 24 * 60 * 60
_MAX_PLAN_BYTES = 6 * MAX_METADATA_BYTES + 2 * 1024 * 1024
_RECEIPT = re.compile(r"wip_[0-9a-f]{32}", re.ASCII)


class InstallReceipts:
    """Exact review authority held in the installed-state aggregate."""

    OPERATION = "install"
    JOURNAL_KIND = "install"
    KEY_PREFIX = "install_receipt:"
    LABEL = "Install"

    def __init__(self, store, owner, *, clock=time.time):
        self.store = store
        self.owner = owner
        self.clock = clock

    @classmethod
    def _key(cls, receipt):
        if not isinstance(receipt, str) or _RECEIPT.fullmatch(receipt) is None:
            raise WisdomValidationError(f"Invalid {cls.OPERATION} receipt identifier")
        return cls.KEY_PREFIX + receipt

    def create(self, plan):
        owner = self.owner()
        now = self.clock()
        receipt = "wip_" + uuid.uuid4().hex
        value = {
            **copy.deepcopy(plan), "receipt": receipt, "operation": self.OPERATION,
            "owner": copy.deepcopy(owner), "created_at": now,
            "expires_at": now + REVIEW_SECONDS,
        }
        try:
            size = len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())
        except (TypeError, ValueError) as exc:
            raise WisdomValidationError("Install review is not serializable") from exc
        if size > _MAX_PLAN_BYTES:
            raise WisdomValidationError("Install review exceeds the size limit")
        if self.owner() != owner:
            raise WisdomAuthError("Installation owner changed during review")
        self.store.save_review(self._key(receipt), {
            "plan": value, "owner": copy.deepcopy(owner),
            "operation_id": None, "result": None,
        })
        return copy.deepcopy(value)

    def _read(self, receipt):
        owner = self.owner()
        record = self.store.review(self._key(receipt))
        if not isinstance(record, dict) or record.get("owner") != owner:
            raise WisdomAuthError("Install receipt does not belong to this enrolment")
        plan = record.get("plan")
        if (
            not isinstance(plan, dict) or plan.get("owner") != owner
            or plan.get("receipt") != receipt or plan.get("operation") != self.OPERATION
        ):
            raise WisdomValidationError(f"Invalid {self.OPERATION} receipt binding")
        if self.owner() != owner:
            raise WisdomAuthError("Installation owner changed")
        return copy.deepcopy(plan), record

    def _operation(self, receipt, record):
        operation = self.store.operation(record.get("operation_id") or "")
        payload = operation.get("payload") if isinstance(operation, dict) else None
        if (
            not isinstance(operation, dict) or operation.get("kind") != self.JOURNAL_KIND
            or not isinstance(payload, dict) or payload.get("owner") != record["owner"]
            or payload.get("receipt") != receipt
        ):
            raise WisdomConflict(f"{self.LABEL} recovery does not match the reviewed operation")
        return operation

    def load(self, receipt, *, allow_started=False):
        plan, record = self._read(receipt)
        if allow_started and record.get("operation_id"):
            self._operation(receipt, record)
        elif self.clock() >= plan["expires_at"]:
            raise WisdomConflict("Install review expired; rerun install --plan to recheck")
        return plan

    def start(self, receipt, operation_id):
        _plan, record = self._read(receipt)
        if record.get("operation_id") not in (None, operation_id):
            raise WisdomConflict(f"{self.LABEL} receipt is already in use")
        self.store.bind_review(self._key(receipt), operation_id, kind=self.JOURNAL_KIND)

    def outcome(self, receipt):
        self.load(receipt, allow_started=True)
        return copy.deepcopy(self.store.review(self._key(receipt)).get("result"))

    def complete(self, receipt, result):
        self.load(receipt, allow_started=True)
        self.store.complete_review(self._key(receipt), result, kind=self.JOURNAL_KIND)
