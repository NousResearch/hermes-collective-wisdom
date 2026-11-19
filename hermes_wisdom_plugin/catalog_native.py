"""Bridge existing native review choices into core's one reservation service."""
from __future__ import annotations

from .catalog_mutations import CatalogMutation
from .catalog_reviews import project_review
from .package import PackagePolicyError
from .skill_catalog import WisdomCatalog, digest


class NativeCatalog:
    """Optional per-executor native admission collaborator; errors never mean no owner."""

    def __init__(self, api, plugin_key, host):
        self.api = api
        self.plugin_key = plugin_key
        self.host = host

    def admit(self, executor, plan, action, choices, *, payload=None, operation_id=None):
        # pylint: disable=too-many-arguments
        """Reserve exact native authority, retaining its replay input in the journal."""
        source = WisdomCatalog(None, self.plugin_key, self.host).source
        retained = (payload or {}).get("catalog_native")
        if retained is None:
            review = project_review(plan, action, source, self.host,
                                    review_id=digest([action, plan["receipt"]]),
                                    token=plan["receipt"], now=plan["created_at"])
            approved = {key: value for key, value in choices.items() if key in {
                "accept_partial", "preserve_modified", "accept_sensitive", "accept_warnings",
            } and isinstance(value, bool)}
            policy = plan.get("policy") or {}
            warnings = policy.get("warning_acceptance") or {}
            accepted = {
                "security": warnings.get("accepted") is True,
                "compatibility": choices.get("accept_partial") is True
                    or policy.get("accepted_partial") is True,
                "sensitive_expansion": policy.get("accepted_sensitive") is True,
            }
            approved["acknowledgements"] = [
                row["id"] for row in review["scan"]["findings"]
                if row["acknowledgement_required"] and accepted.get(row["category"],
                                                                    accepted["security"])
            ]
            retained = {"review": {**review, "native_receipt_id": plan["receipt"]},
                        "choices": approved}
        result = self.api.admit_native(retained["review"], retained["choices"])
        if result is None:
            if (payload or {}).get("catalog_native") is not None:
                raise PackagePolicyError("catalog recovery admission is unavailable")
            return None
        if not isinstance(result, dict) or set(result) != {"admission", "operation_id"}:
            raise PackagePolicyError("catalog admission is unavailable")
        if operation_id is not None and result["operation_id"] != operation_id:
            raise PackagePolicyError("catalog recovery operation changed")
        executor.catalog = CatalogMutation(self.api, result["admission"], action,
                                           source + ":" + plan["skill_id"], plan, self.host)
        executor.catalog.native = retained
        if payload is not None:
            payload["catalog_native"] = retained
        return result["operation_id"]

    def resume(self, executor, operation, plan, action, choices):
        """Re-admit the same durable receipt before any pending execution."""
        if executor.catalog is not None:
            return
        payload = dict(operation["payload"])
        self.admit(executor, plan, action, choices, payload=payload,
                   operation_id=operation["id"])
        executor.store.checkpoint(operation["id"], payload=payload)
