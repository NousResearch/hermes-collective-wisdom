# F1 Profile Store Extraction

## Result

The runnable F1 profile-store slice is complete on branch
`codex/wisdom-store-extraction`. This is a partial implementation of
`CW-M2-003`: it supplies the current-schema SQLite store and its unit coverage,
but does not claim profile/settings host integration, registration, live
verification, or completion of the tracker feature.

No tracker, root packaging, registration, or shared documentation file was
changed.

## Provenance

- Read-only source checkout: `/private/tmp/cw-r5-agent`
- Source revision: `0924f7946b937c2ed02d083fc26752d577c54b3b`
- Source store: `hermes_wisdom/store.py`, 1,804 lines, SHA-256
  `2c931fe59b9d88321b5caf2c0658727f0d8d0aeeffa1ce25ee11181036d1d616`
- Source tests: `tests/wisdom/test_store.py`, 691 lines, SHA-256
  `4a328d26e25124eb4a182c4f9f1e05310daa218fe15d239049fcee7f5b9d2386`
- Mechanical-copy commit: `71c39b0`

The mechanical commit preserves both pinned files byte for byte at their new
paths before adaptation.

## Adaptation

- Moved the runtime module to `hermes_wisdom_plugin/store.py` and tests to
  `tests/test_store.py`.
- Made the profile root mandatory. The caller must pass
  `hostctx.state.data_dir`; the store creates `wisdom.db` directly inside that
  directory. There is no `~/.hermes/wisdom` fallback or private host import.
- Replaced incremental ALTER/backfill logic with direct creation of the
  source-current store-owned columns, indexes, and constraints under the
  independently versioned plugin schema v1. WAL behavior,
  `BEGIN IMMEDIATE` transactions, the process-local reentrant lock, and
  directory/database permissions are retained.
- Added the non-secret schema family marker
  `hermes_wisdom_plugin.profile_store` with an integer version column. Existing
  databases are accepted only when the family is exact, plugin version is the
  integer `1`, matching metadata is present, and every required table/index SQL
  definition matches the expected current shape. Unmarked old feature
  databases, foreign/malformed markers, and incomplete marked databases are
  refused without creating or adopting current tables. Unrelated additional
  objects may coexist for future slices. Data-integrity scanning is not run on
  normal store construction; an explicit recovery diagnostic remains future
  work.
- Retained the coherent store-owned tables and APIs, including
  `managed_install`, operation journals/locks, qualification state, local and
  feed delivery ledgers, drafts/receipts, and professionalism review state.
- Removed the integration hook to `mediation_store.create_schema`; mediation
  tables are not used by `WisdomStore` methods and no fake replacement schema
  or stub was added.
- Selectively copied the exact `WisdomError`/`WisdomConflict` behavior from the
  pinned client into `hermes_wisdom_plugin/errors.py`; the store imports that
  shared class, preserving future client catch behavior, the
  `account_session_changed` code, and exit code 5.

Adapted outputs after this guard fix:

- `hermes_wisdom_plugin/errors.py`: SHA-256
  `375ef990e5338d8f702c330dbc553ccf444b2153768427ade8546b2ba2d31d67`
- `hermes_wisdom_plugin/store.py`: SHA-256
  `232ad5ca4754d6cd247bfc35e95c10d89212771645fe89340378d690fae736e0`
- `tests/test_store.py`: SHA-256
  `d945c1619ee15ef33636d0d0622bdc3bf8b39370944bbbdfd5e1e151729c9e48`

## Test Provenance

Seventeen non-migration source tests were retained. The schema assertion test
was renamed from its historical v9 name and extended to cover the final v12
feed columns and indexes. Assertions continue to cover permissions, identity
and rename behavior, non-adoption after delete/recreate, transaction restart,
candidate reconciliation/sequence/editorial metadata, organization rotation,
managed-install deactivation, professionalism leases, and independent
Telegram/Slack/local feed delivery.

Nine extraction/guard tests were added:

- required explicit root;
- use of `hostctx.state.data_dir` as the database root;
- refusal of an unversioned existing database without adopting its state;
- refusal of an old feature database carrying schema version 12;
- refusal of plugin version 1 metadata without the family marker;
- refusal of foreign and malformed family/version markers;
- refusal of a marked but incomplete schema;
- successful reopen of a complete family-marked plugin schema v1; and
- preservation of the feed-generation conflict error contract.

These four migration-only source tests were intentionally retired because F1
is fresh-start/current-schema-only and must not adopt legacy state:

- `test_schema_v9_backfills_active_organization_events_chronologically`
- `test_schema_v9_rebuilds_legacy_event_identity_without_losing_delivery`
- `test_schema_v7_preserves_v4_usage_in_an_explicit_utc_bucket`
- `test_v7_seeds_legacy_telegram_delivery_into_surface_ledger`

No integration-dependent test was replaced with a fake stub.

## Verification

Commands used the parent repository venv while pinning imports to this
worktree:

```bash
PYTHONPATH="$PWD" /Users/shannon/Workspace/Nous/hermes-collective-wisdom/.venv/bin/python \
  -c 'import hermes_wisdom_plugin.store as s; print(s.__file__)'
PYTHONPATH="$PWD" /Users/shannon/Workspace/Nous/hermes-collective-wisdom/.venv/bin/pytest
PYTHONPATH="$PWD" /Users/shannon/Workspace/Nous/hermes-collective-wisdom/.venv/bin/pylint \
  hermes_wisdom_plugin/errors.py hermes_wisdom_plugin/store.py tests/test_store.py
```

Results:

- Import origin:
  `/Users/shannon/Workspace/Nous/hermes-collective-wisdom-store/hermes_wisdom_plugin/store.py`
- Pytest: 30 passed, including 26 store tests.
- Pylint: 10.00/10.
- Live actions: not run; none are part of F1.

## Remaining CW-M2-003 Work

Parent integration still needs to wire the store root from the real host
context, combine this slice with settings/profile behavior owned elsewhere,
set tracker ownership/evidence/status, and run any broader integration or live
acceptance required for `CW-M2-003`.
