# P1 Local Preflight Extraction Verification

Date: 2026-09-12. Scope: independently runnable local preflight policy only.

## Boundary

`hermes_wisdom_plugin/credential_scan.py` owns the legacy credential patterns
and location-only detector. `hermes_wisdom_plugin/local_security.py` owns local
preflight aggregation. The regexes and decision algorithm are unchanged from
the pinned source except for adapting the detector import to the plugin package.

The host skills guard remains authoritative. This slice accepts its result as
the existing explicit `scan` input; it does not copy `tools.skills_guard`, call
private host code, or invent a replacement guard. A missing, malformed, denied,
or unknown guard result never passes and never allows upload. Raw guard findings,
scanner reasons, and credential values are not included in output details.

This extraction is runnable and tested, but P1 remains partial. It does not
complete `CW-M5-001` until parent integration invokes the real host guard and
binds the returned facts into preparation before any upload. It does not begin
`CW-M5-002`: professionalism, editorial checks, and publisher evidence are
intentionally reserved for later P1 work. Workflow/card integration and shared
registration or tracker changes are outside this worktree.

## Provenance

Copied and adapted from read-only Hermes Agent revision
`0924f7946b937c2ed02d083fc26752d577c54b3b`:

- `hermes_wisdom/local_security.py`: `prepared_security_check` and its complete
  73-line module behavior, with only the detector import path adapted.
- `hermes_wisdom/agent_led/share_flow.py`: only `CREDENTIAL_PATTERNS` and
  `scan_credentials`; no `ShareFlow`, model callback, persistence, packaging,
  organization-specific scanner, or dependency closure was copied.
- `tests/wisdom/test_local_security.py`: relevant pure preflight assertions.
- `tests/wisdom/test_agent_led.py`: long-skill-name and key-shape detector cases,
  plus the direct AWS detector assertion from the share-flow rejection test.

## Source Test Disposition

| Source test | Disposition |
| --- | --- |
| `test_local_scan_blocks_credentials_without_echoing_them` | Copied and import-adapted with both file and description cases and all three finding groups unchanged. |
| `test_local_guard_failure_is_not_a_pass` | Copied; blocked and unknown host results still deny upload. Missing-host and advisory cases were added at the same pure boundary. |
| `test_local_checklist_does_not_claim_gateway_checks_have_run` | Partly adapted; local pass, both pending Gateway rows, exact pending detail, and summary claims are asserted directly. `full_review_text` icon/render assertions are deferred to the presentation owner. |
| `test_approval_card_surfaces_local_results_and_preserves_blocking` | Deferred in full; `WisdomConsent`, store state, action projection, compact/expanded card text, and confirm-action visibility belong to later workflow/card integration. Its underlying blocked-versus-allowed preflight facts remain covered here. |
| `test_credential_detector_allows_long_skill_names` | Adapted from `PackagedFile` validation to direct `scan_credentials`; both original long names remain accepted. |
| `test_credential_detector_still_rejects_key_shapes` | Adapted to direct detector findings for all six original prefixes, with source-valid character bodies for each unchanged regex. |
| `test_share_flow_rejects_leaked_credentials_in_package` | Only its direct AWS `scan_credentials` assertion is copied into the key-shape matrix. Share-flow state, model packaging, and `SchemaRejected` assertions are deferred to P2/schema integration. |

No card, model, store, share-flow, professionalism, editorial, or publisher
fixture was stubbed merely to run these tests. Deferred assertions are not
counted as passing.

## Focused Results

Run from this worktree with the parent repository environment and this worktree
first on `PYTHONPATH`:

| Command | Result |
| --- | --- |
| `PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/local_security.py hermes_wisdom_plugin/credential_scan.py tests/test_local_security.py tests/test_credential_scan.py` | Passed, 10.00/10 |
| `PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pytest tests/test_local_security.py tests/test_credential_scan.py` | Passed, 20 tests |

No host integration, plugin registration, credential read, network call, model
call, upload, live action, push, or deployment was performed. Remote CI,
cross-repository smoke, and live acceptance remain not run.
