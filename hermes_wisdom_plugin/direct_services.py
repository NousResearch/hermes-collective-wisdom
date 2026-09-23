"""Small PluginState-owned enrollment, setup, and publication services."""
# pylint: disable=too-many-lines,missing-function-docstring,too-many-public-methods
# pylint: disable=line-too-long,broad-exception-caught,too-many-arguments

from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contribution_review import exact_review_files
from .credential_scan import scan_credentials
from .contract import author_description_hash, parse_manifest_bytes, sha256_address
from .errors import WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError
from .installed_setup import inspect_installed_setup
from .package import (
    MAX_METADATA_BYTES, MAX_TREE_BYTES, PackagePolicyError,
    read_validated_package_files, source_fingerprint, verify_content_files,
)
from .setup_lock import managed_operation_guard
from .wire import ObjectSet, build_commit, build_tree_from_snapshot

WISDOM_DISCLOSURE = (
    "Candidate signals stay on this profile. Only owner-approved private draft bytes, "
    "author copy, manifest metadata, and managed-install state reach the Gateway."
)

_KEY = "wisdom-core"
_AGGREGATE_LOCK = "\0wisdom-core-aggregate"
_OWNER_FIELDS = {
    "profile_name", "profile_home", "account_id", "organization_id", "base_url",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_dict(value: object, label: str) -> dict[str, Any]:
    try:
        payload = value.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError) as exc:
        raise WisdomValidationError(f"Gateway returned an invalid {label}") from exc
    if not isinstance(payload, dict):
        raise WisdomValidationError(f"Gateway returned an invalid {label}")
    return payload


def _current(may_apply: Callable[[], bool] | None) -> None:
    if may_apply is not None and may_apply() is not True:
        raise WisdomAuthError("Collective Wisdom review is no longer current")


class DirectState:
    """One bounded clean-format aggregate over the injected public PluginState."""

    # These small public-state guards intentionally repeat across independent domains.
    # pylint: disable=duplicate-code
    def __init__(self, state: Any, root: Path) -> None:
        if state is None or not callable(getattr(state, "get", None)) or not callable(
            getattr(state, "set", None)
        ):
            raise TypeError("host PluginState is required")
        self.state = state
        self.root = Path(root).resolve(strict=False)
    # pylint: enable=duplicate-code

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "identity": None,
            "enrollment_history": [],
            "drafts": {},
            "professionalism": {},
            "publication_reviews": {},
            "publications": {},
            "setup_reviews": {},
            "setup_operations": {},
            "setup_progress": {},
            "notices": {
                "cursor": None,
                "has_more": False,
                "offered": {},
            },
        }

    def _path(self) -> Path:
        path = Path(self.state.path).resolve(strict=False)
        if path.parent != self.root:
            raise RuntimeError("Collective Wisdom profile root changed during state operation")
        return path

    def _load(self) -> dict[str, Any]:
        value = self.state.get(_KEY)
        self._path()
        if value is None:
            return self._empty()
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("Collective Wisdom direct state is invalid")
        value.setdefault("enrollment_history", [])
        if not isinstance(value["enrollment_history"], list):
            raise RuntimeError("Collective Wisdom direct state is invalid")
        for key in (
            "drafts", "professionalism", "publication_reviews", "publications", "setup_reviews",
            "setup_operations", "setup_progress",
        ):
            if not isinstance(value.get(key), dict):
                raise RuntimeError("Collective Wisdom direct state is invalid")
        notices = value.get("notices")
        if not isinstance(notices, dict) or not isinstance(notices.get("offered"), dict):
            raise RuntimeError("Collective Wisdom direct state is invalid")
        return value

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    # Domain-local RMW, not a shared store facade.
    # pylint: disable=duplicate-code
    @contextmanager
    def change(self) -> Iterator[dict[str, Any]]:
        with managed_operation_guard(self.root, _AGGREGATE_LOCK, wait=True):
            value = self._load()
            yield value
            self._path()
            self.state.set(_KEY, value)
            self._path()
    # pylint: enable=duplicate-code

    def identity(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._load().get("identity"))

    def set_identity(self, record: dict[str, Any]) -> None:
        with self.change() as value:
            if value["identity"] != record:
                if isinstance(value["identity"], dict):
                    value["enrollment_history"].append(copy.deepcopy(value["identity"]))
                value["notices"] = self._empty()["notices"]
            value["identity"] = copy.deepcopy(record)

    def compare_identity(self, attempt_id: str, record: dict[str, Any]) -> None:
        with self.change() as value:
            current = value.get("identity")
            if not isinstance(current, dict) or current.get("attempt_id") != attempt_id:
                raise WisdomConflict("Collective Wisdom setup was superseded; retry setup")
            if value["identity"] != record:
                value["notices"] = self._empty()["notices"]
            value["identity"] = copy.deepcopy(record)

    def notice_state(self) -> dict[str, Any]:
        return copy.deepcopy(self._load()["notices"])

    def finish_notice_poll(
        self, *, identity: dict[str, Any] | None, cursor: str | None,
        has_more: bool, keys: list[str], now: float, limit: int = 128,
    ) -> list[str]:
        with self.change() as value:
            if value["identity"] != identity:
                raise WisdomAuthError("Collective Wisdom notice owner changed")
            value["notices"].update({"cursor": cursor, "has_more": bool(has_more)})
            offered = value["notices"]["offered"]
            selected = [key for key in keys if key not in offered]
            for key in selected:
                offered[key] = now
            if len(offered) > limit:
                oldest = sorted(offered, key=offered.get)[: len(offered) - limit]
                for key in oldest:
                    offered.pop(key, None)
            return selected

    def save_setup_review(self, receipt: str, record: dict[str, Any]) -> None:
        with self.change() as value:
            if receipt in value["setup_reviews"]:
                raise WisdomConflict("Setup review already exists")
            value["setup_reviews"][receipt] = copy.deepcopy(record)

    def setup_review(self, receipt: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._load()["setup_reviews"].get(receipt))

    def begin_setup(self, receipt: str, operation: dict[str, Any]) -> str:
        with self.change() as value:
            review = value["setup_reviews"].get(receipt)
            if not isinstance(review, dict):
                raise WisdomConflict("Setup review disappeared")
            existing_id = review.get("operation_id")
            if existing_id:
                return str(existing_id)
            skill_id = operation["skill_id"]
            if any(
                row.get("skill_id") == skill_id and row.get("status") == "pending"
                for row in value["setup_operations"].values()
            ):
                raise PackagePolicyError("another setup operation is unfinished for this skill")
            operation_id = uuid.uuid4().hex
            value["setup_operations"][operation_id] = {
                **copy.deepcopy(operation), "id": operation_id, "status": "pending",
                "updated_at": _now(),
            }
            review["operation_id"] = operation_id
            return operation_id

    def setup_operation(self, operation_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._load()["setup_operations"].get(operation_id))

    def setup_pending(self, skill_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row) for row in self._load()["setup_operations"].values()
            if row.get("skill_id") == skill_id and row.get("status") == "pending"
        ]

    def update_setup(self, operation_id: str, **changes: Any) -> dict[str, Any]:
        with self.change() as value:
            row = value["setup_operations"].get(operation_id)
            if not isinstance(row, dict):
                raise WisdomConflict("setup operation disappeared")
            row.update(copy.deepcopy(changes))
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    def finish_setup(self, operation_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.change() as value:
            operation = value["setup_operations"].get(operation_id)
            if not isinstance(operation, dict):
                raise WisdomConflict("setup operation disappeared")
            receipt = operation["receipt"]
            review = value["setup_reviews"].get(receipt)
            if not isinstance(review, dict) or review.get("operation_id") != operation_id:
                raise WisdomConflict("setup review no longer matches its operation")
            if review.get("result") is not None:
                if review["result"] != result:
                    raise WisdomConflict("setup review already has another result")
                return copy.deepcopy(review["result"])
            operation.update({"status": "done", "state": result["state"],
                              "result": copy.deepcopy(result), "updated_at": _now()})
            review["result"] = copy.deepcopy(result)
            value["setup_progress"].setdefault(operation["skill_id"], []).append({
                "operation_id": operation_id, "receipt": receipt,
                "owner": copy.deepcopy(operation["owner"]),
                "version": operation["version"], "content_hash": operation["content_hash"],
                "phase": operation["step"]["phase"], "index": operation["step"]["index"],
                "state": result["state"],
                **({"exit_code": result["exit_code"]} if "exit_code" in result else {}),
            })
            return copy.deepcopy(result)

    def setup_progress(self, owner: dict[str, str], inspection: dict[str, Any]) -> list[dict]:
        state = self._load()
        completed = [
            {key: copy.deepcopy(row[key]) for key in (
                "operation_id", "receipt", "phase", "index", "state", "exit_code"
            ) if key in row}
            for row in state["setup_progress"].get(inspection["skill_id"], [])
            if row.get("owner") == owner and row.get("version") == inspection["version"]
            and row.get("content_hash") == inspection["content_hash"]
        ]
        pending = [
            {
                "operation_id": row["id"], "receipt": row["receipt"],
                "phase": row["step"]["phase"], "index": row["step"]["index"],
                "state": "unknown" if row.get("state") == "starting" else row.get("state"),
            }
            for row in state["setup_operations"].values()
            if row.get("status") == "pending" and row.get("skill_id") == inspection["skill_id"]
            and row.get("owner") == owner and row.get("version") == inspection["version"]
            and row.get("content_hash") == inspection["content_hash"]
        ]
        return completed + pending

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._load()["drafts"].get(draft_id))

    def has_draft(self, skill_id: str, source_hash: str) -> bool:
        return any(
            row.get("skill_id") == skill_id and row.get("source_hash") == source_hash
            for row in self._load()["drafts"].values()
        )

    def prepared_draft(
        self, skill_id: str, source_hash: str, owner: dict[str, Any]
    ) -> dict[str, Any] | None:
        matches = [
            row for row in self._load()["drafts"].values()
            if row.get("skill_id") == skill_id and row.get("source_hash") == source_hash
            and row.get("state") != "declined" and row.get("owner") == owner
        ]
        return copy.deepcopy(max(matches, key=lambda row: row["updated_at"])) if matches else None

    def save_draft(self, record: dict[str, Any]) -> None:
        with self.change() as value:
            now = _now()
            previous = value["drafts"].get(record["id"], {})
            value["drafts"][record["id"]] = {
                **copy.deepcopy(record), "created_at": previous.get("created_at", now),
                "updated_at": now,
            }

    def replace_draft(
        self, draft_id: str, *, owner: dict[str, str], hashes: dict[str, str],
        record: dict[str, Any],
    ) -> bool:
        with self.change() as value:
            current = value["drafts"].get(draft_id)
            if not isinstance(current, dict) or current.get("owner") != owner or current.get(
                "state"
            ) != "prepared" or hashes != {
                "content": current.get("content_hash"),
                "author_description": current.get("description_hash"),
                "package_manifest": current.get("manifest_hash"),
            }:
                return False
            value["drafts"][draft_id] = {
                **copy.deepcopy(record), "id": draft_id,
                "created_at": current["created_at"], "updated_at": _now(),
            }
            return True

    def set_draft_state(self, draft_id: str, state: str) -> None:
        with self.change() as value:
            row = value["drafts"].get(draft_id)
            if not isinstance(row, dict):
                raise WisdomConflict("prepared draft disappeared")
            row.update({"state": state, "updated_at": _now()})

    @staticmethod
    def _professionalism_key(values: dict[str, str]) -> str:
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def professionalism(self, values: dict[str, str]) -> dict[str, Any] | None:
        return copy.deepcopy(
            self._load()["professionalism"].get(self._professionalism_key(values))
        )

    def save_professionalism(
        self, values: dict[str, str], result: dict[str, Any]
    ) -> None:
        with self.change() as value:
            value["professionalism"][self._professionalism_key(values)] = {
                "values": copy.deepcopy(values), "result": copy.deepcopy(result),
                "updated_at": _now(),
            }

    def save_publication_review(self, record: dict[str, Any]) -> str:
        receipt = uuid.uuid4().hex
        draft_id = record["draft_id"]
        with self.change() as value:
            for existing in value["publication_reviews"].values():
                if existing.get("draft_id") == draft_id and existing.get("consumed_at") is None:
                    existing["consumed_at"] = _now()
            value["publication_reviews"][receipt] = {
                **copy.deepcopy(record), "id": receipt, "consumed_at": None,
            }
        return receipt

    def publication_review(self, receipt: str) -> dict[str, Any] | None:
        row = self._load()["publication_reviews"].get(receipt)
        return copy.deepcopy(row) if isinstance(row, dict) and row.get("consumed_at") is None else None

    def begin_publication(self, receipt: str, record: dict[str, Any]) -> str:
        with self.change() as value:
            review = value["publication_reviews"].get(receipt)
            if not isinstance(review, dict) or review.get("consumed_at") is not None:
                raise WisdomConflict("Publication review receipt is missing or already used")
            for row in value["publications"].values():
                if row.get("local_draft_id") == record["local_draft_id"] and row.get(
                    "status"
                ) != "done":
                    raise WisdomConflict("A publication operation already owns this draft")
            operation_id = uuid.uuid4().hex
            now = _now()
            value["publications"][operation_id] = {
                **copy.deepcopy(record), "id": operation_id, "status": "pending",
                "phase": "intent", "created_at": now, "updated_at": now,
            }
            review["consumed_at"] = now
            return operation_id

    def consume_publication_review(
        self, receipt: str, operation_id: str, *, owner: dict[str, str],
        phase: str, requested_action: str | None,
    ) -> dict[str, Any]:
        """Consume an existing-operation receipt and advance it in one state change."""

        with self.change() as value:
            review = value["publication_reviews"].get(receipt)
            operation = value["publications"].get(operation_id)
            if not isinstance(review, dict) or review.get("consumed_at") is not None:
                raise WisdomConflict("Publication review receipt is missing or already used")
            if review.get("operation_id") != operation_id or not isinstance(operation, dict):
                raise WisdomConflict("Publication review no longer matches its operation")
            if review.get("owner") != owner or operation.get("owner") != owner:
                raise WisdomAuthError("Collective Wisdom publication owner changed")
            if operation.get("phase") not in {
                "policy_review_required", "approval_rejected",
            }:
                raise WisdomConflict("Publication operation is not awaiting review")
            now = _now()
            review["consumed_at"] = now
            operation.update({
                "policy": copy.deepcopy(review["policy"]), "phase": phase,
                "resume_phase": None, "requested_action": requested_action,
                "gateway_warning_acceptance": copy.deepcopy(
                    review.get("gateway_warning_acceptance")
                ),
                "updated_at": now,
            })
            return copy.deepcopy(operation)

    def publication(self, operation_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._load()["publications"].get(operation_id))

    def published_for_source(
        self, owner: dict[str, str], source: Path, source_hash: str,
    ) -> list[dict[str, Any]]:
        """Return retained published operations for one exact owner/source snapshot."""

        state = self._load()
        source_path = str(source)
        matches = []
        for row in state["publications"].values():
            if (
                row.get("owner") != owner
                or row.get("status") != "done"
                or row.get("publication_state") != "published"
            ):
                continue
            draft = state["drafts"].get(row.get("local_draft_id"))
            if (
                isinstance(draft, dict)
                and draft.get("owner") == owner
                and draft.get("source_path") == source_path
                and draft.get("source_hash") == source_hash
            ):
                matches.append(copy.deepcopy(row))
        return matches

    def publication_for_draft(self, draft_id: str) -> dict[str, Any] | None:
        for row in self._load()["publications"].values():
            if row.get("local_draft_id") == draft_id:
                return copy.deepcopy(row)
        return None

    def publication_for_candidate(self, assessment_id: str) -> dict[str, Any] | None:
        for row in self._load()["publications"].values():
            if row.get("candidate_assessment_id") == assessment_id:
                return copy.deepcopy(row)
        return None

    def local_publication_install_action(
        self, owner: dict[str, str], *, creator_user_id: str, content_hash: str,
    ) -> str:
        if not content_hash or creator_user_id != owner.get("account_id"):
            return "install"
        state = self._load()
        for publication in state["publications"].values():
            if (
                publication.get("owner") != owner
                or publication.get("publication_state") != "published"
                or (publication.get("hashes") or {}).get("content") != content_hash
            ):
                continue
            draft = state["drafts"].get(publication.get("local_draft_id"))
            if not isinstance(draft, dict) or draft.get("content_hash") != content_hash:
                continue
            source = Path(str(draft.get("source_path") or ""))
            if source.is_dir() and (source / "SKILL.md").is_file():
                return "none"
        return "install"

    def update_publication(self, operation_id: str, **changes: Any) -> dict[str, Any]:
        with self.change() as value:
            row = value["publications"].get(operation_id)
            if not isinstance(row, dict):
                raise WisdomConflict("Publication operation not found")
            row.update(copy.deepcopy(changes))
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    def finish_publication(self, operation_id: str, state: str) -> dict[str, Any]:
        with self.change() as value:
            row = value["publications"].get(operation_id)
            if not isinstance(row, dict):
                raise WisdomConflict("Publication operation not found")
            previous = row.get("publication_state")
            terminal = {
                "published", "pending_moderation", "changes_requested", "declined", "invalidated"
            }
            if (
                previous in terminal and previous != state
                and not (previous == "pending_moderation" and state == "published")
            ):
                raise WisdomConflict("Publication terminal state cannot move backwards")
            row.update({
                "status": "done", "phase": state, "publication_state": state,
                "updated_at": _now(),
            })
            value["drafts"][row["local_draft_id"]]["state"] = state
            return copy.deepcopy(row)

    def pending_direct_operations(self, owner: dict[str, str]) -> list[dict[str, Any]]:
        setup = [
            {"operation_id": row["id"], "kind": "wisdom_setup",
             "phase": row["state"], "skill_id": row["skill_id"]}
            for row in self._load()["setup_operations"].values()
            if row.get("status") == "pending" and row.get("owner") == owner
        ]
        publications = [
            {"operation_id": row["id"], "kind": "publication",
             "phase": row["phase"], "skill_id": row["skill_id"]}
            for row in self._load()["publications"].values()
            if row.get("status") != "done" and row.get("owner") == owner
        ]
        return setup + publications


class EnrollmentService:
    """Direct enrollment with local status and one durable registration intent."""

    def __init__(self, state: DirectState, account: Any, client: Any) -> None:
        self.state = state
        self.account = account
        self.client = client

    def _owner(self) -> dict[str, str]:
        owner = {
            key: str(getattr(self.account, key, "") or "").strip()
            for key in _OWNER_FIELDS
        }
        if not all(owner.values()):
            raise WisdomAuthError("Collective Wisdom account context is unavailable")
        return owner

    def _authorize(self) -> dict[str, str]:
        self.account.credentials(scope="wisdom:read")
        self.account.credentials(scope="wisdom:install")
        return self._owner()

    def _previous(self, owner: dict[str, str]) -> tuple[dict[str, Any] | None, bool]:
        previous = self.state.identity()
        same_owner = bool(previous and previous.get("owner") == owner)
        return previous, same_owner

    def _assert_owner(self, owner: dict[str, str]) -> None:
        if self._authorize() != owner:
            raise WisdomAuthError("Collective Wisdom account changed during setup")

    def setup(
        self, accept_disclosure: bool, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if accept_disclosure is not True:
            raise WisdomValidationError(
                "setup requires explicit disclosure acceptance", code="disclosure_required"
            )
        _current(may_apply)
        owner = self._authorize()
        _current(may_apply)
        previous, same_owner = self._previous(owner)
        if same_owner and previous and previous.get("state") == "active":
            result = self.status()
            result["disclosure"] = WISDOM_DISCLOSURE
            return result
        attempt_id = uuid.uuid4().hex
        record = {
            "owner": owner,
            "installation_id": (
                str(previous.get("installation_id"))
                if same_owner and previous and previous.get("installation_id") else "hwi_" + uuid.uuid4().hex
            ),
            "disclosure_version": 1,
            "disclosure_accepted_at": (
                str(previous.get("disclosure_accepted_at"))
                if same_owner and previous and previous.get("disclosure_accepted_at") else _now()
            ),
            "attempt_id": attempt_id,
            "state": "registering",
            "conflict_rotated": bool(
                same_owner and previous and previous.get("conflict_rotated")
            ),
        }
        self.state.set_identity(record)
        _current(may_apply)
        capability = _model_dict(self.client.capability(), "capability response")
        features = capability.get("features")
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise WisdomValidationError("Gateway returned an invalid capability response")
        if "wisdom" not in features:
            raise WisdomAuthError("Gateway does not advertise Collective Wisdom")
        _current(may_apply)
        self._assert_owner(owner)
        self.state.compare_identity(attempt_id, record)
        try:
            registered = _model_dict(
                self.client.register_identity(record["installation_id"]),
                "installation identity response",
            )
        except WisdomConflict as exc:
            if exc.code != "identity_conflict" or record.get("conflict_rotated"):
                raise
            _current(may_apply)
            record = {**record, "installation_id": "hwi_" + uuid.uuid4().hex,
                      "conflict_rotated": True}
            self.state.compare_identity(attempt_id, record)
            self._assert_owner(owner)
            registered = _model_dict(
                self.client.register_identity(record["installation_id"]),
                "installation identity response",
            )
        _current(may_apply)
        self._assert_owner(owner)
        if registered.get("installation_id") != record["installation_id"] or registered.get(
            "state"
        ) != "active":
            raise WisdomValidationError(
                "Gateway did not activate the reserved installation identity"
            )
        active = {**record, "state": "active", "capabilities": {"features": features},
                  "activated_at": _now()}
        self.state.compare_identity(attempt_id, active)
        result = self.status()
        result["disclosure"] = WISDOM_DISCLOSURE
        return result

    def status(self) -> dict[str, Any]:
        """Read local state only; unconfigured status never reads credentials."""
        try:
            record = self.state.identity()
        except Exception:  # pylint: disable=broad-exception-caught
            return {"configured": False, "state": "unavailable", "reason": "local_state_unavailable"}
        if record is None:
            return {"configured": False, "state": "not_configured", "reason": "not_configured"}
        try:
            self.account.credentials(scope="wisdom:read")
            owner = self._owner()
        except Exception:  # pylint: disable=broad-exception-caught
            owner = None
        matches = owner == record.get("owner")
        configured = bool(matches and record.get("state") == "active")
        reason = None if configured else (
            "authentication_unavailable" if owner is None else
            "owner_changed" if not matches else "registration_incomplete"
        )
        shown_owner = owner or {}
        return {
            "configured": configured,
            "state": record.get("state"),
            "reason": reason,
            "profile_name": shown_owner.get("profile_name"),
            "account_id": shown_owner.get("account_id"),
            "organization_id": shown_owner.get("organization_id"),
            "installation_id": record.get("installation_id"),
            "disclosure_accepted_at": record.get("disclosure_accepted_at"),
            "capabilities": copy.deepcopy(record.get("capabilities")),
        }

    def require_setup(self) -> None:
        self.account.credentials(scope="wisdom:read")
        status = self.status()
        if status["configured"] is not True:
            raise WisdomConflict(
                "Collective Wisdom is not set up for this profile; run setup first",
                code=str(status.get("reason") or "setup_required"),
            )

    def owner(self) -> dict[str, str]:
        self.require_setup()
        record = self.state.identity()
        assert record is not None
        return {**record["owner"], "installation_id": str(record["installation_id"])}


class SetupStep(BaseModel):
    """One exact separately approved installed-setup action."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)
    phase: str
    index: int = Field(default=0, ge=0, le=39)
    command: str = Field(default="", max_length=1200)

    @model_validator(mode="after")
    def _valid(self):
        if self.phase not in {"prerequisite", "setup", "verify"}:
            raise ValueError("invalid setup phase")
        if self.phase == "prerequisite" and self.command:
            raise ValueError("prerequisite acknowledgements do not contain commands")
        if self.phase != "prerequisite" and not self.command.strip():  # pylint: disable=no-member
            raise ValueError("supply the exact setup command for separate review")
        if self.phase == "verify" and self.index != 0:
            raise ValueError("verification has index zero")
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in self.command):
            raise ValueError("setup commands must be one visible line")
        if self.command and scan_credentials(
            [{"path": "setup-command", "content": self.command}]
        ):
            raise ValueError("setup command contains a credential-shaped string")
        return self


class SetupService:
    """Direct installed-setup review, effect, status, and explicit recovery."""

    REVIEW_SECONDS = 24 * 60 * 60
    _RECEIPT = re.compile(r"wip_[0-9a-f]{32}", re.ASCII)

    def __init__(
        self, state: DirectState, installed: Any, inspection_host: Any,
        execution_host: Any, owner: Callable[[], dict[str, str]], *, clock=time.time,
    ) -> None:
        self.state = state
        self.installed = installed
        self.inspection_host = inspection_host
        self.execution_host = execution_host
        self.owner_provider = owner
        self.clock = clock

    def _owner(self) -> dict[str, str]:
        value = self.owner_provider()
        if not isinstance(value, dict):
            raise WisdomAuthError("setup owner is unavailable")
        return copy.deepcopy(value)

    def inspect(self, skill_id: str, *, version: int | None = None) -> dict[str, Any]:
        return inspect_installed_setup(
            self.installed, self.state, self.inspection_host, self._owner(), skill_id,
            version=version,
        )

    def _build_plan(self, info: dict[str, Any], step: SetupStep) -> dict[str, Any]:
        guidance = info.get("guidance")
        if not isinstance(guidance, dict):
            raise PackagePolicyError("this installed version has no valid setup guidance")
        latest = {(row["phase"], row["index"]): row for row in info["setup_progress"]}
        requirement = None
        if step.phase == "prerequisite":
            requirements = info["prerequisites"]
            if step.index >= len(requirements):
                raise PackagePolicyError("prerequisite is not in the installed guidance")
            requirement = requirements[step.index]
            allowed = requirement["status"] != "missing"
            instruction = requirement["purpose"]
        elif step.phase == "setup":
            instructions = guidance["setup_instructions"]
            if step.index >= len(instructions):
                raise PackagePolicyError("setup step is not in the installed guidance")
            if any(
                latest.get(("setup", index), {}).get("state") != "passed"
                for index in range(step.index)
            ):
                raise PackagePolicyError("finish earlier setup steps first")
            allowed, instruction = True, instructions[step.index]
        else:
            prerequisites_ready = all(
                item["status"] == "present" or (
                    item["status"] == "acknowledgement_required"
                    and latest.get(("prerequisite", index), {}).get("state") == "passed"
                ) for index, item in enumerate(info["prerequisites"])
            )
            setup_ready = all(
                latest.get(("setup", index), {}).get("state") == "passed"
                for index in range(len(guidance["setup_instructions"]))
            )
            if not prerequisites_ready or not setup_ready:
                raise PackagePolicyError("finish declared prerequisites and setup before verification")
            allowed, instruction = True, guidance["verification_step"]
        identity = {
            "owner": self._owner(), "skill_id": info["skill_id"],
            "version": info["version"], "content_hash": info["content_hash"],
            "target_path": info["target_path"], "step": step.model_dump(),
        }
        plan = {
            **identity,
            "setup_key": hashlib.sha256(
                json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest(),
            "setup_instruction": instruction, "allowed": allowed,
        }
        if requirement is not None:
            plan["setup_requirement"] = requirement
        return plan

    def plan(
        self, skill_id: str, *, version: int, phase: str, index: int = 0,
        command: str = "",
    ) -> dict[str, Any]:
        if self.state.setup_pending(skill_id):
            raise PackagePolicyError("a setup operation is unfinished; inspect or recover it first")
        step = SetupStep.model_validate(
            {"phase": phase, "index": index, "command": command}, strict=True
        )
        value = self._build_plan(self.inspect(skill_id, version=version), step)
        receipt = "wip_" + uuid.uuid4().hex
        review = {
            **value, "receipt": receipt, "operation": "installed_setup",
            "created_at": self.clock(), "expires_at": self.clock() + self.REVIEW_SECONDS,
        }
        if len(json.dumps(review, ensure_ascii=True).encode()) > 6 * MAX_METADATA_BYTES + 2_097_152:
            raise WisdomValidationError("Setup review exceeds the size limit")
        self.state.save_setup_review(receipt, {
            "plan": review, "owner": review["owner"], "operation_id": None, "result": None,
        })
        return copy.deepcopy(review)

    def _review(self, receipt: str, *, allow_started: bool = False) -> dict[str, Any]:
        if not isinstance(receipt, str) or self._RECEIPT.fullmatch(receipt) is None:
            raise WisdomValidationError("Invalid installed_setup receipt identifier")
        record = self.state.setup_review(receipt)
        owner = self._owner()
        if not isinstance(record, dict) or record.get("owner") != owner:
            raise WisdomAuthError("Setup receipt does not belong to this enrolment")
        plan = record.get("plan")
        if not isinstance(plan, dict) or plan.get("owner") != owner:
            raise WisdomValidationError("Invalid installed_setup receipt binding")
        if self.clock() >= plan["expires_at"] and not (
            allow_started and record.get("operation_id")
        ):
            raise WisdomConflict("Setup review expired; create a new review")
        return plan

    @staticmethod
    def _result(operation: dict[str, Any]) -> dict[str, Any]:
        result = operation.get("result")
        if isinstance(result, dict):
            return copy.deepcopy(result)
        return {
            "state": operation.get("state", "unknown"),
            "operation_id": operation["id"], "receipt": operation["receipt"],
        }

    def apply(
        self, receipt: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        plan = self._review(receipt, allow_started=True)
        record = self.state.setup_review(receipt)
        assert record is not None
        if isinstance(record.get("result"), dict):
            return copy.deepcopy(record["result"])
        skill_id = plan["skill_id"]
        with managed_operation_guard(self.state.root, "setup:" + skill_id):
            record = self.state.setup_review(receipt)
            assert record is not None
            if record.get("operation_id"):
                operation = self.state.setup_operation(str(record["operation_id"]))
                if operation is None:
                    raise WisdomConflict("setup operation disappeared")
                return self._resume(operation, may_apply=may_apply)
            if any(
                row["kind"] in {"install", "update", "uninstall"}
                for row in self.installed.pending_operations(skill_id)
            ):
                raise PackagePolicyError("an install, update, or uninstall operation is unfinished")
            fresh = self._build_plan(
                self.inspect(skill_id, version=plan["version"]),
                SetupStep.model_validate(plan["step"], strict=True),
            )
            if fresh["setup_key"] != plan["setup_key"] or not fresh["allowed"]:
                raise PackagePolicyError("installed package, prerequisites, or reviewed setup step changed")
            _current(may_apply)
            self._review(receipt)
            operation_id = self.state.begin_setup(receipt, {
                "receipt": receipt, "skill_id": skill_id, "owner": self._owner(),
                "version": plan["version"], "content_hash": plan["content_hash"],
                "target_path": plan["target_path"], "setup_key": plan["setup_key"],
                "step": plan["step"], "task_id": "wisdom-setup:" + receipt,
                "handle": None, "state": "starting",
            })
            operation = self.state.setup_operation(operation_id)
            assert operation is not None
            if plan["step"]["phase"] == "prerequisite":
                return self._finish(operation, {"state": "passed"})

            def started(handle: dict[str, Any]) -> None:
                self.state.update_setup(operation_id, handle=handle, state="running")

            _current(may_apply)
            result = self.execution_host.execute(
                plan["step"]["command"], workdir=Path(plan["target_path"]),
                task_id=operation["task_id"], on_started=started,
            )
            operation = self.state.setup_operation(operation_id)
            assert operation is not None
            if result.get("state") in {"passed", "failed", "blocked"}:
                return self._finish(operation, result)
            self.state.update_setup(operation_id, state="unknown")
            return {"state": "unknown", "operation_id": operation_id, "receipt": receipt}

    def _finish(self, operation: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        stored = {
            "state": str(result.get("state") or "unknown"),
            "operation_id": operation["id"], "receipt": operation["receipt"],
        }
        for key in ("exit_code", "output", "output_truncated"):
            if key in result:
                stored[key] = result[key]
        return self.state.finish_setup(operation["id"], stored)

    def _resume(
        self, operation: dict[str, Any], *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if operation["status"] == "done":
            return self._result(operation)
        handle = operation.get("handle")
        if not isinstance(handle, dict):
            if operation.get("state") != "unknown":
                _current(may_apply)
                self.state.update_setup(operation["id"], state="unknown")
            return self._result({**operation, "state": "unknown"})
        result = self.execution_host.status(
            handle, command=operation["step"]["command"],
            workdir=Path(operation["target_path"]), task_id=operation["task_id"],
        )
        if result.get("state") in {"passed", "failed", "blocked"}:
            _current(may_apply)
            return self._finish(operation, result)
        if result.get("state") == "running":
            return self._result({**operation, "state": "running"})
        _current(may_apply)
        self.state.update_setup(operation["id"], state="unknown")
        return self._result({**operation, "state": "unknown"})

    def status(self, skill_id: str, *, version: int | None = None) -> dict[str, Any]:
        with managed_operation_guard(self.state.root, "setup:" + skill_id):
            info = self.inspect(skill_id, version=version)
            for operation in self.state.setup_pending(skill_id):
                if operation.get("owner") == self._owner() and operation.get(
                    "content_hash"
                ) == info["content_hash"]:
                    self._resume(operation)
            return self.inspect(skill_id, version=version)

    def recovery_status(self, skill_id: str, operation_id: str) -> dict[str, Any]:
        operation = self.state.setup_operation(operation_id)
        if operation is None or operation.get("skill_id") != skill_id:
            raise PackagePolicyError("setup recovery operation was not found")
        if operation.get("owner") != self._owner():
            raise WisdomAuthError("setup recovery belongs to another enrolment")
        return {
            "kind": "wisdom_setup", "skill_id": skill_id,
            "operation_id": operation_id, "receipt": operation["receipt"],
            "journal_phase": operation["state"], "journal_state": operation["status"],
            "version": operation["version"], "content_hash": operation["content_hash"],
            "target_path": operation["target_path"], "setup_key": operation["setup_key"],
            "step": copy.deepcopy(operation["step"]), "task_id": operation["task_id"],
            "handle": copy.deepcopy(operation.get("handle")),
            "result": copy.deepcopy(operation.get("result")),
            "can_recover": operation["status"] != "done",
        }

    def recover(
        self, skill_id: str, operation_id: str, *,
        acknowledged_effects_checked_and_stopped: bool = False,
        expected_recovery: dict | None = None,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with managed_operation_guard(self.state.root, "setup:" + skill_id):
            before = self.recovery_status(skill_id, operation_id)
            if expected_recovery is not None and expected_recovery != before:
                raise WisdomConflict("setup recovery changed after it was reviewed")
            operation = self.state.setup_operation(operation_id)
            assert operation is not None
            current = self._resume(operation, may_apply=may_apply)
            if current["state"] != "unknown":
                return current
            if not acknowledged_effects_checked_and_stopped:
                return {
                    **current, "recovery_review": True,
                    "recovery_consequence": (
                        "Abandoning records the outcome as abandoned only after effects were "
                        "checked and the recorded process was stopped; it never reruns."
                    ),
                }
            _current(may_apply)
            latest = self.state.setup_operation(operation_id)
            assert latest is not None
            return self._finish(latest, {"state": "abandoned"})


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


class PublicationService:  # pylint: disable=too-many-public-methods
    """Direct contribution publication with durable intent and evidence reconciliation."""

    REVIEW_TTL = timedelta(minutes=30)
    TERMINAL = {"published", "pending_moderation", "changes_requested", "declined", "invalidated"}

    def __init__(
        self, state: DirectState, reviewer: Any, client: Any,
        owner: Callable[[], dict[str, str]],
        enrollment: Any | None = None,
    ) -> None:
        self.state = state
        self.store = state  # inspectable domain authority, not a relational compatibility facade
        self.reviewer = reviewer
        self.client = client
        self.owner_provider = owner
        self.enrollment = enrollment

    def _owner(self) -> dict[str, str]:
        owner = self.owner_provider()
        required = {
            "profile_name", "profile_home", "account_id", "organization_id",
            "base_url", "installation_id",
        }
        if not isinstance(owner, dict) or set(owner) != required or any(
            not isinstance(owner.get(key), str) or not owner[key] for key in required
        ):
            raise WisdomAuthError("Collective Wisdom publication owner is unavailable")
        return copy.deepcopy(owner)

    def _policy(
        self, owner: dict[str, str], may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        _current(may_apply)
        policy = self.client.agent_led_policy()
        _current(may_apply)
        if _value(policy, "org_id") != owner["organization_id"]:
            raise WisdomAuthError("Collective Wisdom publication policy owner changed")
        return {
            "publication_mode": _value(policy, "publication_mode"),
            "version": _value(policy, "version"),
        }

    @staticmethod
    def _scan_fingerprint(prepared: dict[str, Any]) -> str:
        return sha256_address(json.dumps(
            prepared.get("local_scan"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True,
        ).encode())

    @staticmethod
    def _gateway_evidence(remote: Any) -> str:
        evidence = {
            key: _value(remote, key) for key in (
                "id", "orgId", "ownerUserId", "slug", "draftCommit", "contentHash",
                "authorDescription", "authorDescriptionHash", "packageManifestHash",
                "state", "scanVerdict", "scan", "supersedesDraftId",
            )
        }
        return sha256_address(json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode())

    @staticmethod
    def _gateway_check(remote: Any) -> dict[str, Any]:
        verdict = str(_value(remote, "scanVerdict") or "unavailable")
        scan = _value(remote, "scan")
        check = copy.deepcopy(scan) if isinstance(scan, dict) else {}
        check["status"] = verdict
        check.setdefault("verdict", verdict)
        check.setdefault("summary", f"Gateway security scan: {verdict}")
        return check

    def _security(
        self, prepared: dict[str, Any], *, accept_warnings: bool,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        security = prepared.get("security_check") or {}
        if security.get("upload_allowed") is True:
            if expected is not None:
                raise WisdomConflict("The scanner findings changed; review them again")
            return None
        if security.get("local_status") != "warning_required":
            raise WisdomConflict(str(
                security.get("summary") or "Local security checks must complete before sharing"
            ))
        acceptance = {
            "accepted": True, "operation": "publication",
            "content_hash": prepared["hashes"]["content"],
            "scan_fingerprint": self._scan_fingerprint(prepared),
        }
        if expected is not None:
            if expected != acceptance:
                raise WisdomConflict("The scanner findings changed; review them again")
            return acceptance
        if not accept_warnings:
            raise WisdomConflict(
                "security scanner warnings require explicit acknowledgement before sharing review"
            )
        return acceptance

    def review(
        self, draft_id: str, *, accept_warnings: bool = False,
        may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        _current(may_apply)
        owner = self._owner()
        existing = self.state.publication(draft_id)
        if existing is not None:
            return self._review_existing(existing, owner, may_apply, accept_warnings)
        prepared = self.reviewer.reopen(draft_id, may_apply=may_apply)
        draft = self.state.draft(draft_id)
        if draft is None or draft.get("owner") != owner or draft.get("state") != "prepared":
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")
        warning = self._security(prepared, accept_warnings=accept_warnings)
        policy = self._policy(owner, may_apply)
        scan = self._scan_fingerprint(prepared)
        receipt = self.state.save_publication_review({
            "draft_id": draft_id, "owner": owner, "policy": policy,
            "hashes": copy.deepcopy(prepared["hashes"]), "scan_fingerprint": scan,
            "warning_acceptance": warning, "reviewed_at": _now(),
            "expires_at": (datetime.now(timezone.utc) + self.REVIEW_TTL).isoformat(),
        })
        return {
            **prepared, "receipt": receipt,
            "slug": self._slug(str(prepared.get("editorial_name") or draft["slug"])),
            "publication_mode": policy["publication_mode"],
            "policy_version": policy["version"], "scan_fingerprint": scan,
            "warning_acceptance": warning,
            "authorization_expires_at": (
                datetime.now(timezone.utc) + self.REVIEW_TTL
            ).isoformat(),
            "notice": (
                "Nothing has been uploaded. Submitting this exact receipt is the one final "
                "owner confirmation; the Gateway alone chooses direct or moderated handling."
            ),
        }

    def _review_existing(
        self, operation: dict[str, Any], owner: dict[str, str],
        may_apply: Callable[[], bool] | None, accept_warnings: bool,
    ) -> dict[str, Any]:
        if operation.get("owner") != owner or operation.get("phase") not in {
            "policy_review_required", "approval_rejected",
        }:
            raise WisdomConflict("Publication operation is not awaiting review")
        prepared = self._operation_review(operation)
        remote = self._remote(operation, may_apply)
        if operation.get("phase") == "approval_rejected" and (
            _value(remote, "state") != "ready" or _value(remote, "scanVerdict") != "blocked"
        ):
            raise WisdomConflict("The rejected publication draft changed; reopen it first")
        policy = self._policy(owner, may_apply)
        gateway_evidence = self._gateway_evidence(remote)
        gateway_acceptance = None
        if _value(remote, "scanVerdict") == "advisory":
            if not accept_warnings:
                raise WisdomConflict(
                    "Gateway advisory warnings require explicit acknowledgement before review"
                )
            gateway_acceptance = {
                "accepted": True, "operation": "publication",
                "content_hash": operation["hashes"]["content"],
                "gateway_evidence_hash": gateway_evidence,
            }
        receipt = self.state.save_publication_review({
            "draft_id": operation["local_draft_id"], "owner": owner, "policy": policy,
            "hashes": copy.deepcopy(operation["hashes"]),
            "scan_fingerprint": operation["scan_fingerprint"],
            "warning_acceptance": operation.get("warning_acceptance"),
            "gateway_warning_acceptance": gateway_acceptance,
            "operation_id": operation["id"], "remote_revision": _value(remote, "updatedAt"),
            "gateway_evidence_hash": gateway_evidence,
            "reviewed_at": _now(),
            "expires_at": (datetime.now(timezone.utc) + self.REVIEW_TTL).isoformat(),
        })
        return {
            **prepared, "state": "policy_review_required",
            "draft_id": operation["local_draft_id"], "operation_id": operation["id"],
            "hashes": copy.deepcopy(operation["hashes"]), "receipt": receipt,
            "publication_mode": policy["publication_mode"], "policy_version": policy["version"],
            "warning_acceptance": gateway_acceptance or copy.deepcopy(
                operation.get("warning_acceptance")
            ),
            "server_checks": {"security_check": self._gateway_check(remote)},
            "network_submission": False,
        }

    @staticmethod
    def _slug(value: str) -> str:
        candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", candidate) is None:
            raise WisdomValidationError("Skill name cannot form a valid publication slug")
        return candidate

    def _snapshot(
        self, draft: dict[str, Any], owner: dict[str, str]
    ) -> tuple[dict[str, tuple[str, bytes]], dict[str, Any]]:
        raw = read_validated_package_files(Path(draft["overlay_path"]))
        _records, content_hash = verify_content_files(raw)
        manifest_bytes = next((body for path, _mode, body in raw if path == "skill.manifest.json"), None)
        if manifest_bytes is None:
            raise WisdomValidationError("Prepared package manifest is unavailable")
        manifest = parse_manifest_bytes(manifest_bytes)
        objects = ObjectSet()
        tree = build_tree_from_snapshot(raw, objects, max_object_bytes=MAX_TREE_BYTES)
        commit = build_commit(
            tree, [], owner=owner["account_id"], device=owner["installation_id"],
            message="Collective Wisdom owner-private draft", objects=objects,
            ts=datetime.fromisoformat(draft["created_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        request = {
            "slug": self._slug(manifest.name), "commit": commit,
            "content_hash": content_hash, "description": draft["description"],
            "description_hash": author_description_hash(draft["description"]),
            "manifest_hash": sha256_address(manifest_bytes),
        }
        return dict(objects.objects), request

    def _operation_snapshot(self, operation: dict[str, Any]) -> dict[str, Any]:
        package = operation.get("prepared_package")
        request = operation.get("request")
        if not isinstance(package, dict) or not isinstance(request, dict):
            raise WisdomValidationError("Publication package snapshot is unavailable")
        snapshot = {
            "overlay_path": package.get("overlay_path"),
            "created_at": package.get("created_at"),
            "description": request.get("description"),
        }
        if any(not isinstance(value, str) or not value for value in snapshot.values()):
            raise WisdomValidationError("Publication package snapshot is unavailable")
        return snapshot

    def _operation_review(self, operation: dict[str, Any]) -> dict[str, Any]:
        """Rebuild full review bytes from the operation's immutable package pointer."""

        snapshot = self._operation_snapshot(operation)
        try:
            files, content_hash = exact_review_files(Path(snapshot["overlay_path"]))
            _objects, request = self._snapshot(snapshot, operation["owner"])
        except (OSError, PackagePolicyError, ValueError) as exc:
            raise WisdomConflict(
                "The prepared publication snapshot is missing or changed; publication is blocked"
            ) from exc
        exact = (
            "slug", "commit", "content_hash", "description", "description_hash", "manifest_hash"
        )
        hashes = operation.get("hashes")
        request_matches_hashes = isinstance(hashes, dict) and all((
            request["content_hash"] == hashes.get("content"),
            request["description_hash"] == hashes.get("author_description"),
            request["manifest_hash"] == hashes.get("package_manifest"),
        ))
        if not isinstance(hashes, dict) or (
            not request_matches_hashes
        ) or content_hash != hashes["content"] or any(
            request[key] != operation["request"][key] for key in exact
        ):
            raise WisdomConflict(
                "The prepared publication snapshot is missing or changed; publication is blocked"
            )
        review = copy.deepcopy(operation.get("review"))
        if not isinstance(review, dict) or self._scan_fingerprint(review) != operation.get(
            "scan_fingerprint"
        ):
            raise WisdomConflict("Publication security review no longer matches its package")
        security = review.get("security_check")
        if (
            not isinstance(security, dict)
            or security.get("content_hash") != content_hash
            or security.get("author_description_hash") != hashes["author_description"]
        ):
            raise WisdomConflict("Publication security review no longer matches its package")
        prepared = {**review, "files": files, "hashes": copy.deepcopy(hashes)}
        self._security(
            prepared, accept_warnings=False, expected=operation.get("warning_acceptance")
        )
        prepared["warning_acceptance"] = copy.deepcopy(operation.get("warning_acceptance"))
        return prepared

    def _authorized(
        self, receipt: str, may_apply: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
        _current(may_apply)
        review = self.state.publication_review(receipt)
        if review is None:
            raise WisdomConflict("Publication review receipt is missing or already used")
        owner = self._owner()
        if review.get("owner") != owner:
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(review["expires_at"]):
            raise WisdomConflict("Publication review expired; review the complete package again")
        draft = self.state.draft(review["draft_id"])
        if draft is None or draft.get("owner") != owner:
            raise WisdomAuthError("Collective Wisdom contribution draft owner changed")
        operation = self.state.publication(str(review.get("operation_id") or ""))
        policy = self._policy(owner, may_apply)
        if policy != review["policy"]:
            raise WisdomConflict("The package or team publication policy changed; review it again")
        if operation is not None:
            prepared = self._operation_review(operation)
            remote = self._remote(operation, may_apply)
            if (
                _value(remote, "updatedAt") != review.get("remote_revision")
                or self._gateway_evidence(remote) != review.get("gateway_evidence_hash")
            ):
                raise WisdomConflict("The publication changed; review it again")
            return draft, prepared, owner, policy
        prepared = self.reviewer.reopen(draft["id"], may_apply=may_apply)
        if self._scan_fingerprint(prepared) != review["scan_fingerprint"]:
            raise WisdomConflict("The scanner findings changed; review them again")
        self._security(
            prepared, accept_warnings=False, expected=review.get("warning_acceptance")
        )
        prepared["warning_acceptance"] = copy.deepcopy(review.get("warning_acceptance"))
        if prepared["hashes"] != review["hashes"] or source_fingerprint(
            Path(draft["source_path"])
        ) != draft["source_hash"]:
            raise WisdomConflict("The package changed after review. Review it again before submitting")
        return draft, prepared, owner, policy

    def _intent(
        self, draft: dict[str, Any], prepared: dict[str, Any], owner: dict[str, str],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        _objects, request = self._snapshot(draft, owner)
        request["professionalism_review"] = prepared.get("professionalism_check")
        hashes = {
            "content": request["content_hash"],
            "author_description": request["description_hash"],
            "package_manifest": request["manifest_hash"],
        }
        if hashes != prepared["hashes"]:
            raise WisdomConflict("Prepared package changed while creating publication intent")
        return {
            "local_draft_id": draft["id"], "skill_id": draft["skill_id"],
            "owner": owner, "policy": policy, "hashes": hashes,
            "scan_fingerprint": self._scan_fingerprint(prepared),
            "warning_acceptance": prepared.get("warning_acceptance"),
            "request": request,
            "prepared_package": {
                "overlay_path": draft["overlay_path"], "created_at": draft["created_at"],
            },
            "review": {
                key: copy.deepcopy(prepared.get(key)) for key in (
                    "security_check", "professionalism_check", "source_hash",
                    "description", "system_specification", "local_scan",
                )
            },
        }

    def submit(
        self, receipt: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        return self._submit(receipt, private=False, may_apply=may_apply)

    def submit_private_draft(
        self, receipt: str, *, may_apply: Callable[[], bool] | None = None,
        candidate_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            receipt, private=True, may_apply=may_apply,
            candidate_assessment_id=candidate_assessment_id,
        )

    def _submit(  # pylint: disable=too-many-locals
        self, receipt: str, *, private: bool,
        may_apply: Callable[[], bool] | None,
        candidate_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        draft, _prepared, _owner, _policy = self._authorized(receipt, may_apply)
        current_review = self.state.publication_review(receipt)
        if current_review is None:
            raise WisdomConflict("Publication review receipt is missing or already used")
        existing_id = current_review.get("operation_id")
        lock_key = "publication:" + draft["skill_id"]
        with managed_operation_guard(self.state.root, lock_key):
            draft, prepared, owner, policy = self._authorized(receipt, may_apply)
            _current(may_apply)
            if existing_id:
                operation = self.state.publication(existing_id)
                if operation is None or operation.get("phase") not in {
                    "policy_review_required", "approval_rejected",
                }:
                    raise WisdomConflict("Publication operation is not awaiting review")
                phase = (
                    "revision_required" if operation.get("phase") == "approval_rejected"
                    else operation.get("resume_phase", "submitted")
                )
                requested_action = (
                    "private_draft" if private else None
                )
                self.state.consume_publication_review(
                    receipt, existing_id, owner=owner, phase=phase,
                    requested_action=requested_action,
                )
                return self._drive(existing_id, may_apply=may_apply)
            intent = self._intent(draft, prepared, owner, policy)
            if private:
                intent["requested_action"] = "private_draft"
            if candidate_assessment_id:
                intent["candidate_assessment_id"] = candidate_assessment_id
            _current(may_apply)
            operation_id = self.state.begin_publication(receipt, intent)
            return self._drive(operation_id, may_apply=may_apply)

    @staticmethod
    def _matches(remote: Any, operation: dict[str, Any]) -> bool:
        request, owner = operation["request"], operation["owner"]
        return all((
            _value(remote, "orgId") == owner["organization_id"],
            _value(remote, "ownerUserId") == owner["account_id"],
            _value(remote, "slug") == request["slug"],
            _value(remote, "draftCommit") == request["commit"],
            _value(remote, "contentHash") == request["content_hash"],
            _value(remote, "authorDescription") == request["description"],
            _value(remote, "authorDescriptionHash") == request["description_hash"],
            _value(remote, "packageManifestHash") == request["manifest_hash"],
        ))

    def _remote(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> Any:
        remote_id = operation.get("remote_draft_id")
        if not remote_id:
            raise WisdomConflict("Publication draft identity is unavailable")
        _current(may_apply)
        remote = self.client.draft(remote_id).draft
        _current(may_apply)
        if not self._matches(remote, operation):
            raise WisdomValidationError("Authoritative publication draft no longer matches intent")
        return remote

    def _operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.state.publication(operation_id)
        if operation is None:
            raise WisdomNotFound("Publication operation not found")
        if operation.get("owner") != self._owner():
            raise WisdomAuthError("Collective Wisdom publication owner changed")
        return operation

    @staticmethod
    def _uncertain(operation_id: str, phase: str, detail: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "state": "uncertain", "phase": phase,
                "detail": detail, "retry_safe": False}

    def _done(self, operation: dict[str, Any]) -> dict[str, Any]:
        if operation.get("publication_state") == "rejected":
            return {
                "operation_id": operation["id"], "state": "rejected",
                "phase": "rejected", "detail": operation["rejection_detail"],
                "code": operation["rejection_code"], "retry_safe": False,
            }
        result = {
            "operation_id": operation["id"], "state": "done",
            "publication_state": operation.get("publication_state"),
            "remote_draft_id": operation.get("remote_draft_id"),
            "submitted_request": {
                key: value for key, value in operation["request"].items()
                if key != "professionalism_review"
            },
        }
        if isinstance(operation.get("publication_result"), dict):
            result["publication_result"] = copy.deepcopy(operation["publication_result"])
        if isinstance(operation.get("managed_enrollment"), dict):
            result["managed_enrollment"] = copy.deepcopy(operation["managed_enrollment"])
            if isinstance(operation["managed_enrollment"].get("detail"), str):
                result["detail"] = operation["managed_enrollment"]["detail"]
        return result

    def _finish(
        self, operation_id: str, state: str, *, publication_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if publication_result is not None:
            self.state.update_publication(
                operation_id, publication_result=copy.deepcopy(publication_result)
            )
        return self._done(self.state.finish_publication(operation_id, state))

    def _finish_published(
        self, operation_id: str, result: dict[str, Any],
        may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        completed = self._finish(
            operation_id, "published", publication_result=result,
        )
        if self.enrollment is None:
            return completed
        try:
            linked = self.enrollment.link(
                self._operation(operation_id), copy.deepcopy(result), may_apply=may_apply,
            )
        except Exception:  # publication success must remain truthful
            linked = self._not_linked(operation_id)
        operation = self.state.update_publication(
            operation_id, managed_enrollment=linked,
        )
        return self._done(operation)

    def _finish_reconciled_published(
        self, operation_id: str, may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        """Finish a GET-observed publication, then explicitly register its exact source."""

        completed = self._finish(operation_id, "published")
        if self.enrollment is None:
            return completed
        try:
            linked = self.enrollment.link_existing(
                self._operation(operation_id), may_apply=may_apply,
            )
        except Exception:  # remote publication remains successful and must not be replayed
            linked = self._not_linked(operation_id)
        return self._done(self.state.update_publication(
            operation_id, managed_enrollment=linked,
        ))

    def _not_linked(self, operation_id: str) -> dict[str, str]:
        operation = self._operation(operation_id)
        draft = self.state.draft(str(operation.get("local_draft_id") or ""))
        skill_name = draft.get("slug") if isinstance(draft, dict) else None
        if not isinstance(skill_name, str) or not skill_name:
            skill_name = "<local-skill-name>"
        return {
            "state": "not_linked",
            "detail": (
                "Published successfully, but the original source is not linked to this version "
                "locally. "
                f"Recover with: hermes wisdom publication enroll {shlex.quote(skill_name)}"
            ),
        }

    def _reject_submission(
        self, operation_id: str, exc: WisdomValidationError,
    ) -> dict[str, Any]:
        code = str(exc.code or "")
        if re.fullmatch(r"[a-z0-9_]{1,128}", code) is None:
            code = "validation_rejected"
        detail = "Gateway rejected the publication draft before creation (HTTP 422)."
        operation = self.state.update_publication(
            operation_id, status="done", phase="rejected", publication_state="rejected",
            rejection_code=code, rejection_detail=detail,
        )
        return self._done(operation)

    def _record_remote(self, operation_id: str, remote: Any) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if not self._matches(remote, operation):
            raise WisdomValidationError("Gateway acknowledged a different publication draft")
        verdict = str(_value(remote, "scanVerdict") or "")
        if verdict not in {"pass", "advisory", "blocked"}:
            raise WisdomValidationError("Gateway draft scan verdict is unavailable")
        self.state.set_draft_state(operation["local_draft_id"], "submitted")
        return self.state.update_publication(
            operation_id, remote_draft_id=str(_value(remote, "id")),
            remote_state=str(_value(remote, "state")),
            remote_revision=str(_value(remote, "updatedAt")), phase="submitted",
            remote_scan_verdict=verdict,
            gateway_evidence_hash=self._gateway_evidence(remote),
        )

    def _reconcile_submission(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any] | None:
        _current(may_apply)
        matches = [item for item in self.client.list_drafts() if self._matches(item, operation)]
        _current(may_apply)
        if len(matches) != 1:
            return None
        return self._record_remote(operation["id"], matches[0])

    def _upload_intent(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        _current(may_apply)
        snapshot = self._operation_snapshot(operation)
        try:
            objects, request = self._snapshot(snapshot, operation["owner"])
        except (OSError, PackagePolicyError, ValueError) as exc:
            raise WisdomConflict(
                "The prepared publication snapshot is missing or changed; publication is blocked"
            ) from exc
        exact = ("slug", "commit", "content_hash", "description", "description_hash", "manifest_hash")
        if any(request[key] != operation["request"][key] for key in exact):
            raise WisdomConflict("Prepared package changed after publication intent")
        self.client.upload_private_objects(objects)
        self.state.update_publication(operation["id"], phase="objects_uploaded")
        if may_apply is not None and may_apply() is not True:
            return operation, self.status(operation["id"])
        return self._operation(operation["id"]), None

    def _submit_remote_draft(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        operation_id = operation["id"]
        self.state.update_publication(operation_id, phase="draft_submission_unknown")
        _current(may_apply)
        try:
            remote = self.client.submit_draft(**operation["request"])
        except WisdomValidationError as exc:
            if exc.status == 422:
                return operation, self._reject_submission(operation_id, exc)
            return operation, self._uncertain(
                operation_id, "draft_submission_unknown",
                "Draft submission outcome is unknown; exact reconciliation is required.",
            )
        except Exception:  # acknowledgement may follow a committed effect
            return operation, self._uncertain(
                operation_id, "draft_submission_unknown",
                "Draft submission outcome is unknown; exact reconciliation is required.",
            )
        try:
            return self._record_remote(operation_id, remote), None
        except WisdomValidationError:
            return operation, self._uncertain(
                operation_id, "draft_submission_unknown",
                "Draft submission acknowledgement was invalid; exact reconciliation is required.",
            )

    def _submission(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
        reconcile: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        phase = operation["phase"]
        if phase == "draft_submission_unknown":
            if not reconcile:
                return operation, self._uncertain(
                    operation["id"], phase, "Exact draft reconciliation is required"
                )
            reconciled = self._reconcile_submission(operation, may_apply)
            if reconciled is None:
                return operation, self._uncertain(
                    operation["id"], phase,
                    "No unique exact owner draft is visible; nothing was retried.",
                )
            operation = reconciled
        elif phase == "intent":
            operation, result = self._upload_intent(operation, may_apply)
            if result is not None:
                return operation, result
        if operation["phase"] == "objects_uploaded":
            return self._submit_remote_draft(operation, may_apply)
        return operation, None

    def _approval(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        operation_id = operation["id"]
        if operation.get("requested_action") == "private_draft" and operation["phase"] == "submitted":
            self.state.update_publication(
                operation_id, phase="policy_review_required", resume_phase="submitted"
            )
            return operation, {
                "operation_id": operation_id, "state": "pending",
                "phase": "policy_review_required",
                "publication_state": operation.get("remote_state"),
                "remote_draft_id": operation.get("remote_draft_id"), "retry_safe": False,
                "detail": "Private draft submitted. Final sharing still requires a fresh exact-package approval.",
            }
        if operation["phase"] == "policy_review_required":
            return operation, self.status(operation_id)
        if operation.get("remote_draft_id"):
            remote = self._remote(operation, may_apply)
            remote_state = str(_value(remote, "state"))
            if remote_state in self.TERMINAL:
                result = (
                    self._finish_reconciled_published(operation_id, may_apply)
                    if remote_state == "published"
                    else self._finish(operation_id, remote_state)
                )
                return operation, result
            operation = self.state.update_publication(
                operation_id, remote_state=remote_state,
                remote_revision=str(_value(remote, "updatedAt")),
                remote_scan_verdict=str(_value(remote, "scanVerdict") or ""),
                gateway_evidence_hash=self._gateway_evidence(remote),
            )
        if self._policy(operation["owner"], may_apply) != operation["policy"]:
            self.state.update_publication(
                operation_id, phase="policy_review_required", resume_phase=operation["phase"]
            )
            return operation, self.status(operation_id)
        return self._approve_ready(operation)

    def _require_approval_scan(self, operation: dict[str, Any]) -> None:
        """Require a known verdict and exact acknowledgement of Gateway advisories."""
        verdict = operation.get("remote_scan_verdict")
        acceptance = operation.get("gateway_warning_acceptance")
        advisory_accepted = (
            verdict == "advisory" and isinstance(acceptance, dict)
            and acceptance.get("accepted") is True
            and acceptance.get("gateway_evidence_hash") == operation.get("gateway_evidence_hash")
        )
        if verdict not in {"pass", "blocked", "advisory"} or (
            verdict == "advisory" and not advisory_accepted
        ):
            self.state.update_publication(
                operation["id"], phase="policy_review_required", resume_phase="submitted"
            )
            raise WisdomValidationError("Gateway scan verdict requires a fresh review")

    def _approve_ready(
        self, operation: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        operation_id = operation["id"]
        if operation.get("remote_state") != "ready":
            return operation, None
        self._require_approval_scan(operation)
        if operation["phase"] == "approval_unknown":
            return operation, self._uncertain(
                operation_id, "approval_unknown",
                "Gateway does not show owner approval; nothing was retried.",
            )
        self.state.update_publication(operation_id, phase="approval_unknown")
        try:
            approved = self.client.approve(
                operation["remote_draft_id"],
                content_hash=operation["hashes"]["content"],
                description_hash=operation["hashes"]["author_description"],
                manifest_hash=operation["hashes"]["package_manifest"],
            )
        except WisdomValidationError as exc:
            if exc.status == 422 and exc.code == "scan_blocked":
                self.state.update_publication(
                    operation_id, phase="approval_rejected",
                    rejection_code="scan_blocked",
                )
                return operation, self._approval_rejection(operation_id)
            if exc.status is None:
                return operation, self._uncertain(
                    operation_id, "approval_unknown",
                    "Owner approval acknowledgement was invalid; exact reconciliation is required.",
                )
            self.state.update_publication(operation_id, phase=operation["phase"])
            raise
        except (WisdomAuthError, WisdomConflict):
            self.state.update_publication(operation_id, phase=operation["phase"])
            raise
        except Exception:
            return operation, self._uncertain(
                operation_id, "approval_unknown", "Owner approval outcome is unknown"
            )
        if not self._matches(approved, operation):
            raise WisdomValidationError("Gateway approved a different publication draft")
        return self.state.update_publication(
            operation_id, phase="approved", remote_state=str(_value(approved, "state"))
        ), None

    def _approval_rejection(self, operation_id: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id, "state": "rejected",
            "phase": "approval_rejected", "code": "scan_blocked",
            "detail": (
                "Gateway rejected approval because its stored scan blocked this draft. "
                "Review the retained operation again to authorize an idempotent rescan revision."
            ),
            "retry_safe": False,
        }

    def _record_revision(
        self, operation_id: str, predecessor_id: str, revised: Any,
    ) -> dict[str, Any]:
        operation = self._operation(operation_id)
        verdict = str(_value(revised, "scanVerdict") or "")
        if (
            str(_value(revised, "id") or "") == predecessor_id
            or _value(revised, "supersedesDraftId") != predecessor_id
            or _value(revised, "state") != "ready"
            or not self._matches(revised, operation)
            or verdict not in {"pass", "advisory", "blocked"}
        ):
            raise WisdomValidationError("Gateway returned an invalid publication revision")
        history = list(operation.get("remote_draft_history") or [])
        if predecessor_id not in history:
            history.append(predecessor_id)
        phase = {
            "pass": "submitted", "advisory": "policy_review_required",
            "blocked": "approval_rejected",
        }[verdict]
        return self.state.update_publication(
            operation_id,
            remote_draft_id=str(_value(revised, "id")),
            remote_draft_history=history,
            remote_state=str(_value(revised, "state")),
            remote_revision=str(_value(revised, "updatedAt")),
            phase=phase, remote_scan_verdict=verdict,
            gateway_evidence_hash=self._gateway_evidence(revised),
            gateway_warning_acceptance=None,
            revision_predecessor_id=None,
        )

    def _revision_outcome(
        self, operation: dict[str, Any], revised: Any,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        verdict = str(_value(revised, "scanVerdict") or "")
        if verdict == "blocked":
            operation = self.state.update_publication(
                operation["id"], rejection_code="scan_blocked",
            )
            return operation, self._approval_rejection(operation["id"])
        if verdict == "advisory":
            result = self.status(operation["id"])
            result["server_checks"] = {"security_check": self._gateway_check(revised)}
            result["detail"] = (
                "Gateway rescan produced advisory findings; review the successor before approval."
            )
            return operation, result
        if verdict != "pass":
            raise WisdomValidationError("Gateway revision scan verdict is unavailable")
        return operation, None

    def _revise_blocked(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        operation_id = operation["id"]
        predecessor_id = str(
            operation.get("revision_predecessor_id") or operation.get("remote_draft_id") or ""
        )
        if not predecessor_id:
            raise WisdomConflict("Publication draft identity is unavailable")
        if operation["phase"] == "revision_unknown":
            _current(may_apply)
            matches = [
                draft for draft in self.client.list_drafts()
                if _value(draft, "supersedesDraftId") == predecessor_id
                and self._matches(draft, operation)
            ]
            _current(may_apply)
            if len(matches) > 1:
                raise WisdomValidationError("Gateway returned multiple publication revisions")
            if matches:
                _current(may_apply)
                recorded = self._record_revision(operation_id, predecessor_id, matches[0])
                return self._revision_outcome(recorded, matches[0])
        request = operation["request"]
        hashes = operation["hashes"]
        self.state.update_publication(
            operation_id, phase="revision_unknown", revision_predecessor_id=predecessor_id,
        )
        _current(may_apply)
        try:
            revised = self.client.revise_draft(
                predecessor_id,
                commit=request["commit"], content_hash=request["content_hash"],
                description=request["description"],
                description_hash=request["description_hash"],
                manifest_hash=request["manifest_hash"],
                professionalism_review=request.get("professionalism_review"),
                expected_content_hash=hashes["content"],
                expected_description_hash=hashes["author_description"],
                expected_manifest_hash=hashes["package_manifest"],
            )
        except (WisdomAuthError, WisdomConflict, WisdomNotFound, WisdomValidationError) as exc:
            if exc.status is None:
                return operation, self._uncertain(
                    operation_id, "revision_unknown",
                    "Draft revision acknowledgement was invalid; authoritative reconciliation is required.",
                )
            self.state.update_publication(operation_id, phase="approval_rejected")
            raise
        except Exception:  # idempotent revision is reconciled before any retry
            return operation, self._uncertain(
                operation_id, "revision_unknown",
                "Draft revision acknowledgement was lost; authoritative successor reconciliation is required.",
            )
        _current(may_apply)
        recorded = self._record_revision(operation_id, predecessor_id, revised)
        return self._revision_outcome(recorded, revised)

    def _publish(
        self, operation: dict[str, Any], may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        operation_id = operation["id"]
        if operation["phase"] == "publication_unknown":
            return self._uncertain(
                operation_id, "publication_unknown",
                "Publication outcome remains unknown; nothing was retried.",
            )
        _current(may_apply)
        head = self.client.organization_head()
        _current(may_apply)
        self.state.update_publication(operation_id, phase="publication_unknown")
        try:
            result = self.client.publish(
                operation["remote_draft_id"],
                content_hash=operation["hashes"]["content"], base_commit=head,
            )
        except WisdomConflict as exc:
            if exc.code != "base_mismatch":
                raise
            self.state.update_publication(operation_id, phase="base_conflict")
            return self.status(operation_id)
        except Exception:
            return self._uncertain(
                operation_id, "publication_unknown",
                "Publication acknowledgement was lost; status will reconcile without resubmitting.",
            )
        state = str((result or {}).get("state") or "")
        if state == "published":
            return self._finish_published(operation_id, result, may_apply)
        if state == "pending_moderation":
            return self._finish(operation_id, state)
        self.state.update_publication(operation_id, phase="server_pending")
        return self._uncertain(
            operation_id, "server_pending", f"Gateway publication is {state or 'unknown'}"
        )

    def _drive(
        self, operation_id: str, *, may_apply: Callable[[], bool] | None,
        reconcile: bool = False,
    ) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation["status"] == "done":
            return self._done(operation)
        operation, result = self._submission(operation, may_apply, reconcile)
        if result is not None:
            return result
        if operation["phase"] in {"revision_required", "revision_unknown"}:
            operation, result = self._revise_blocked(operation, may_apply)
            if result is not None:
                return result
        operation, result = self._approval(operation, may_apply)
        if result is not None:
            return result
        return self._publish(operation, may_apply)

    def status(
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        _current(may_apply)
        operation = self._operation(operation_id)
        if operation["status"] == "done":
            return self._done(operation)
        phase = operation["phase"]
        if phase == "approval_rejected":
            return self._approval_rejection(operation_id)
        return {
            "operation_id": operation_id,
            "state": "uncertain" if "unknown" in phase else "pending",
            "phase": phase, "remote_draft_id": operation.get("remote_draft_id"),
            "reason": "base_mismatch" if phase == "base_conflict" else None,
            "retryable_with_fresh_base": phase == "base_conflict",
            "retry_safe": phase in {"intent", "objects_uploaded"},
        }

    def enroll(
        self, skill_name: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Discover exact publication proof, then register its unchanged source."""

        _current(may_apply)
        owner = self._owner()
        source = self.reviewer.source(skill_name)
        source_hash = source_fingerprint(source)
        _current(may_apply)
        if self._owner() != owner:
            raise WisdomAuthError("Collective Wisdom publication owner changed")
        matches = self.state.published_for_source(owner, source, source_hash)
        if not matches:
            raise WisdomNotFound(
                f"No retained published proof matches current local skill '{skill_name}'"
            )
        if len(matches) != 1:
            raise WisdomConflict(
                f"Multiple retained publications match current local skill '{skill_name}'"
            )
        operation = matches[0]
        operation_id = str(operation["id"])
        if self.enrollment is None:
            raise WisdomConflict("Publisher managed enrollment is unavailable")
        linked = self.enrollment.link_existing(operation, may_apply=may_apply)
        return self._done(self.state.update_publication(
            operation_id, managed_enrollment=linked,
        ))

    def _reconcile_completed(
        self, operation_id: str, operation: dict[str, Any],
        may_apply: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        """Reconcile a completed operation without replaying its remote effect."""

        if not operation.get("remote_draft_id"):
            return self._done(operation)
        remote_state = str(_value(self._remote(operation, may_apply), "state"))
        if remote_state == operation.get("publication_state"):
            return self._done(operation)
        if remote_state == "published":
            return self._finish_reconciled_published(operation_id, may_apply)
        return self._finish(operation_id, remote_state)

    def reopen(  # pylint: disable=too-many-return-statements
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        operation = self._operation(operation_id)
        lock_key = "publication:" + operation["skill_id"]
        with managed_operation_guard(self.state.root, lock_key):
            operation = self._operation(operation_id)
            if operation["status"] == "done":
                return self._reconcile_completed(operation_id, operation, may_apply)
            if operation["phase"] in {"approval_unknown", "publication_unknown", "decline_unknown"}:
                remote = self._remote(operation, may_apply)
                remote_state = str(_value(remote, "state"))
                if remote_state in self.TERMINAL:
                    if remote_state == "published":
                        return self._finish_reconciled_published(operation_id, may_apply)
                    return self._finish(operation_id, remote_state)
                if operation["phase"] == "approval_unknown" and remote_state == "owner_approved":
                    self.state.update_publication(
                        operation_id, phase="approved", remote_state=remote_state
                    )
                elif operation["phase"] == "approval_unknown" and remote_state == "ready":
                    if _value(remote, "scanVerdict") == "blocked":
                        self.state.update_publication(
                            operation_id, phase="approval_rejected",
                            remote_state=remote_state,
                            remote_revision=str(_value(remote, "updatedAt")),
                            rejection_code="scan_blocked",
                        )
                        return self._approval_rejection(operation_id)
                    self.state.update_publication(
                        operation_id, phase="policy_review_required",
                        resume_phase="submitted", remote_state=remote_state,
                        remote_revision=str(_value(remote, "updatedAt")),
                    )
                    return self.status(operation_id)
                else:
                    return self._uncertain(
                        operation_id, operation["phase"],
                        f"Gateway draft is {remote_state}; nothing was retried.",
                    )
            if operation["phase"] == "revision_unknown":
                return self._drive(operation_id, may_apply=may_apply, reconcile=True)
            if operation["phase"] == "base_conflict":
                self.state.update_publication(operation_id, phase="approved")
            return self._drive(operation_id, may_apply=may_apply, reconcile=True)

    def decline(
        self, operation_id: str, *, may_apply: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        operation = self._operation(operation_id)
        with managed_operation_guard(self.state.root, "publication:" + operation["skill_id"]):
            operation = self._operation(operation_id)
            remote = self._remote(operation, may_apply)
            state = str(_value(remote, "state"))
            if state == "declined":
                return self._finish(operation_id, state)
            if state == "published":
                raise WisdomConflict("Published content cannot be declined")
            self.state.update_publication(operation_id, phase="decline_unknown")
            try:
                result = self.client.decline(operation["remote_draft_id"])
            except Exception:
                return self._uncertain(
                    operation_id, "decline_unknown",
                    "Decline acknowledgement was lost; reconcile before any other action.",
                )
            if str((result or {}).get("state")) != "declined":
                raise WisdomValidationError("Gateway returned an invalid decline state")
            return self._finish(operation_id, "declined")

    def for_candidate(self, assessment_id: str) -> dict[str, Any] | None:
        return self.state.publication_for_candidate(assessment_id)
