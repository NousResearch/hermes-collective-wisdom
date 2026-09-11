# Foundation Integration Evidence

Date: 2026-09-12. Plugin source at this verification:
`fe81c44` on `codex/first-release-foundation`.
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

Source and test dispositions are in F1-entitlement.md, F1-store.md, F2.md,
F2-wire.md, and P1-preflight.md in this directory. Schema initialization and
input adapters are documented changes; copied hashing/scanning rules retain
their reference behavior. No legacy database was opened or migrated.

## Local Verification

Python 3.11.14 in the plugin development environment, not the old Agent runtime:

```bash
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway-checkout .venv/bin/python -m pytest
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m pip check
```

Results: **102 passed**, Pylint **10.00/10**, no broken requirements.
The distribution test builds the sdist and wheel and checks packaged resource
digests. The supplied Gateway checkout contains immutable contract revision
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`; the verifier reads that git object,
not uncommitted or current-checkout OpenAPI contents.

Without WISDOM_GATEWAY_DIR, the real OpenAPI-source test is explicitly skipped.
Runtime artifact hashes, wire vectors, and the rejection test still execute.
That normal CI skip does not establish the four-repository release gate.

The earlier batch at `7ffb174b54681a578e86991acb046074ad6600f6` passed remote
CI on Python 3.11 and 3.13 (run 34654819322). Newer-head CI must be checked
separately; that earlier run is not evidence for subsequent store/preflight code.

## Not Completed

These kernels do not complete F1/F2/P1 or any user-facing workflow. Native
registration remains inert. Host credential projection, settings/account wiring,
full client/package preparation, consent, installation/setup/update/publication,
qualification, mediation, native delivery/recovery, and H2 remain unfinished.
The separate Agent H1 branch is a partial command-context implementation, not
the complete trusted tool/lifecycle interface.

No plugin was enabled in a user profile, no gateway was restarted, and no live
skill was installed, submitted or published. Live CLI/TUI/Telegram/Slack checks,
four-repository smoke, and complete first-release acceptance are **not run**.
