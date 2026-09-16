# CW-M5 client-driven publication versioning

## Scope and dependencies

This change covers the publication-versioning portion of **CW-M5-003**, **CW-M5-004**, and **CW-M5-005**. Their tracker dependencies remain **CW-M5-001**, **CW-M5-002**, and **CW-M2-001**; the durable recovery behavior also relies on the existing **CW-M7-004** operation journal and owner/session lock fences.

The client now reads the active organization's exact `refs/org/<org-id>/HEAD` through `GET /v1/sync/org/refs` with publish-scoped credentials before every known-safe publish attempt. An absent exact HEAD is the only empty-catalog result represented as `null`; malformed responses, duplicate/foreign refs, failed reads, and invalid hashes fail closed. `publish` requires an explicit `base_commit` argument.

This is a **Gateway-first rollout dependency**. The pre-change org-refs route is
restricted to `tool_gateway_admin` and therefore returns 403 to an ordinary
publisher. [Gateway PR #283](https://github.com/NousResearch/gateway-gateway/pull/283)
must first admit `wisdom:publish`
callers to this read while returning only their exact organization HEAD in the
existing refs shape. Until that Gateway revision is deployed, the plugin fails
closed on 403; it intentionally has no fallback route or inferred base.

The publication controller preserves the same local draft, content hash, approval, and operation while retrying only a server-confirmed `base_mismatch`, at most three times per explicit submit/reopen drive. It records the durable `base_conflict` phase and reconciles the authoritative draft before retrying uncertain or old `server_pending` operations. Terminal publication/moderation/decline states win; an unknown acknowledgement without confirmed conflict evidence is never blindly repeated.

The legacy bare `202 state=pending_approve_conflict` response is preserved as
pending but is **not** sufficient retry authority: the old Gateway used that
state for failures other than base contention. Automatic conflict recovery
requires either the structured response fields `reason=base_mismatch` and
`retryable_with_fresh_base=true`, or the same evidence from the authoritative
draft `publication` projection. Old `server_pending` records likewise require
the new projection before recovery.

## Automated verification

Automated checks are local and credential-free. They cover non-empty and truly empty org HEAD, malformed/foreign/failed ref reads, explicit publish bases, bounded repeated contention, process reopen, lost acknowledgements, legacy stranded state, terminal reconciliation, and denial of generic conflict/network retry.

Verified from the isolated worktree based on `238ce20`:

- `PYTHONPATH=$PWD python -m pylint hermes_wisdom_plugin tests scripts/test_wisdom_offline_acceptance.py` — passing, 10.00/10.
- `PYTHONPATH=$PWD python -m pytest -q` — 1,066 passed, 20 skipped.
- `PYTHONPATH=$PWD python -m build --outdir /tmp/cw-versioning-build` — sdist and wheel built successfully.

Local cross-repository HTTP verification against Gateway `467e22a`, disposable
PostgreSQL and MinIO passed: empty HEAD, existing HEAD, contention, lost conflict
acknowledgement, and lost success acknowledgement. The five cases produced five
drafts, seven proposals, and exactly five published versions. This does not
replace installed-profile or production acceptance.

## Live verification

**Not run.** No production Gateway, Portal, installed profile, publication,
moderation action, flag, or endpoint was changed. Publications above used local
disposable fixtures only. Live acceptance remains a separate authorized gate
after the coordinating Gateway-first org-refs rollout is available.
