# Verification Plan

## Evidence Levels

1. **Scaffold:** import/registration, package contents, Pylint, and unit tests.
2. **Feature:** focused tests for the selected workflow and its denial/recovery cases.
3. **Host integration:** plugin loaded into the pinned host through real dispatch.
4. **Cross-repository:** real disposable Postgres/MinIO plus pinned services/plugin.
5. **Live acceptance:** authorized human interaction on the actual surface.

Passing one level does not prove the next. Record them independently in
[FEATURES.md](FEATURES.md). The old PR's tests are reference evidence only.

## Milestone 0

Use the README commands in a clean virtual environment. Verify that both regular
package import and native synthetic-namespace loading work, registration touches
no host APIs, and the source/wheel artifacts contain the intended code. Build
artifacts and test caches stay ignored. No network credentials or live host are
required beyond installing development dependencies.

## Focused Feature Matrix

| Area | Required scenarios |
| --- | --- |
| Entitlement | Missing/malformed/expired/mismatched scopes; ordinary member allowed; nonmember/personal/out-of-cohort denied; no credential refresh on read |
| Scope | Wrong actor/profile/account/org/chat/thread; rotated conversation; subagent/background cannot present; member cannot moderate |
| Discovery | Pagination/back navigation; exact version and checks; browse includes usable review/install controls |
| Consent | Explicit confirmation only; duplicate callback; expiry/Recheck; source/version/manifest/description drift; completed states stay completed |
| Install/update | Exact reviewed bytes; unavailable checks; partial compatibility; sensitive expansion; local modifications preserved; automatic-policy boundaries |
| Setup | Installation does not approve setup; prerequisites and verification separately consented; redacted failure diagnostics |
| Qualification | Genuine use and edit/refinement facts; policy thresholds, weekly limits, stability/cooldown; no plaintext trigger spoofing |
| Publication | Private local prep; approval before upload; private draft edit/recheck; direct versus moderated policy; rejection and retry; status reconciles |
| Recommendations | Agent copy default; fixed opt-out; scoped aggregate publisher evidence; exclude original publisher; coalesce newer versions |
| Preferences | Not Now != rejection; no immediate resurfacing; explicit browse/reopen works; scoped mute and offline preference synchronization |
| Delivery | Main reply delivered before follow-up; callback acknowledgement; same-card edits; no redundant assessment/success; confirmed/uncertain sends distinguished |
| Recovery | Worker kill/restart; unload/reload cancellation; lease expiry; duplicate feed events; retries do not double-apply or blindly resend |
| Presentation | Escaped/bounded hostile content; left-side check icons; clear version; Show/Hide checks and View Assessment; no false pass/security certification |

Test the real controller/store/filesystem with small fixtures for core safety
contracts. Mock remote transports when appropriate, not the very authority
decision a test claims to cover. Avoid repeating an entire broad suite for a
small copy change; run the focused gate plus applicable integration contract.

## Four-Repository Smoke

Adapt the existing Gateway workflow and script to add a full-SHA plugin checkout
and explicit plugin location/environment. Do not overlay feature files onto
Agent or rely on the deleted in-core `hermes_wisdom` import path.

The release workflow must:

- Verify all immutable inputs and contract hashes; pin actions and dependencies.
- Install the plugin in the tested host environment from its real artifact/layout.
- Keep read-only repository permissions and `persist-credentials: false`.
- Require read access to both private Portal and plugin repositories; never log it.
- Retain revision/log evidence and fail if runtime contract pins differ.
- Run existing delivery, outbox/recovery, and publication drivers against
  disposable services; preserve canonical Unicode and stored-address checks.
- Clean up only containers/processes created by this run.

Workflow dispatch depends on the workflow reaching its default branch and an
administrator configuring appropriate private-repository read credentials.
These are release dependencies, not proof of completed application behavior.

## Live First-Release Acceptance

Use newly initialized test profiles and an inert test skill. Do not reset an
existing profile until the exact reset is authorized. Record all source revisions.

1. A member invokes and later refines a local skill through the agent; each
   qualification path eventually surfaces a correctly scoped review card.
2. Review first creates the approved private draft and changes the existing
   control to View Skill. Edit/recheck, submit, moderate, and verify the same
   card transitions through pending moderation to published.
3. A different member receives one recommendation, inspects, installs, and
   reviews setup separately. The publisher receives no self-install/update advice.
4. Publish a later version and exercise manual and allowed automatic update paths.
5. Choose Not Now, verify suppression, then explicitly browse/reopen and install.
6. Exercise Show/Hide checks, View Assessment, expired reviews/Recheck, reconnects,
   and a restart during pending work without duplicate actions or notifications.
7. Repeat representative flows on Telegram and Slack, plus CLI/TUI confirmations.

Use actual lifecycle triggers, not manually injected success cards. Slack slash
commands in threads may have platform limitations; test supported command entry
and native button callbacks independently and document the limitation clearly.

## Desktop and Dashboard Acceptance

Test installed external bundles, not only component stories or source builds.
Test each SDK independently with the shared backend.

- Discover, inspect, edit files/manifest, review checks, install/update, and approve
  setup through native local controls.
- Final local publish confirmation submits directly; the user is not sent to
  Portal to approve the same package twice. Moderation still requires an admin.
- Verify private-draft, changed-source, pending, published, rejected, and failed
  states, including notifications opening the correct review and stale recheck.
- Direct API calls with missing/wrong entitlement or member moderation fail.
- Switch profile/account/remote connection mid-request; old results/actions must
  not appear in or mutate the new context. Expiry hides UI and denies execution.
- Disable/reload the plugin, disconnect/reconnect, and verify listener/task cleanup.
- Exercise Desktop OAuth remotes with polling only; sockets are an optimization.
- Verify keyboard focus, dialog labels, responsive layouts, long text, themes,
  and scrolling without blank surfaces, overlap, or accidental duplicate controls.

Use browser/desktop screenshots and interaction evidence for visual checks.
Do not label unavailable Desktop E2E as passing merely because upstream disables
its job. Record the blocker or run the appropriate supported local harness.
