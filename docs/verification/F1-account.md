# Plugin-Owned Account Adapter

Date: 2026-09-12. Feature slices: CW-M1-001, CW-M2-001 and CW-M2-004.
Adapter source commit: `52528ef`. No Agent changes or PR #108645 dependency.

## Boundary and Provenance

`account.py` binds the existing client to one profile, account, organization and
Gateway endpoint. It rereads host credentials before each use. The retained
binding contains no bearer; current credentials exclude the bearer from repr.
Removed read scope denies before transport. Changed identity/destination,
missing/expired/malformed credentials and host failures deny with redacted errors.
Same-account token rotation is allowed; account changes within one org are not.

Existing upstream APIs at Agent
`bf51fee548ceaa28d2c299a41c0f65066c04c061`:

- `hermes_constants.get_hermes_home`
- `hermes_cli.profiles.get_active_profile_name`
- `hermes_cli.auth.get_provider_auth_state`
- `hermes_cli.anon_auth.is_guest_state`
- `tools.skills_sync_client.resolve_sync_base_url`
- `hermes_cli.auth_constants._decode_jwt_claims`

The last function is a deliberate, narrow private API coupling, covered by the
real-host check. There is no new JWT parser, refresh flow, credential store,
runtime version-equality gate, invocation framework or core command edit.
Use the Sync endpoint resolver, not `portal_base_url`, for Gateway requests.

This adapts the account/claim boundary referenced by the original
`hermes_wisdom/entitlement.py` and `hermes_wisdom/client.py` at reference
`0924f7946b937c2ed02d083fc26752d577c54b3b`. Existing credential-free entitlement
tests remain unchanged. Host-profile/expiry integration deferred in the initial
F1 extraction now has focused coverage; service/setup/UI tests remain deferred.

## Integration

```python
adapter = HermesAccountAdapter.current()
client = WisdomClient(adapter.credentials, adapter.organization_id)
```

Initialization with no account raises the plugin auth error. The future
registration/controller must catch it and remain available for a later login
without enabling protected work. Before access returns, construct a fresh
binding from current host state. Higher-level workflow wiring still owns
pending-approval invalidation, disclosure, local opt-in and operation-specific
scopes; this adapter does not implement those workflows or proactive delivery.

## Verification

Local integrated results: **232 passed**, including the pinned Gateway artifact
check and real-host account probe; Pylint **10.00/10**. The distribution test
builds and inspects the sdist/wheel. This is not live service acceptance.

The probe uses a separate process, temporary HOME/HERMES_HOME, synthetic unsigned
JWT fixtures, actual host credential files/readers and a blocked socket connect.
It asserts that auth comes from the chosen unmodified checkout and that neither
the rejected invocation module nor old in-core Wisdom is imported. It tests
initial denial, login, rotation, same-org account change, org/profile/endpoint
change, removed scopes, expiry and logout. Unsigned fixtures test local projection
only; they never establish server authorization or contact the Gateway.

Run with the plugin's development Python:

```bash
WISDOM_AGENT_DIR=/path/to/unmodified/hermes-agent \
WISDOM_HOST_PYTHON=/path/to/host/venv/bin/python \
WISDOM_HOST_DEPENDENCIES=/path/to/host/dependencies/site-packages \
WISDOM_GATEWAY_DIR=/path/to/pinned/gateway \
.venv/bin/python -m pytest
```

`WISDOM_HOST_PYTHON` and `WISDOM_HOST_DEPENDENCIES` are optional when the current
Python already has host dependencies. Without `WISDOM_AGENT_DIR`, the probe
explicitly skips. Normal plugin CI does not install Agent; its result must not
be described as passing real-host integration. Without the Gateway checkout,
the separate development artifact check also skips.

This is the foundation's historical result. The subsequent
[terminal-discovery slice](terminal-discovery.md) adds native command wiring,
scope-aware setup, and refresh-free host credential-pool fallback. It also uses
the existing host `_nous_portal_base_url` resolver for Portal navigation; that
narrow private API coupling is covered by real-host tests. Native callbacks,
approval invalidation, proactive selection and four-surface live acceptance remain pending.
No user profile was reset, logged in/out, installed into, or otherwise changed.
