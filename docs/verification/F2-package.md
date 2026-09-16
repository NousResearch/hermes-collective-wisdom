# F2 Package And Compatibility Kernel

Date: 2026-09-12. Scope: instruction-only package preparation, exact downloaded
content verification, and deterministic compatibility evaluation. No service,
installation, remote client, registration, or live action is included.

## Provenance

Mechanical extraction commit `45bc141391093b54ac857608f77e4de1eb1e3ffd`
copied both modules byte for byte from read-only Hermes Agent revision
`0924f7946b937c2ed02d083fc26752d577c54b3b`:

| Source | Source/mechanical SHA-256 |
| --- | --- |
| `hermes_wisdom/package.py` | `7c8ea1581c26e49d3962eb876643b05628a801f6b181b359184011adb686301f` |
| `hermes_wisdom/compatibility.py` | `bd5d388849a56111673373b862694dc964cc83f84a037bd1d498be892f26a3b0` |
| `tests/wisdom/test_package.py` | `5323aada41d3493fe60fcb8b1f25f79ea941241e0b5495a88258b9461767b5aa` |
| `tests/wisdom/test_compatibility.py` | `c52a8d995ebe28119d9efd83838a88d52467727d9a2789ca55e02cbed84e29be` |

The adaptation uses the extracted `.contract` and `.wire` modules. An AST audit
after removing docstrings found exact matches for 15 unchanged package
definitions and 6 unchanged compatibility definitions. Path policy, byte
bounds, canonical hashes, content validation, requirement inference, version
comparison, normalization, and compatibility outcomes therefore retain their
source algorithms.

## Host Boundary

`prepare_package` now requires a `PackageHostProvider`. There is no default and
no import of `agent`, `hermes_cli`, `tools`, or `toolsets`. The provider owns:

- Current Hermes version for a generated manifest.
- Supported SKILL.md frontmatter parsing.
- The host skill security guard verdict for the final exact overlay.

Only `PackageSecurityDecision(allowed=True)` permits preparation to complete.
Blocked, unavailable, malformed, and raising providers become typed fail-closed
`PackagePolicyError` results. Tests also prove that the guard sees the generated
manifest and every staged file. The plugin does not replace or approximate the
host guard.

Editorial metadata is Wisdom business logic, not a host extension. When reviewed
editorial values are present, preparation requires a separate plugin-owned
`EditorialMetadataApplier`; values cannot silently disappear. The pure legacy
editorial dependency closure includes frontmatter parsing, YAML round-tripping,
and atomic writing. Its extraction is deferred rather than mislabelled as host
responsibility. Model-assisted copy lives in the same source file but is not a
dependency of overlay editing and need not be copied with that helper.

The source behavior that treats a frontmatter-provider exception as an empty
mapping is preserved. A future supported host adapter must not use that fallback
to bypass declared requirements: malformed or unreadable requirement metadata
must produce a non-allowing exact-overlay security decision.

`verify_content_files` remains a pure bounded-byte validator. It does not
authorize installation. The later install workflow must materialize the exact
verified bytes and obtain a fresh host-owned install guard verdict before any
activation.

Compatibility detection was intentionally removed because its source
implementation imports private host configuration, toolset, model, credential,
and version APIs. The kernel instead evaluates an explicit `LocalCapabilities`
snapshot. A supported host provider for producing those facts is follow-up work.

## Legacy Test Disposition

All 17 package test functions were retained, including their parameterized path,
allowlist, collision, binary, executable, size, hard-link, special-file,
duplicate-manifest, active-reference, exact-manifest, and hashing cases.

Four compatibility tests were copied directly: deterministic outcomes,
skill-evaluator exclusion, unversioned tool/plugin presence, and platform/
architecture aliases. The old profile-detection test was split: its pure scoped
fact, secret non-retention, and evaluation assertions remain; private host
detection and monkeypatches were omitted pending the supported provider.

New boundary coverage rejects missing, blocked, unavailable, malformed, and
raising host providers; verifies the guard receives the final exact overlay;
and verifies explicit plugin ownership of editorial application.

## Verification

Environment: macOS arm64, parent `.venv`, Python 3.11.14, pytest 9.1.1,
Pydantic 2.13.4, packaging 26.3.

| Check | Result |
| --- | --- |
| Selected-definition AST audit | 15 package and 6 compatibility definitions matched exactly |
| `PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pytest -q tests/test_package.py tests/test_compatibility.py` | 72 passed |
| Same interpreter with `tests/test_contract.py tests/test_wire.py tests/test_package.py tests/test_compatibility.py` | 93 passed |
| `PYTHONPATH="$PWD" ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/package.py hermes_wisdom_plugin/compatibility.py tests/test_package.py tests/test_compatibility.py` | Passed, 10.00/10 |
| Static host-import search over both modules | No private host imports |

Parent integration must add `packaging==26.0` as an explicit runtime dependency,
matching the immutable source revision. No package-data or subpackage changes are
required for these Python modules.

Package installation, atomic replacement, local-edit protection, compatibility
fact detection, service orchestration, consent/recheck, and remote clients remain
incomplete follow-up work.
