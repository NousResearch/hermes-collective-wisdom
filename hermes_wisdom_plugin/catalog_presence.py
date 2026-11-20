"""An inert invalid receipt detects an owner without creating a reservation."""
from __future__ import annotations


def catalog_live(api, plugin_key):
    """None means stock; live-owner refusals stay fail-closed, never a raw writer.

    admit_native resolves the live owner before validating receipt fields. This
    incomplete, non-action receipt cannot reserve or authorize any operation.
    """
    if api is None:
        return False
    try:
        receipt = {"identifier": f"plugin.{plugin_key}.team:presence"}
        return api.admit_native(receipt, {}) is not None
    except Exception:  # pylint: disable=broad-exception-caught
        # Errors are not evidence that raw effects are safe. Actual admission
        # reports its concrete failure through the normal reviewed workflow.
        return True
