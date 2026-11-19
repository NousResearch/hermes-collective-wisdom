"""Review and execution adapter for the experimental skill catalog."""
# JSON wire validation deliberately rejects coercion and bool-as-int.
# pylint: disable=unidiomatic-typecheck,protected-access
from __future__ import annotations

from .catalog_mutations import CatalogMutation
from .catalog_reviews import CatalogReviews
from .errors import WisdomValidationError
from .setup_lock import managed_operation_guard
from .skill_catalog import endpoint


class CatalogProvider(CatalogReviews):
    """Bind catalog confirmation to the existing durable Wisdom executor."""

    def __init__(self, *args, mutations=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutations = mutations

    @endpoint("operation_id")
    def operation(self, request):
        """Read retained owner-bound operation data without network retry or effects."""
        with self.opened() as (service, owner):
            row = service.installed.operation(request["operation_id"])
            if row is None or row["payload"].get("owner") != owner:
                raise WisdomValidationError("unknown operation")
            return self.project_operation(service, row)

    @endpoint("action", "id", "review_token", "acknowledgements", "admission")
    def confirm(self, request):  # pylint: disable=too-many-locals,too-many-return-statements
        """Replay terminal authority first; otherwise consume exactly one native receipt."""
        with self.opened() as (service, _owner):
            record = self.record(service, request["review_token"])
            review = record["review"]
            if (request["action"] != review["action"]
                    or self.source + ":" + request["id"] != review["identifier"]):
                raise WisdomValidationError("confirmation identity changed")
            with managed_operation_guard(service.installed.root,
                                         "catalog-confirm:" + request["id"], wait=True):
                receipts = self.receipts(service, record["action"])
                plan, native = receipts._read(record["native_receipt_id"])
                row = service.installed.operation(native["operation_id"]) \
                    if native.get("operation_id") else None
                if row is not None and row["status"] == "done":
                    return self.project_operation(service, row)
                if row is None:
                    refused = self.authorize(service, record, request["acknowledgements"])
                    if refused is not None:
                        raise WisdomValidationError(refused["error"]["message"],
                                                    code=refused["error"]["code"])
                admission = request.get("admission")
                if (self.mutations is None or not isinstance(admission, dict)
                        or admission.get("review_id") != review["review_id"]
                        or admission.get("generation") != request["context"]["generation"]
                        or admission.get("expected_installation_revision")
                        != review["installation_revision"]):
                    raise WisdomValidationError("This review has no current admission.",
                                                code="ownership_conflict")
                action = record["action"]
                executor = (service._update_services()[1] if action == "update"
                            else service._installation_services()[1])
                executor.catalog = CatalogMutation(self.mutations, admission, action,
                                                   review["identifier"], plan, self.host)
                receipt = record["native_receipt_id"]
                if action == "uninstall":
                    executor.uninstall_reviewed(receipt, receipts=receipts)
                elif action == "install":
                    executor.apply(receipt, accept_partial=True)
                else:
                    executor.apply(receipt)
                _plan, native = receipts._read(receipt)
                return self.project_operation(service, service.installed.operation(
                    native["operation_id"]))
