# Security and Authority

This document specifies the target behavior. Milestone 0 implements no Wisdom
operations. A Python plugin runs in the trusted host process; plugin API
capability gates are not a sandbox against malicious Python code.

## Entitlement and Identity

- Reuse existing OAuth credentials. Locally decoded Wisdom scopes, organization
  identity, and expiry are availability hints, not proof of server authority.
- Missing, expired, malformed, or mismatched entitlement fails closed. Never
  refresh credentials simply to populate menus or presentation state.
- Do not hardcode a Nous organization ID or grant access to global admins.
  Portal flag targeting controls newly issued scopes; Gateway checks requests.
- Keep plugin enablement, feature opt-in, disclosure, and server entitlement
  distinct. Proactive qualification/reviews/delivery require all applicable gates.
- Check current profile/account/org/scopes/expiry inside the plugin before
  protected operations, not only at load. Ownership changes invalidate cached
  state and pending approvals. Background availability checks never refresh.
- Ordinary commands rely on existing host admission and reply-to-caller routing;
  no new universal invocation context or core menu/completion gate is required.
  Installed but unavailable commands may return a clear denial instead of hiding.
- Native callbacks bind the actor, message/route, profile/account and exact
  operation; the host account's entitlement is not authority for another person
  to click a shared approval. Preserve host admission and group privacy.
- Proactive recommendations and consent target only an eligible active main
  user-facing session. Subagents/background jobs cannot present or approve them;
  they are not ordinary command replies with an existing return route.
- Members can use member-scoped operations. Policy management and moderation
  remain OWNER/ADMIN-only server operations; UI visibility is not enforcement.

## Consent and Content

The model may explain and recommend. It cannot supply authoritative receipts,
hashes, callback payloads, destinations, identities, permissions, or approvals.
Do not treat conversational agreement as native confirmation.

Bind consent to exact version, content, manifest, description, and operation.
Recheck at apply time. Expiry, source drift, changed requirements, or local
modifications require a fresh review, not silent renewal. Do not overwrite
local edits or expand sensitive permissions under a previous approval.

Installation does not authorize setup commands, credentials, elevated actions,
or verification commands. Present those requirements separately. Unknown
security/compatibility results are not successful checks. Professionalism
assessments are advisory; scan wording is not a safety certification.

## Delivery and Persistence

Scope native callback handlers narrowly. Revalidate actor and route before
execution; reject cross-profile or cross-thread controls. Repeated callbacks
must not repeat an operation. A completed render is not delivery acknowledgement.
Ambiguous sends require reconciliation, not blind retry.

Keep transactional queues/receipts in the plugin's profile data directory.
Cancel owned tasks on unload. Resume durable work only after fresh authorization
and generation checks. Stale callbacks cannot revive consent after reload.

## Frontends and Privacy

Desktop/Dashboard backend routes enforce the same checks as tools and commands.
Clear query data and invalidate pending actions when profile/account/connection
changes. Use host authentication helpers, never read browser token globals or
build credential-bearing URLs by hand. Polling remains a fallback for unavailable
or disconnected sockets, including Desktop OAuth remotes.

Escape and bound skill, publisher, and model text. Do not upload local package
contents until the explicit private-draft approval. Usage evidence is aggregate,
client-reported, editable, and bound to the approved description; no invented
publisher reliance or silent telemetry. Redact secrets from diagnostics.

## Reporting

Report security issues privately to repository maintainers. Do not attach live
tokens or private skill contents to issues or PRs. Production rollout, flag
changes, and email enablement require separate operational authorization.
