# Architecture

The [feature tracker](FEATURES.md) distinguishes implemented workflows from
planned features and records automated and live acceptance separately.

## Ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Hermes host | Existing caller admission/reply routing, credentials, session lifecycle, plugin loading and native surface access | Wisdom policy or special-case command logic |
| Python plugin | Local state, package preparation, scans, qualification, plans/receipts, recommendations, operation reconciliation | Server authorization or forged user consent |
| Surface adapters | Commands, native cards, callback translation, UI rendering | Independent copies of install/publish business logic |
| Gateway/Portal | Scoped remote access, content storage, publication policy, moderation, manager email | Local user setup approval or active-session guessing |

The native registration entrypoint delegates to the Python workflow, storage,
host-integration and surface adapters. Relative imports support native loading.

## Data Flow

```text
trusted user command / host lifecycle fact / server feed
    -> identity + entitlement + opt-in checks
    -> profile-scoped workflow service and durable state
    -> canonical package facts / plan / allowed actions
    -> optional advisory model explanation
    -> active user surface review
    -> authenticated callback or explicit local confirmation
    -> revalidate exact plan + actor + current authority
    -> apply / submit / defer / recheck
    -> durable outcome -> update existing surface
```

Periodic workers can collect/reconcile events but cannot manufacture a user
turn, select an arbitrary destination, or approve anything. Contextual delivery
waits for an eligible active root session and host lifecycle authorization.

## State and Recovery

Use `ctx.get_config` / `ctx.set_config` for namespaced non-secret settings.
Use the host's `ctx.state.data_dir` for the plugin's transactional SQLite state,
including candidates, leases, receipts, operations, feed cursors, suppressions,
and delivery references. The small JSON state facade is not a replacement for
transactional operation tables.

Bootstrap a fresh current schema. Do not import the reverted implementation's
databases, queued approvals, legacy callback forms, or migration backfills.
New-version schema upgrades can be designed when actually needed later.

Keep process/profile leases and durable next-run checkpoints. Existing
`spawn_task` owns cancellable async work; it requires a running host event loop.
Use the host lifecycle bridge rather than starting unmanaged threads at import
or assuming every CLI command has an event loop. Unload and account changes
stop work; restart resumes only after rechecking current gates.

Keep states for not-sent, confirmed-sent, and uncertain-send distinct. Persist
platform message references only with appropriate acknowledgement. Repeated
callbacks and worker recovery must converge on one recorded operation outcome.

The shared consumption slice binds its feed checkpoint to the exact account,
organization, installation, Gateway base route, and monotonic generation. Every
page is transactionally persisted before projection; replayed event IDs are
idempotent. Explicit maintenance clears a stale projection before its next request
and caches at most eight exact skill/version/publisher metadata records per pass.
Passive inbox and notification reads verify the stored owner and generation before
reading rows; they never rebind, poll, fetch metadata, run a model, deliver, or
mutate installations.

Explicit maintenance also owns two bounded producers. Mediation production persists at
most eight exact owner-bound feed/weekly references after current policy, mute, and
suppression checks. Owner-scoped queue membership is applied before the feed projection
bound, so retries reach later feed rows and qualified weekly evidence while preserving the
weekly policy cap and cooldown. Counts reflect atomic inserts; queueing does not alter native
seen/delivery state. It neither assesses nor delivers them. Automatic update production
uses a durable owner-bound rotating cursor, so installations beyond the first eight are
eventually considered. It admits only fresh `AUTO_WITH_NOTICE`/`REQUIRED` authority and
reuses the hardened update journal and replacement executor. Completed operations project
idempotent `installation_updated` events into the same local feed; restart repairs a
missing projection from the completed journal without replaying filesystem or Gateway
effects. `MANUAL`, ambiguous, incompatible, unsafe, and unauthorized work remains review.

Mediation, mute/suppression preferences, delivery reservations, and operation
outcomes use additive SQLite tables. A delivery request ID is saved before the
Gateway claim. Claim and settlement responses must match the exact organization,
recipient, request, reference, and event. A lost or remotely uncertain send settles
as `uncertain` and is not reclaimed; a remote deferral is not immediately replayed.
Setup is never reported as an install/update outcome, and verified completion
follows the ordered `files_installed` report.

### Publication transaction boundary

A publication receipt authorizes one exact owner, local draft, immutable file-byte snapshot, content/description/manifest hashes, and observed Gateway policy. CAS blobs, trees, and the commit are serialized from that snapshot; publication must not walk the mutable overlay again after review. Immediately before first upload the controller revalidates current owner, canonical source fingerprint, exact reviewed hashes, and the current local security guard.

The SQLite operation journal records each external-effect intent before sending it. Draft submission, approval, publication, and decline have explicit unknown-outcome phases. Recovery first reads or lists the authoritative owner-private remote draft and verifies its exact slug, commit, content, description, and manifest identity. It retries only after that state proves the effect is still safe; an ambiguous exception is never treated as non-delivery. A policy change blocks only uncommitted work and requires a new receipt bound to the same operation; it cannot hide a remote terminal outcome or create a second draft.

Publication leases fence durable phase changes and external-effect completion by lock key, holder, and unexpired lease. Decline intent is persisted before the request, so reopen cannot accidentally resume approval/publication while cancellation is uncertain. Completing an operation updates its journal payload, local draft state, and remote-draft projection in one SQLite transaction; repeated terminal recovery repairs missing projections idempotently.

## Commands, Tools, and Presentation

Entitlement belongs in the plugin, with a narrow version-tested adapter to
existing host account readers. Check at initialization and before protected
operations; use refresh-free background checks and invalidate pending work on
ownership changes. Do not require new auth-event hooks or core help/menu changes.
An unavailable installed command may return a concise denial. Ordinary replies
use host caller routing, not a new context framework. Native approval callbacks
and proactive delivery retain their distinct ownership/destination checks.
See [the simplification requirement](FIRST-RELEASE.md#simplification-requirement-2026-09-12).

Use one parser/controller for `hermes wisdom ...` and `/wisdom ...`. First keep
CLI/TUI text interactions complete and deterministic, then map the same views
to Telegram and Slack. Tools expose bounded inbox, inspection, and presentation
requests; they do not accept model-selected actors or authoritative receipts.

Canonical views contain immutable identity/version facts, checks, compatibility,
allowed actions, and operation status. The model owns only bounded explanatory
copy. Agent-authored summaries are the default; fixed copy remains explicit
opt-out. Unknown checks stay unknown. Use the final reviewed source wording.

Cards retain their View action after completion, show versions, put check icons
before labels, and expose details/assessments with Show/Hide controls. Do not emit
an extra success message or assessment wall when an existing card can be updated.
Not Now records deferral, not rejection; manual browse/review remains available.

## Desktop Handoffs and Deferred UI Backend

The initial Desktop surface is an external SDK page at `desktop/plugin.js`,
reachable through sidebar and palette contributions. It uses SDK components,
read-only display context and explicit clipboard actions; it does not call a
Wisdom HTTP API, execute commands or store authoritative workflow state.

Commands are static shell-safe examples. Profile display is advisory and never
interpolated into commands. Native execution must occur on the intended host and
profile, where owner checks and fresh full review/approval remain mandatory.
Clipboard failure is visible; copying never means execution or consent.

The proposed shared native-context HTTP backend is withdrawn. Full embedded
private editing/actions and Dashboard SDK work are deferred. No Agent source
changes, private host-manager lookup or separate service is part of this scope.

## Release Boundary

Native Git installation is the initial distribution path. The Python wheel is
also built to catch packaging regressions, but it is not advertised as automatic
Hermes plugin discovery. Runtime dependencies must be explicit; manifest
`python_dependencies` is descriptive and does not install packages automatically.

A release records all four revisions and contract hashes. Do not advance Gateway
or Portal pins just because their main branches moved. The existing smoke must
load the plugin package, not accidentally find `hermes_wisdom` left in an old
Agent checkout or a developer environment.
