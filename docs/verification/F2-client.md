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
There is no public host account/request facade yet, so the native entrypoint
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

## Deferred And Excluded

Published content and raw-copy methods are intentionally omitted until the
package kernel owner supplies the complete per-file, manifest, and aggregate
hash verification dependency. Returning decoded or encoded bytes without that
verification would imply safety this slice does not provide.

Drafts, publication, installation reconciliation, feed processing, preferences,
delivery claims, operation outcomes, private object sync, and every POST, PUT,
DELETE, or upload path remain excluded. No old SyncClient identity resolver,
private host import, JWT parser, or dependency closure was copied.

## Automated Evidence

Run from the isolated `codex/wisdom-discovery-client` checkout:

```bash
PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pytest -q tests/test_client.py
PYTHONPATH=$PWD ../hermes-collective-wisdom/.venv/bin/python -m pylint hermes_wisdom_plugin/client.py tests/test_client.py
```

Result: 41 focused tests passed and Pylint reported 10.00/10. `git diff
--check` also passed. These are isolated fake-transport checks, not native host
integration, live Gateway acceptance, or content-verification evidence.
