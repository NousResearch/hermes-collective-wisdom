"""Owner-bound terminal install receipts, separate from messaging consent.

Retains the reference service's wip_ plan files and operation journal boundary
(Agent 0924f7946b), adding containment, integrity, ownership and review expiry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager

from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError

REVIEW_SECONDS = 24 * 60 * 60
_MAX_PLAN_BYTES = 512 * 1024
_RECEIPT = re.compile(r"wip_[0-9a-f]{32}", re.ASCII)


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()


class InstallReceipts:
    """Immutable review files whose digest and progress live in profile SQLite."""

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

    @contextmanager
    def _directory(self):
        # Open relative to directory descriptors so replacing a parent symlink
        # cannot redirect a subsequent receipt read/write outside plugin state.
        if os.open not in os.supports_dir_fd:
            raise WisdomValidationError("Secure install receipts are unavailable on this platform")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = directory = None
        try:
            root = os.open(self.store.root, flags)
            try:
                os.mkdir("plans", mode=0o700, dir_fd=root)
                os.fsync(root)
            except FileExistsError:
                pass
            directory = os.open("plans", flags, dir_fd=root)
            yield directory
        except OSError as exc:
            raise WisdomValidationError("Install receipt storage is unavailable or unsafe") from exc
        finally:
            if directory is not None:
                os.close(directory)
            if root is not None:
                os.close(root)

    def create(self, plan):
        """Persist a fresh review; never renew an existing receipt's authority."""
        owner = self.owner()
        now = self.clock()
        receipt = "wip_" + uuid.uuid4().hex
        value = {**plan, "receipt": receipt, "operation": self.OPERATION, "owner": owner,
                 "created_at": now, "expires_at": now + REVIEW_SECONDS}
        raw = _bytes(value)
        if len(raw) > _MAX_PLAN_BYTES:
            raise WisdomValidationError("Install review exceeds the size limit")
        with self._directory() as directory:
            descriptor = os.open(receipt + ".json", os.O_WRONLY | os.O_CREAT |
                                 os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        if self.owner() != owner:
            raise WisdomAuthError("Installation owner changed during review")
        self.store.set_metadata(self._key(receipt), {
            "digest": hashlib.sha256(raw).hexdigest(), "owner": owner,
            "operation_id": None, "result": None,
        })
        return value

    def _read(self, receipt):
        key = self._key(receipt)
        owner = self.owner()
        record = self.store.get_metadata(key)
        if not isinstance(record, dict) or record.get("owner") != owner:
            raise WisdomAuthError("Install receipt does not belong to this enrolment")
        with self._directory() as directory:
            descriptor = os.open(receipt + ".json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WisdomValidationError("Unsafe install receipt file")
                raw = stream.read(_MAX_PLAN_BYTES + 1)
        if len(raw) > _MAX_PLAN_BYTES or hashlib.sha256(raw).hexdigest() != record.get("digest"):
            raise WisdomValidationError("Install receipt changed; create a new review")
        try:
            plan = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise WisdomValidationError("Invalid install receipt") from exc
        if (not isinstance(plan, dict) or plan.get("owner") != owner
                or plan.get("receipt") != receipt or plan.get("operation") != self.OPERATION):
            raise WisdomValidationError(f"Invalid {self.OPERATION} receipt binding")
        if self.owner() != owner:
            raise WisdomAuthError("Installation owner changed")
        return plan, record

    def _operation(self, receipt, record, db=None):
        if db is None:
            operation = self.store.operation(record.get("operation_id") or "")
        else:
            row = db.execute("SELECT * FROM operation_journal WHERE id=?",
                             (record.get("operation_id") or "",)).fetchone()
            operation = {**dict(row), "payload": json.loads(row["payload_json"])} if row else None
        if (operation is None or operation["kind"] != self.JOURNAL_KIND
                or operation["payload"].get("owner") != record["owner"]
                or operation["payload"].get("receipt") != receipt):
            raise WisdomConflict(
                f"{self.LABEL} recovery does not match the reviewed operation"
            )
        return operation

    def load(self, receipt, *, allow_started=False):
        """Revalidate a review; only an accepted operation can outlive its TTL."""
        plan, record = self._read(receipt)
        if allow_started and record.get("operation_id"):
            self._operation(receipt, record)
        elif self.clock() >= plan["expires_at"]:
            raise WisdomConflict("Install review expired; rerun install --plan to recheck")
        return plan

    def start(self, receipt, operation_id):
        """Attach one durable authorized operation without extending the review."""
        self.load(receipt)
        with self.store.transaction() as db:
            record = json.loads(db.execute("SELECT value FROM schema_meta WHERE key=?",
                                           (self._key(receipt),)).fetchone()[0])
            if record.get("operation_id") not in (None, operation_id):
                raise WisdomConflict("Install receipt is already in use")
            record["operation_id"] = operation_id
            self._operation(receipt, record, db)
            self.store.set_metadata(self._key(receipt), record, _db=db)

    def outcome(self, receipt):
        """Return only a completed, still-owner-bound operation outcome."""
        self.load(receipt, allow_started=True)
        _, record = self._read(receipt)
        return record.get("result")

    def complete(self, receipt, result):
        """Retain an idempotent outcome rather than silently reusing approval."""
        self.load(receipt, allow_started=True)
        with self.store.transaction() as db:
            record = json.loads(db.execute("SELECT value FROM schema_meta WHERE key=?",
                                           (self._key(receipt),)).fetchone()[0])
            self._operation(receipt, record, db)
            if record.get("result") is not None and record["result"] != result:
                raise WisdomConflict("Install receipt already has a different outcome")
            record["result"] = result
            self.store.set_metadata(self._key(receipt), record, _db=db)
