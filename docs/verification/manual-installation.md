# Safe Manual Installation

## Boundary

Plugin PR #1, based on `cffef378ae9368798f2ef913e7afa308343c348c`.
Behavioral reference: Agent `0924f7946b937c2ed02d083fc26752d577c54b3b`.
Integration host: unmodified Agent `bf51fee548ceaa28d2c299a41c0f65066c04c061`.
Gateway runtime/schema pin remains `16bd05e81b134d1ab8bb230aade6f6de9010cae3`.
No Agent, Gateway, Portal, production, or live-profile changes belong to this slice.

Terminal-only installation follows exact review, explicit confirmation, local
commit and Gateway acknowledgement. Slash requests return safe terminal
instructions. Installation is not setup approval or verified readiness.

## Extraction and Adaptation

| Reference unit | Disposition |
| --- | --- |
| `client.py`: content decoding, install listing/record/deactivation | Selected functions copied/adapted to the bounded install-scoped client; strict known fields with additive wire compatibility |
| `service.py`: install reference resolution, plan and verify | Selected extraction with static capability and host-scan adapters |
| `service.py`: apply/resume journal phases | Extracted atomic filesystem/local-ledger/Gateway sequencing; hidden staging and fresh ownership/recovery checks added |
| `consumption.py`: uninstall/resume | Selected recoverable removal; no update/feed lifecycle copied |
| `compatibility.py`: static detection | Adapted to captured profile and existing host configuration readers; no commands/network/refresh |
| CLI install/uninstall parser and dispatch | Preserve plan/apply, JSON, cancellation, partial acceptance, and removal confirmation |
| CLI `installed` | Intentional addition: reference advertised this but omitted its parser registration |
| CLI receipt files | Hardened with enrolment binding, file containment/integrity, 24-hour expiry and retained operation outcome |
| Native consent, callback, mediation and operation-outbox tests | Deferred to native surface slice; terminal receipts do not replace actor/message binding |
| Setup execution/verification, update modes and automatic update tests | Deferred; storing effective policy does not implement its worker |
| Legacy migrations and old managed-root markers | Excluded; only an additive v1-to-v2 plugin-schema upgrade, preserving enrolment data |

Reference recovery assertions retained cover staged rename failure, rename
winning before journal advance, and retrying only Gateway recording after local
commit. Boundary tests add receipt path/content tampering, expiry, enrolment
switches, inert slash mutation requests, and unmodified-host discovery.

Concrete reference cases from `tests/wisdom/test_service.py` are
`test_portal_install_url_preserves_explicit_version_selector`,
`test_install_retries_from_staged_bytes_after_directory_swap_failure`,
`test_install_recovery_verifies_target_when_swap_won_before_journal_advance`,
and `test_install_retries_only_gateway_record_after_local_commit`.
`tests/wisdom/test_consumption.py` contributes terminal rejection and recoverable
uninstall assertions, including traversal-slug denial. Adapted coverage lives
in `test_install_review.py`, `test_install_host.py`, and
`test_install_execution.py`; reference update-swap/required-update/feed cases
from that same source file remain deferred to their respective slices.

## Host Dependencies

The host adapter uses existing `tools.skills_guard.scan_skill` and
`should_allow_install(force=False)`. Existing target validators remain enforced;
the Hub installer's destructive replacement path is not used. These are narrow,
version-tested internal imports, not claims of a stable public installer SDK.

`get_skills_dir()` supplies the active profile root. Published skills live in
`skills/_wisdom/<org>/<slug>`. Staging is under `.hub/collective-wisdom` and
recoverable removal under `.archive/collective-wisdom`; both are excluded by
the tested host's real skill scanner. Ordinary hidden-directory names alone
are not sufficient. No old `.managed` skill-root marker is consulted.

Receipt I/O uses descriptor-relative no-follow file operations. Hosts without
these filesystem primitives receive an unavailable error, not an unsafe
fallback. Linux/macOS are the verified receipt platforms for this development
slice; Windows installation acceptance remains unverified.

## Verification

Implemented in the same PR as the foundation, with extraction and adaptation
kept in separate commits. Local verification on 2026-09-12:

- Full plugin suite with all optional pinned-host/contract probes enabled:
  **422 passed**, no skips.
- Real native CLI lifecycle: setup, exact v2 planning/application, duplicate
  application, installed-state read and recoverable uninstall passed. The host
  scanner sees committed skills but not staging/trash; slash mutation handoffs
  produce no network calls or private-state changes.
- Focused executor tests: **29 passed**, including startup/rename/ledger/server
  recovery and permanent rejection quarantine; review/host tests: **20 passed**.
- Pylint: **10.00/10**. Wheel and source distribution build: passed.
- CI is tracked on the [current PR head](https://github.com/NousResearch/hermes-collective-wisdom/pull/1/checks),
  separately from these local results. CI without host checkout variables skips
  the four opt-in host/contract probes; their local evidence is recorded above.

Use the existing isolated host-probe environment:

```bash
WISDOM_AGENT_DIR=/path/to/unmodified-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
WISDOM_HOST_DEPENDENCIES=/path/to/host/site-packages \
WISDOM_GATEWAY_DIR=/path/to/pinned-gateway \
.venv/bin/python -m pytest -q
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m build
```

Live acceptance: **Not run**. Disposable synthetic credentials and a loopback
Gateway prove plugin/host integration, not production authorization or dogfood
acceptance. No setup commands, real team skills, notifications, or demo resets
are exercised.
