# First Release: Extract, Adapt, and Verify

Approved execution plan. CLI, TUI, Telegram, and Slack are release one;
Desktop and Dashboard remain the 31-point follow-up. M0 is complete. M1-M8
retain their 63-point baseline; investigation and verification are included.
This document defines tasks, not a second completion tracker: implementation,
automated, and live status remain in [FEATURES.md](FEATURES.md).

## Simplification Requirement (2026-09-12)

Implement entitlement inside the installed/enabled plugin; do not modify core
command dispatch, help, completion, or menus solely for Wisdom availability.
Agent PR #108645 was rejected and is not a dependency of this release.
Automatic hiding of installed but unavailable first-release commands is no
longer required: a concise denied/unavailable response is acceptable.

Read current host credentials at initialization and recheck profile, account,
organization, scopes, and expiry before protected operations. Initialization
alone is not authorization. Background processing uses a lightweight local
recheck; it must not refresh credentials for availability. Invalidate cached
state and pending approvals on ownership changes. Use existing lifecycle hooks
where applicable; current upstream has no dedicated login/logout/org-switch
hooks. Gateway remains authoritative; installation is distribution, not access.

Ordinary user commands rely on host admission and reply-to-caller routing.
Do not add a universal actor/context framework or a second user-login flow for
these replies. Native callbacks still bind the clicking actor and original
message to the approved operation, especially in shared chats. Proactive
delivery separately needs an eligible active user-facing destination; neither
background work nor subagents may invent consent or a human turn. Prove these
paths using existing native handlers first. Propose a minimal generic host
extension only for a concrete, demonstrated gap, not as default architecture.
This also applies to proactive delivery: exhaust existing plugin/native APIs
first. Report an insurmountable gap to the project owner before proposing or
implementing any further Hermes Agent PR. Do not silently relax delivery safety.

Preserve feature IDs and baseline estimates until the plugin-only vertical slice
is verified; scope reduction is not evidence of completed work. Historical host
PR tests remain historical evidence, not proof of this revised implementation.

## Extraction Contract

Use Agent `0924f7946b937c2ed02d083fc26752d577c54b3b` as the immutable
behavioral reference. Copy coherent modules or selected functions/classes with
their tests, fixtures, and required dependencies before changing integration.
Preserve algorithms, constants, state transitions, validation, and assertions.
Prefer separate mechanical-extraction and adaptation commits within one PR.

Do not import the old checkout at runtime or copy a large unexercised dependency
tree. Each extraction must run independently before expanding it. Keep source
attribution and list copied, deferred, superseded, and excluded tests in its
evidence. Deferred tests are not passing tests. Retired migration tests do not
justify removing restart, concurrency, or corruption coverage.

Replace private host calls with existing APIs and a narrow, version-tested
plugin-side account adapter; propose small generic host changes only for proven gaps.
Do not clone authentication or bypass existing installation/security guards.
Native SDK handlers are sufficient for surface rendering. No universal message
framework, general scheduler, fake human continuation, or core Wisdom fork.

## Fact-Finding Packets

| Packet | Owner role | Required output |
| --- | --- | --- |
| A: parity inventory | Luna | Commands, aliases/options, tools, callback effects, state transitions, denial/recovery cases, reference symbols and tests |
| B: extraction map | Luna | Dependency closure for each slice; copy/adapt/public API/generic host/excluded classification; exclusive write scope |
| C: release map | Luna / integrator | Current host call sites, runtime dependencies, four immutable revisions, contract hashes, clean-install and private-repository access requirements |

Evidence starts in [PARITY.md](PARITY.md). Before implementing a slice, its
packet must identify the concrete old symbols/tests, new ownership boundary,
required host inputs, and acceptance commands. Unresolved host access cannot be
papered over with process-wide environment variables or model-provided identity.

## Task Allocation

The feature suffixes below expand to `CW-M< milestone >-< number >`. Each
feature is allocated once; subtasks do not add points to the approved baseline.
Dependencies are implementation readiness, not reasons to defer source reading.

| Task | Feature IDs (CW- prefix omitted) | SP | Output / readiness |
| --- | --- | ---: | --- |
| H1 | M1-001, M1-002, M1-003, M1-006 | 9 | Plugin-owned account adapter/gates; reuse host command routing and native callback identity; verify proactive root-only presentation without requiring PR #108645 |
| H2 | M1-004, M1-005 | 4 | Actual post-delivery facts and fenced idle execution; follows H1 |
| F1 | M2-001, M2-002, M2-003 | 5 | Entitlement, disclosure, fresh profile SQLite/settings; pure extraction can precede H1, real wiring cannot |
| F2 | M2-004 | 2 | Bounded client contracts, hashes, runtime schemas/vectors, development-only OpenAPI verification |
| F3 | M2-005 | 1 | One CLI/slash controller and gated tools; requires H1, F1, F2 |
| C1 | M3-001, M3-002 | 3 | Browse/search/pages, versions, installed state, read-only inspection; requires F1/F2 |
| C2 | M3-003, M3-004 | 3 | Exact consent, atomic install, compatibility, local edits, Recheck, safe uninstall; requires C1, P1, R2 |
| C3 | M3-005 | 2 | Separately approved setup/prerequisites/verification/recovery; requires C2 |
| U1 | M4-001, M4-002, M4-003 | 4 | Manual/bulk/automatic updates and protections; requires C2 and P1 |
| U2 | M4-004, M4-005, M4-006 | 4 | Deferral/reopen/mute/offline preferences/publisher exclusion; requires F1 and C2; R1 consumes exclusions |
| P1 | M5-001, M5-002 | 3 | Local preparation, scans, advisory/editorial checks, publisher evidence; requires F2 and host guard mapping |
| P2 | M5-003, M5-004, M5-005 | 5 | Private draft, exact approval, policy submission, moderation/status reconciliation; requires P1, C2, R2 |
| Q1 | M6-001, M6-002, M6-003 | 5 | Independent use/refinement qualification, stability and cadence; requires H2, F1, P1, U2 |
| R1 | M7-001, M7-002 | 4 | Feed/coalescing and agent-first assessments; requires H2, U1/U2, P2, Q1, R2 |
| R2 | M7-004 | 2 | Durable operation/delivery primitives from F1; integrate recovery with each producer and S1/S2 before acceptance |
| S1 | M7-003 | 1 | Telegram native bridge; start with F3/C2 views, finish against P2/R1/R2 |
| S2 | M7-005 | 1 | Slack native bridge; start with F3/C2 views, finish against P2/R1/R2 |
| V1 | M8-001, M8-002, M8-003 | 5 | Four-repo smoke, artifact installation, diagnostics and live acceptance of all preceding tasks |
| Total | M1-M8 | 63 | Re-estimate after extraction audit and first working vertical slice |

## Worktrees and Delivery

1. Wave 1: one integration owner proves H1 on existing host APIs, then audits H2 gaps. F1 and F2 extract independently.
2. Wave 2: F3, P1, R2 and C1; then C2/C3. Prove real CLI/TUI consumption early.
3. Wave 3: U1/U2 and P2 in separate worktrees; Q1 follows its actual dependencies.
4. Wave 4: R1 and complete S1/S2; then V1 using actual installed plugin artifacts.

Use Sol for implementation and Luna for bounded investigations. Each concurrent
worker has a separate `codex/<task>` branch/worktree and explicit file ownership.
One integrator owns shared types, central schema changes, registration, dependency
manifests, and tracker integration. A worker requests shared changes instead of
editing another worker's files. Schema extraction itself may be delegated to
one named owner; no parallel independent schema additions.

Default PR grouping: three plugin PRs for foundation/
manual consumption, updates/publication, and proactive delivery/surfaces; one
Gateway release-smoke PR. An Agent PR is conditional on a demonstrated missing
capability, not a release prerequisite by default. Keep extraction/adaptation as reviewable commits, not
a PR for every task. No Portal runtime change is planned. Do not silently change
a runtime contract pin just because a repository's main moved.

## Exit Gates

- Copied tests execute against the extracted implementation, not old modules.
- Current host dispatch proves authorization and lifecycle behavior; fakes alone
  are insufficient, and missing mandatory hooks leave registration unavailable.
- Exact bytes/version/description/manifest bind confirmation; duplicate clicks,
  local edits, unknown checks, stale plans and setup approval remain protected.
- Deferral is not rejection or consent. Manual browse/reopen still works.
- Ordinary replies use host caller routing. Proactive presentation targets an
  eligible active user-facing session; callbacks preserve actor/message binding.
  Workers may reconcile; subagents and model-generated text cannot approve.
- Card editing failures never undo successful operations or cause double apply.
  Pending/moderated/published status updates the original card where supported.
- Account/profile changes, expiry, unload/reload, restart and uncertain sends have
  explicit recovery coverage. Ambiguous sends are never blindly retried.
- Record hashes and immutable Agent/plugin/Gateway/Portal revisions. Test clean
  installations without any old in-core `hermes_wisdom` package in the environment.
- Live acceptance uses real commands, lifecycle facts and callbacks on all four
  surfaces. Reset/restart/publish actions require specific authorization; no
  incidental demo or production changes are part of source extraction.
