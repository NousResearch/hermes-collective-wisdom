"""Pinned model-facing Collective Wisdom read and native-consent tools.

The model may inspect owner-private state or ask the native surface to present an
exact review.  It cannot install, update, publish, or execute setup directly.
"""

from __future__ import annotations

import json
from typing import Any


_TOOLSET = "skills"


def _schema(
    name: str, description: str, properties: dict[str, Any], required=()
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required),
        },
    }


_SCHEMAS = {
    "wisdom_inbox": _schema(
        "wisdom_inbox",
        "Read this admitted private conversation's owner-bound Wisdom assessments, "
        "pending native reviews, and installed setup references without changing them.",
        {},
    ),
    "wisdom_inspect": _schema(
        "wisdom_inspect",
        "Inspect bounded Collective Wisdom skill/version or owner-private candidate "
        "metadata and local overlap. Inspection never executes setup or grants consent.",
        {
            "kind": {
                "type": "string",
                "enum": ["candidate", "skill", "installed"],
            },
            "identity": {"type": "string", "minLength": 1, "maxLength": 128},
            "version": {"type": ["integer", "null"], "minimum": 1},
        },
        ("kind", "identity"),
    ),
    "present_wisdom_consent": _schema(
        "present_wisdom_consent",
        "Queue native, exact-package Wisdom consent in the current private conversation. "
        "Queued is not displayed; the model cannot approve or execute it. "
        "For skill invitations, explain what it does and relevance using only the user's "
        "known conversation context and inspected skill facts. If relevance is unknown, "
        "say so; never invent workload, organization names or passing checks. "
        "Explanation is labeled advice, never authorization.",
        {
            "kind": {"type": "string", "enum": ["candidate", "skill", "setup"]},
            "identity": {"type": "string", "minLength": 1, "maxLength": 128},
            "version": {"type": ["integer", "null"], "minimum": 1},
            "step": {
                "anyOf": [
                    {
                        "type": "object", "additionalProperties": False,
                        "required": ["phase"],
                        "properties": {
                            "phase": {
                                "type": "string",
                                "enum": ["prerequisite", "setup", "verify"],
                            },
                            "index": {"type": "integer", "minimum": 0, "maximum": 39},
                            "command": {"type": "string", "maxLength": 1200},
                        },
                    },
                    {"type": "null"},
                ]
            },
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "explanation": {"type": "string", "minLength": 1, "maxLength": 1200},
        },
        ("kind", "identity", "title", "explanation"),
    ),
}


class NativeModelTools:
    """Bind the historical tool contract to this plugin runtime."""

    def __init__(self, command_runtime: Any) -> None:
        self.runtime = command_runtime

    def available(self) -> bool:
        """Process reachability only; per-session authorization happens in handlers."""
        return not bool(getattr(self.runtime, "_closed", True))

    def _invocation(self, kwargs: dict[str, Any]):
        invocation = self.runtime.native.tool_invocation(
            task_id=str(kwargs.get("task_id") or ""),
            session_id=str(kwargs.get("session_id") or ""),
        )
        if invocation is None:
            raise PermissionError(
                "Collective Wisdom model tools require a current admitted private turn."
            )
        return invocation

    def inbox(self, _args: dict[str, Any], **kwargs: Any) -> str:
        """Read pending private work for the admitted route."""
        invocation = self._invocation(kwargs)
        service = self._service(invocation)
        try:
            return json.dumps(service.inbox(invocation=invocation), sort_keys=True)
        finally:
            service.close()

    def inspect(self, args: dict[str, Any], **kwargs: Any) -> str:
        """Read one exact private object without changing it."""
        invocation = self._invocation(kwargs)
        kind = str(args.get("kind") or "")
        identity = str(args.get("identity") or "")
        service = self._service(invocation)
        try:
            if kind == "candidate":
                result = service.native_candidate_inspect(identity)
            elif kind == "skill":
                result = service.native_skill_inspect(identity, version=args.get("version"))
            elif kind == "installed":
                installed = service.list_installations()
                result = {
                    "installed": [row for row in installed if row["skill_id"] == identity],
                    "setup": service.installed_setup_inspect(
                        identity, version=args.get("version")
                    ),
                }
            else:
                raise ValueError("Collective Wisdom inspection kind was not recognized.")
            return json.dumps(result, sort_keys=True)
        finally:
            service.close()

    def _service(self, invocation: Any):
        service = self.runtime._service()  # pylint: disable=protected-access
        try:
            self.runtime.native.validate_tool_owner(invocation, service)
            return service
        except Exception:
            service.close()
            raise

    def present(self, args: dict[str, Any], **kwargs: Any) -> str:
        """Queue a native exact review without approving its action."""
        invocation = self._invocation(kwargs)
        if invocation.platform not in {"slack", "telegram"}:
            raise PermissionError("Collective Wisdom native renderer is unavailable.")
        service = self._service(invocation)
        try:
            expected_owner = service.native_owner()
        finally:
            service.close()
        delivery = self.runtime.native.queue_model_consent(
            invocation, args, expected_owner=expected_owner,
        )
        if delivery.get("state") != "queued":
            raise PermissionError("Collective Wisdom native review could not be queued.")
        return json.dumps({
            "status": "queued",
            "delivery": delivery,
            "executed": False,
            "instruction": (
                "Native card delivery is queued for after this turn; it has not been displayed "
                "yet. A conversational yes does not apply this operation."
            ),
        }, sort_keys=True)


def register_native_tools(context: Any, command_runtime: Any) -> list[Any]:
    """Register all three exact historical tool names through the host API."""
    tools = NativeModelTools(command_runtime)
    handlers = {
        "wisdom_inbox": tools.inbox,
        "wisdom_inspect": tools.inspect,
        "present_wisdom_consent": tools.present,
    }
    return [
        context.register_tool(
            name=name, schema=_SCHEMAS[name], toolset=_TOOLSET, handler=handler,
            check_fn=tools.available, is_async=False,
        )
        for name, handler in handlers.items()
    ]
