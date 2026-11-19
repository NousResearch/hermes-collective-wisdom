"""Cooperative core mutation permit for Wisdom's local-then-Gateway journal."""

from __future__ import annotations

# pylint: disable=too-many-arguments,too-many-positional-arguments
# The six constructor fields mirror one core admission and one native plan.
from contextlib import contextmanager
from pathlib import Path

from .package import PackagePolicyError
from .package_application import tree_hashes
from .skill_catalog import digest


class CatalogMutation:
    """One reservation, one filesystem phase, then read/network-only reconciliation."""

    def __init__(self, api, admission, action, identifier, plan, host):
        self.api = api
        self.admission = admission
        self.action = action
        self.identifier = identifier
        self.plan = plan
        self.host = host

    def operation(self, operation_id, record, *, state="pending", failed=False):
        """Project local state; pending uses a revision with no terminal outcome."""
        revision = digest(record) if record else None
        managed = "removed" if self.action == "uninstall" else "installed"
        outcome = None if state == "pending" else (
            "refused" if failed else {"install": "installed", "update": "updated",
                                     "uninstall": "uninstalled"}[self.action])
        return {"operation_id": operation_id, "identifier": self.identifier,
                "action": self.action, "state": "failed" if failed else state,
                "outcome": outcome, "managed_state": managed,
                "message": "Gateway reconciliation pending." if state == "pending" else (
                    "Gateway refused; local files remain as committed." if failed else ""),
                "installation_revision": revision}

    def refuse_local_edits(self, plan):
        """An update cannot preserve a second unmanaged target under one reservation."""
        if self.action != "update":
            return False
        previous = plan.get("previous_installation") or {}
        return (tree_hashes(Path(plan["target_path"])) != previous.get("baseline")
                or plan.get("preservation", {}).get("action", "none") != "none")

    def refuse(self, operation_id, payload, store, receipts):
        """Retain a zero-write refusal and unchanged ownership before settlement."""
        self._bind(operation_id)
        record = store.installation(payload["skill_id"])
        operation = self.operation(operation_id, record, state="completed", failed=True)
        relative = Path(payload["target_path"]).relative_to(self.host.skills_root).as_posix()
        operation["message"] = (f"Local edits present at {relative}. "
                                "Archive or rename it in Skills, then update.")
        store.save_review("catalog_operation:" + operation_id, operation)
        result = {"updated": False, "rejected": True, "skill_id": payload["skill_id"],
                  "message": operation["message"]}
        receipts.start(payload["receipt"], operation_id)
        receipts.complete(payload["receipt"], result)
        store.finish(operation_id, result, stage="complete")
        self.settle(operation_id, store, failed=True)
        return result

    def _bind(self, operation_id):
        self.api.bind({key: self.admission[key] for key in (
            "registration_id", "review_id", "reservation_id", "expected_path_revision",
            "expected_installation_revision",
        )} | {"operation_id": operation_id})

    @contextmanager
    def commit(self, operation_id, payload, store):
        """Core derives and locks the target; repeat provider byte checks inside."""
        self._bind(operation_id)
        with self.api.commit(self.admission, operation_id) as commit:
            relative = (commit.get("target_relative_path") if isinstance(commit, dict)
                        else commit.target_relative_path)
            target = Path(payload["target_path"])
            if target.relative_to(self.host.skills_root).as_posix() != relative:
                raise PackagePolicyError("catalog target changed after review")
            actual = tree_hashes(target)
            expected = None if self.action == "install" else self.plan.get("local_snapshot")
            if actual != expected or (self.action == "install" and target.is_symlink()):
                raise PackagePolicyError("managed bytes changed after review")
            yield
            payload["catalog_local_committed"] = True
            store.checkpoint(operation_id, payload=payload)
            record = store.installation(payload["skill_id"])
            commit.settle(self.operation(operation_id, record))

    def reject(self, operation_id, payload, store, receipts):
        """Final Gateway refusal never performs a compensating filesystem mutation."""
        result = {"rejected": True,
                  {"install": "installed", "update": "updated", "uninstall": "uninstalled"}[
                      self.action]: False,
                  "skill_id": payload["skill_id"], "version": payload["version"],
                  "path": payload["target_path"], "setup_execution_authorized": False}
        payload["result"] = result
        store.checkpoint(operation_id, payload=payload, stage="outcome_recorded")
        self.retain(operation_id, store, failed=True)
        receipts.complete(payload["receipt"], result)
        store.finish(operation_id, result, stage="complete")
        self.settle(operation_id, store, failed=True)
        return result

    def retain(self, operation_id, store, *, failed=False):
        """Save immutable wire outcome before the native journal becomes terminal."""
        key = "catalog_operation:" + operation_id
        saved = store.review(key)
        if saved is None:
            record = store.installation(self.plan["skill_id"])
            saved = self.operation(operation_id, record, state="completed", failed=failed)
            store.save_review(key, saved)
        return saved

    def settle(self, operation_id, store, *, failed=False):
        """Phase two has no writer permit and never performs a filesystem effect."""
        result = self.retain(operation_id, store, failed=failed)
        self.api.settle({"reservation_id": self.admission["reservation_id"],
                         "operation_id": operation_id,
                         "expected_path_revision": self.admission["expected_path_revision"],
                         "result": result})
        return result
