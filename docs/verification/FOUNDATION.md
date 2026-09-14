# Foundation Integration Evidence

Date: 2026-09-12. Plugin source at this verification:
`c9809a3` on `codex/first-release-foundation` (subsequent changes are docs only).
Draft delivery PR: https://github.com/NousResearch/hermes-collective-wisdom/pull/1

## Integrated Code

- Credential-free entitlement rules and explicit local-work gates, including
  fail-closed behavior for failed claim/gate readers.
- Pinned contract models, canonical content hashes, runtime schemas/vectors,
  and exact-byte wire object/tree/commit builders.
- Fresh profile store with transactional persistence, separate plugin schema
  identity/version, and rejection of legacy/foreign/incomplete schemas.
- Credential preflight and existing host-guard-result composition. This does
  not invoke a host guard or replace its enforcement.
- Package-resource verification and a development-only pinned OpenAPI check.
- Instruction-only package preparation/content verification and deterministic
  compatibility evaluation, with explicit host guard and capability boundaries.
- Read-only discovery client with bounded responses, per-request organization
  binding, no redirects, and no ambient credential lookup.

Source and test dispositions are in F1-entitlement.md, F1-store.md, F2.md,
F2-wire.md, F2-package.md, F2-client.md, and P1-preflight.md in this directory. Schema initialization and
input adapters are documented changes; copied hashing/scanning rules retain
their reference behavior. No legacy database was opened or migrated.

## Local Verification

Python 3.11.14 in the plugin development environment, not the old Agent runtime:

```bash
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway-checkout .venv/bin/python -m pytest
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m pip check
```

Results: **216 passed**, Pylint **10.00/10**, no broken requirements.
The distribution test builds the sdist and wheel and checks packaged resource
digests. The supplied Gateway checkout contains immutable contract revision
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`; the verifier reads that git object,
not uncommitted or current-checkout OpenAPI contents.

Without WISDOM_GATEWAY_DIR, the real OpenAPI-source test is explicitly skipped.
Runtime artifact hashes, wire vectors, and the rejection test still execute.
That normal CI skip does not establish the four-repository release gate.

The earlier batch at `f9d22198227c472a5e2a282f588095885da72bd5` passed remote
CI on Python 3.11 and 3.13 (run 34655621492). Newer-head CI must be checked
separately; that earlier run is not evidence for subsequent client/package code.

## Clean Wheel Check

Built the wheel and installed it with runtime dependencies only into a fresh
temporary Python 3.11 environment. With Python isolated mode (`-I`), imported
all ten runtime modules, checked the packaged vector digest, and asserted that
`hermes_wisdom`, `hermes_cli`, `agent`, and `tools` were not importable.
Imports and `pip check` passed. The wheel SHA-256 was
`592cb885c4abc13bbb320fa667a52296e6d85e383b3cadd9d2c47c4d8b6dc776`.
This is package independence evidence, not clean-host workflow acceptance.

The real loopback HTTP test additionally checks request path/header/schema
behavior and rejects an organization switch before a second request. It does
not contact Gateway, Portal, or a live account.

## Evidence Boundary

This is a historical kernel/extraction result, not current workflow status.
[Terminal discovery](terminal-discovery.md) and
[manual installation](manual-installation.md) record the subsequent integrated
slices; [FEATURES.md](../FEATURES.md) owns remaining scope and acceptance.
The rejected [host-context experiment](H1-commands.md) is not a dependency.
No live profile, gateway, or team skill was changed during these checks.
