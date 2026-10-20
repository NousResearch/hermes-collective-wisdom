"""One-shot terminal adapter over the existing exact-hash publication workflow."""
from __future__ import annotations

import base64
import json
import shlex
import sys

from .errors import ShareCopyError, WisdomAuthError, WisdomError, WisdomNotFound, WisdomConflict
from .package import PackagePolicyError
from .portal_urls import safe_portal_url
from .terminal_ui import (
    edit_prepared, has_reviewable_warnings, render_file_preview, render_findings,
    render_inventory, review_identity, safe_scan_details, text,
)

_AWAITING_REVIEW = frozenset({"policy_review_required", "approval_rejected"})


def _result(outcome, skill, detail, version=None, portal_url=None):
    return {"outcome": outcome, "skill": skill, "version": version,
            "portal_url": safe_portal_url(portal_url), "detail": detail}


def _catalog(service, skill):
    try:
        return service.show(skill)
    except WisdomNotFound:
        return {}


def _latest(detail):
    return (detail.get("latest_version_detail") or {}).get("version") or {}


def _already_shared(prepared, detail, owner):
    skill = detail.get("skill") or {}
    latest = _latest(detail)
    hashes = prepared["hashes"]
    return (
        skill.get("state") == "active"
        and skill.get("created_by_user_id") == owner["account_id"]
        and isinstance(latest.get("version"), int)
        and all(latest.get(remote) == hashes[local] for remote, local in (
            ("content_hash", "content"), ("author_description_hash", "author_description"),
            ("package_manifest_hash", "package_manifest"),
        ))
    )


def _screen(prepared, owner, detail, mode):
    team = text(owner["organization_id"])
    title = text(prepared.get("editorial_name") or prepared.get("slug") or "Local skill")
    version = _latest(detail).get("version")
    version_line = f"v{version} → v{version + 1}" if isinstance(version, int) else "New skill (v1)"
    visibility = (f"Everyone in {team}, immediately. No reviewer." if mode == "open"
                  else "Team review required; not immediately available.")
    print(f'Share "{title}"\nTeam        {team}\nVisibility  {visibility}'
          f'\nVersion     {version_line}')
    print(text(prepared.get("description") or "", limit=4096))
    if prepared.get("copy_provenance_line"):
        print(text(prepared["copy_provenance_line"]))
        labels = {"editorial_name": "name", "editorial_description": "description"}
        original = prepared.get("copy_original") or {}
        for key in prepared.get("copy_written_by_hermes") or []:
            if key in labels:
                print(f"Original {labels[key]} in SKILL.md: "
                      + text(original.get(key) or "(missing)", limit=4096))
    inventory = {**prepared, "files": [
        {**item, "size": len(base64.b64decode(item["content_base64"], validate=True))}
        for item in prepared["files"]
    ]}
    print(render_inventory(inventory).replace("[executable]", "[script]"))
    print(render_findings(prepared))
    print("Server scan runs on submit. Nothing has been uploaded.")


def _warnings(args, prepared):
    if not has_reviewable_warnings(prepared):
        return False
    if not safe_scan_details(prepared.get("local_scan")):
        raise WisdomConflict("Scanner warning details are unavailable; sharing is blocked")
    if args.accept_warnings:
        return True
    if args.json or args.yes or not sys.stdin.isatty():
        return None
    print(render_findings(prepared))
    answer = input("Acknowledge the displayed scanner warnings for this exact package? [y/N] ")
    return True if answer.strip().lower() in {"y", "yes", "a"} else None


def _view_file(prepared):
    print(render_inventory(prepared))
    answer = input("View file number (Enter to return): ").strip()
    if answer.isdigit() and 0 < int(answer) <= len(prepared["files"]):
        print(render_file_preview(prepared["files"][int(answer) - 1]))


def _edit_description(service, draft_id):
    payload = service.prepared_inspect(draft_id)
    description = input("Description (Enter to keep current): ").strip()
    if description:
        service.prepared_save(
            draft_id, author_description=description, files=payload["files"],
            expected_hashes=payload["expected_hashes"],
        )


def _navigate(action, service, review):
    if action == "v":
        _view_file(review)
    if action == "e":
        _edit_description(service, review["draft_id"])
    if action == "f":
        edit_prepared(service, review["draft_id"])


def _review(args, service, prepared, owner, detail):
    """Warn separately, then present exactly the receipt that Share will consume.

    An operation awaiting review is re-reviewed by its id; its submitted local
    draft is immutable, so only viewing files is offered.
    """
    draft_id = prepared["draft_id"]
    operation_id = prepared.get("operation_id")
    target = operation_id or draft_id
    while True:
        accepted = _warnings(args, prepared)
        if accepted is None:
            return None, "warnings_required"
        review = service.publication_review(target, accept_warnings=accepted)
        if review_identity(prepared) != review_identity(review):
            raise WisdomConflict("Package or scanner findings changed; review again")
        if not args.json:
            _screen(review, owner, detail, review["publication_mode"])
        if args.yes:
            return review, None
        if args.json or not sys.stdin.isatty():
            return None, "confirmation_required"
        action = input(("[v] View a file   " if operation_id else
                        "[v] View a file   [e] Edit description   [f] Edit files   ")
                       + "[s] Share   [q] Cancel\n> ").strip().lower()
        if action in {"", "q"}:
            return None, "cancelled"
        if action == "s":
            return review, None
        if operation_id:
            if action == "v":
                _view_file(review)
            prepared = review
            continue
        _navigate(action, service, review)
        # No warning acceptance survives editing or a new review.
        args.accept_warnings = False
        prepared = service.prepared_review(draft_id, allow_model_review=False)


def _outcome(result, skill, team):
    operation = shlex.quote(text(result.get("operation_id") or skill))
    if result.get("code") == "scan_blocked":
        return _result("blocked_by_scan", skill,
                       "Not shared. The server scan blocked this package. "
                       "Review the findings with: hermes wisdom publication status " + operation), 1
    if result.get("code") == "slug_owned":
        return _result("slug_owned", skill,
                       f'"{text(skill)}" is already shared by another author. '
                       "Rename your skill."), 1
    if (result.get("state"), result.get("publication_state")) == ("done", "published"):
        publication = result.get("publication_result") or {}
        version = publication.get("version")
        label = f" as v{version}" if isinstance(version, int) else ""
        return _result("shared", skill, f"Shared with {team}{label}", version), 0
    if (result.get("state"), result.get("publication_state")) == ("done", "pending_moderation"):
        return _result("pending_moderation", skill,
                       "Submitted for team review; not shared yet."), 0
    return _result("uncertain", skill, "We could not confirm the result. Nothing will be resent. "
                   "Check with: hermes wisdom publication status " + operation), 2


def _submit(service, receipt, skill):
    try:
        return service.publication_submit(receipt)
    except WisdomError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        return {"state": "uncertain", "operation_id": skill}


def _share(args, service):
    owner = service.binding()
    pending = service.share_status(args.skill)
    operation_id = None
    if pending is not None:
        if pending.get("phase") not in _AWAITING_REVIEW or not pending.get("operation_id"):
            return _outcome(pending, args.skill, text(owner["organization_id"]))
        # Finish the earlier share's retained operation; never prepare a duplicate.
        operation_id = str(pending["operation_id"])
        prepared = service.publication_review(operation_id)
        if prepared.get("operation_id") != operation_id:
            raise WisdomConflict("The earlier share changed; check publication status")
    else:
        prepared = service.suggest(args.skill, allow_model_review=False)
        if prepared.get("editorial_problem"):
            # The skill's own name or description cannot be shared as written.
            return _result("copy_incomplete", args.skill,
                           text(prepared["editorial_problem"], limit=4096)), 1
    prepared = {**prepared, "slug": args.skill}
    detail = _catalog(service, args.skill)
    if operation_id is None and _already_shared(prepared, detail, owner):
        version = _latest(detail)["version"]
        return _result("already_shared", args.skill,
                       f"Nothing to share: v{version} already matches your local skill",
                       version, detail.get("portal_url")), 0
    review, stop = _review(args, service, prepared, owner, detail)
    if stop:
        messages = {
            "cancelled": "Sharing cancelled. Nothing has been uploaded.",
            "warnings_required": "Not shared. Review the warnings, then use --accept-warnings.",
            "confirmation_required": "Nothing shared. To confirm, run: hermes wisdom share "
                                     + shlex.quote(args.skill) + " --yes",
        }
        return _result(stop, args.skill, messages[stop]), 0 if stop == "cancelled" else 1
    assert review is not None
    result = _submit(service, review["receipt"], args.skill)
    outcome, code = _outcome(result, args.skill, text(owner["organization_id"]))
    if outcome["outcome"] == "shared":
        # Publication success is independent of optional post-submit link discovery.
        try:
            published = _catalog(service, args.skill)
            outcome["portal_url"] = safe_portal_url(published.get("portal_url"))
        except Exception:  # pylint: disable=broad-exception-caught
            # Publication is already authoritative; link discovery is best effort.
            pass
    return outcome, code


def _emit_result(args, result):
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
        return
    print(result["detail"])
    if result["portal_url"]:
        print("Portal   " + result["portal_url"])
    if result["outcome"] == "shared":
        name = shlex.quote(text(args.skill))
        print(f"Install  hermes wisdom install {name}")


def run_share(args, service):
    """Print one stable JSON envelope or the human screen/outcome; never auto-retry."""
    try:
        result, code = _share(args, service)
    except WisdomAuthError:
        result, code = _result("not_entitled", args.skill,
                              "Collective Wisdom is not enabled for this account and team"), 1
    except ShareCopyError as exc:
        # The skill's own copy cannot be shared and Hermes could not propose it.
        result, code = _result("copy_incomplete", args.skill, text(exc, limit=4096)), 1
    except WisdomError as exc:
        if exc.code in {"slug_owned", "scan_blocked"}:
            result, code = _outcome({"code": exc.code}, args.skill, "")
        else:
            result, code = _result("not_shared", args.skill, text(exc, limit=4096)), 1
    except PackagePolicyError as exc:
        result, code = _result("invalid_package", args.skill,
                              f"Can't share {text(args.skill)}: {text(exc)}. "
                              "Shared skills can contain text and scripts only."), 1
    except (KeyboardInterrupt, EOFError):
        result, code = _result("cancelled", args.skill, "Sharing cancelled."), 1
    except Exception:  # pylint: disable=broad-exception-caught
        result, code = _result(
            "not_shared", args.skill,
            "Sharing could not complete. Inspect hermes wisdom status before trying again.",
        ), 1
    _emit_result(args, result)
    return code
