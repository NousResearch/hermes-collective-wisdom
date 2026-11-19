"""Retained terminal refusal when D11 cannot recognize its exact local effect."""
from .install_receipts import InstallReceipts
from .uninstall_review import UninstallReceipts
from .update_receipts import UpdateReceipts

REASON = "Catalog recovery evidence changed. Local files were preserved."


def abandon(mutation, hold, operation_id, payload, store):
    """Keep the provider journal terminal without changing any managed files."""
    operation = {
        **mutation.operation(operation_id, None, state="completed", failed=True),
        "managed_state": "unmanaged_local", "message": REASON,
    }
    result = {"rejected": True, "skill_id": payload["skill_id"], "message": REASON,
              {"install": "installed", "update": "updated", "uninstall": "uninstalled"}[
                  mutation.action]: False}
    payload["result"] = result
    store.save_review("catalog_operation:" + operation_id, operation)
    store.checkpoint(operation_id, payload=payload, stage="outcome_recorded")
    hold.decide({"kind": "abandon", "reason": REASON})
    receipts = {"install": InstallReceipts, "update": UpdateReceipts,
                "uninstall": UninstallReceipts}[mutation.action](store, lambda: payload["owner"])
    receipts.complete(payload["receipt"], result)
    store.finish(operation_id, result, stage="complete")
