"""Review and execution adapter for the experimental skill catalog."""
# JSON wire validation deliberately rejects coercion and bool-as-int.
# pylint: disable=unidiomatic-typecheck,protected-access
from __future__ import annotations

from .catalog_mutations import CatalogMutation
from .catalog_reviews import CatalogReviews
from .errors import WisdomNotFound, WisdomValidationError
from .setup_lock import managed_operation_guard
from .skill_catalog import endpoint, digest, _ID


class CatalogProvider(CatalogReviews):
    """Bind catalog confirmation to the existing durable Wisdom executor."""

    def __init__(self, *args, mutations=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutations = mutations

    @endpoint("ids", "cursor")
    def check(self, request):
        """Discover updates via the existing authoritative checker without review or writes."""
        ids = request.get("ids")
        if (type(ids) is not list or len(ids) > 100
                or any(type(value) is not str or not _ID.fullmatch(value) for value in ids)
                or len(set(ids)) != len(ids)):
            raise WisdomValidationError("invalid ids")
        with self.opened() as (service, owner):
            scope = digest([self._scope(request, owner, "check"), ids])
            if request.get("cursor") is not None:
                return self._page(request, scope)
            rows = {row["skill_id"]: row for row in service.installed.installations(owner=owner)
                    if row.get("state") == "active"
                    and (row.get("target_provenance") or {}).get("kind") != "publisher_source"}
            if any(value not in rows for value in ids):
                raise WisdomValidationError("unknown installation")
            selected = ids or sorted(rows)
            items = []
            for skill_id in selected:
                local = self.installation(rows[skill_id], owner)
                if local["managed_state"] != "installed":
                    state = "removed" if local["managed_state"] == "removed" else "unavailable"
                    checked = {"state": state, "latest_version": None}
                else:
                    checked = service.check_update(skill_id)
                    state = checked["state"]
                state = {"unverifiable": "unavailable", "taken_down": "blocked",
                         "archived": "blocked"}.get(state, state)
                if state not in {"current", "update_available", "removed", "pending", "blocked"}:
                    state = "unavailable"
                latest = checked.get("latest_version")
                items.append({"identifier": local["identifier"],
                              "installed_version": local["installed_version"],
                              "latest_version": str(latest) if type(latest) is int else None,
                              "state": state,
                              "installation_revision": local["installation_revision"]})
            return self._page(request, scope, items)

    @endpoint("operation_id")
    def operation(self, request):
        """Read retained owner-bound operation data without network retry or effects."""
        with self.opened() as (service, owner):
            row = service.installed.operation(request["operation_id"])
            if row is None or row["payload"].get("owner") != owner:
                raise WisdomNotFound("unknown operation")
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
