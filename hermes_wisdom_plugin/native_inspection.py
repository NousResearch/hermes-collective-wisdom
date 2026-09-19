"""Lossless, bounded projections of exact plans; never alter consent objects."""
import html
import json

from .professionalism import CHECK_LABELS

SECURITY_LABELS = {
    "private_keys": "Private keys", "live_credentials": "Live credentials",
    "secret_assignments": "Secret-like assignments",
    "skills_guard": "Harmful instruction patterns",
    "organization_policy": "Organization policy", "personal_information": "Personal information",
}


def _passed(check, labels):
    rows = check.get("checks") or []
    return (
        check.get("status") == "pass" and len(rows) == len(labels)
        and {row.get("key") for row in rows} == set(labels)
        and all(row.get("status") == "pass" and row.get("finding_count") == 0
                and not row.get("findings") for row in rows)
    )


def sharing_checks(plan):
    """Claim all-passed only for every required check and current assessment hashes."""
    security = plan.get("security_check") or {}
    professional = plan.get("professionalism_check") or {}
    hashes = plan.get("hashes") or {}
    safe = bool(
        hashes.get("content") and hashes.get("author_description")
        and professional.get("content_hash") == hashes["content"]
        and professional.get("author_description_hash") == hashes["author_description"]
        and _passed(security, SECURITY_LABELS) and _passed(professional, CHECK_LABELS)
    )
    lines = []
    for check, labels in ((security, SECURITY_LABELS), (professional, CHECK_LABELS)):
        statuses = {row.get("key"): row.get("status") for row in check.get("checks") or []}
        for key, label in labels.items():
            status = statuses.get(key, "unavailable")
            if status not in {
                "pass", "pending", "unavailable", "blocked", "advisory", "warning_required",
            }:
                status = "unavailable"
            lines.append(f"  {label}: {status}")
    return safe, "\n".join(lines)


def review_pages(plan):
    """Split canonical JSON at character boundaries within both native encodings.

    JSON escapes control characters so multiline and binary/base64 file content
    stays inspectable. The conservative encoded budget leaves room for headings
    and controls in Slack sections and Telegram's plain fallback.
    """
    source = json.dumps(plan, ensure_ascii=True, sort_keys=True, indent=2)
    pages = []
    start = 0
    cost = 0
    for index, character in enumerate(source):
        size = len(html.escape(character))
        if cost + size > 2200:
            pages.append(source[start:index])
            start = index
            cost = 0
        cost += size
    pages.append(source[start:])
    return pages
