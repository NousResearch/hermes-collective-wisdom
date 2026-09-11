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
slug/author fields. Slug resolution requires exactly one match. Preserve private
installation/compatibility details only on private surfaces: group discovery
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
slice, **not excluded from release one**. Their source tests remain pending and
must pass again when consumption is integrated. Do not claim browse parity or
ship a release-one browse card without the planned install/review controls.

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
| Desktop/Dashboard implementation and their surface tests | M9-M12 follow-up, not first release | Retain all existing feature records and estimates |
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
