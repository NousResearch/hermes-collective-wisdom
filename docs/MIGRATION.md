# Migration Plan

## Agreed Boundaries

The selected delivery is stock-Agent-compatible native workflows and external
Desktop management pages calling the existing plugin REST namespace. This replaces
the initial command-copy guide: passthrough means invoking shared workflows and
rendering their results, not asking users to run CLI commands themselves.
No Agent changes or shared native-session HTTP bridge. Dashboard SDK work and full
six-surface parity remain deferred. Keep one workflow implementation. Older milestone
and bridge records below are historical and do not authorize host changes.
No legacy profiles need migration; only the developers used the reverted core
feature. A fresh demo reset is a separately authorized operational step.

Gateway PR [255](https://github.com/NousResearch/gateway-gateway/pull/255)
and Portal PR [1022](https://github.com/NousResearch/hermes-portal/pull/1022)
are verified merged. Their moderation, authorization, flag targeting, and
manager-email logic stays server-side. Changes to their release harness or
runtime contract pins are separate from moving Agent functionality.

## Local cadence simplification — 2026-09-15

Shannon approved a simpler acceptance boundary: do not repeat the same advice in
every session, and do not let background sessions receive or claim it away from
an interactive surface. A synthetic continuation inside an eligible interactive
session does not inherently disqualify that surface. This supersedes the proposed
requirement for a comprehensive host turn-origin taxonomy.

Reuse the existing host presentation opportunity and shared owner-scoped
assessment/delivery records. Cooldown controls refresh frequency; durable item
identity, leases and receipts prevent duplicate delivery. Neither renderer
acceptance nor an eligible surface grants consent to install or publish. Existing
owner, enrollment, mute, suppression and uncertain-outcome checks remain required.

The plugin may change its earlier delivery-only callback arrangement if that
provides a smaller correct implementation. Model work must retain the owning
session's actual route and authorized context; no fabricated route, prompt/history
mutation or new scheduler is implied by this design change. Verification status
and any remaining host dependency are recorded in [TESTING.md](TESTING.md).

## Historical generic TUI review bridge — superseded

After the exact inspection journey, Shannon explicitly authorized a minimal
generic review/action bridge in the isolated Agent worktree rather than another
terminal-only handoff. That permission was subsequently withdrawn; this records
the earlier design, not current authorization. The development host owns session-bound literal display, deliberate
named selection and cancellation; Wisdom retains its existing receipt, ownership,
install/update and separately approved setup kernels. No parallel consent engine,
model approval, scheduler, or prompt/history change is introduced.

The development host remains unpublished. Local code and fixture validation do
not authorize installed-runtime changes, service calls, publication, or deployment.
Stock-Agent fallback uses explicit terminal handoffs. Full embedded parity is
deferred; development-host results do not prove stock local presentation.

## Historical Full-Parity Milestones

Feature-level estimates and acceptance live in [FEATURES.md](FEATURES.md).
Milestone totals preserve the former full-parity baseline, not the current
Option 1 acceptance requirements or additional work points.

| ID | Deliverable | Depends on | Points | Exit boundary |
| --- | --- | --- | ---: | --- |
| M0 | Scaffold, docs, tracker, quality | None | 3 | Clean install/lint/tests/build; initial main pushed |
| M1 | Plugin-first host integration | M0 | 13 | Existing routing, plugin entitlement, callback binding and lifecycle verified; host extensions only for proven gaps |
| M2 | Plugin foundation and surface wiring | M1 | 8 | Fresh-profile entitlement/state/commands/contracts work |
| M3 | Browse, inspect, install, setup | M2 | 8 | First manual CLI/TUI consumption flow works |
| M4 | Updates and preferences | M3 | 8 | Policies, protected edits, defer/reopen/mute work |
| M5 | Sharing and publication | M2, consent from M3 | 8 | Exact reviewed draft follows policy; status reconciles |
| M6 | Qualification | M5 | 5 | Real usage and refinement events qualify correctly |
| M7 | Recommendations and native delivery | M3-M6 | 8 | Telegram/Slack adapters cover all flows and recovery |
| M8 | Core-surface acceptance | M1-M7 | 5 | Four-repo smoke and live CLI/TUI/Telegram/Slack flows pass; release remains blocked on M9-M12 |
| M9 | Shared UI backend and contracts | M3-M5, M7 | 5 | Scoped REST/state/event contract passes denial tests |
| M10 | Desktop parity | M9 | 13 | External Desktop contribution covers the feature set |
| M11 | Dashboard parity | M9 | 8 | External Dashboard contribution covers the feature set |
| M12 | UI packaging and release acceptance | M10, M11 | 5 | Both installed bundles pass integration and live checks against deployed endpoints using Shannon's Nous Portal login |
| | **Total** | | **97** | |

The original planning baseline assigned **66 points** to M0-M8 and **31 points**
to M9-M12: **97 points** total, or approximately **120-125 with contingency**.
It provisionally allowed **6-9 engineering weeks** for full scope, excluding
upstream reviews and deployment waits. These are historical baseline estimates,
not remaining-work calculations or delivery promises. Re-estimate from current
evidence rather than carrying them forward as velocity. M9 can start after its
dependencies; M10/M11 can proceed independently.

## Historical Delivery Order and PR Shape

1. Land this planning/scaffold repository without registering functional tools.
2. Prove M1 using existing host APIs and a narrow plugin-side account adapter.
   No core command/menu changes or PR #108645 dependency for entitlement. Propose
   a generic host extension only for a demonstrated callback/lifecycle gap.
3. Build the manual CLI/TUI browse-inspect-confirm-install path using the shared
   service. Keep package checks and exact-version consent intact from the start.
4. Add update, preference, and publishing workflows as separate slices. The
   bounded terminal prepared-draft editor and policy-routed publication operation
   are implemented: one exact-content receipt drives owner-private upload,
   authoritative recovery, decline, and durable terminal status. This remains
   locally verified, including explicit Slack/Telegram original-card controls;
   do not represent it as complete file-manifest UI, proactive delivery, or live
   publication parity. Preserve operation receipts and statuses while remaining
   adapters are added.
   The shared consumption state block is now extracted: feed checkpoints/events,
   mediation leases, mute/suppression preferences, delivery reservations,
   operation outcomes, and explicit terminal recovery are present. Explicit maintenance
   now produces bounded owner-scoped mediation references and can apply freshly authorized
   automatic updates behind `--apply-automatic`; no scheduler is started. Model assessment and private native advisory delivery now have separate local
   verification gates. CLI/TUI proactive presentation, live acceptance, and operation
   reporting without a legitimate Gateway delivery/event receipt remain open.
5. Add lifecycle-based qualification and contextual recommendations. Then finish
   Telegram/Slack presentation and run their full workflows end to end.
6. Complete core-surface acceptance before broad dogfooding. Registration, a
   green unit test, and a sent card are different gates.
7. Add the shared UI API, then Desktop and Dashboard independently. Use the
   same operation service, not parallel frontend-controlled implementations.
8. Before release, verify both installed UI bundles and the complete workflow
   against deployed endpoints using Shannon's Nous Portal login. Mocks, stories,
   and local scripting support this gate but cannot replace it.

The approved execution breakdown is [FIRST-RELEASE.md](FIRST-RELEASE.md).
Selectively copy working code and its tests before adapting plugin boundaries.
Keep mechanical extraction distinct from behavioral changes within a PR; a task
does not require its own PR. Default to three plugin delivery PRs and one Gateway
release-smoke PR; any Agent API PR is conditional, not mandatory. Parallel task worktrees have
exclusive write scopes and one integration owner. Preserve feature IDs and
points rather than double-counting extraction as new work.

## Deferred Full-Parity Desktop Phase

Use an external unified package at `desktop/plugin.js`, importing only the
supported Desktop SDK/React surface. Author maintainable source and build the
reviewed ESM artifact; the host does not compile arbitrary TSX at install time.

Deliver a Collective Wisdom route/pane with discovery, installed versions,
candidates, exact checks, file and manifest editing, install/update/setup
reviews, notification preferences, and operation history. Notification actions
open the relevant local review; do not require core thread-component patches.
If a necessary contribution is absent, propose a generic host slot separately.

Final local confirmation submits the exact reviewed package directly to the
configured publication workflow. Pending moderation is not an invitation to
approve again in Portal; organization moderation remains a separate actor/action.

Desktop is app-level while data belongs to a connection/profile/session. Clear
cached data and invalidate pending controls when that ownership changes.
Account for local and remote backends, expiry, disable/reload, reconnects, and
the SDK's OAuth-remote socket no-op: authenticated polling must work alone.

## Deferred Dashboard Phase

Ship a separate `dashboard/manifest.json` and reviewed JS bundle using the
Dashboard SDK's supplied React, authentication helpers, and tab/slot registry.
Share the Python backend namespace with Desktop, not its renderer SDK.

Cover discovery/installed state, candidate and version inspection, draft file/
manifest editing, checks, install/update/setup approval, publication, activity,
and preferences. Preserve direct submission after final inline review, live
operation states, Not Now reopening, and stale review recheck.

Keep styling scoped, responsive, and consistent with host components. Treat
loading, disconnected, denied, expired, and retryable states explicitly. No new
Dashboard-only product features; its expected deprecation affects priority only.

## Size Control

- Approximately half of the original added lines were tests and contract data.
  Do not remove security behavior or tests merely to advertise a smaller diff.
- Keep shared Python domain logic and one consent/presentation model. Extract
  thin platform renderers rather than wholesale copies of Agent adapters.
- Replace the 13,295-line full OpenAPI snapshot with a development/CI fetch from
  an immutable revision plus hash verification. Package required runtime schemas
  and vectors locally. A corrupt/missing fetched artifact fails the check.
- Consolidate fixtures and parameterize truly equivalent cases. Retain separate
  tests for different authorization and failure paths.
- Omit legacy migration code and unrelated frontend locale bundles from the
  core-surface sequence. UI localization belongs in the plugin's own artifacts.
- Sequencing Desktop/Dashboard after core surfaces avoided about 18k legacy
  UI/test lines initially, not permanently. Track runtime, tests, generated data, docs, and host changes
  separately so the new footprint remains explainable.

## Reset and Rollout

When explicitly authorized, inventory the exact local demo profiles/processes,
back up developer fixtures, and reset only their Wisdom test state. Never
delete all Hermes configuration, credentials, installed user skills, or unrelated
processes. No reset or data migration is required for ordinary isolated checks.

Install immutable plugin builds into disposable profiles first. Then test with
an entitled member, an entitled organization admin, and an unrelated account.
Use the existing flag/scopes and expiry semantics; this repo does not configure
Vercel or production email delivery. Disabling the plugin must stop owned work
without uninstalling user skills or hiding unresolved operation outcomes.
