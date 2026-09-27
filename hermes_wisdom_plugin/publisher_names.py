"""Bounded owner-scoped NAS publisher projection for exact Gateway versions."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests

from .errors import WisdomAuthError, WisdomValidationError


def _validated_references(references: list[dict[str, Any]]) -> list[tuple[str, int]]:
    if not 1 <= len(references) <= 8:
        raise WisdomValidationError("Publisher lookup requires 1-8 exact references")
    keys: list[tuple[str, int]] = []
    for ref in references:
        skill_id, version = ref.get("skill_id"), ref.get("version")
        if (  # pylint: disable=too-many-boolean-expressions
                not isinstance(skill_id, str) or not 1 <= len(skill_id) <= 128
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in skill_id)
                or not isinstance(version, int) or isinstance(version, bool)
                or version < 1 or version > 9007199254740991):
            raise WisdomValidationError("Publisher reference is invalid")
        keys.append((skill_id, version))
    if len(set(keys)) != len(keys):
        raise WisdomValidationError("Duplicate publisher reference")
    return keys


def _portal_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (  # pylint: disable=too-many-boolean-expressions
            parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise WisdomAuthError("Portal origin is unavailable")
    return origin


def _validated_names(
    rows: Any, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], dict[str, str | None]]:
    if not isinstance(rows, list) or len(rows) != len(keys):
        return {}
    result: dict[tuple[str, int], dict[str, str | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}
        key = (row.get("skill_id"), row.get("version"))
        publisher = row.get("published_by_user_id")
        name, team = row.get("publisher_display_name"), row.get("team_display_name")
        if (  # pylint: disable=too-many-boolean-expressions
                key not in keys or key in result or not isinstance(publisher, str)
                or not publisher or not isinstance(team, str)
                or not team or not (name is None or isinstance(name, str))):
            return {}
        result[key] = {"published_by_user_id": publisher,
                       "publisher_display_name": name.strip()[:200] or None if name else None,
                       "team_display_name": team.strip()[:200] or None}
    return result if len(result) == len(keys) else {}


def publisher_names(  # pylint: disable=too-many-locals,too-many-return-statements,too-many-branches
    account: Any, references: list[dict[str, Any]], *,
    session: requests.Session | None = None,
) -> dict[tuple[str, int], dict[str, str | None]]:
    """Resolve one batch; unavailable names never become publisher IDs or emails."""
    keys = _validated_references(references)
    origin = _portal_origin(account.portal_base_url)
    credentials = account.credentials()
    transport = session or requests.Session()
    try:
        response = transport.request(
            "POST", origin.rstrip("/") + "/api/v1/wisdom/publishers",
            headers={"Authorization": f"Bearer {credentials.access_token}",
                     "Accept": "application/json"},
            json={"references": [{"skill_id": skill, "version": version}
                                 for skill, version in keys]},
            timeout=(5, 12), stream=True, allow_redirects=False,
        )
        try:
            if response.status_code != 200:
                return {}
            if int(response.headers.get("Content-Length", "0")) > 4096:
                return {}
            payload = response.raw.read(4097)
            if len(payload) > 4096:
                return {}
            import json  # pylint: disable=import-outside-toplevel
            rows = json.loads(payload).get("publishers")
        finally:
            response.close()
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return {}
    return _validated_names(rows, keys)
