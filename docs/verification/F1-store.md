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
- Replaced incremental ALTER/backfill logic with direct creation of schema v12.
  Current store-owned columns, indexes, constraints, WAL behavior,
  `BEGIN IMMEDIATE` transactions, the process-local reentrant lock, and
  directory/database permissions are retained.
- Existing databases are accepted only when `schema_meta.schema_version` is
  exactly 12. Unversioned, malformed, older, and newer schemas are refused
  without creating or adopting current tables.
- Retained the coherent store-owned tables and APIs, including
  `managed_install`, operation journals/locks, qualification state, local and
  feed delivery ledgers, drafts/receipts, and professionalism review state.
- Removed the integration hook to `mediation_store.create_schema`; mediation
  tables are not used by `WisdomStore` methods and no fake replacement schema
  or stub was added.
- Replaced the late private `.client.WisdomConflict` import with a local error
  preserving the message, `account_session_changed` code, and exit code 5.

## Test Provenance

Seventeen non-migration source tests were retained. The schema assertion test
was renamed from its historical v9 name and extended to cover the final v12
feed columns and indexes. Assertions continue to cover permissions, identity
and rename behavior, non-adoption after delete/recreate, transaction restart,
candidate reconciliation/sequence/editorial metadata, organization rotation,
managed-install deactivation, professionalism leases, and independent
Telegram/Slack/local feed delivery.

Five extraction tests were added:

- required explicit root;
- use of `hostctx.state.data_dir` as the database root;
- refusal of an unversioned existing database without adopting its state;
- refusal of an unsupported schema version; and
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
  hermes_wisdom_plugin/store.py tests/test_store.py
```

Results:

- Import origin:
  `/Users/shannon/Workspace/Nous/hermes-collective-wisdom-store/hermes_wisdom_plugin/store.py`
- Pytest: 26 passed, including 22 store tests.
- Pylint: 10.00/10.
- Live actions: not run; none are part of F1.

## Remaining CW-M2-003 Work

Parent integration still needs to wire the store root from the real host
context, combine this slice with settings/profile behavior owned elsewhere,
set tracker ownership/evidence/status, and run any broader integration or live
acceptance required for `CW-M2-003`.
