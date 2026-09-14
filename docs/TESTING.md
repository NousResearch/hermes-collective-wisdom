# Verification Plan

## Native Candidate Not Now Gate — 2026-09-15

Candidate advisory deferral is integrated into the existing shared assessment
queue. Private Slack/Telegram callbacks stage one offline-capable suppression
using the policy snapshot bound before delivery, edit the original card, and do
not approve, publish, install, or renew consent. Replays do not extend the
cooldown; expiry permits a new assessment while preserving the old receipt.

Parent review repaired recording-SDK policy type/empty-keyboard fixtures and
added fresh admission/runtime checks after policy reads, before suppression,
and immediately before the terminal card edit. The source-snapshot runtime-close
regression failed before the guard repair. A post-commit shutdown test proves
suppression remains durable while the stale card edit is denied. Transactional
owner checks reuse the existing SQLite connection rather than opening a nested
write transaction.

Evidence:
- Pre-commit checkpoint rerun of the final integrated tree: **838 passed in
  52.26s**, no skips; canonical Pylint **10.00/10, exit 0**. Log:
  `/tmp/wisdom-checkpoint-tests.log`. All 115 packaged runtime/test/script files
  matched the already-verified candidate artifact byte-for-byte.
- Earlier full integrated pinned-host/Gateway suite: **837 passed in 48.75s**, no skips.
- A subsequent post-commit shutdown regression and test-only cleanup passed;
  complete affected native module: **27 passed in 1.81s**, and final callback
  regression group: **5 passed in 0.34s**. Runtime bytes are unchanged since the
  full gate; do not label these separate runs an 838-test full-suite result.
- Canonical `python -m pylint hermes_wisdom_plugin tests
  scripts/test_wisdom_offline_acceptance.py`: **10.00/10, exit 0**.
- Fresh offline harness, Portal contract check, build, isolated artifact import,
  and **57-file** wheel/sdist package parity passed. Parent recomputed both
  archive SHA256s from the report.
- Exact extracted sdist: `tests/test_host_commands_integration.py
  tests/test_host_native_update_integration.py tests/test_native_surfaces.py`
  passed **31 tests in 24.57s**, including real pinned-host registration and
  recording Slack/Telegram SDK journeys, with reusable local dependencies.

Logs: `/tmp/wisdom-candidate-integrated-{tests,lint}.log`.
Artifact report: `/tmp/wisdom-candidate-acceptance-20260915/offline-acceptance-report.json`.
Artifacts predate this evidence-only documentation update.
Host/Gateway pins are unchanged from the integrated gate below; Portal checkout
is `94d688ebda860d3ec3129b95914f67d75bea410e`.

This closes native candidate deferral, not the weekly scheduling/cadence policy,
CLI/TUI host opportunity/acknowledgement boundary, four-service smoke, or live
release acceptance. No host changes, Docker startup, publication, or deployment
were performed.

## Core Milestone Continuation — 2026-09-14

The terminal complete-file-set draft slice is integrated and locally verified:
parent ran **186 affected tests** (`test_contribution_review`, `test_publication`,
`test_commands`, `test_package`) in **4.15s**, with touched-module Pylint
**10.00/10**, exit 0. Add/remove/rename uses the existing `files[]` complete-set
save and owner/hash CAS; required roots and path/security checks remain. Real
CLI inspect/save and stale publication receipt/fresh exact-byte publication are
covered with local fixtures. Gateway closed schemas accept no visibility choice;
effective publication policy remains server-owned. Rich file editing UI and
live publication acceptance are not implied.

Native settings and offline artifact acceptance are under parent review in
isolated worktrees; neither is accepted by this checkpoint. The previous
784-test native-delivery gate remains the latest whole-project acceptance
baseline until final integrated checks complete.

## Native Ready-Advice Delivery Gate — 2026-09-14

The bounded private Slack/Telegram delivery slice is locally accepted:
**784 passed, no skips (49.76s)**; canonical package/tests Pylint **10.00/10**
with exit code 0. Wheel and sdist built under `/tmp/wisdom-delivery-dist` and
match all **57 package Python/contract files**. The native sdist contains its
root `plugin.yaml` and `__init__.py`. The final rebuild includes the updated documentation.

```bash
WISDOM_AGENT_DIR=/Users/shannon/Workspace/Nous/hermes-wisdom-plugin-host-check \
WISDOM_HOST_PYTHON=/Users/shannon/.hermes/hermes-agent/venv/bin/python \
WISDOM_GATEWAY_DIR=/Users/shannon/Workspace/Nous/gateway-wisdom-analytics-privacy \
.venv/bin/python -m pytest -q -o faulthandler_timeout=45
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m build --outdir /tmp/wisdom-delivery-dist
```

Host pin: `bf51fee548ceaa28d2c299a41c0f65066c04c061`.
Gateway pin: `16bd05e81b134d1ab8bb230aade6f6de9010cae3`.
Logs: `/tmp/wisdom-delivery-closeout-{pytest,pylint}.log` and
`/tmp/wisdom-delivery-build.log`.

The owning private native lifecycle schedules a bounded drain independently of
whether generation produced new advice. Existing queue leases, owner election,
preferences, remote reservations and SDK send/receipt paths are reused. Ready
rows are not reassessed; pending generation is not claimed by delivery. Cards
contain advisory provenance and exact `wisdom_inspect` handoffs, not approval
controls or implicit consent. CLI/TUI remain manual-inbox surfaces pending the
[verified host presentation gap](HOST-API.md#verified-local-presentation-gap--2026-09-14).

Coverage separates the lifecycle bridge tests from real Slack Bolt/PTB journeys
with recording request transports. It includes ready/fallback draining,
known-non-send retry, unknown-ack non-resend, exact receipts, source qualification,
owner-generation isolation, and original-reservation cancellation after revocation.
Independent review findings were repaired; parent RED/GREEN regressions then
caught late lease expiry during the final metadata read, a valid candidate
fallback rejected solely for absent prose, and owner replacement during the
identity read immediately before settlement. These now deny stale effects or
permit only source-verified fallback, as appropriate.

Cleanup removed the superseded `on_ready`/assessment-ID callback path and its
unused per-job mutations. SDK Gateway/service doubles now live in
`tests/native_sdk_fixtures.py`, sharing policy data with generation tests; the
journey still binds response classes from the actual directory-loaded plugin
namespace. No production module, schema, dependency, scheduler, or host API was
added relative to the 774-test assessment checkpoint; five existing production
modules changed.

This is not live model-quality, deployed Gateway, real-chat delivery, full M7
presentation parity, Desktop/Dashboard, or release acceptance. No live profile,
service, credentials, notification settings, or publication state was changed.

## Queued Recommendation Assessment Gate — 2026-09-14

Closed before starting human-facing delivery: **774 passed, no skips (73.05s)**
with the pinned host/Gateway environment below; canonical
`python -m pylint hermes_wisdom_plugin tests` scored **10.00/10**. Wheel and sdist
built in `/tmp/wisdom-assessment-dist`; both match all **57 package Python/contract
files**. No live service/profile changes or model-provider requests were used.

```bash
WISDOM_AGENT_DIR=/Users/shannon/Workspace/Nous/hermes-wisdom-plugin-host-check \
WISDOM_HOST_PYTHON=/Users/shannon/.hermes/hermes-agent/venv/bin/python \
WISDOM_GATEWAY_DIR=/Users/shannon/Workspace/Nous/gateway-wisdom-analytics-privacy \
.venv/bin/python -m pytest -q -o faulthandler_timeout=45
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m build --outdir /tmp/wisdom-assessment-dist
```

Reference: Agent `0924f7946b937c2ed02d083fc26752d577c54b3b`,
`hermes_wisdom/mediation.py` assessment schemas, history filtering, `assess`,
qualification explanations and assessment portions of `prepare`. The plugin
reuses existing queue leases, exact-version inspection, qualification snapshots,
preferences and inbox; it does not copy legacy credential extraction or consent
machinery. One selective mediation module was added; no new schema, dependency,
scheduler or host modification.

Coverage includes exact batch IDs, bounded/safe prose, canonical response-model
provenance, default/fixed mode, quotas/mutes/suppression, retries/exhaustion,
unread-ready starvation, current owner and all-lease fencing, unload/private
admission, missing/different context-local main model, changed version facts,
real candidate event/source validation and producer → ready → inbox → native
candidate inspection. Registered CLI/TUI hook tests traverse actual `PluginLlm`
and `auxiliary_client.call_llm` with recording HTTP transport. They do not prove
live model quality or actual native proactive delivery.

Parent RED/GREEN checks additionally reproduced owner/lease/admission changes
during the final introduction-history read (3 failures before the fix). That
read now precedes the final authorization fence. All 31 mediation tests passed
after the fix, followed by the full gate above. Independent Luna findings and
parent probes were addressed before acceptance.

The active-route assertion narrowly reads the pinned host's internal
`_RUNTIME_MAIN_CONTEXT`, projecting only session/provider/model. Missing context
waits without global fallback; no public route-assertion API is claimed.
Assessment does not mark content seen, introduced, delivered or approved.
Proactive presentation, settings UI, live model quality, deployed/native delivery
and full release acceptance remain separate work.

## Evidence Levels

1. **Scaffold:** import/registration, package contents, Pylint, and unit tests.
2. **Feature:** focused tests for the selected workflow and its denial/recovery cases.
3. **Host integration:** plugin loaded into the pinned host through real dispatch.
4. **Cross-repository:** real disposable Postgres/MinIO plus pinned services/plugin.
5. **Live acceptance:** authorized human interaction on the actual surface.

Passing one level does not prove the next. Record them independently in
[FEATURES.md](FEATURES.md). The old PR's tests are reference evidence only.

## Authorized Ambiguous Refinement Classifier — 2026-09-14

CW-M6-002 now connects structural-first detection to the real registered host
auxiliary task, after successful private owning-human turns. The explicit
model-data opt-in and payload are documented in the [README](../README.md).
This is advisory qualification evidence, not publication approval or proactive
delivery.

- Parent full pinned-host/Gateway gate: **743 passed in 45.98s**, no skips;
  `.venv/bin/python -m pytest -vv -o faulthandler_timeout=45` with the same
  companion environment/pins recorded below. Both companion checkouts remain clean.
- Canonical package/tests Pylint **10.00/10** and `git diff --check` pass.
  Wheel/sdist under `/tmp/wisdom-refinement-dist` were built and compared against
  all **56 package Python/contract files** and the native sdist entrypoints.
  This evidence-only documentation was finalized after the build.
- The registered-host recording transport verifies complete before/after text,
  including frontmatter and indented/fenced code, configured auxiliary routing,
  no tools, and no model request during skill mutation. SQLite/filesystem tests
  cover deterministic no-call paths, strict malformed/error fallback, private
  admission, and owner/profile/org/installation/config/unload revocation.
- Review reproduced pre-call owner leakage and acceptance after unobserved file
  drift. Both are fixed and rechecked by parent probes. Exact raw text is now
  digest-verified; old normalized/mismatched snapshot text cannot reach the model.
- Removed the obsolete synchronous classifier and its injection-only test path.
  Relative to the verified inbox wheel, three existing source modules changed:
  **353 insertions / 49 deletions**. No new module, schema, dependency or scheduler.
- Pending observations are bounded and in-memory; restart does not replay them.
  Model work is synchronous at turn end, with a 30-second per-request timeout.
  Filesystem checks are not an atomic transaction with arbitrary external writers.
  No live model-quality, Portal-account, native-client, UI or deployed acceptance
  is claimed. No live configuration was enabled, and no commit or push occurred.

## Private Inbox Integration — 2026-09-14

`hermes wisdom inbox` and the registered `wisdom_inbox` now read durable
assessments, unresolved native reviews and installed setup references rather than
returning the feed DTO. `notifications` retains the feed projection. This closes
only the read path; assessment generation and proactive delivery remain open.

The gate covers real SQLite projection, exact enrolment/route isolation, fixed-mode
filtering, newest-first bounded reviews, read-only expiry/uncertain states, and
actual pinned-host terminal dispatch plus registered-tool dispatch using recording
transport. No passive HTTP/model requests or callback execution occur. Independent
review reproduced two first-use defects: missing qualification ownership rejected
an otherwise valid inbox, and a bound owner's first read initialized assessment
tables. Both were repaired. Absent/current/old qualification-marker cases now keep
native reviews visible while leaving SQLite bytes unchanged; reenrollment excludes
prior-owner assessments as well as reviews.

```bash
WISDOM_AGENT_DIR=/Users/shannon/Workspace/Nous/hermes-wisdom-plugin-host-check \
WISDOM_HOST_PYTHON=/Users/shannon/.hermes/hermes-agent/venv/bin/python \
WISDOM_GATEWAY_DIR=/Users/shannon/Workspace/Nous/gateway-wisdom-analytics-privacy \
.venv/bin/python -m pytest -vv -o faulthandler_timeout=45
.venv/bin/python -m pylint hermes_wisdom_plugin tests
git diff --check
.venv/bin/python -m build --outdir /tmp/wisdom-tight-inbox-dist
```

- Parent final run: **718 passed in 42.28s**, no skips. The repair writer also
  recorded 718 passed in 39.70s. One parent `-q -ra` run timed out at 180s without
  a reported failure; the diagnostic rerun above passed unchanged. Cause was not
  established; this is not evidence of deterministic timing.
- Canonical Pylint **10.00/10**, whitespace checks and sdist/wheel build passed.
  All **56 package Python/contract files** match both new archives; native sdist
  entrypoints match too. Evidence documentation was finalized after that build.
- Companion pins: host `bf51fee548ceaa28d2c299a41c0f65066c04c061`, Gateway
  `16bd05e81b134d1ab8bb230aade6f6de9010cae3`; neither checkout was modified.
- Source delta from the verified pre-inbox wheel: four existing modules,
  **110 insertions / 13 deletions**. No new modules, schema, dependency or scheduler.
- No clean installed-profile, live Slack/Telegram, TUI, Desktop, Dashboard,
  four-repository or deployed Portal-account acceptance is claimed. No commit,
  push, publication, live installation or restart was performed.

## Repository Baseline

Use the README commands in a clean virtual environment. Verify that both regular
package import and native synthetic-namespace loading work and that source/wheel
artifacts contain the intended code. Build artifacts and test caches stay
ignored. Run feature-specific registration and behavior checks separately; the
baseline requires no live credentials.

## Focused Feature Matrix

| Area | Required scenarios |
| --- | --- |
| Entitlement | Missing/malformed/expired/mismatched scopes; ordinary member allowed; nonmember/personal/out-of-cohort denied; no credential refresh on read |
| Scope | Ordinary command results use host caller routing; callbacks reject wrong actor/profile/account/org/message/thread; proactive delivery rejects stale routes and subagent/background presentation; member cannot moderate |
| Plugin-owned gate | Load while signed out, login after load, expiry and account/org/profile changes before execution; background checks never refresh; unavailable installed commands deny without core visibility changes or new auth hooks |
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

## Local Contribution Preparation Gate

Independent integration closeout (2026-09-14): 584 full-suite tests passed with
pinned host/Gateway enabled, no skips; after the final test-only route correction,
15 host/contribution tests passed. Canonical Pylint was 10.00/10 and whitespace
checks passed. Both distribution archives matched all 39 package code/contract
files, and 36 modules imported from the extracted wheel outside the checkout
using repository dependencies. This is not a clean-host installation. The later
worker also reported the final full suite passing (584). Artifacts are under
`/tmp/wisdom-contribution-dist`, before final documentation-only edits.

The parent-reproduced duplicate inline YAML metadata defect is fixed by restoring
the reference ruamel round-trip editor. Owner binding is checked before reopening
and model disclosure; unexpected symlinks/special files are rejected rather than
silently omitted from hashes. Full native/update/setup regressions remain green.

Bounded post-review correction (2026-09-14): the real SQLite acknowledgement-loss
probe now reports the committed description with both the current overlay and
original source still present. Cleanup removes a failed edit stage only after a
durable reread proves that stage is unreferenced; an uncertain reread retains it.
Real-store tests cover both post-commit response loss with fresh-store reopen and
pre-commit failure with prior-pointer preservation. Prepared inspect/save now use
a package-limit-derived JSON envelope that round-trips a maximum 512 KiB package
with worst-case control-character escaping, rejects one byte over the derived
bound, and rejects duplicate JSON keys at root or nested object depth. The pinned
focused contribution/host gate passed **194 tests**; the full pinned Agent/Gateway
suite passed **606 tests in 28.64s**, with no skips. Canonical Pylint returned
**10.00/10**, `git diff --check` passed, and both archives rebuilt successfully
under `/tmp/wisdom-draft-edit-dist`. All 39 packaged Python/contract files matched
the working tree in both archives; sdist root `plugin.yaml` and `__init__.py`
matched as well. The integration owner independently reproduced **606 passed in
29.16s**, no skips, Pylint **10.00/10**, and the post-commit probe's retained
current overlay. Independent archive inspection confirmed all 39 package files
and both native sdist entrypoints; all 36 non-root package modules imported from
the extracted wheel outside the checkout using repository dependencies. This is
not clean-host installation or live acceptance.


The implemented contribution boundary starts with
`hermes wisdom suggest <local-skill>` and continues through bounded local
`prepared inspect`, `prepared save <json-file>`, and `prepared review` commands:
owner-private package preparation, complete-existing-file-set editing, local
scanning, editorial metadata, and an optional professionalism review. Run the
local behavior and pinned-host route checks separately:

```bash
.venv/bin/python -m pytest -q \
  tests/test_contribution_review.py tests/test_package.py tests/test_store.py \
  tests/test_commands.py tests/test_runtime.py

WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
WISDOM_HOST_DEPENDENCIES="$PWD/.venv/lib/python3.11/site-packages" \
.venv/bin/python -m pytest -q tests/test_host_commands_integration.py
```

The local gate proves exact prepared bytes and hashes, a save-ready full JSON
export, owner-bound reopening, UUID overlay staging, atomic hash/state/owner CAS
across independent SQLite instances, stale-writer rejection, cleanup after
validation/database failure, unchanged source bytes, complete existing file-set
enforcement, bounded no-follow JSON/overlay reads, local scan behavior, and
truthful unavailable model review. Without `--allow-model-review`, no model
request is permitted during preparation, save, or re-review. With that flag,
the pinned-host probe keeps the real
synchronous `PluginLlm -> call_llm -> OpenAI client` path and the configured
default provider/model; it replaces only the HTTP transport. The recording
transport verifies the exact prepared/edited package and owner description,
omitted tools, and returned provider/model provenance. The same probe exercises
terminal inspect -> edit payload -> save -> local recheck -> authorized re-review
and proves no Wisdom Gateway submission and unchanged source bytes.

These are different evidence levels. Most focused preparation tests are local and use injected model doubles. The pinned-host test exercises real host routing with in-memory recording transports, not deployed services or a real model provider. Preparation alone is not publication evidence.

## Qualification, Weekly Queue, and Publisher Evidence Gate

Independent local closeout: **665 passed in 36.80s**, no skips, with the pinned
Agent/Gateway variables used by the other gates. Canonical Pylint **10.00/10**,
exit 0; source/wheel build and whitespace checks passed. One preceding full-suite
run timed out at 180 seconds without a failure report; a verbose rerun with a
600-second ceiling completed in 36.80s. No timing root cause is established.

The reference `qualification.py`, `weekly_queue.py`, `publisher_evidence.py` and
selected source tests are ported into the shared plugin. Real public host
`skill_view` and approved `skill_manage` routes trigger `on_skill_lifecycle` and
persist observations in disposable state; no manual hook invocation substitutes
for that integration proof. A successful load is a load observation, not proof
of a successful enclosing task. `on_session_end` processes bounded due stability
jobs and is never counted as use.

Coverage includes both independent qualification paths, business-day/timezone
windows, recent-use/stability conditions, snapshot validity, restart/dedupe,
same-org account switches, fresh evidence after reenrollment, preserved audit
rows, schema v3-to-current migration, frozen reviewed publisher copy, and weekly
selection/suppression rules. Parent RED/GREEN verified that swapping a regular
file for a FIFO immediately before open is rejected without blocking.

`hermes wisdom review-week --dry-run --json` is evidence-only; without dry-run,
the command uses current server policy to queue local evidence. It neither
calls a model nor delivers a message. Weekly model-selection helpers and
3/30/60-day deferral kernels have local tests; owning-human assessment delivery,
idle polling, native Not Now wiring and proactive feed presentation remain the
next consumption/surface stages. Ambiguous refinement has a conservative
non-meaningful fallback without an explicitly authorized classifier. This gate
does not claim that an owning-model classifier surface is already wired.

## Shared Consumption and Recovery Gate

Run the owner/read-fence, exact transport, outbox, store, and registered-host
journeys with all pinned checkouts enabled. The host test discovers and registers
the plugin through the unmodified host, dispatches maintenance, notifications,
inbox, mute, and sync through the real CLI parser/controller, and records requests
at the HTTP transport seam rather than replacing the Wisdom client.

```bash
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host-python \
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway \
.venv/bin/python -m pytest -q \
  tests/test_consumption_shared.py tests/test_consumption_transport.py \
  tests/test_outbox_shared.py tests/test_store.py tests/test_client.py \
  tests/test_host_commands_integration.py
```

Coverage includes an org/account/installation/Gateway-route checkpoint and
generation fence before local rows are exposed, no implicit read rebinding,
zero-network passive notifications/inbox/mute-status/sync-status, explicit sync
of pending mute intent, eight-item maintenance enrichment, exact skill/version
metadata validation, and nonvacuous immediate-cadence own-author versus teammate
next-version advice. Delivery claims and recovery reject mismatched organization,
recipient, request, reference, or event acknowledgements; uncertain claims remain
uncertain and remote deferrals are not immediately reclaimed.

This gate proves bounded terminal feed projection and reusable state machines. Explicit
maintenance now queues owner-bound feed/weekly references after policy and preference
checks, but does not run model assessment, send native cards, or invent operation-report
authority.

## Explicit Automatic Update and Mediation Producer Gate

Run the policy/recovery, producer, consumption, and registered-host journey together:

```bash
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host-python \
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway \
.venv/bin/python -m pytest -q \
  tests/test_automatic_maintenance.py tests/test_mediation_producer.py \
  tests/test_consumption_shared.py tests/test_host_commands_integration.py
```

The gate covers `MANUAL` non-mutation, clean `AUTO_WITH_NOTICE`, exact modified-tree
preservation under `REQUIRED`, stale policy refusal, post-promotion recovery, durable
rotation past eight installations, and local notice repair after a post-completion write
failure without repeating the update or Gateway commit. The pinned-host command journey
uses exact policy, mute, suppression, installation, version, content, and record-install
routes through the real parser/controller and recording HTTP seam, then reads the local
`installation_updated` projection with zero network effects. Producer tests verify exact
owner isolation, policy defaults, mute/fixed-mode gates, bounded references, and no model
or delivery call. The real consumption/store regression queues eight feed rows on its first
pass, the ninth feed row plus qualified weekly evidence on its second, and zero new rows on
replay; native feed rows remain unseen and no native weekly assessment is fabricated. This
is isolated host integration, not live Gateway/native delivery.

Local closeout, 2026-09-14: **699 passed in 37.93s**, no skips, with
`WISDOM_AGENT_DIR`, `WISDOM_HOST_PYTHON`, and `WISDOM_GATEWAY_DIR` set to the
pinned local checkouts/interpreter. Canonical Pylint was **10.00/10**, exit 0;
`git diff --check` and isolated sdist/wheel build passed. All 54 plugin source
files exactly matched both archives, and the wheel contains no tests. Build artifacts
were written to `/tmp/wisdom-auto-update-dist`. This is local transport and pinned-host
evidence, not live Gateway/profile, deployed, or release evidence.

## Policy-Routed Publication Gate

The terminal publication continuation adds `publication review`, `submit`, `status`, `reopen`, and `decline`. Its focused gate is:

```bash
.venv/bin/python -m pytest -q \
  tests/test_publication.py tests/test_client_publication.py tests/test_store.py \
  tests/test_commands.py tests/test_runtime.py

WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway \
.venv/bin/python -m pytest -q tests/test_host_commands_integration.py
```

The local regressions prove that the uploaded object closure is constructed from the exact immutable reviewed byte snapshot, and that source drift or a newly blocking current security scan stops before upload. They exercise authoritative exact remote-draft recovery after ambiguous submit outcomes, policy changes before uncommitted effects, one fresh authorization on the same operation, atomic repair of final operation/draft projections, persisted decline intent, and lease fencing against a stale worker.

The clean-host fixture uses the full member response from pinned Gateway `WisdomAgentLedPolicyResponse` rather than an abbreviated invented policy schema. Through the real registered host command it executes review, one receipt confirmation, owner-private CAS/draft submission, owner approval, moderated publication, durable status, host reload, and authoritative `changes_requested` reconciliation. The Gateway is still a local recording server: this is not a deployed/live publication, manager-email, card, or UI acceptance gate.

Pinned host revision: `bf51fee548ceaa28d2c299a41c0f65066c04c061`.
Pinned Gateway contract revision: `16bd05e81b134d1ab8bb230aade6f6de9010cae3`.

Publication closeout (2026-09-14): the integration owner independently ran the
complete pinned Agent/Gateway suite and reported **629 passed, no skips**. Final
publication cleanup then ran **122 changed/client/command tests** and canonical
`.venv/bin/python -m pylint hermes_wisdom_plugin tests`; both passed, with Pylint
**10.00/10** (exit 0). This supersedes the earlier publication worker's partial
620-pass/9-skip run and lint exit 30. The 606-test result above remains the
separate pre-publication baseline, whose canonical Pylint result was 10.00/10.

## C3 Local Setup Gate

Run the C3 document, installed inspection, execution, command/runtime and shared
install-lock tests with the normal repository suite. To enable real-host refusal,
single-spawn acknowledgement-loss, subprocess restart and setup/verification
checks, supply an unmodified pinned Agent checkout and its dependency-capable
Python interpreter:

```bash
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
.venv/bin/python -m pytest -q tests/test_host_setup_integration.py
```

The probe verifies the host revision and import origin, creates disposable
`HOME`/`HERMES_HOME`, and uses harmless marker files and synthetic ownership.
It does not install into a live profile or authenticate to deployed services.
Without `WISDOM_AGENT_DIR` the probe explicitly skips. A controller/host journey
is not native CLI/TUI approval-dispatch or live-surface acceptance.

## U1 Named-Skill Manual Update Gate

Run the review/execution/command/runtime/setup regressions in the normal suite.
To enable the disposable real-host CLI journey, pin the unmodified Agent checkout
and dependency-capable interpreter:

```bash
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
.venv/bin/python -m pytest -q tests/test_host_update_integration.py
```

The focused tests cover absent versus unverifiable authority, immutable review
acknowledgements, sensitive/partial gates, default local-edit blocking, exact
modified-copy preservation, owner/lease loss, both pre-ledger crash windows,
terminal authority loss, explicit terminal versus retryable Gateway codes,
policy-clamped lost responses (including a newer catalog publication), canonical
managed-target enforcement, redirected staging/archive rejection, and final
completion-acknowledgement loss. The
pinned-host test adds real CLI parsing/dispatch, real `UpdateReviewer`, host
skill discovery exclusion for interrupted `.hub` staging, v1 readiness reset,
stale v1 receipt rejection, separate v2 verification and fresh-process ready
state. It uses synthetic authority and no network/live profile.

Local gate, 2026-09-13: **508 passed, no skips** with both `WISDOM_AGENT_DIR`
and `WISDOM_GATEWAY_DIR` enabled; Pylint **10.00/10**, sdist/wheel build and
`git diff --check` passed. Agent revision:
`bf51fee548ceaa28d2c299a41c0f65066c04c061`; Gateway contract checkout:
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`. This is local uncommitted-tree
evidence, not remote CI, live Gateway execution or release approval.

Passing U1 is feature/host-integration evidence only. The later explicit automatic-update
gate is separate; native TUI controls, qualification, proactive feed delivery,
Desktop/Dashboard, and deployed acceptance remain mandatory rather than exclusions.

## Native Slack/Telegram Browse, Install, Update, Setup, and Publication Gate

Run the native controller/surface tests and the pinned-host SDK journeys with an
isolated `HOME`/`HERMES_HOME`:

```bash
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
.venv/bin/python -m pytest -q \
  tests/test_native_review.py tests/test_native_surfaces.py \
  tests/test_native_reference_parity.py tests/test_host_native_update_integration.py
```

The local host gate loads the plugin through `PluginManager`, admits and dispatches
the browse/install/update/publication commands through `GatewayRunner`, dispatches callbacks through real Slack
Bolt and python-telegram-bot applications with recording network transports, and
starts each setup/verification proposal from a later admitted human lifecycle turn.
The host-scoped model path uses a recording HTTP transport; setup execution uses a
disposable local profile and harmless `true` commands. Exact inspection reconstructs
all verified UTF-8 install/update file content across pages and separately checks
rendered Slack blocks. Catalog detail and immutable-version cards retain exact source
descriptions and trusted Portal URLs; publication retains the reviewed hashes,
authoritative status/reopen, and Portal URL on the original card. Denial, duplicate,
edit-retry, stale-handler, delivery fencing, recovery,
and restart behavior also remain covered by the focused controller/host tests.
Long setup guidance is projected through both actual renderers: when the exact
command cannot remain visible, native approval is absent, controller-level forged
confirmation performs no execution, and the card supplies the complete-review
terminal handoff. The same fail-closed rule removes publication approval when the
actual platform renderer truncates its exact review, including forged callbacks.
Pending update reconciliation remains reachable with the same
receipt after consent TTL expiry; generic update recovery cannot replay setup.

This is automated local and pinned-host evidence only. It does not exercise live
Slack Socket Mode, Telegram polling, real provider/Gateway requests, TUI interaction,
Desktop, Dashboard, proactive recommendation delivery, or deployed acceptance.
Native edit ordering is guaranteed only among callbacks sharing one active runtime;
the durable CAS reports outcomes but is not a distributed message-ordering system.

### Final bounded native publication/tool cleanup — 2026-09-14

The final pinned local gate used Agent
`bf51fee548ceaa28d2c299a41c0f65066c04c061`, Gateway
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`, and the host Python recorded
below. The full suite passed **710 tests in 39.21s**, with no skips; the focused
runtime/native/tool/command/registered-host batch passed **127 tests in 16.63s**.
Canonical Pylint passed at **10.00/10**, exit 0. The isolated build at
`/tmp/wisdom-native-publication-dist` produced an sdist and wheel; all 56 packaged
Python/contract source files matched both archives, both native sdist entrypoints
matched source, the wheel contained no tests, and all 54 Python modules imported
from an extracted wheel outside the checkout using the repository environment.

The closing privacy regression uses the real `WisdomStore` and proves that
candidate inspection neither binds nor mutates qualification ownership. An exact
prior owner can inspect before an account change; a same-organization new owner is
denied before reenrollment; after reenrollment the tombstoned prior source remains
denied, while the new owner's fresh candidate is visible only to that exact owner.
Public version validation continues to reject booleans as versions.

At this checkpoint, the three registered model tools were bounded local evidence,
not full historical tool parity, and `wisdom_inbox` was still feed-only. The later
[private inbox gate](#private-inbox-integration--2026-09-14) supersedes that limitation.
Neither checkpoint establishes proactive delivery, TUI, Desktop, Dashboard, or
deployed acceptance.

Session-recovery verification (2026-09-14) independently reproduced **710 passed
in 56.70s**, no skips, canonical Pylint **10.00/10**, and `git diff --check`.
At that recovery checkpoint, the same two existing archives were inspected
without rebuilding: all 56 package Python/contract files and native sdist
entrypoints matched source. Those archives predate the subsequent inbox changes. This confirms code-artifact correspondence, not clean installation,
UI bundles, or deployed acceptance. Both companion checkouts remain clean at the
pins above. GitHub draft PR #1 still points to `c999775` and does not contain this
uncommitted implementation; its CI is not evidence for the current tree.

### Independent native closeout — 2026-09-13

The integration owner independently reviewed the final setup-card repair and
executed the final local tree on `shannonsands/collective-wisdom` (uncommitted):

- Focused command above: **49 passed in 8.35s**.
- Full suite with `WISDOM_AGENT_DIR`, `WISDOM_HOST_PYTHON`, and
  `WISDOM_GATEWAY_DIR`: **567 passed in 25.22s**, no skips.
- `python -m pylint hermes_wisdom_plugin tests`: **10.00/10**, exit 0.
- `python -m build --outdir /tmp/wisdom-native-closeout-dist` and
  `git diff --check`: exit 0.
- All 36 package Python/contract files in both archives matched source bytes;
  sdist `plugin.yaml` and root `__init__.py` matched too. All 33 non-root Python
  modules imported from the extracted wheel in a disposable directory using
  isolated Python, with existing development-environment dependencies. This is
  not a clean-host dependency installation or a live plugin installation.

Host revision: `bf51fee548ceaa28d2c299a41c0f65066c04c061`.
Gateway contract revision: `16bd05e81b134d1ab8bb230aade6f6de9010cae3`.
Both companion worktrees were clean. The host Python was
`/Users/shannon/.hermes/hermes-agent/venv/bin/python`.

The linked SDK journeys now prove original-card Check setup is status-only when
another card owns setup consent, wrong-message approval is denied, correct-card
execution works, and failed → Recheck → Not Now → explicit Check setup produces
a fresh receipt before later approval. An intervening ordinary human turn does
not resurface deferred setup. The real reviewer omits slug; successor metadata
is preserved without adding a fictitious field to the fixture.

Artifact SHA-256:
- Wheel: `7b09ebbb8ca622e321204e3f4583f8171194a7e0b7525f90b15a9d8c91a4140c`
- Sdist: `09712d4cf3b475678d56f301734edec26d09e96cfe1c14ccdc7f26f46a83983c`

This historical checkpoint closed the bounded **local native manual-update/setup**
gate only; later local browse/install/publication coverage does not convert it into
live acceptance. The
live, TUI, full lifecycle, installed-host and release limitations above remain.

## Integrated Draft, Native Settings, and Offline Artifact Gate — 2026-09-14

Parent integrated the isolated native settings source/tests without overwriting
concurrent draft/harness changes or copying stale worker documentation. Bounded
independent native review reported no actionable findings. This is local evidence,
not full core-surface or live acceptance.

- Full pinned-host/Gateway suite: **828 passed in 44.07s**, no skips.
- Canonical `pylint hermes_wisdom_plugin tests scripts/test_wisdom_offline_acceptance.py`:
  **10.00/10, exit 0**. After lint-only cleanup, harness tests: **19 passed**.
- Combined-suite failure exposed acceptance introspection deleting caller package
  modules. A focused regression failed before replacing that import with isolated
  `python -I -B`; full-suite GREEN establishes the repair, not exception masking.
- Real public registration and Slack/PTB recording journeys cover settings
  persistence, exact mode/mute choices and manual review remaining available.
- Offline harness passed against the final integrated runtime: explicit clean
  Agent/Gateway/Portal heads, Portal contract check, 15 snapshot checks, wheel/sdist
  build, 57-file package-byte parity and isolated extracted-sdist import.
- Parent independently extracted that exact sdist and ran
  `tests/test_host_commands_integration.py tests/test_host_native_update_integration.py`
  from the extracted root: **4 passed in 16.79s**. The existing fixtures register
  the extracted plugin through the clean pinned host in disposable profiles and
  exercise CLI/TUI command dispatch plus recording native SDK paths. This extends
  evidence beyond bare import; it uses existing host dependencies, not a fresh
  dependency installation or live renderer/service acceptance.
- Later harness-only timeout handling was observed RED then repaired; harness
  plus tracker checks: **21 passed**, touched-file lint and whitespace clean.
  Runtime package bytes are unchanged by that diagnostic repair.
- Report: `/tmp/wisdom-core-integrated-acceptance-20260914/offline-acceptance-report.json`.
  Parent independently verified both recorded artifact hashes.
- Snapshot SHA-256: `22166ac695a807c3dafea376601d1491924d48fd1ea3896ee76b7ea7534d61b5`.
- Wheel SHA-256: `e6fd18e3d6dccd02970044c32ee81919910fdaad18c72beebb8f3483b46fa0a5`.
- Sdist SHA-256: `e077f2ecb1b74be56ad9dc4a1ec1bb104695036555b2dbe7d3ebe76550a9b156`.

Host `bf51fee548ceaa28d2c299a41c0f65066c04c061`, Gateway
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`, Portal diagnostic checkout
`94d688ebda860d3ec3129b95914f67d75bea410e`. Plugin work is uncommitted.
The artifacts predate subsequent evidence documentation and the harness-only
timeout diagnostic repair; runtime package bytes are unchanged. The offline
scope is not full four-repository service acceptance or fresh dependency
installation. Extracted-artifact registration has the separate evidence above.
CLI/TUI proactive presentation/cadence, remaining UX, real callbacks
against live accounts, and Desktop/Dashboard/deployed release acceptance remain.

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

## Core-Surface Live Acceptance

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
Passing this section is necessary but not sufficient for release; Desktop and
Dashboard remain required.

## Desktop, Dashboard, and Deployment Acceptance

Test installed external bundles, not only component stories or source builds.
Test each SDK independently with the shared backend.
Complete the final release journeys against the deployed endpoints using
Shannon's Nous Portal login. Local scripts, mocks, and stories may establish
lower evidence levels, but they cannot satisfy this release gate. Gateway PR
255 and Portal PR 1022 are verified merged; still record the deployed revisions
and endpoint evidence used for acceptance.

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
