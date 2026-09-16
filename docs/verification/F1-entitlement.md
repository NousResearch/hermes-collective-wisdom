# F1 Entitlement Extraction Verification

Date: 2026-09-12. Scope: independently runnable entitlement policy only.

## Boundary

`hermes_wisdom_plugin/entitlement.py` owns a credential-free local availability
decision. The host must eventually supply a read-only callable that projects
only organization, Wisdom scopes, expiry, and not-before values from its current
authenticated profile. The module imports no Hermes code, reads no credentials,
config, profile, or store directly, performs no refresh or network call, and
caches no positive decision.

Feature opt-in, disclosure acknowledgement, installation registration, and the
active organization are separate explicit inputs. All must pass alongside a
fresh matching entitlement before local work is allowed. Host administrator
status and organization names have no bypass path. Malformed gate objects and
exceptions raised while reading caller-supplied gate state fail closed.

This extraction is runnable and tested, but it does **not** complete
`CW-M2-001`. That tracker feature depends on `CW-M1-001`, and no host reader,
profile binding, protected command/tool handler, or service operation uses this
module yet. The parent integration must add that wiring and prove denial before
network work. The shared tracker and registration remain unchanged in this
slice.

## Provenance

Adapted from the read-only Hermes Agent source at
`0924f7946b937c2ed02d083fc26752d577c54b3b`:

- `hermes_wisdom/entitlement.py`
- `tests/wisdom/test_entitlement.py`

The implementation preserves the source policy while replacing private
`hermes_cli` auth imports, token decoding, service config access, and store
access with narrow caller-supplied values. No bearer, refresh token, credential
file, config object, profile object, store, or general invocation context was
copied into the boundary.

## Legacy Test Disposition

| Legacy test | Disposition |
| --- | --- |
| `test_ordinary_member_entitled_without_global_admin` | Adapted; ordinary read/publish scope succeeds and org/admin-scope mismatches deny. |
| `test_missing_malformed_expired_or_no_read_fails_closed` | Adapted; every malformed value row is retained, with missing and wrong boundary types added. |
| `test_logout_and_token_replacement_have_no_positive_cache` | Adapted to claim absence/replacement; every operation rereads the provider. |
| `test_no_refresh_or_runtime_resolution_for_gate` | Adapted to the read-only callable boundary; a refresh-capable spy remains untouched. |
| `test_local_work_requires_opt_in_and_current_matching_org` | Partly adapted; all local gates, org/scope mismatch, and broad gate-reader failure denial are asserted without copying the store/service. Service errors remain deferred. |
| `test_tools_need_entitlement_and_config` | Deferred; command/tool availability registration belongs to parent host wiring. |
| `test_setup_denied_before_network_for_ineligible_account` | Deferred; setup service is not part of F1. Parent must preserve the before-network assertion. |
| `test_unentitled_status_does_not_probe_gateway` | Deferred; status/service transport is not part of F1. Parent must preserve the no-probe assertion. |
| `test_member_home_no_longer_requires_global_admin` | Deferred; command-home behavior is not present in this extraction. |
| `test_profile_context_selects_its_own_claims` | Deferred; trusted profile ownership and host reader wiring belong to `CW-M1-001` and parent integration. |
| `test_expiry_rechecked_without_token_replacement` | Adapted; expiry and not-before are reevaluated at each operation boundary. |
| `test_real_local_auth_snapshot_never_refreshes` | Private auth access intentionally omitted; fresh rereads, expiry, and no-refresh behavior are covered here, while real host snapshot integration remains deferred. |

`tests/run_agent/test_entitlement_fail_closed.py` was not copied because it
covers OpenAI Codex model fallback entitlement errors, not Collective Wisdom.
No assertion was weakened to make the extraction independent; mixed integration
assertions are named above for later restoration at their owning boundaries.

## Focused Results

Run from this worktree with the parent repository's development environment and
this worktree first on `PYTHONPATH`:

| Command | Result |
| --- | --- |
| `PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/entitlement.py tests/test_entitlement.py` | Passed, 10.00/10 |
| `PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pytest tests/test_entitlement.py` | Passed, 29 tests |

No plugin registration, host installation, live authentication, network call,
push, deployment, or production action was performed. Host integration, remote
CI, cross-repository smoke, and live acceptance remain not run.
