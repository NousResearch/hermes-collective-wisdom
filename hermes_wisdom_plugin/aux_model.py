"""One bounded structured call to the Hermes auxiliary model for Collective Wisdom.

Mirrors the plugin's existing ``complete_structured`` pattern (see
``qualification._resolve_ambiguous`` and ``professionalism``): untrusted inputs
are JSON data, the schema is fixed, and any error, timeout or malformed answer
returns ``None`` so callers fall back to their no-model path. Never retried here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

ASSIST_TASK = "hermes_collective_wisdom_assist"
logger = logging.getLogger(__name__)


def structured_call(  # pylint: disable=too-many-arguments
    llm: Any | None,
    *,
    instructions: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    schema_name: str,
    purpose: str,
    max_tokens: int,
    timeout: float,
    max_payload_bytes: int = 512 * 1024,
) -> dict[str, Any] | None:
    """Return the parsed object, or ``None`` when no model / error / timeout."""

    if llm is None or not callable(getattr(llm, "complete_structured", None)):
        return None
    try:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(serialized.encode("utf-8")) > max_payload_bytes:
        return None
    try:
        result = llm.complete_structured(
            instructions=instructions,
            input=[{"type": "text", "text": serialized}],
            json_schema=schema,
            json_mode=True,
            schema_name=schema_name,
            purpose=purpose,
            task=ASSIST_TASK,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        parsed = getattr(result, "parsed", None)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("Collective Wisdom auxiliary call unavailable", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["ASSIST_TASK", "structured_call"]
