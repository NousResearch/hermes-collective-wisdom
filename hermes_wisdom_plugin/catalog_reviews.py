"""Generic catalog reviews retain exact native Wisdom review authority."""

# JSON types are strict, and projection mirrors the frozen wire fields.
# pylint: disable=unidiomatic-typecheck
from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path

from .catalog_mutations import CatalogMutation
from .contract import org_directory_name
from .errors import WisdomAuthError, WisdomConflict, WisdomValidationError
from .skill_catalog import WisdomCatalog, digest, endpoint, failure
from .uninstall_review import UninstallReceipts, UninstallReviewer

_PREFIX = "catalog_review:"


def findings(plan):
    """Give every full native finding a stable id bound to all of its fields."""
    scan = plan.get("local_scan") or {"available": True, "allowed": True, "findings": []}
    result = []
    for index, row in enumerate(scan.get("findings") or []):
        severity = row.get("severity", "info")
        if severity not in {"info", "low", "medium", "high", "critical"}:
            severity = "info"
        result.append({
            "id": "finding-" + digest([index, row]), "severity": severity,
            "category": row.get("category") or "security", "file": row.get("file"),
            "line": row.get("line"),
            "description": row.get("description") or row.get("message") or "",
            "acknowledgement_required": scan.get("allowed") is not True,
        })
    # Some host policies report a warning summary without individual findings.
    if scan.get("allowed") is not True and not result:
        result.append({"id": "finding-" + digest(scan), "severity": "medium",
                       "category": "security", "file": None, "line": None,
                       "description": scan.get("summary") or "Review the scanner warning.",
                       "acknowledgement_required": True})
    compatibility = plan.get("compatibility") or {}
    if compatibility.get("outcome") in {"partial", "compatible_after_setup"}:
        details = [compatibility["outcome"], *(compatibility.get("setup_actions") or []),
                   *(compatibility.get("limitations") or [])]
        result.append({"id": "compatibility-" + digest(compatibility), "severity": "info",
                       "category": "compatibility", "file": None, "line": None,
                       "description": "\n".join(details), "acknowledgement_required": True})
    else:
        for index, text in enumerate(compatibility.get("limitations") or []):
            result.append({"id": "compatibility-" + digest([index, text]), "severity": "info",
                           "category": "compatibility", "file": None, "line": None,
                           "description": text, "acknowledgement_required": False})
    for index, text in enumerate(plan.get("sensitive_expansion") or []):
        result.append({"id": "sensitive-" + digest([index, text]), "severity": "medium",
                       "category": "sensitive_expansion", "file": None, "line": None,
                       "description": text, "acknowledgement_required": True})
    return result


def scan_verdict(plan):
    """Map host policy vocabulary to the catalog's bounded verdict enum."""
    verdict = (plan.get("local_scan") or {}).get("verdict", "safe")
    if verdict == "allow":
        return "safe"
    return verdict if verdict in {"safe", "caution", "dangerous", "unknown"} else "unknown"


def _incoming_inventory(plan):
    """Keep known file facts without treating missing inventory as an empty package."""
    rows = plan.get("files")
    if type(rows) is not list:
        return {}, False
    inventory, complete = {}, True
    for row in rows:
        if type(row) is not dict or type(row.get("path")) is not str or not row["path"]:
            complete = False
            continue
        path = row["path"]
        if path in inventory:
            complete = False
        inventory[path] = row
        size = row.get("size")
        if type(size) is not int or not 0 <= size <= 9007199254740991:
            complete = False
    return inventory, complete


def _update_changes(plan):  # pylint: disable=too-many-locals
    """Review the actual old/incoming union, including removals and executable intent."""
    incoming, complete = _incoming_inventory(plan)
    snapshot = plan.get("local_snapshot")
    if type(snapshot) is not dict:
        snapshot, complete = {}, False
    old = {path: identity for path, identity in snapshot.items()
           if type(path) is str and path and type(identity) is str and identity}
    complete = complete and len(old) == len(snapshot)
    identities = {}
    for path, row in incoming.items():
        identity, mode = row.get("hash"), row.get("mode")
        if type(identity) is not str or not identity or mode not in ("file", "exec"):
            complete = False
            continue
        identities[path] = ("exec:" if mode == "exec" else "") + identity
    if "baseline" in plan and plan["baseline"] != identities:
        complete = False
    paths = sorted(old.keys() | incoming.keys())
    complete = complete and len(paths) <= 2000
    changes = []
    for path in paths[:2000]:
        if path not in incoming:
            changes.append({"path": path, "size": 0, "change": "remove"})
            continue
        size = incoming[path].get("size")
        if type(size) is not int or not 0 <= size <= 9007199254740991:
            continue
        change = "add" if path not in old else (
            "unchanged" if old[path] == identities.get(path) else "replace")
        changes.append({"path": path, "size": size, "change": change})
    return changes, complete


def project_review(plan, action, source, host, *, review_id, token, now):
    # pylint: disable=too-many-arguments,too-many-locals
    """Pure projection; native exceptional choices do not imply generic consent."""
    owner = plan["owner"]
    target = Path(plan["target_path"]) if plan.get("target_path") else (
        host.skills_root / "_wisdom" / org_directory_name(owner["organization_id"]) / plan["slug"])
    existing = plan.get("previous_installation") or plan.get("installation")
    found = findings(plan)
    required = [row["id"] for row in found if row["acknowledgement_required"]]
    complete = len(found) <= 1000
    allowed = plan.get("allowed", True) is True
    policy = "block" if not allowed else "acknowledge" if required else "allow"
    if action == "update":
        changes, inventory_complete = _update_changes(plan)
        complete = complete and inventory_complete
    else:
        changes = [{"path": row["path"], "size": row["size"], "change": "add"}
                   for row in plan.get("files") or []]
        complete = complete and len(changes) <= 2000
    if action == "uninstall":
        changes = ([{"path": row["path"], "size": row["size"], "change": "remove"}
                    for row in plan["files"]] if "files" in plan else [
                        {"path": path, "size": (target / path).stat().st_size, "change": "remove"}
                        for path in (plan.get("local_snapshot") or {})])
    confirmable = allowed and complete
    return {
        "review_id": review_id, "action": action, "identifier": source + ":" + plan["skill_id"],
        "version": str(plan["version"]),
        "installation_revision": digest(existing) if existing else None,
        "target_relative_path": target.relative_to(host.skills_root).as_posix(),
        "summary": f"{action.capitalize()} {plan['slug']} version {plan['version']}.",
        "files": changes, "preview_complete": complete,
        "scan": {"verdict": scan_verdict(plan),
                 "policy": policy, "summary": (plan.get("local_scan") or {}).get("summary", ""),
                 "findings": found, "complete": complete},
        "acknowledgements_required": required,
        "policy_revision": digest([plan.get("policy"), plan.get("takedown_generation")]),
        "content_digest": plan["content_hash"],
        "setup_guidance": [{"id": f"setup-{index}", "summary": "Declared setup (not executed)",
                            "detail": command}
                           for index, command in enumerate(plan.get("setup_commands") or [])],
        "confirmable": confirmable, "review_token": token if confirmable else None,
        "expires_at_ms": int((now + 900) * 1000) if confirmable else None,
    }


class CatalogReviews(WisdomCatalog):
    """Persist the generic token beside, not instead of, the existing native receipt."""

    def __init__(self, *args, clock=time.time, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock

    @staticmethod
    def receipts(service, action):
        """Resolve the same receipt service the corresponding executor uses."""
        if action == "uninstall":
            return UninstallReceipts(service.installed, service.native_owner)
        if action == "install":
            return service._installation_services()[0].receipts  # pylint: disable=protected-access
        return service._update_services()[0].receipts  # pylint: disable=protected-access

    @endpoint("action", "id", "version")
    def review(self, request):  # pylint: disable=too-many-locals
        """Build a native exact-byte review, then bind its entire JSON and generic findings."""
        action = request.get("action")
        if action not in {"install", "update", "uninstall"}:
            raise WisdomValidationError("invalid action")
        version = request.get("version")
        if version is not None and (type(version) is not str or not version.isascii()
                                    or not version.isdecimal() or version.startswith("0")):
            raise WisdomValidationError("invalid version")
        if action == "uninstall" and version is not None:
            raise WisdomValidationError("uninstall cannot select a version")
        with self.opened() as (service, owner):
            if action == "install":
                reviewer = service._installation_services()[0]  # pylint: disable=protected-access
                reference = request["id"] + ("@v" + version if version else "")
                plan = reviewer.plan(reference, accept_warnings=True)
            elif action == "update":
                reviewer = service._update_services()[0]  # pylint: disable=protected-access
                plan = reviewer.plan(request["id"], accept_warnings=True, accept_partial=True,
                                     accept_sensitive=True)
                if version is not None and str(plan["version"]) != version:
                    raise WisdomConflict("Review the current update version")
            else:
                reviewer = UninstallReviewer(service.installed, self.receipts(service, action),
                                             self.host, service.native_owner)
                plan = reviewer.plan(request["id"])
            token, review_id = uuid.uuid4().hex, uuid.uuid4().hex
            review = project_review(plan, action, self.source, self.host, review_id=review_id,
                                    token=token, now=self.clock())
            if plan["skill_id"] != request["id"]:
                raise WisdomValidationError("review identity changed")
            if review["confirmable"]:
                service.installed.save_review(_PREFIX + token, {
                    "review": review, "owner": owner, "native_receipt_id": plan["receipt"],
                    "plan_digest": digest(plan), "action": action,
                    "implementation_revision": self.implementation_revision,
                })
            return review

    def record(self, service, token):
        """Read exact owner/implementation authority without enforcing fresh-action expiry."""
        record = service.installed.review(_PREFIX + token)
        if not record or record.get("owner") != service.native_owner():
            raise WisdomAuthError("catalog token belongs to another enrolment")
        if record.get("implementation_revision") != self.implementation_revision:
            raise WisdomConflict("catalog implementation changed")
        return record

    def authorize(self, service, record, acknowledgements):
        """Require the exact unique finding set, exact saved native facts, then freshness."""
        review = record["review"]
        required = review["acknowledgements_required"]
        if (type(acknowledgements) is not list
                or any(type(row) is not str for row in acknowledgements)
                or len(set(acknowledgements)) != len(acknowledgements)
                or set(acknowledgements) != set(required)):
            return failure("acknowledgement_required", "Acknowledge exactly the reviewed findings.")
        if self.clock() * 1000 >= review["expires_at_ms"]:
            return failure("review_expired", "This review expired; review the skill again.")
        receipts = self.receipts(service, record["action"])
        plan = receipts.load(record["native_receipt_id"], allow_started=True)
        if digest(plan) != record["plan_digest"]:
            return failure("review_changed", "The reviewed facts changed; review the skill again.")
        return None

    def project_operation(self, service, row):
        """Prefer immutable saved terminal wire data over current install prerequisites."""
        saved = service.installed.review("catalog_operation:" + row["id"])
        if saved is not None:
            return saved
        payload = row["payload"]
        mutation = CatalogMutation(None, None, row["kind"],
                                   self.source + ":" + row["skill_id"], payload, self.host)
        return mutation.operation(row["id"], service.installed.installation(row["skill_id"]))

    @endpoint("review_token")
    def reviewed(self, request):
        """Return stored authority; completed/pending journal reads precede freshness."""
        with self.opened() as (service, _owner):
            record = self.record(service, request["review_token"])
            receipt_service = self.receipts(service, record["action"])
            plan, native = receipt_service._read(  # pylint: disable=protected-access
                record["native_receipt_id"])
            if native.get("operation_id"):
                operation = service.installed.operation(native["operation_id"])
                return {"kind": "completed" if operation["status"] == "done" else "pending",
                        "review": copy.deepcopy(record["review"]), "operation":
                        self.project_operation(service, operation)}
            if self.clock() * 1000 >= record["review"]["expires_at_ms"]:
                raise WisdomValidationError("review expired", code="review_expired")
            if digest(plan) != record["plan_digest"]:
                raise WisdomValidationError("review changed", code="review_changed")
            return {"kind": "unstarted", "review": copy.deepcopy(record["review"]),
                    "operation": None}
