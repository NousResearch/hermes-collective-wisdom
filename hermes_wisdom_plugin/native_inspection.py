"""Lossless, bounded projections of exact plans; never alter consent objects."""
import html
import json
from datetime import datetime, timezone

from .professionalism import CHECK_LABELS
from .local_security import safe_scan_findings
from .release_scope import active as release_scope

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
    server = plan.get("server_checks") or {}
    gateway = server.get("security_check") or server.get("scan")
    gateway_safe = (gateway.get("status") in {"pass", "passed"} if gateway else
                    plan.get("kind") != "candidate" and plan.get("candidate_flow") is not True)
    safe = bool(
        hashes.get("content") and hashes.get("author_description")
        and professional.get("content_hash") == hashes["content"]
        and professional.get("author_description_hash") == hashes["author_description"]
        and _passed(security, SECURITY_LABELS) and _passed(professional, CHECK_LABELS)
        and gateway_safe
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
            lines.append(f"  {_status_icon(status)} {label}: {status.replace('_', ' ')}")
    if gateway:
        status = gateway.get('status') or 'unavailable'
        lines.append(f"  {_status_icon(status)} Gateway security: {status}")
    elif plan.get("kind") == "candidate" or plan.get("candidate_flow") is True:
        lines.append("  ⏳ Gateway security: pending until submission")
    return safe, "\n".join(lines)

def _status_icon(status):
    return {"pass": "✅", "passed": "✅", "pending": "⏳",
            "pending until submission": "⏳", "warning_required": "⚠️",
            "advisory": "⚠️", "blocked": "❌", "unavailable": "⚠️"}.get(status, "⚠️")

def _date(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        if not isinstance(value, str):
            return "Unavailable"
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M %Z")
    except (TypeError, ValueError, OverflowError):
        return "Unavailable"

_HIDDEN = {"receipt", "draft_id", "local_draft_id", "skill_id", "event_id",
           "account_id", "organization_id", "profile_home", "base_url", "route",
           "owner", "hashes", "content_hash", "manifest_hash", "published_by_user_id",
           "installation_id", "review_expires_at", "allowed", "operation", "candidate_flow"}
_LABELS = {**SECURITY_LABELS, **CHECK_LABELS, "editorial_name": "Skill name",
           "editorial_description": "What it does", "coworker_benefit": "Why teammates benefit",
           "slug": "Publication name", "publication_mode": "Publication policy",
           "authorization_expires_at": "Authorization expires", "security_check": "Local security",
           "professionalism_check": "Professionalism", "local_scan": "Local scanner",
           "server_checks": "Gateway checks", "package_files": "Verified package files"}

def _readable(value):
    if value is None:
        return "Unavailable"
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "".join(f"\\u{ord(char):04x}" if ord(char) < 32 or ord(char) == 127
                   else char for char in str(value))

def _detail_lines(value, label=""):
    """Present metadata as labelled facts; file bodies alone use reversible quoting."""
    if isinstance(value, dict):
        if isinstance(value.get("key"), str) and "status" in value:
            label = _LABELS.get(value["key"]) or value["key"].replace("_", " ").capitalize()
        lines = [f"• {label}"] if label else []
        for key, child in value.items():
            if key in _HIDDEN or (key == "key" and "status" in value):
                continue
            name = _LABELS.get(key) or key.replace("_", " ").capitalize()
            if key == "checks" and isinstance(child, list):
                lines.extend(_detail_lines(child, "Checks"))
            elif key in {"content", "content_utf8", "content_base64"}:
                lines.append(f"  • {name} (exact escaped bytes): "
                             + json.dumps(child, ensure_ascii=True))
            elif key.endswith("expires_at"):
                lines.append(f"  ⏳ {name}: {_date(child)}")
            else:
                lines.extend(_detail_lines(child, name))
        return lines
    if isinstance(value, list):
        lines = [f"• {label}: {len(value)} entries"]
        for index, row in enumerate(value, 1):
            lines.extend(_detail_lines(row, f"{label} {index}"))
        return lines
    status = str(value).lower() if label.lower() == "status" else ""
    return [f"  {_status_icon(status) if status else '•'} {label}: {_readable(value)}"]

def _pages(text, budget=1200):
    pages, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(html.escape(current + line)) > budget:
            pages.append(current)
            current = ""
        if len(html.escape(line)) > budget:
            for character in line:
                if current and len(html.escape(current + character)) > budget:
                    pages.append(current)
                    current = ""
                current += character
        else:
            current += line
    return pages + [current.rstrip("\n")] if current or not pages else pages

def _finding_lines(finding):
    """Render safe scanner facts, never raw matched content or dictionary repr."""
    if not isinstance(finding, dict):
        return [f"  ℹ️ {_readable(finding)}"]
    safe = safe_scan_findings({"findings": [{
        **finding, "description": finding.get("description") or finding.get("detail")
        or finding.get("message"),
    }]})[0]
    lines = [f"  ⚠️ {label}: {_readable(safe[key])}" for key, label in (
        ("severity", "Severity"), ("file", "File"), ("line", "Line"),
        ("category", "Category"), ("description", "Finding"),
    ) if safe.get(key)]
    return lines or ["  ⚠️ Finding details unavailable"]


def _check_group_rows(check, labels, group):
    """Keep each known check's verdict and actual adverse evidence together."""
    lines = []
    rows = {row.get("key"): row for row in check.get("checks") or []
            if isinstance(row, dict)}
    for key, label in labels.items():
        row = rows.get(key) or {}
        status = row.get("status", "unavailable")
        if status == "pass" and not row.get("findings") and not row.get("finding_count"):
            result = "No issues detected"
        elif status == "pending":
            result = ("Checked by Gateway after authorized submission"
                      if group == "security_check" else "Pending review")
        else:
            result = str(status).replace("_", " ")
        lines.append(f"{_status_icon(status)} {label}: {result}")
        if row.get("finding_count"):
            lines.append(f"  ⚠️ {row['finding_count']} finding(s) reported")
        if status != "pass" or row.get("finding_count") or row.get("findings"):
            for detail in (row.get("findings") or row.get("details") or []):
                lines.extend(_finding_lines(detail))
    for row in (row for key, row in rows.items() if key not in labels):
        lines.append(f"⚠️ Additional check {_readable(row.get('key') or 'unnamed')}: "
                     f"{_readable(row.get('status') or 'unavailable')}")
        for detail in (row.get("findings") or row.get("details") or []):
            lines.extend(_finding_lines(detail))
    if check.get("status") not in {"pass", "pending"}:
        lines.append(f"⚠️ {group.replace('_', ' ').capitalize()}: "
                     f"{_readable(check.get('summary') or check.get('status'))}")
    return lines

def _human_check_rows(plan):
    """Project the existing named check vocabulary once, retaining adverse evidence."""
    lines = []
    for group, labels in (("security_check", SECURITY_LABELS),
                          ("professionalism_check", CHECK_LABELS)):
        lines.extend(_check_group_rows(plan.get(group) or {}, labels, group))
    security = plan.get("security_check") or {}
    if security.get("status") == "pending":
        lines.append("⏳ Gateway security: Checked after authorized submission, "
                     "before publication")
    return lines

def _candidate_lines(plan):
    facts = plan.get("facts") or {}
    editorial = facts.get("editorial") or {}
    lines = [
        "ℹ️ Skill name: " + _readable(editorial.get('editorial_name') or facts.get('name')
                                      or 'Name unavailable'),
        "ℹ️ What it does: " + _readable(editorial.get('editorial_description')
                                       or 'Description not yet available'),
        "ℹ️ Why teammates benefit: " + _readable(editorial.get('coworker_benefit')
                                                or 'Benefit not yet available'),
        "ℹ️ Why suggested: Frequent use" if facts.get("qualification") == "high_usage" else
        "ℹ️ Why suggested: Local skill activity",
        "✅ Draft preparation: Complete" if facts.get("editorial_status") == "complete" else
        "⚠️ Draft preparation: Incomplete",
        "ℹ️ Nothing has been uploaded or shared. This is a local candidate.",
    ]
    if facts.get("editorial_status") != "complete" and facts.get("editorial_failure"):
        lines.append("⚠️ Preparation needs attention: "
                     + _readable(facts['editorial_failure']).replace('_', ' '))
    return lines + _human_check_rows(plan)

def _copy_origin_lines(plan):
    """Which shown copy Hermes wrote, and the skill's own original wording."""
    line = plan.get("copy_provenance_line")
    if not line:
        return []
    labels = {"editorial_name": "name", "editorial_description": "description"}
    original = plan.get("copy_original") if isinstance(plan.get("copy_original"), dict) else {}
    return ["ℹ️ " + _readable(line)] + [
        f"ℹ️ Original {label} in SKILL.md: "
        + _readable(original.get(key) or "(missing)")
        for key, label in labels.items() if key in (plan.get("copy_written_by_hermes") or [])
    ]


def _sharing_lines(plan):
    benefit = plan.get('coworker_benefit')
    lines = [
        "ℹ️ Skill name: " + _readable(plan.get('editorial_name') or plan.get('slug')
                                      or 'Name unavailable'),
        "ℹ️ What it does: " + _readable(plan.get('editorial_description')
                                       or 'Description unavailable'),
        *(["ℹ️ Why teammates benefit: " + _readable(benefit or 'Benefit unavailable')]
          if benefit or release_scope().model_editorial else []),
        f"ℹ️ Publication name: {_readable(plan.get('slug') or 'Unavailable')}",
        *_copy_origin_lines(plan),
        "ℹ️ Nothing has been uploaded or shared by viewing these details.",
    ]
    lines.extend(_human_check_rows(plan))
    scan = plan.get("local_scan")
    if isinstance(scan, dict):
        findings = scan.get("findings")
        lines.append("Local scanner: " + (
            "allowed" if scan.get("allowed") is True else "needs review"
        ) + f"; findings: {len(findings) if isinstance(findings, list) else 'unavailable'}")
        if isinstance(findings, list):
            for finding in findings:
                lines.extend(_finding_lines(finding))
    for item in plan.get("files") or []:
        if not isinstance(item, dict):
            continue
        lines.append(f"📄 File: {_readable(item.get('path') or 'Path unavailable')}")
        if item.get("mode") is not None:
            lines.append(f"  Mode: {_readable(item['mode'])}" +
                         (" (executable)" if item["mode"] == "exec" else ""))
        for key in ("content_utf8", "content", "content_base64"):
            if key in item:
                lines.append("  Exact escaped file content: "
                             + json.dumps(item[key], ensure_ascii=True))
                break
    return lines


def inspection_pages(record):
    """Keep separately verified file bytes outside the immutable consent plan."""
    plan = record["plan"]
    if record.get("operation") == "candidate":
        return _pages("\n".join(_candidate_lines(plan)))
    if record.get("operation") == "publish":
        return _pages("\n".join(_sharing_lines(plan)))
    if "owner" in plan and isinstance(record.get("owner"), dict):
        plan = {**plan, "owner": record["owner"]}
    if "package_files" in record:
        plan = {"review": plan, "package_files": record["package_files"]}
    return review_pages(plan)


def review_pages(plan):
    """Render review details while retaining complete exact-file inspection."""
    return _pages("\n".join(_detail_lines(plan)))
