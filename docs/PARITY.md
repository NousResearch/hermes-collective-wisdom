# Reference Parity and Extraction Ledger

Reference: Agent `0924f7946b937c2ed02d083fc26752d577c54b3b` (reverted PR
94266). Host audit: `31d0a2428e9db346d6781da66f5b37ff3e12def2`. Paths below
are relative to the reference unless explicitly marked host. This ledger is
source evidence, not a claim that the plugin already implements these behaviors.

## Public Behavior Inventory

| Family | Required entries | Primary reference |
| --- | --- | --- |
| Discovery | setup, status, browse/list, show, versions, candidates | hermes_cli/subcommands/wisdom.py; gateway/wisdom_command.py |
| Contribution | scan, suggest/submit, drafts, review, approve, decline | hermes_cli/subcommands/wisdom.py; hermes_wisdom/share_queue.py |
| Consumption | install, installed, installed-setup, check, update (including bulk), uninstall | hermes_wisdom/service.py; hermes_wisdom/setup_execution.py |
| Control/recovery | notifications, mute, sync status/retry, inbox, consent, help | gateway/wisdom_command.py; hermes_wisdom/sync_recovery.py |
| Model tools | wisdom_inbox, wisdom_inspect, present_wisdom_consent | tools/wisdom_tool.py |
| Local/TUI | parser, JSON output, exit codes, pagination/Back, private visibility, explicit confirmation | hermes_cli/cli_wisdom_mixin.py; tui_gateway/session_wisdom.py |
| Mediation actions | inspect, defer, confirm, recheck, checks.show/hide, assessment.show/hide | hermes_wisdom/consent.py; gateway/wisdom_mediation.py |
| Setup actions | setup.status, setup.recover, setup.clear | gateway/wisdom_command_consent.py; hermes_wisdom/installed_setup.py |
| Publication controls | private draft, publish, decline, defer, cancel | gateway/wisdom_command.py; hermes_wisdom/publication_cards.py |
| Telegram | authorized private/group behavior, bounded/escaped content, callback binding, original-message edits | plugins/platforms/telegram/wisdom_adapter.py |
| Slack | workspace/channel/thread/user binding, acknowledgement, Block Kit, update/fallback behavior | plugins/platforms/slack/wisdom_adapter.py; wisdom_blocks.py |

`approve` in the local publication path is owner approval, not permission for a
member to perform organization moderation. Server authorization stays decisive.
Preserve supported public command aliases/options. Old `wi:*` callback encodings
are reference behaviors, not a compatibility requirement for already-sent demo
cards. Fresh-plugin controls must have authenticated, scoped, expiring bindings;
old/unknown tokens cannot authorize operations.

### Read-Surface Extraction Detail

The first command extraction must preserve these distinctions from
`hermes_cli/subcommands/wisdom.py`, not conflate CLI and slash aliases:

| CLI command | Arguments | Output |
| --- | --- | --- |
| `status` | `--json` | Service status; denied entitlement must not probe Gateway |
| `list` | `--json` | Discovery response including cursor |
| `browse` | optional query, `--json` | `skills` wrapper around filtered results |
| `show` | required skill ID, `--json` | Detail including highest positive published version |
| `versions` | required skill ID, `--json` | `versions` wrapper |

Slash `list` aliases `browse`; its human-readable result uses five-item pages.
Search traverses at most 100 pages, detects repeated cursors, and case-folds
slug/author fields. Slug resolution requires exactly one match. This development
slice uses shared-chat-safe output for every slash invocation, including TUI:
private installation/compatibility annotations are deferred. Group discovery
must not read device state. Back navigation must survive browse/show/versions.

CLI JSON is indented by two spaces, key-sorted, Unicode-preserving, and uses
`default=str`. Non-JSON dict/list output remains pretty JSON. Success exits 0;
authentication/not-found/conflict/validation/generic errors exit 3/4/5/6/8 and
retain the `ok`, `error`, `category` envelope. Interruption/declined interactive
confirmation exits 7. Missing/invalid parser arguments retain argparse behavior.

Source view types and text rendering are in `gateway/wisdom_command.py`:
`WisdomCommandContext`, `WisdomAction`, `WisdomItem`, `WisdomView`,
`_local_action_lines`, `render_local_view`, and `_attach_navigation`. Keep readable
commands and Portal URLs in text fallback; never print opaque callback payloads.

Install/update controls are deferred from an initial read-only development
slice, **not excluded from release**. Their source tests remain pending and
must pass again when consumption is integrated. Do not claim browse parity or
ship a release browse card without the planned install/review controls.

## Extraction Units and Reference Tests

All units begin as **pending extraction**, except where a linked verification
record records specific copied code/tests. Tests not yet ported remain pending;
they are not silently omitted or counted as passing.

| Unit | Code to inspect/copy together | Reference test families | Disposition / boundary |
| --- | --- | --- | --- |
| Entitlement | entitlement.py | test_entitlement.py; test_account_session.py | Copy rules; adapt read-only credential/config/profile inputs; host dispatch tests remain separate |
| Profile store | store.py | test_store.py | Copy current schema and transactional operations; explicit plugin data directory; retire only historical migrations |
| Contract/hash | contract.py; package.py; sync wire helpers | test_contract.py; test_package.py; sync-client vectors | Copy pure algorithms/runtime schemas; private identity access is not part of hash logic |
| Client | client.py; client_delivery.py; client_outcome.py | client/delivery/operation outcome tests | Copy bounded validation/wire logic; host credentials/routing via supported boundary |
| Install/compatibility | service.py selected operations; package.py; compatibility.py; consent.py | test_service.py; test_package.py; test_compatibility.py; local consent tests | Preserve exact-content checks and atomic effects; no replacement auth or installguard |
| Setup | setup_execution.py; setup_queue.py; setup_handoff.py; installed_setup.py | test_setup_execution.py; test_setup_prerequisites.py; test_installed_setup.py | Copy state/execution rules; separately approved host execution |
| Updates/preferences | preferences.py; service.py update paths | test_native_install_policy.py; test_operation_outbox.py; command tests | Preserve policy values, modified-file guards, suppression and offline ordering |
| Local checks | local_security.py; professionalism.py; editorial.py; publisher_evidence.py | corresponding security/advisory/editorial/evidence tests | Copy plugin checks, preserve host install-time guards and redaction |
| Publication | share_queue.py; agent_led/share_flow.py; publication_cards.py | test_share_queue.py; test_share_staging.py; test_publication_recovery.py; test_publication_cards.py | Copy workflow; retain source/description invalidation and policy routing |
| Qualification | qualification.py; weekly_queue.py; consumption.py | test_qualification.py; test_weekly_queue.py | Copy deterministic use/refinement/cadence; adapt successful lifecycle facts and skill paths |
| Mediation/feed | mediation.py; mediation_store.py; mediation_view.py | test_mediation.py; test_mediation_store.py; feed coalescing/account lifecycle/reactivation tests | Copy durable rules; replace gateway-owned view types and active-session access |
| Outboxes/recovery | operation_outbox.py; delivery_outbox.py; delivery.py; sync_recovery.py | test_operation_outbox.py; test_delivery_outbox.py; test_delivery.py; test_sync_recovery.py | Copy leases/receipts/reconciliation; native receipt acknowledgement is not a mocked success |
| CLI/TUI | controller/parser from gateway and CLI/TUI integration | parser/slash/command/mediation tests | Copy parser/view behavior, replace registration/dispatch integration |
| Telegram/Slack | native Wisdom adapters/renderers | Telegram command/approval/mute controls; test_slack_wisdom.py | Selectively copy presentation/callback translation; no replacement platform plugins |

Test family names are navigation aids, not an exhaustive test-case count. Each
slice's verification record must give exact source test files and copied cases,
plus reasons for any cases excluded from that extraction. A source test which
depends on an unported workflow is deferred, not deleted for convenience.

## Import Boundary Decisions

### Manual Installation Extraction

The [manual installation record](verification/manual-installation.md) identifies
the selected install/client/compatibility/uninstall units and their replacement
boundary tests. Terminal review, receipts, recovery and removal are this slice's
scope. Reference native-consent, update-worker and callback
tests remain deferred, not removed or counted as passing. The `installed` CLI
parser is an intentional correction of the reference parser gap. Shared slash
installation remains a terminal handoff, so native TUI mutation parity is not
complete.

### Terminal Installed Setup (C3)

`setup_document.py` selectively adapts the reference
`hermes_wisdom/agent_led/setup_document.py` format and setup schema; inspection,
prerequisite/readiness distinctions and explicit uncertain recovery draw from
`installed_setup.py`, `setup_handoff.py`, and `setup_execution.py`. The plugin
uses its own existing hardened receipt store rather than copying the reference
mediation/queue system. No reference test family is counted wholesale as passed.

Replacement coverage lives in `test_setup_document.py`, `test_installed_setup.py`,
`test_setup_execution.py` and `test_host_setup_integration.py`, with CLI/runtime
and install-exclusion cases in their existing suites. The real-host probe checks
approval refusal, single launch, acknowledgement loss, fresh-process status and
an installed-package setup/verification journey with durable reopened state.
It uses synthetic ownership and temporary markers, not Gateway/live login.

The pinned host's foreground runner retries after execution exceptions, so setup
instead uses its guarded background spawn once and waits for a validated result.
Missing or host-redacted process identity/content remains unknown, never a reason
to weaken binding or replay. Source native setup consent, model-authored proposals,
mediation queue/cards, callbacks and TUI/Telegram/Slack/Desktop/Dashboard journeys
remain deferred. Terminal receipt coverage does not close those parity gates.

### Terminal Local Contribution Preparation

`contribution_review.py` selectively adapts the reference local-preparation
boundary from `hermes_wisdom/service.py`, with the package/security kernel,
editorial metadata handling and fixed professionalism schema in this plugin.
`suggest` resolves one explicitly named local skill, creates an owner-private
overlay, records complete hash-bound preparation output, and never submits it.
Prepared draft reopening is bound to the complete enrollment owner. Unsafe or
changed source/overlay bytes fail closed; model-authored copy and assessment do
not certify technical quality, security, or publication eligibility.

The bounded plugin-only continuation adapts reference
`WisdomService.save_prepared` and its `test_service.py` save journey. The new
terminal `prepared inspect` command exports one complete save-ready JSON object;
`prepared save` retains the reference complete-existing-file-set restriction;
and `prepared review` re-runs exact checks. Plugin boundary adaptations add full
owner checking before byte/model disclosure, bounded no-follow input reads, a
fresh UUID overlay, and one durable hash/state/owner compare-and-swap. This is a
new terminal adapter over reference service behavior: no old terminal command
parity is claimed, because the reference save entry was a web route.

Replacement coverage lives in `test_contribution_review.py`, `test_package.py`,
`test_store.py`, `test_commands.py`, `test_runtime.py`, and
`test_host_commands_integration.py`. The pinned-host test preserves the actual
synchronous default `PluginLlm`/auxiliary caller and configured provider/model,
replacing only the OpenAI HTTP transport. It verifies that no request occurs
without explicit `--allow-model-review`; with authorization, the configured
route receives the exact prepared package and owner description with no tools,
and real returned route provenance is retained. The same pinned-host recording
exercises inspect -> edit -> save -> local recheck -> authorized model re-review
without replacing `_sync_caller`. It separately verifies zero Wisdom Gateway
submission and unchanged source bytes.

This closes only local preparation plus bounded complete-existing-file-set
terminal editing evidence. It does not implement or
accept the reference submit/approve/decline flow, private server draft, direct or
moderated publication policy, publication status cards, recovery/outboxes,
publisher ledger, qualification, file add/remove/rename or visibility UI, native
presentation, or live/deployed paths.
No whole M5 feature or reference test family is claimed complete.

### Terminal Policy-Routed Publication

`publication.py` is a bounded adaptation of publication review, owner approval,
policy routing, and recovery behavior from Agent
`0924f7946b937c2ed02d083fc26752d577c54b3b`:
`hermes_wisdom/share_queue.py`, `hermes_wisdom/agent_led/share_flow.py`, and
`hermes_wisdom/publication_cards.py`. Reference navigation and assertions came
from `tests/wisdom/test_share_queue.py`, `test_share_staging.py`, and
`test_publication_recovery.py`; only terminal-relevant cases were adapted.

The plugin-specific boundary persists an immutable reviewed byte closure,
owner/policy-bound receipt, remote draft identity, ambiguous-effect phases,
decline intent, and terminal projections in its existing store. Gateway contract
types and paths are pinned separately; the local client does not select a
publication mode. Replacement evidence is in `test_publication.py`,
`test_client_publication.py`, and the registered-host publication journey in
`test_host_commands_integration.py`. These tests do not claim wholesale passage
of any reference family or publication-card renderer coverage.

### Terminal Named-Skill Manual Update (U1)

`update_review.py` and `update_execution.py` selectively adapt the reference
named-skill check/update behavior while retaining this plugin's owner-bound
receipts, descriptor-held operation locks, host guards and durable journal.
`check` is read-only and distinguishes absent authority from malformed or
otherwise unverifiable Gateway rows. `update --plan` binds the prior managed
record, actual local snapshot, exact incoming package, authority/policy and any
explicit edited-copy destination; apply-time flags cannot widen that receipt.

Directory promotion uses atomic rename; the journal recovers the gap between
moving the old tree and promoting the new one. This is not a cross-system
transaction. The exact prior tree is retained for recovery. Modified
bytes block by default and, only after explicit review, are durably copied via
host-excluded `.hub` staging before promotion to the reviewed unmanaged path.
Recovery validates the ledger and target bytes permitted at each journal phase,
rechecks owner/lease/authority before effects, accepts a server-clamped policy
only after evidence of a committed target version, and quarantines target bytes
after terminal authority rejection. Concurrent/unknown errors remain pending.
Final acknowledgement retry cannot repeat the Gateway commit or file mutation.
Review and recovery require the canonical owner/organization/slug managed path;
redirected staging/archive ancestors are rejected before filesystem mutation.

Replacement coverage lives in `test_update_review.py`,
`test_update_execution.py`, `test_host_update_integration.py`, and the existing
command/runtime/setup suites. The pinned-host CLI journey installs and verifies
v1, edits it, interrupts preservation and target promotion, retries the same
receipt, proves exact-copy preservation and hidden partial staging, rejects an
old v1 setup receipt, separately verifies v2, and reopens v2 ready in a fresh
process. That U1 evidence does not itself prove automatic updates, Not Now, native rich
controls, feed/delivery orchestration, qualification, or live-surface parity; those are
tracked and evidenced by their own gates.

### Explicit Automatic Update and Mediation Maintenance

`automatic_updates.py` selectively adapts pinned legacy
`hermes_wisdom/consumption.py` policy admission and bounded managed-install iteration,
while all file effects remain in this plugin's hardened `UpdateReviewer` and
`UpdateExecutor`. The only modes are `MANUAL`, `AUTO_WITH_NOTICE`, and `REQUIRED`.
Policy authorization is recorded separately from human confirmation; authority, package,
hash, compatibility, local scan, owner, and path facts are revalidated before mutation.
The approved legacy REQUIRED fork-and-converge rule is preserved through the executor's
exact recovery-tree copy before incoming promotion. A rotating owner-bound cursor prevents
later installations from starving across bounded passes.

Completed automatic operations create an idempotent local `installation_updated` feed
projection. The operation journal is the repair authority after a notice-write crash, so
repair cannot repeat file or Gateway effects. `mediation_producer.py` adapts the reference
feed/weekly candidate selection into owner-bound queue references only after fresh policy,
mute, suppression, and quota gates. Existing owner-scoped queue rows are removed before the
eight-item feed bound, so retries reach later feed and weekly work while preserving weekly
quota/cooldown eligibility. Counts come from atomic inserts and native seen/delivery state is
unchanged. It deliberately stops before assessment or delivery.
Coverage lives in `test_automatic_maintenance.py`, `test_mediation_producer.py`, and the
real registered command/HTTP journey in `test_host_commands_integration.py`; no legacy
filesystem executor, fabricated session, model consent, scheduler, or live send is copied.

### Native Slack/Telegram Browse, Install, Update, Setup, Publication, and Tools

The plugin projects native browse/detail/version cards, exact-version install,
named-skill manual update, separately approved setup/verification, and publication
review/status controls through native Slack and Telegram. Publication outcomes and
trusted Portal links update the original card. Exact paged package/file inspection,
receipt acknowledgements, `Not Now`, explicit reopen, renderer-loss fallback, and
callback route/card ownership remain shared controller behavior. Active-runtime
serialization is keyed by physical route/message; delivery CAS records the network
outcome but does not claim distributed ordering across processes.

Pinned-host tests use real plugin discovery, gateway command admission, ordinary
human lifecycle hooks, scoped model routing, Slack Bolt/PTB callback dispatch, the
shared controller/store/filesystem, and recording transports. Registered
`wisdom_inspect` and `present_wisdom_consent` retain read/queue-only boundaries and
revalidate the live admitted private human route. Candidate inspection requires the
exact qualification owner marker and a current non-tombstoned source; same-org
reenrollment cannot disclose the prior member's local candidate.

`wisdom_inbox` now projects owner-bound assessments, unresolved native reviews,
and installed setup inspection references. The admitted model route fences native
reviews to its session/actor/platform/address; terminal inspection may see the
exact enrolled owner's reviews across private routes. Fixed mode retains explicit
work while hiding proactive assessments. Native reviews use the actual
`wisdom_consent` authority rather than a second consent table; manually created
reviews have no assessment ID and none is fabricated. Plans, receipts, commands,
and callback authority are excluded. This is read-only projection parity, not
assessment generation or proactive delivery. See the
[private inbox gate](TESTING.md#private-inbox-integration--2026-09-14). Native TUI, proactive
mediation presentation, Desktop, Dashboard, and all live gates remain open. Explicit
bounded automatic maintenance is covered separately; no background scheduler is claimed.

- Pure contract validation, hashing and deterministic state rules stay in the
  plugin with upstream attribution and their original behavioral tests.
- `hermes_constants.get_hermes_home` becomes an explicit profile data directory
  supplied from `ctx.state.data_dir`, never a fallback to the old database.
- `agent.skill_utils` and private `tools.skill_usage` helpers require call-graph
  classification: extract pure metadata processing, use host facts for ownership
  and successful skill events, and do not infer events from message substrings.
- `tools.skills_guard`, install-time enforcement, and host redaction must retain
  host authority. Use supported hooks or narrowly expose the existing function;
  do not silently replace it with a weaker local copy.
- `tools.skills_sync_client*` mixes wire algorithms with identity/routing. Copy
  necessary wire code, but keep credential ownership with the host and apply
  Gateway execution-time authorization. No unrestricted Sync bypass.
- `gateway.wisdom_command` view classes become plugin-owned canonical views;
  thin adapters translate them without separate business-logic implementations.

## Intentional Exclusions

| Reference material | Reason | Required replacement evidence |
| --- | --- | --- |
| Legacy SQLite migrations/backfills and old callback token acceptance | Fresh start was explicitly approved | Fresh-schema/reopen/version rejection tests; restart and recovery still required |
| Desktop/Dashboard implementation and their surface tests | Sequenced after M0-M8 core work; required for release | Retain all existing feature records, historical estimates, and deployed acceptance |
| Full OpenAPI snapshot | Large development artifact, not required runtime source | Immutable revision plus SHA verification; package runtime schemas/vectors |
| Core Wisdom registration and platform replacement glue | External plugin architecture | Real supported plugin dispatch, lifecycle, native callback, unload and denial tests |
| Duplicated fixture setup | Consolidation only where behavior is equivalent | Original assertions retained; distinct security failures remain separate cases |

## Release Harness Findings

Gateway `scripts/test-wisdom-cross-repo.sh` currently invokes Agent's
`scripts/verify_wisdom_contract.py` and imports `hermes_wisdom.contract` from the
Agent directory. Its delivery/outbox/publication drivers also import old in-core
modules. Merely adding a plugin checkout would produce misleading results.

V1 must change these to load the installed external package, verify its module
origin, and retain real disposable Postgres/MinIO drivers. Record all four full
revisions; preserve existing canonical Unicode/stored-address/recovery checks.
The read token needs access to both private Portal and plugin repositories.
Gateway workflow availability on its default branch and repository-secret
configuration are administrative dependencies, not completed application tests.
Gateway PR 255 and Portal PR 1022 are verified merged, but merge evidence is not
release acceptance. Final proof must exercise the deployed endpoints with
Shannon's Nous Portal login; mocks and local scripts remain supporting evidence.
