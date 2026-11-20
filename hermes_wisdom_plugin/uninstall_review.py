"""Durable, owner-bound uninstall reviews over the existing install journal."""

from __future__ import annotations

import copy
import os

from .errors import WisdomConflict
from .install_receipts import InstallReceipts
from .managed_target import validate_managed_target
from .package import PackagePolicyError
from .package_application import tree_hashes


class UninstallReceipts(InstallReceipts):
    """Uninstall authority shares persistence, never the install receipt namespace."""

    OPERATION = "uninstall"
    JOURNAL_KIND = "uninstall"
    KEY_PREFIX = "uninstall_receipt:"
    LABEL = "Uninstall"
    REVIEW_SECONDS = 15 * 60

    def verified_outcome(self, receipt):
        """Read terminal journal authority even if the receipt's final save was lost."""
        _plan, binding = self._read(receipt)
        if binding.get("result") is not None:
            return copy.deepcopy(binding["result"])
        if binding.get("operation_id"):
            operation = self._operation(receipt, binding)
            if operation.get("status") == "done":
                result = operation.get("result")
                if not isinstance(result, dict):
                    raise PackagePolicyError("uninstall outcome is unavailable")
                return copy.deepcopy(result)
        return None


class UninstallReviewer:  # pylint: disable=too-few-public-methods
    """Snapshot the current installation and actual bytes without removing anything."""

    def __init__(self, store, receipts, host, owner):
        self.store = store
        self.receipts = receipts
        self.host = host
        self.owner = owner

    def plan(self, skill_id):
        """Persist exact removal consent; a missing tree is a valid reviewed state."""
        owner = self.owner()
        record = self.store.installation(skill_id, owner=owner)
        if not record or record.get("state") != "active":
            raise PackagePolicyError("managed installation not found")
        target = validate_managed_target(self.host, record, owner, require_tree=False)
        snapshot = tree_hashes(target)
        if snapshot is None and os.path.lexists(target):
            raise PackagePolicyError("managed uninstall tree is unsafe")
        if self.owner() != owner or self.store.installation(skill_id, owner=owner) != record:
            raise WisdomConflict("managed installation changed during review")
        return self.receipts.create({
            "skill_id": skill_id, "slug": record["slug"], "version": record["version"],
            "content_hash": record["content_hash"], "target_path": str(target),
            "installation": copy.deepcopy(record), "local_snapshot": snapshot,
            "files": [{"path": path, "size": (target / path).stat().st_size}
                      for path in snapshot or {}],
        })

    def verify(self, plan):
        """Recheck before journaling; callers repeat this inside the mutation permit."""
        owner = self.owner()
        record = self.store.installation(plan["skill_id"], owner=owner)
        if plan.get("owner") != owner or record != plan.get("installation"):
            raise PackagePolicyError("managed installation changed after review")
        target = validate_managed_target(self.host, record, owner, require_tree=False)
        if str(target) != plan.get("target_path") or tree_hashes(target) != plan["local_snapshot"]:
            raise PackagePolicyError("managed bytes changed after review")
        if self.owner() != owner:
            raise WisdomConflict("managed installation owner changed")
