# F2 Wire Algorithm Extraction

Date: 2026-09-12. Scope: pure content-addressed object, tree, and commit
builders needed by the later Collective Wisdom package and client extraction.
No host integration, authentication, remote client, or package preparation is
implemented by this slice.

## Provenance And Selection

Source: read-only Hermes Agent revision
`0924f7946b937c2ed02d083fc26752d577c54b3b`, file
`tools/skills_sync_client_wire.py` (343 lines), source SHA-256
`1922415856eeff42086de6747b1117a17750475461a44c6ad390d199edfcd0bf`.

The extracted closure contains the constants and implementations for
`wire_address`, `canonical_json_bytes`, `ObjectSet`, `_entry`, `_add_tree`,
`_file_mode`, `build_tree`, and `build_commit`. Formatting, variable names, and
Pylint docstrings are adaptations; the addressing, canonicalization, UTF-8
sort key, mode detection, size bound, object deduplication, and commit shape are
unchanged.

Deliberately omitted as unused by this kernel:

- Sync-manifest build/parse routines.
- Profile-root nesting and root-tree assembly.
- HTTP/CAS clients, capability/version checks, remote tree reads, and
  materialization.
- Organization scope, merge, push/pull, host identity, and authentication.

The old Wisdom package imports `ObjectSet`, `build_tree`, and `build_commit`;
the old Wisdom client imports `wire_address` and the blob/tree/commit kind
constants. Those clients are not copied in this slice.

## Original Test Disposition

The ten relevant tests from legacy `tests/tools/test_skills_sync_client.py` are
adapted into `tests/test_wire.py`: full SHA-256 address, distinction from the
truncated local namespace, sorted compact canonical JSON, canonical stability,
blob/exec tree entries, mixed BMP/astral UTF-8 ordering and fixed hash, blob
deduplication, symlink exclusion, bounded-file rejection, and commit shape.

Access-gate, sync-manifest, mock HTTP server, push/pull, merge, organization,
device identity, and remote materialization tests are omitted because their
production routines were intentionally not extracted.

## Verification

Run with the parent environment while forcing imports to this worktree:

```bash
PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pytest -q \
  tests/test_wire.py
PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pylint \
  hermes_wisdom_plugin/wire.py tests/test_wire.py
```

Environment: macOS arm64, parent `.venv`, Python 3.11.14, pytest 9.1.1.

| Check | Result |
| --- | --- |
| Selected-definition AST comparison after removing docstrings | All 8 definitions match exactly |
| `PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pytest -q tests/test_wire.py` | 10 passed |
| `PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/wire.py tests/test_wire.py` | Passed, 10.00/10 |

Package preparation and client integration remain follow-up work.
