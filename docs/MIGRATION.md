# Migration Plan

## Agreed Boundaries

The first implementation increment is **milestone 0 only**: an inert native
plugin scaffold, quality checks, and this document set, committed and pushed to
the private repository. No demo reset, host patch, or Wisdom workflow is part of
that increment.

First release covers CLI, TUI, Telegram, and Slack. Desktop and Dashboard are
fully scoped follow-ups, not exclusions. All use one workflow implementation.
No legacy profiles need migration; only the developers used the reverted core
feature. A fresh demo reset is a separately authorized operational step.

Gateway and Portal are already merged. Their moderation, authorization, flag
targeting, and manager-email logic stays server-side. Changes to their release
harness or runtime contract pins are separate from moving Agent functionality.

## Milestones

Feature-level estimates and acceptance live in [FEATURES.md](FEATURES.md).
Milestone totals below are the approved budget, not additional work points.

| ID | Deliverable | Depends on | Points | Exit boundary |
| --- | --- | --- | ---: | --- |
| M0 | Scaffold, docs, tracker, quality | None | 3 | Clean install/lint/tests/build; initial main pushed |
| M1 | Generic host extension contracts | M0 | 13 | Neutral fixtures prove context, availability, lifecycle |
| M2 | Plugin foundation and surface wiring | M1 | 8 | Fresh-profile entitlement/state/commands/contracts work |
| M3 | Browse, inspect, install, setup | M2 | 8 | First manual CLI/TUI consumption flow works |
| M4 | Updates and preferences | M3 | 8 | Policies, protected edits, defer/reopen/mute work |
| M5 | Sharing and publication | M2, consent from M3 | 8 | Exact reviewed draft follows policy; status reconciles |
| M6 | Qualification | M5 | 5 | Real usage and refinement events qualify correctly |
| M7 | Recommendations and native delivery | M3-M6 | 8 | Telegram/Slack adapters cover all flows and recovery |
| M8 | First-release acceptance | M1-M7 | 5 | Four-repo smoke and live representative flows pass |
| M9 | Shared UI backend and contracts | M3-M5, M7 | 5 | Scoped REST/state/event contract passes denial tests |
| M10 | Desktop parity | M9 | 13 | External Desktop contribution covers the feature set |
| M11 | Dashboard parity | M9 | 8 | External Dashboard contribution covers the feature set |
| M12 | UI packaging and acceptance | M10, M11 | 5 | Both installed bundles pass integration and live checks |
| | **Total** | | **97** | |

M0-M8 total **66 points**. Full scope is **97 points**, or approximately
**120-125 with contingency**. Provisionally allow **6-9 engineering weeks** for
full scope, excluding upstream reviews and deployment waits. This is not a
velocity-calibrated promise. Re-estimate after M1 and again after the first
working vertical slice. Track upstream wait time separately from engineering
effort. M9 can start after its dependencies; M10/M11 can proceed independently.

## Delivery Order and PR Shape

1. Land this planning/scaffold repository without registering functional tools.
2. Propose M1 host APIs against current Agent in independent PRs. Include neutral
   sample consumers and actual call-site tests; no Wisdom names or business rules.
3. Build the manual CLI/TUI browse-inspect-confirm-install path using the shared
   service. Keep package checks and exact-version consent intact from the start.
4. Add update, preference, and publishing workflows as separate slices. Preserve
   operation receipts and statuses even before all rich adapters are complete.
5. Add lifecycle-based qualification and contextual recommendations. Then finish
   Telegram/Slack presentation and run their full workflows end to end.
6. Complete first-release acceptance before declaring the plugin usable for
   dogfooding. Registration, a green unit test, and a sent card are different gates.
7. Add the shared UI API, then Desktop and Dashboard independently. Use the
   same operation service, not parallel frontend-controlled implementations.

The approved execution breakdown is [FIRST-RELEASE.md](FIRST-RELEASE.md).
Selectively copy working code and its tests before adapting plugin boundaries.
Keep mechanical extraction distinct from behavioral changes within a PR; a task
does not require its own PR. Default to one generic Agent API PR, three plugin
delivery PRs, and one Gateway release-smoke PR. Parallel task worktrees have
exclusive write scopes and one integration owner. Preserve feature IDs and
points rather than double-counting extraction as new work.

## Desktop Follow-Up

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

## Dashboard Follow-Up

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
  first release. Later UI localization belongs in the plugin's own artifacts.
- Desktop/Dashboard deferral avoids about 18k legacy UI/test lines initially,
  not permanently. Track runtime, tests, generated data, docs, and host changes
  separately so the new footprint remains explainable.

## Reset and Rollout

When explicitly authorized, inventory the exact local demo profiles/processes,
back up developer fixtures, and reset only their Wisdom test state. Never
delete all Hermes configuration, credentials, installed user skills, or unrelated
processes. No reset or data migration is required to validate milestone 0.

Install immutable plugin builds into disposable profiles first. Then test with
an entitled member, an entitled organization admin, and an unrelated account.
Use the existing flag/scopes and expiry semantics; this repo does not configure
Vercel or production email delivery. Disabling the plugin must stop owned work
without uninstalling user skills or hiding unresolved operation outcomes.
