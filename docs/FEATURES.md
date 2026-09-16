# Collective Wisdom Feature Tracker

This tracker is the authoritative feature inventory for Collective Wisdom. Stable IDs are never reused. Point totals, dependencies, scope, acceptance criteria, and the three status dimensions are maintained here even when implementation detail lives elsewhere.

Source notation is defined in [`docs/SOURCES.md`](./SOURCES.md): `legacy:` resolves at Agent `0924f7946b`, `host:` at Agent `31d0a2428e`, and `new:` identifies work introduced by this repository. This is a fresh implementation; no legacy migration or compatibility with legacy persisted state is planned.

## Status And Record Defaults

Allowed implementation states are `Planned`, `In progress`, `Implemented`, and `Blocked`. Automated and live states are `Not run`, `Passing`, `Failing`, and `Blocked`. Passing automated checks never implies live acceptance.

| Field | Global default | Per-record override rule |
| --- | --- | --- |
| Owner | Unassigned | Add the stable feature ID and named owner to the override table below. |
| PR | None | Add the stable feature ID and PR URL/number only after one exists. |
| Evidence | None | Add links to tests, runs, screenshots, logs, or acceptance notes; do not infer evidence from status. |

### Owner / PR / Evidence Overrides

| ID | Owner | PR | Evidence |
| --- | --- | --- | --- |
| All records | Unassigned | None | None |
| CW-M0-001 | Codex / Sol | Bootstrap on main | [Local scaffold evidence](verification/M0.md) |
| CW-M0-002 | Codex / Sol | Bootstrap on main | [Tracker validation](verification/M0.md) |
| CW-M0-003 | Codex / Sol | Bootstrap on main | [Quality checks and CI boundary](verification/M0.md) |
| CW-M1-001, CW-M1-002, CW-M1-003 | Sol / plugin integration owner | [Plugin PR 1 (draft)](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Terminal setup/discovery slice](verification/terminal-discovery.md), [account adapter](verification/F1-account.md), [delivery audit](verification/plugin-only-delivery-audit.md); native callbacks/proactive wiring remain pending. No Agent PR dependency. |
| CW-M2-001, CW-M2-002, CW-M2-005, CW-M3-001, CW-M3-002 | Sol / terminal workflow workers; Codex integration | [Plugin PR 1 (draft)](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Terminal slice evidence](verification/terminal-discovery.md), extended by [manual installation](verification/manual-installation.md). Model tools, native mutation controls and real-account acceptance remain pending. |
| CW-M2-003 | Sol / store extraction | [Plugin PR 1 (draft)](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Fresh store tests](verification/F1-store.md); settings and host integration pending |
| CW-M2-004 | Sol / contract extraction | [Plugin PR 1 (draft)](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Contract/hash extraction](verification/F2.md), [wire builders](verification/F2-wire.md), [package kernel](verification/F2-package.md), [bounded discovery client](verification/F2-client.md); host and full client integration pending |
| CW-M5-001 | Sol / preflight extraction | [Plugin PR 1 (draft)](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Pure preflight tests](verification/P1-preflight.md); host guard and workflow integration pending |
| CW-M3-002, CW-M3-003, CW-M3-004, CW-M7-004 | Sol / manual review, execution and host tests; Codex integration | [Plugin PR 1](https://github.com/NousResearch/hermes-collective-wisdom/pull/1) | [Manual-installation slice evidence](verification/manual-installation.md). Terminal review/install/inspection/uninstall and explicit recovery implemented; 422 local tests passed including actual unmodified-host dispatch/discovery. Native consent, update handling and delivery recovery remain separate; live acceptance not run. |

## Release Boundaries

| Release | Milestones | Points | Current state |
| --- | --- | ---: | --- |
| First release | M0-M8: CLI, TUI, Telegram, Slack, contracts, service, and live acceptance | 66 | M0 complete; extraction and plugin-only integration underway; no live acceptance run |
| Future UI | M9-M12: shared UI backend, Desktop, Dashboard, packaging, and UI E2E | 31 | Planned; all automated/live checks not run |
| Full goal | M0-M12 | 97 | 62 stable feature records |

## M0 - Foundation (3 SP)

Destination / scope: shared Python package, repository documentation contract, and CI quality gates.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M0-001 | Package scaffold and module boundaries (shared runtime) | 1 | - | `new:pyproject.toml`; `new:hermes_wisdom_plugin/` | Package builds and imports; synthetic native loading succeeds; registration is inert and imports no host or UI code. Live host installation belongs to M8. | Implemented | Passing | Not run |
| CW-M0-002 | Authoritative docs and feature tracker (shared planning) | 1 | - | `new:docs/FEATURES.md`; `new:docs/SOURCES.md` | Stable IDs, immutable sources, scope, dependencies, acceptance, estimates, owner/PR/evidence fields, and separate statuses are recorded; automated totals equal 97 overall and 66 for M0-M8. | Implemented | Passing | Not run |
| CW-M0-003 | CI and quality baseline (shared repository) | 1 | CW-M0-001 | `new:.github/workflows/ci.yml`; `new:tests/` | Workflow defines Pylint, pytest, and build on Python 3.11/3.13 without live credentials; local checks pass. Consult linked evidence for remote CI separately. | Implemented | Passing | Not run |

## M1 - Plugin Integration And Events (13 SP)

Destination / scope: plugin adapters using existing Agent registration, routing,
account readers, and native platform handlers. Host extensions are conditional
on proven gaps, not a prerequisite. Baseline points remain provisional until the
revised vertical slice is verified; no implementation is marked complete here.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M1-001 | Existing CLI/TUI routing and account adapter | 3 | CW-M0-001 | `host:hermes_cli/`; `legacy:hermes_wisdom/service.py` | Plugin uses existing registration and reply-to-caller routing without core surface changes or a universal invocation context; version-tested account adapter reads current profile credentials, never model-supplied identity. | In progress | Not run | Not run |
| CW-M1-002 | Native Telegram/Slack callback binding | 2 | CW-M1-001 | `legacy:plugins/platforms/telegram/wisdom_adapter.py`; `legacy:plugins/platforms/slack/wisdom_adapter.py` | Ordinary replies rely on host admission/routing; native callbacks bind the actual actor and original message/route to the operation, preserve group privacy, and reject another person's or profile's controls without core surface edits. | In progress | Not run | Not run |
| CW-M1-003 | Plugin-owned availability enforcement | 3 | CW-M1-001, CW-M1-002 | `legacy:gateway/wisdom_command.py`; `legacy:tools/wisdom_tool.py`; `host:hermes_cli/plugins.py` | Installed/enabled plugin registers without reading credentials and checks entitlement before protected actions; unavailable commands may remain visible with a clear denial. No core menu/completion changes, auth-event hook dependency, or mid-turn toolset rebuild. Background checks are local and refresh-free. | In progress | Not run | Not run |
| CW-M1-004 | Post-delivery non-human events | 2 | CW-M1-001 | `legacy:hermes_wisdom/consumption.py`; `host:hermes_cli/` | Plugin observes real delivery facts via existing lifecycle/native APIs and records typed events, never fabricated user messages. Report an insurmountable API gap before any Agent PR. | Planned | Not run | Not run |
| CW-M1-005 | Idle non-human events | 2 | CW-M1-004 | `legacy:hermes_wisdom/weekly_queue.py`; `host:hermes_cli/` | Plugin uses existing lifecycle/native APIs for safe idle work with deduplication, timestamps and cancellation; no human-message injection or new Agent PR without a reported insurmountable gap. | Planned | Not run | Not run |
| CW-M1-006 | Human-only presentation gate | 1 | CW-M1-003, CW-M1-004, CW-M1-005 | `legacy:hermes_wisdom/mediation.py`; `legacy:hermes_wisdom/mediation_store.py` | Post-delivery/idle processing may enqueue work, but presentation occurs only for an active eligible human surface; subagents cannot present and no event masquerades as human input. | Planned | Not run | Not run |

## M2 - Entitlement, Profile, Contracts (8 SP)

Destination / scope: local Agent persistence and pinned Gateway contract used by all first-release surfaces.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M2-001 | Entitlement state and auth expiry | 2 | CW-M1-001 | `legacy:hermes_wisdom/entitlement.py` | Entitlement is checked before each protected operation; expiry denies execution and ownership changes invalidate cached state and pending approvals. First-release commands need not be hidden. Load-time or logged-in status alone never grants access; Gateway remains authoritative. | In progress | Not run | Not run |
| CW-M2-002 | Disclosure and consent contract | 1 | CW-M2-001, CW-M1-006 | `legacy:hermes_wisdom/consent.py` | The user sees concise data-use and publication disclosure before enabling or submitting; refusal leaves the feature disabled without blocking unrelated Agent work. | In progress | Not run | Not run |
| CW-M2-003 | Profile SQLite store and settings | 2 | CW-M0-001, CW-M2-002 | `legacy:hermes_wisdom/store.py`; `legacy:hermes_wisdom/preferences.py` | Versioned SQLite state persists profile, consent, existing policy, mute, and disclosure timestamps transactionally; a fresh install starts empty and performs no legacy migration. | In progress | Not run | Not run |
| CW-M2-004 | Pinned contracts and package hashing | 2 | CW-M0-003 | `legacy:hermes_wisdom/contract.py`; `legacy:hermes_wisdom/package.py`; `new:contracts/` | OpenAPI, manifest schema, and canonical vectors are pinned to `docs/SOURCES.md`; canonical package/file hashes are deterministic and reject mutation. | In progress | Not run | Not run |
| CW-M2-005 | Gated command and tool registration | 1 | CW-M1-003, CW-M2-001, CW-M2-004 | `legacy:gateway/wisdom_command.py`; `legacy:tools/wisdom_tool.py`; `host:hermes_cli/plugins.py`; `host:hermes_cli/plugins_manifest.py` | Commands and tools register through supported host APIs and dispatch to the workflow controller for entitled users; absent host hooks or entitlement deny access. Disable/unload removes owned registrations. | In progress | Not run | Not run |

## M3 - Browse, Inspect, Install, Setup (8 SP)

Destination / scope: CLI/TUI discovery and installation against pinned service contracts.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M3-001 | Browse, search, and pagination | 2 | CW-M2-001, CW-M2-004 | `legacy:hermes_wisdom/service.py` | CLI/TUI can browse and search with stable ordering, explicit empty/error states, bounded pages, and forward/back pagination without duplicates. | In progress | Not run | Not run |
| CW-M3-002 | Version inspection and update checks | 1 | CW-M3-001 | `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/service.py` | Inspection shows installed, available, and compatible versions; checks distinguish current, update available, unavailable, and unverifiable. | In progress | Not run | Not run |
| CW-M3-003 | Hash-bound install consent and confirmation | 2 | CW-M2-004, CW-M3-002 | `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/consent.py` | Installation shows the exact version, manifest, and package hash, requires explicit hash-bound consent and confirmation rather than conversational yes, verifies every declared file hash before activation, and preserves the prior installation on failure. Stale package consent exposes `Recheck` before confirmation. | In progress (terminal implemented) | Passed (terminal slice) | Not run |
| CW-M3-004 | Compatibility and local-edit protection | 1 | CW-M3-002, CW-M3-003 | `legacy:hermes_wisdom/compatibility.py`; `legacy:hermes_wisdom/package.py` | Incompatible Agent/API versions are blocked with diagnostics; changed local files are detected and never overwritten without a distinct confirmed action. | In progress (manual install implemented) | Passed (terminal slice) | Not run |
| CW-M3-005 | Separately approved setup commands and prerequisite verification | 2 | CW-M3-003, CW-M3-004 | `legacy:hermes_wisdom/setup_execution.py` | Install completion is distinct from setup; prerequisites can be checked independently, exact setup commands are shown and separately approved through structured controls rather than conversational yes, execution is resumable, and failures identify the unmet check and recovery path. | Planned | Not run | Not run |

## M4 - Updates And User Control (8 SP)

Destination / scope: local CLI/TUI update policy, deferral, mute, offline synchronization, and publisher controls.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M4-001 | Manual update workflow | 2 | CW-M3-002, CW-M3-003 | `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/consent.py` | The user can inspect version, changes, compatibility, manifest, and hashes before explicit hash-bound update confirmation; success is atomic and failure preserves the installed version. | Planned | Not run | Not run |
| CW-M4-002 | Automatic update policy | 1 | CW-M4-001, CW-M2-003 | `legacy:hermes_wisdom/preferences.py`; `legacy:hermes_wisdom/package.py` | The existing automatic-update policy is preserved and applied per profile; eligible automatic updates require verified compatible content and record their result without inventing new policy modes. | Planned | Not run | Not run |
| CW-M4-003 | Sensitive and local-edit guard | 1 | CW-M3-004, CW-M4-002 | `legacy:hermes_wisdom/local_security.py`; `legacy:hermes_wisdom/package.py` | Automatic update is suppressed when the established security checks require review or local edits are present, and the user is routed to manual review; unknown check results never count as passing. | Planned | Not run | Not run |
| CW-M4-004 | Not Now and manual reopen recovery | 2 | CW-M1-006, CW-M4-001 | `legacy:hermes_wisdom/mediation_store.py`; `legacy:hermes_wisdom/preferences.py` | `Not Now` records deferral without granting consent, rejecting the package, or changing update policy; it survives restart, respects cooldown, and leaves a working manual reopen path. | Planned | Not run | Not run |
| CW-M4-005 | Mute, existing settings, and offline sync | 1 | CW-M2-003, CW-M4-004 | `legacy:hermes_wisdom/preferences.py`; `legacy:hermes_wisdom/operation_outbox.py` | Existing mute, preference, and update-policy settings remain editable; offline changes queue safely, retain ordering, and converge after reconnect without inventing new policy values. | Planned | Not run | Not run |
| CW-M4-006 | Self-publisher advice exclusion | 1 | CW-M4-002 | `legacy:hermes_wisdom/consumption.py`; `legacy:hermes_wisdom/service.py` | Install/update advice is automatically suppressed when the current authenticated organization member is the publisher; this is not an editable blocklist and does not block manual inspection. | Planned | Not run | Not run |

## M5 - Contribution And Publication (8 SP)

Destination / scope: local preparation and first-release direct/moderated submission with durable publication state.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M5-001 | Local preparation and security scan | 2 | CW-M2-004, CW-M3-005 | `legacy:hermes_wisdom/local_security.py` | Candidate content is assembled locally and evaluated with the established baseline checks; known failures and manifest/hash mismatches block submission with file-level findings, and unknown or unavailable results never count as passing. | In progress | Not run | Not run |
| CW-M5-002 | Editorial professionalism and publisher evidence | 1 | CW-M5-001 | `legacy:hermes_wisdom/professionalism.py`; `legacy:hermes_wisdom/editorial.py`; `legacy:hermes_wisdom/publisher_evidence.py` | Review reports actionable professionalism findings and displays provenance/publisher evidence without rewriting or certifying claims automatically. | Planned | Not run | Not run |
| CW-M5-003 | Private draft and file-manifest review | 2 | CW-M5-001, CW-M5-002 | `legacy:hermes_wisdom/share_queue.py`; `legacy:hermes_wisdom/package.py` | Drafts remain private until confirmation; the user can inspect every file, hash, visibility choice, and complete validation result before submission. | Planned | Not run | Not run |
| CW-M5-004 | Policy-routed submission confirmation | 1 | CW-M5-003, CW-M2-001 | `legacy:hermes_wisdom/service.py`; `legacy:hermes_wisdom/share_queue.py` | One explicit confirmation submits the reviewed draft; server policy decides direct versus moderated handling, and the user cannot choose a path that bypasses policy. | Planned | Not run | Not run |
| CW-M5-005 | Pending and publication status cards | 2 | CW-M5-004 | `legacy:hermes_wisdom/publication_cards.py`; `legacy:hermes_wisdom/service.py` | Pending, moderated, published, and rejected transitions update the original card/message in place rather than posting replacements, with idempotent terminal state handling. | Planned | Not run | Not run |

## M6 - Qualification And Cadence (5 SP)

Destination / scope: deterministic candidate qualification and presentation scheduling in the Agent runtime.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M6-001 | Usage-based candidate qualification | 2 | CW-M1-004, CW-M5-001 | `legacy:hermes_wisdom/qualification.py`; `legacy:hermes_wisdom/consumption.py` | Deterministic local rules qualify repeated successful use with traceable counters; model text cannot independently qualify or publish a candidate. | Planned | Not run | Not run |
| CW-M6-002 | Edit/refinement qualification path | 2 | CW-M1-004 | `legacy:hermes_wisdom/qualification.py`; `legacy:hermes_wisdom/weekly_queue.py` | Repeated EDIT/REFINEMENT observations use their own threshold and stability path to qualify a candidate independently of usage qualification; this is not post-qualification draft editing. | Planned | Not run | Not run |
| CW-M6-003 | Weekly cadence, quota, and cooldown | 1 | CW-M1-005, CW-M4-004, CW-M6-001, CW-M6-002 | `legacy:hermes_wisdom/weekly_queue.py` | Weekly limits, per-profile quota, dedupe window, and Not Now cooldown are deterministic, persisted, timezone-safe, and manually reopenable. | Planned | Not run | Not run |

## M7 - Team Delivery Surfaces (8 SP)

Destination / scope: shared team feed plus Telegram and Slack cards/callbacks with durable delivery.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M7-001 | Team feed and cross-surface dedupe | 2 | CW-M5-005, CW-M6-003 | `legacy:hermes_wisdom/mediation.py`; `legacy:hermes_wisdom/mediation_store.py` | A canonical event ID yields one feed item across retries and surfaces; no duplicate assessments, prompts, or publication cards are created. | Planned | Not run | Not run |
| CW-M7-002 | Agent-generated assessment default and fixed opt-out | 2 | CW-M7-001, CW-M4-005 | `legacy:hermes_wisdom/mediation.py`; `legacy:hermes_wisdom/mediation_store.py`; `legacy:hermes_wisdom/preferences.py` | Agent-generated assessment is clearly labeled and enabled by default; a persistent fixed opt-out disables generation without disabling manual review or changing historical records. | Planned | Not run | Not run |
| CW-M7-003 | Telegram cards and callbacks | 1 | CW-M1-002, CW-M5-005, CW-M7-001 | `legacy:plugins/platforms/telegram/wisdom_adapter.py`; `legacy:hermes_wisdom/publication_cards.py` | Real Telegram callbacks authenticate actor and original message, invoke the shared state machine idempotently, and update the original card through pending/publication outcomes. | Planned | Not run | Not run |
| CW-M7-004 | Durable outbox, retry, and recovery | 2 | CW-M7-001, CW-M7-003 | `legacy:hermes_wisdom/delivery_outbox.py`; `legacy:hermes_wisdom/operation_outbox.py` | Operations and deliveries persist before send, retry known-safe failures with bounded backoff and idempotency keys, recover after restart, and never blindly resend a message whose delivery outcome is uncertain; uncertain state is reconciled or surfaced for recovery. | Planned | Not run | Not run |
| CW-M7-005 | Slack cards and callbacks | 1 | CW-M1-002, CW-M5-005, CW-M7-001, CW-M7-004 | `legacy:plugins/platforms/slack/wisdom_adapter.py`; `legacy:hermes_wisdom/publication_cards.py` | Real Slack callbacks authenticate actor and original message, invoke the shared state machine idempotently, and update the original card through pending/publication outcomes. | Planned | Not run | Not run |

## M8 - First-Release Acceptance (5 SP)

Destination / scope: pinned four-repository integration and live CLI/TUI/Telegram/Slack proof.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M8-001 | Four-repository pinned smoke gate | 2 | CW-M3-005, CW-M5-005, CW-M7-004 | `gateway:.github/workflows/wisdom-release-smoke.yml`; `gateway:scripts/test-wisdom-cross-repo.sh`; `new:scripts/test-wisdom-cross-repo.sh` | Agent, Gateway, Portal, and this package run at recorded immutable revisions/contracts; smoke covers discovery, hash-bound install consent, separately approved setup, contribution, and delivery without floating main branches. | Planned | Not run | Not run |
| CW-M8-002 | Live first-release surface acceptance | 2 | CW-M8-001 | `new:docs/acceptance/first-release.md` | CLI, TUI, Telegram, and Slack are exercised via actual host delivery/idle triggers and real callbacks, not manual event injection; proof covers subagent exclusion, no fake user continuations, auth-expiry denial and recovery, stale-consent Recheck, Not Now recovery, original-card updates, and no duplicate assessments. | Planned | Not run | Not run |
| CW-M8-003 | Artifact install and diagnostics | 1 | CW-M8-001, CW-M8-002 | `new:dist/`; `new:hermes_wisdom_plugin/` | A clean supported host installs the built artifact, verifies contract/package hashes and prerequisites, and produces actionable redacted diagnostics for setup, network, entitlement, and delivery failures. | Planned | Not run | Not run |

## M9 - Shared UI Backend (5 SP, Future)

Destination / scope: future REST/DTO/profile-cache layer shared by Desktop and Dashboard.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M9-001 | Workflow REST API | 2 | CW-M3-005, CW-M5-005, CW-M7-004 | `legacy:hermes_wisdom/service.py`; `legacy:hermes_wisdom/setup_execution.py` | Scoped REST operations cover browse, inspect, install/update/setup, draft, confirm publish, settings, and activity with idempotent mutations and typed errors; every mutation revalidates actor, entitlement, and reviewed content. | Planned | Not run | Not run |
| CW-M9-002 | Query and event DTOs | 1 | CW-M9-001 | `legacy:hermes_wisdom/contract.py`; `new:contracts/ui/` | Generated/shared DTOs encode pagination, lifecycle, callbacks, activity, and publication state; unknown versions fail explicitly and event ordering is testable. | Planned | Not run | Not run |
| CW-M9-003 | Auth, profile cache, and lifecycle | 2 | CW-M2-001, CW-M2-003, CW-M9-001 | `legacy:hermes_wisdom/entitlement.py`; `legacy:hermes_wisdom/store.py` | UI sessions bind authenticated profile, isolate cache keys, expire and hide/disable protected actions on auth or entitlement loss, and require real authentication before access returns. | Planned | Not run | Not run |

## M10 - HERMES Desktop (13 SP, Future)

Destination / scope: HERMES Desktop plugin SDK shell and complete native Collective Wisdom workflow.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M10-001 | HERMES Desktop SDK shell package | 1 | CW-M9-002 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `host:website/docs/developer-guide/desktop-plugin-sdk.md`; `host:apps/desktop/src/sdk/index.ts` | Feature loads through the supported HERMES Desktop SDK/package boundary with isolated routes, permissions, error boundary, and no host-source patching. | Planned | Not run | Not run |
| CW-M10-002 | Desktop browse and installed views | 1 | CW-M10-001, CW-M9-001 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `host:apps/desktop/src/sdk/index.ts` | Browse/search/pages and installed state remain distinct, keyboard accessible, and preserve selection and scroll across detail navigation. | Planned | Not run | Not run |
| CW-M10-003 | Desktop candidates and version checks | 2 | CW-M10-002, CW-M9-002 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/qualification.py` | Candidate and detail views show compatibility, installed/available versions, hashes, provenance, assessment label, and unambiguous update status. | Planned | Not run | Not run |
| CW-M10-004 | Desktop file-manifest editor | 2 | CW-M10-003 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `legacy:hermes_wisdom/share_queue.py` | Private drafts support file add/remove/edit and manifest review with dirty-state protection; left-side checks use Show/Hide, omit redundant Pass labels, and keep complete check details accessible, including successful and unknown results. | Planned | Not run | Not run |
| CW-M10-005 | Desktop install, update, and setup | 2 | CW-M10-003, CW-M9-001 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/setup_execution.py` | Install/update requires explicit hash-bound consent and confirmation, protects local edits, and treats stale consent as Recheck; setup commands are separately approved and prerequisite verification is recoverable, with no conversational-yes approval. | Planned | Not run | Not run |
| CW-M10-006 | Desktop single-confirm publish | 1 | CW-M10-004, CW-M9-001 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `legacy:hermes_wisdom/share_queue.py` | Final file manifest and server-governed submission outcome are reviewed together; one explicit confirmation submits, while server policy alone decides direct versus moderated handling. | Planned | Not run | Not run |
| CW-M10-007 | Desktop activity, notifications, and settings | 2 | CW-M10-001, CW-M9-003 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `legacy:hermes_wisdom/publication_cards.py`; `legacy:hermes_wisdom/preferences.py` | Activity reflects in-place publication transitions; notifications, mute, fixed assessment opt-out, existing update policy, and self-publisher advice suppression remain consistent with shared state. | Planned | Not run | Not run |
| CW-M10-008 | Desktop remote profile, expiry, disable, and reconnect lifecycle | 2 | CW-M10-001, CW-M9-003 | `host:website/docs/developer-guide/desktop-plugin-sdk.md`; `legacy:hermes_wisdom/entitlement.py`; `legacy:hermes_wisdom/store.py` | Profile/account/connection switches cancel requests and invalidate cached data and pending controls; expiry or disable hides protected actions. Authenticated data polling works without plugin sockets on OAuth remotes. Existing login behavior is unchanged. | Planned | Not run | Not run |

## M11 - Dashboard (8 SP, Future)

Destination / scope: web plugin SDK bundle and responsive browser workflow.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M11-001 | Dashboard SDK shell bundle | 1 | CW-M9-002 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `host:web/src/plugins/sdk.d.ts`; `host:web/src/plugins/registry.ts` | Feature loads as a supported Dashboard plugin bundle with scoped styles, declared permissions, route cleanup, and no host-source patching. | Planned | Not run | Not run |
| CW-M11-002 | Dashboard browse and inspect | 1 | CW-M11-001, CW-M9-001 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/service.py` | Responsive browse/search/pagination and detail inspection preserve URL state, focus, and scroll and expose versions, hashes, compatibility, and provenance. | Planned | Not run | Not run |
| CW-M11-003 | Dashboard private draft editor | 2 | CW-M11-002 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/share_queue.py` | Draft/file-manifest editing protects unsaved work and uses Show/Hide for left checks with no redundant Pass label; complete details remain accessible for passing, failing, and unknown results. | Planned | Not run | Not run |
| CW-M11-004 | Dashboard install, update, and setup | 1 | CW-M11-002, CW-M9-001 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/package.py`; `legacy:hermes_wisdom/setup_execution.py` | Browser install/update requires explicit hash-bound consent and confirmation, protects local edits, and uses Recheck for stale consent; setup commands are separately approved and verified without conversational yes. | Planned | Not run | Not run |
| CW-M11-005 | Dashboard single-confirm publish | 1 | CW-M11-003, CW-M9-001 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/share_queue.py`; `legacy:hermes_wisdom/publication_cards.py` | A complete manifest review reaches submission with one explicit confirmation; server policy decides direct versus moderated handling, and status tracks pending through in-place published/rejected state. | Planned | Not run | Not run |
| CW-M11-006 | Dashboard activity and settings | 1 | CW-M11-001, CW-M9-003 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/preferences.py`; `legacy:hermes_wisdom/publication_cards.py` | Activity, notifications, mute, fixed assessment opt-out, existing update policy, and automatic self-publisher advice suppression are responsive, accessible, and consistent with first-release state. | Planned | Not run | Not run |
| CW-M11-007 | Dashboard expiry, responsive layout, and auth lifecycle | 1 | CW-M11-001, CW-M9-003 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `legacy:hermes_wisdom/entitlement.py` | Expiry hides/disables protected actions until real authentication succeeds; latest merged copy is used; narrow/wide browser layouts scroll without clipped controls, overlapping text, or inaccessible actions. | Planned | Not run | Not run |

## M12 - UI Packaging And E2E (5 SP, Future)

Destination / scope: distributable frontend artifacts and real Desktop/browser end-to-end acceptance.

| ID | Feature | SP | Depends | Source | Acceptance | Impl | Auto | Live |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| CW-M12-001 | Frontend packaging | 1 | CW-M10-001, CW-M11-001 | `host:apps/desktop/src/sdk/index.ts`; `host:web/src/plugins/sdk.d.ts`; `host:web/src/plugins/registry.ts` | Reproducible versioned, hash-verified HERMES Desktop and Dashboard artifacts contain only declared files, pin DTO/contract versions, and install through supported SDK mechanisms. | Planned | Not run | Not run |
| CW-M12-002 | HERMES Desktop E2E acceptance | 2 | CW-M10-002, CW-M10-003, CW-M10-004, CW-M10-005, CW-M10-006, CW-M10-007, CW-M10-008, CW-M12-001 | `legacy:apps/desktop/src/app/skills/collective-tab.tsx`; `new:tests/desktop-e2e/` | Packaged HERMES Desktop workflow passes browse-to-install/setup and candidate-to-publication journeys, real-auth expiry recovery, stale-consent Recheck, OAuth polling fallback, Not Now recovery, original-card/activity updates, no duplicate assessments, keyboard/accessibility, and restart persistence. | Planned | Not run | Not run |
| CW-M12-003 | Dashboard E2E acceptance | 2 | CW-M11-002, CW-M11-003, CW-M11-004, CW-M11-005, CW-M11-006, CW-M11-007, CW-M12-001 | `legacy:web/src/components/CollectiveWisdomPanel.tsx`; `new:tests/dashboard-e2e/` | Packaged browser workflow passes the same lifecycle using actual events/callbacks; latest merged copy, Show/Hide checks without redundant Pass while retaining full details, real-auth expiry recovery, and mobile/desktop layout and scrolling are visually verified. | Planned | Not run | Not run |

## Point Check

| Milestone | Records | Points | Release |
| --- | ---: | ---: | --- |
| M0 | 3 | 3 | First release |
| M1 | 6 | 13 | First release |
| M2 | 5 | 8 | First release |
| M3 | 5 | 8 | First release |
| M4 | 6 | 8 | First release |
| M5 | 5 | 8 | First release |
| M6 | 3 | 5 | First release |
| M7 | 5 | 8 | First release |
| M8 | 3 | 5 | First release |
| **M0-M8 subtotal** | **41** | **66** | **First release** |
| M9 | 3 | 5 | Future UI |
| M10 | 8 | 13 | Future UI |
| M11 | 7 | 8 | Future UI |
| M12 | 3 | 5 | Future UI |
| **M9-M12 subtotal** | **21** | **31** | **Future UI** |
| **Total** | **62** | **97** | **Full goal** |
