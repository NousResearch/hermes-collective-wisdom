"""Shared terminal ordering for native install and update journals."""


def finish_native(executor, operation_id, payload, result):
    """Retain wire outcome before completing receipts and settling core authority."""
    failed = result.get("rejected") is True
    if executor.catalog is not None:
        executor.catalog.retain(operation_id, executor.store, failed=failed)
    executor.receipts.complete(payload["receipt"], result)
    executor.store.finish(operation_id, result, stage="complete")
    if executor.catalog is not None:
        executor.catalog.settle(operation_id, executor.store, failed=failed)
    return result
