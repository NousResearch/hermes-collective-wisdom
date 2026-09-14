"""Owner-bound manual update receipts, isolated from install and setup authority."""

from .install_receipts import InstallReceipts


class UpdateReceipts(InstallReceipts):
    """Receipts that authorize only one exact named-skill update."""

    OPERATION = "update"
    JOURNAL_KIND = "update"
    KEY_PREFIX = "update_receipt:"
    LABEL = "Update"
