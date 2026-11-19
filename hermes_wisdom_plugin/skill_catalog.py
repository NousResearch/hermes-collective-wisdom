"""Plain-data mapping for the experimental Hermes Team skill catalog.

Reads reuse Wisdom's scoped discovery and local install records. No Hub bundle
installer, setup execution or automatic prompt injection is involved.
"""

# Strict JSON contracts deliberately reject bool-as-int and coercion.
# pylint: disable=unidiomatic-typecheck
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from functools import wraps

from .catalog_validation import validate_effect
from .errors import (
    WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomUnavailable, WisdomValidationError,
)
from .managed_target import validate_managed_target
from .package import PackagePolicyError
from .package_application import tree_hashes

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,191}", re.ASCII)
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{16,256}", re.ASCII)
_CONTEXT_FIELDS = {"profile_key", "plugin_key", "generation", "request_id", "deadline_ms"}
_MAX_PAGES = 128


def digest(value):
    """Stable opaque revision for exact JSON facts, not a trust signature."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode()).hexdigest()


def failure(code, message):
    """Return provider-authored text, never a raw service exception."""
    return {"ok": False, "error": {"code": code, "message": message}}


def endpoint(*fields):
    """Validate the consumed request before any scoped service or credential read."""
    def decorate(method):
        @wraps(method)
        def call(self, request):  # pylint: disable=too-many-return-statements
            try:
                self.validate(request, fields)
                return {"ok": True, "data": method(self, request)}
            except WisdomValidationError as exc:
                code = exc.code if exc.code in {"review_expired", "review_changed",
                                               "acknowledgement_required", "ownership_conflict"} \
                    else "invalid_request"
                return failure(code, "The catalog request is invalid or its review changed.")
            except (PackagePolicyError, WisdomConflict):
                return failure("review_changed",
                               "The reviewed facts changed; review the skill again.")
            except WisdomAuthError:
                return failure("sign_in_required",
                               "Sign in to an enabled team and complete Wisdom setup.")
            except WisdomNotFound:
                return failure("not_found", "This team skill is unavailable.")
            except WisdomUnavailable:
                return failure("provider_unavailable",
                               "The team library is temporarily unavailable.")
            except Exception:  # pylint: disable=broad-exception-caught
                return failure("provider_error",
                               "Couldn't read the team library. Please try again.")
        return call
    return decorate


class WisdomCatalog:  # pylint: disable=too-many-instance-attributes
    """Provider mapping independent of new core imports for old-host compatibility."""

    name = "team"
    label = "Team skills"
    implementation_revision = "1"

    def __init__(self, services, plugin_key, host):
        self.services = services
        self.plugin_key = plugin_key
        owner_digest = hashlib.sha256(plugin_key.encode("utf-8")).hexdigest()
        self.source = f"plugin.{owner_digest}.{self.name}"
        self.host = host
        self._pages = OrderedDict()
        self._lock = threading.Lock()

    def validate(self, request, fields):
        """Reject wrong-owner stamps, unknown fields and coercion before work."""
        if type(request) is not dict or set(request) - {"context", *fields}:
            raise WisdomValidationError("invalid request fields")
        context = request.get("context")
        if (type(context) is not dict or set(context) != _CONTEXT_FIELDS
                or context.get("plugin_key") != self.plugin_key):
            raise WisdomValidationError("invalid context")
        if any(type(context[key]) is not str or not _OPAQUE.fullmatch(context[key])
               for key in ("profile_key", "generation", "request_id")):
            raise WisdomValidationError("invalid context identity")
        self._validate_fields(request, fields, context)
        cursor = request.get("cursor")
        if cursor is not None and (type(cursor) is not str or not _OPAQUE.fullmatch(cursor)):
            raise WisdomValidationError("invalid cursor")
        validate_effect(request, fields)

    def _validate_fields(self, request, fields, context):
        """Keep catalog-specific admission separate from the native sequence."""
        deadline = context["deadline_ms"]
        if type(deadline) is not int or not 1 <= deadline <= 9007199254740991:
            raise WisdomValidationError("invalid deadline")
        if "limit" in request and (type(request["limit"]) is not int
                                   or not 1 <= request["limit"] <= 100):
            raise WisdomValidationError("invalid limit")
        if "query" in request and (type(request["query"]) is not str
                                   or len(request["query"]) > 256):
            raise WisdomValidationError("invalid query")
        for key in ("id",):
            if key in fields and (type(request.get(key)) is not str
                                  or not _ID.fullmatch(request[key])):
                raise WisdomValidationError("invalid item")
        for key in ("review_token", "operation_id"):
            if key in fields and (type(request.get(key)) is not str
                                  or not _OPAQUE.fullmatch(request[key])):
                raise WisdomValidationError("invalid authority")


    @contextmanager
    def opened(self):
        """Recheck the exact enrolled owner before and after every read."""
        with self.services() as service:
            owner = service.native_owner()
            yield service, owner
            if service.native_owner() != owner:
                raise WisdomAuthError("Catalog owner changed")

    @endpoint()
    def availability(self, _request):
        """Known ineligibility is hidden; failure is never a flag-off inference."""
        try:
            with self.opened() as (service, _owner):
                service.list_skills()
                return {"state": "ready", "message": ""}
        except WisdomAuthError:
            return {"state": "hidden", "message": ""}
        except WisdomValidationError as exc:
            if exc.code in {"setup_required", "setup_pending"}:
                return {"state": "hidden", "message": ""}
            raise
        except WisdomUnavailable:
            return {"state": "unavailable",
                    "message": "The team library is temporarily unavailable."}

    def _page(self, request, scope, items=None):
        """Keep continuation scope and snapshot stable without slicing away later rows."""
        limit = request.get("limit", 20)
        cursor = request.get("cursor")
        with self._lock:
            if cursor is not None:
                saved = self._pages.get(cursor)
                if saved is None or saved[0] != scope:
                    raise WisdomValidationError("cursor scope changed")
                items = saved[1]
            selected = copy.deepcopy(items[:limit])
            remaining = items[limit:]
            next_cursor = None
            if remaining:
                next_cursor = uuid.uuid4().hex
                self._pages[next_cursor] = (scope, copy.deepcopy(remaining))
                while len(self._pages) > _MAX_PAGES:
                    self._pages.popitem(last=False)
        return {"items": selected, "next_cursor": next_cursor}

    def _scope(self, request, owner, method):
        context = request["context"]
        return digest([owner, context["profile_key"], context["generation"],
                       method, request.get("query", "")])

    def entry(self, row):
        """Map catalog metadata only; core stamps the authoritative owner/source."""
        skill_id = row["id"]
        if type(skill_id) is not str or not _ID.fullmatch(skill_id):
            raise WisdomValidationError("invalid team skill id")
        status = row.get("installation_status") or {}
        state = status.get("state", "not_installed")
        if state not in {"not_installed", "installed", "removed", "unmanaged_local",
                          "pending", "unavailable"}:
            state = "unavailable"
        actions = ["install"] if state in {"not_installed", "removed"} else (
            ["update", "uninstall"] if state == "installed" else [])
        version = row.get("latest_version")
        return {
            "id": skill_id, "name": row.get("editorial_name") or row["slug"],
            "description": row.get("author_description") or "", "source": self.source,
            "identifier": self.source + ":" + skill_id, "trust_level": "community",
            "repo": None, "path": None, "tags": list(row.get("tags") or []),
            "provider": {"id": self.name, "plugin_id": self.plugin_key, "label": self.label,
                         "version": str(version) if version is not None else None,
                         "author": row.get("created_by_display_name") or row.get("author_name"),
                         "managed_state": state, "scan_verdict": "unknown", "actions": actions},
        }

    @endpoint("query", "limit", "cursor")
    def search(self, request):
        """Browse/search the same authoritative paginated library as native Wisdom."""
        with self.opened() as (service, owner):
            scope = self._scope(request, owner, "search")
            if request.get("cursor") is not None:
                return self._page(request, scope)
            rows = service.search_skills(request.get("query", ""))
            return self._page(request, scope, [self.entry(row) for row in rows])

    @endpoint("id", "version")
    def inspect(self, request):  # pylint: disable=too-many-locals
        """Read exact content for preview without preparing or activating an install."""
        from .package import verify_content_files  # pylint: disable=import-outside-toplevel
        with self.opened() as (service, _owner):
            skill_id = request["id"]
            detail = service.resolve_skill(skill_id)
            skill = detail["skill"]
            if skill.get("id") != skill_id:
                raise WisdomValidationError("catalog identity changed")
            version = request.get("version")
            if version is None:
                latest = detail.get("latest_version_detail") or {}
                version = (latest.get("version") or {}).get("version")
                version = str(version) if type(version) is int else None
            if (type(version) is not str or not version.isascii() or not version.isdecimal()
                    or version.startswith("0") or len(version) > 16):
                raise WisdomValidationError("invalid version")
            selected = service.client.version(skill_id, int(version))
            content, files = service.client.content(
                skill_id, int(version), installation_id=_owner["installation_id"],
                takedown_generation=selected.skill["takedown_generation"],
            )
            _records, content_hash = verify_content_files(files)
            if (content_hash != content.content_hash
                    or content_hash != selected.version["content_hash"]):
                raise WisdomValidationError("preview content changed")
            text = next((body for path, _mode, body in files if path == "SKILL.md"), b"")
            complete = len(text) <= 256 * 1024
            preview = text.decode("utf-8") if complete else ""
            entry = self.entry({**skill, "latest_version": int(version),
                                "installation_status": detail.get("installation_status")})
            return {"entry": entry, "skill_md": preview, "preview_complete": complete,
                    "files": [{"path": path, "size": len(body), "change": "add"}
                              for path, _mode, body in files]}

    def installation(self, row, owner):
        """Reclaim only exact matching trees; changed/replaced copies remain local."""
        state = "unavailable"
        try:
            target = validate_managed_target(self.host, row, owner, require_tree=False)
            relative = target.relative_to(self.host.skills_root).as_posix()
            actual = tree_hashes(target)
            state = ("removed" if not os.path.lexists(target) else "unavailable") \
                if actual is None else (
                    "installed" if actual == row["baseline"] else "unmanaged_local")
        except (OSError, ValueError, PackagePolicyError):
            # The record's canonical target is still useful display data, not a claim.
            relative = self._record_relative(row)
        return {
            "identifier": self.source + ":" + row["skill_id"], "name": row["slug"],
            "target_relative_path": relative, "installed_version": str(row["version"]),
            "latest_version": None, "managed_state": state,
            "installation_revision": digest(row),
            "actions": ["update", "uninstall"] if state == "installed" else (
                ["install"] if state == "removed" else []),
        }

    def _record_relative(self, row):
        from pathlib import Path  # pylint: disable=import-outside-toplevel
        return Path(row["target_path"]).relative_to(self.host.skills_root).as_posix()

    @endpoint("limit", "cursor")
    def installed(self, request):
        """Read only local provider records; no remote installation listing or writes."""
        with self.opened() as (service, owner):
            scope = self._scope(request, owner, "installed")
            if request.get("cursor") is not None:
                return self._page(request, scope)
            rows = service.installed.installations(owner=owner)
            return self._page(request, scope, [self.installation(row, owner) for row in rows
                                                if row.get("state") == "active"
                                                and (row.get("target_provenance") or {}).get("kind")
                                                != "publisher_source"])
