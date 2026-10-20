"""Validated Nous Portal links shared by terminal and native projections."""

from __future__ import annotations

import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def safe_portal_url(value: object) -> str | None:
    """Accept a bounded credential-free HTTP(S) URL without control characters."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    if any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return value


def portal_review_url(base_url: str, _organization_id: str, draft_id: str) -> str:
    """Build the authenticated Portal route for one owner-private draft."""

    return f"{base_url.rstrip('/')}/wisdom/review/{quote(draft_id, safe='')}"


def portal_skill_url(
    base_url: str, skill_id: str, *, org_slug: str | None, organization_id: str,
    version: int | None = None,
) -> str:
    """Build the canonical org skill route, or Portal's slug-resolving redirect route.

    ``/orgs/{slug}/wisdom/skills/{id}`` is the page itself. Without a verified slug,
    ``/wisdom/skills/{id}?org_id=...`` asks Portal to resolve the slug and redirect.
    """
    root = base_url.rstrip("/")
    skill = quote(skill_id, safe="")
    if org_slug:
        url = f"{root}/orgs/{quote(org_slug, safe='')}/wisdom/skills/{skill}"
        query = {}
    else:
        url = f"{root}/wisdom/skills/{skill}"
        query = {"org_id": organization_id}
    if version is not None:
        query["version"] = str(version)
    return url + ("?" + urlencode(query) if query else "")


def portal_version_url(base_url: str | None, version: int) -> str | None:
    """Add an immutable version selector to an already validated Portal URL."""
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["version"] = str(version)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
