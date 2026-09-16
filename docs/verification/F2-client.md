# F2 Read-Only Client Verification

## Scope

This slice extracts only the Collective Wisdom Gateway discovery client used to
list organization skills and inspect skill and version metadata. It does not
register the native plugin, read host internals, parse JWTs, refresh credentials,
fall back to ambient credentials, mutate Gateway state, or perform live calls.

The client accepts a caller-owned `Callable[[], WisdomGatewayCredentials]` and a
separate `expected_organization_id`. Construction and `organization_context()`
do not call the provider. Each HTTP request obtains one credential snapshot,
rejects an organization mismatch before I/O, sends authorization only in that
request, disables redirects, and closes the streamed response.

Availability remains owned by the credential-free entitlement claims reader.
Historical integration boundary at extraction time; a new host facade is no
longer required. The next slice uses a version-tested plugin-side adapter over
existing host readers. Until that adapter is verified, the native entrypoint
remains inert and no private Hermes API is substituted.

## Provenance

- Behavioral source: Hermes Agent
  `0924f7946b937c2ed02d083fc26752d577c54b3b`.
- `hermes_wisdom/client.py`: 887 lines, SHA-256
  `735d136bf885b194fb9c847ca3d9d5c3de470e26002cb68f336291f3fc5e6f62`.
- `tests/wisdom/test_client.py`: 696 lines, SHA-256
  `3c663a8f94bcea58eaadb4b66be0823a711dd4b2ff7a2ddcd33e976d03e2c1c4`.
- Shared error and transport prerequisite cherry-picked as `0eaf60f` from the
  latest foundation commit; it pins `requests==2.33.0`, aligned with the Agent
  lockfile.
- Mechanical DTO/path/error extraction: `4f55ff5`.
- Credential and bounded-transport adaptation: `babff40` plus this boundary
  follow-up.

## Preserved And Adapted

Preserved response DTOs are `SkillSummary`, `SkillList`, `SkillDetail`, and
`VersionDetail`. `SkillSummary.system_spec` continues to use the shared
`SystemSpecification` contract rather than an arbitrary dictionary. Additive
Gateway fields remain allowed while declared fields use strict validation.

Preserved request behavior includes URL quoting, cursor pagination, GET-only
browse/detail/version paths, timeout and connection error mapping, schema
failure handling, and the shared HTTP status/code/exit-code mapping for 401,
403, 404, 409, 422, and other failures.

Adaptation tests add behavior absent from the mechanical source extraction:

- lazy explicit credential-provider access and non-secret organization binding;
- expected-organization rejection before transport;
- no redirects or session-global authorization header;
- local identifier, version, cursor, limit, timeout, and size validation;
- a 2 MiB maximum decoded streamed response, checked against both declared
  `Content-Length` and accumulated chunks;
- JSON content-type, UTF-8/JSON, malformed header, stream-timeout, and response
  cleanup cases.
- one stdlib loopback HTTP server exercise of the default real
  `requests.Session`, including path/header/schema success followed by an
  organization-provider change denied without a second server request.

The 42 focused tests are the adapted/new suite for this client boundary; they
are not represented as 42 mechanically copied legacy tests. Strict declared
response fields, request validation, redirect refusal, and response-byte
bounds are deliberate adaptations beyond the mechanical extraction.

## Reference Test Disposition

The immutable reference contains exactly 31 functions matching `^def test_`.
Only the opaque 404 assertion is directly adapted here. Every workflow test
below is retained as first-release work under its named feature rather than
being excluded from the release.

| Reference test function | Exact disposition |
| --- | --- |
| `test_changes_requested_requires_complete_moderator_metadata` | Deferred to publication-state handling, CW-M5-005. |
| `test_delivery_claim_is_minimal_and_does_not_upload_private_metadata` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_operation_outcome_sends_only_typed_metadata_and_replays_same_identity` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_operation_outcome_refuses_invalid_or_private_fields_before_http` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_operation_outcome_rejects_misbound_responses` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_operation_outcome_rechecks_account_after_request` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_operation_outcome_rejects_untrusted_response_claims` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_operation_outcome_preserves_exact_result_without_implying_verification` | Deferred to durable operation reconciliation, CW-M7-004. |
| `test_delivery_claim_rejects_misbound_responses` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_delivery_claim_rejects_inconsistent_directives` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_delivery_response_rechecks_identity_after_request` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_settlement_sends_only_hashed_receipt_identifiers` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_settlement_without_send_never_fabricates_a_receipt` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_settlement_requires_a_valid_receipt_and_matching_result` | Deferred to durable delivery/outbox work, CW-M7-004. |
| `test_capability_uses_gateway_features_field` | Deferred to artifact diagnostics and host wiring, CW-M8-003. |
| `test_agent_led_policy_reads_member_endpoint_with_bounded_request` | Deferred to assessment policy, CW-M7-002. |
| `test_agent_led_policy_rejects_wrong_org_or_invalid_response` | Deferred to assessment policy, CW-M7-002. |
| `test_unavailable_agent_led_policy_defers_instead_of_enabling_defaults` | Deferred to assessment policy, CW-M7-002. |
| `test_fixed_mode_does_not_fetch_agent_led_policy` | Deferred to fixed assessment opt-out, CW-M7-002. |
| `test_suppression_wire_contains_only_opaque_keys_and_empty_write_body` | Deferred to preferences/offline sync, CW-M4-005. |
| `test_suppression_response_cannot_inject_unrequested_or_foreign_keys` | Deferred to preferences/offline sync, CW-M4-005. |
| `test_mute_wire_preserves_native_duration_vocabulary` | Deferred to mute/settings handling, CW-M4-005. |
| `test_guarded_mute_clear_keeps_idempotency_metadata_on_put` | Deferred to mute/offline synchronization, CW-M4-005. |
| `test_submit_body_has_no_local_candidate_or_activity_signals` | Deferred to private draft review and submission, CW-M5-003/CW-M5-004. |
| `test_not_found_is_opaque` | Directly adapted into `test_http_failures_preserve_status_code_and_exit_contract`; 404 remains `WisdomNotFound`, exit 4, with the server error code preserved and an opaque public message. |
| `test_approve_exact_three_hash_body` | Deferred to hash-bound policy submission, CW-M5-004. |
| `test_revise_binds_predecessor_hashes_and_new_private_commit` | Deferred to private draft/file-manifest review, CW-M5-003. |
| `test_content_fetch_is_bound_to_installation_identity_and_takedown_generation` | Deferred to hash-bound install/content verification, CW-M3-003. |
| `test_raw_copy_fetches_hash_verified_published_bytes_without_install_state` | Deferred to hash-bound package inspection, CW-M3-003. |
| `test_installation_reconciliation_uses_identity_path_and_owned_delete` | Deferred to install and update workflows, CW-M3-003/CW-M4-001. |
| `test_installation_and_feed_responses_are_typed_fail_closed` | Deferred as two coupled assertions: installation reconciliation to CW-M3-003 and feed handling to CW-M7-001. |

## Deferred Work And Excluded Dependencies

Published content and raw-copy methods are intentionally omitted until the
package kernel owner supplies the complete per-file, manifest, and aggregate
hash verification dependency. Returning decoded or encoded bytes without that
verification would imply safety this slice does not provide.

Drafts, publication, installation reconciliation, feed processing, preferences,
delivery claims, operation outcomes, private object sync, and every POST, PUT,
DELETE, or upload path are deferred to the first-release features named above;
they are outside this client slice, not excluded from the release. The excluded
implementation dependencies are the old SyncClient identity resolver, private
host imports, JWT parser, and unrelated dependency closure.

## Automated Evidence

Run from the isolated `codex/wisdom-discovery-client` checkout:

```bash
PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pytest -q tests/test_client.py
PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/client.py tests/test_client.py
```

Result: 42 focused tests passed and Pylint reported 10.00/10. `git diff
--check` also passed. These are isolated fake and loopback transport checks,
not native host integration, external Gateway acceptance, live-profile access,
or content-verification evidence.
