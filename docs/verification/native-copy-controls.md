# Native recommendation copy and controls follow-up

## Scope and provenance

Focused follow-up to the copy work in
[Agent PR 4](https://github.com/shannonsands/hermes-agent/pull/4) and
[Agent PR 5](https://github.com/shannonsands/hermes-agent/pull/5), on plugin
base `238ce20f73800b72bb68990cf16936b28d2d3d10`.

Implementation: complete for the bounded native controls below.
Automated: passing isolated Python/controller/Slack delivery checks and Desktop
handoff checks. Live Slack/Telegram, installed-host and service acceptance: **not run**.
This does not promote the broader feature tracker to live acceptance.

## Before / after

- Ready recipient advice previously ended with a `wisdom_inspect` handoff and
  had no recipient actions. It now introduces a newly shared skill in the
  recipient's organization, retains the real assessment explanation, and asks
  “Would you like to install this skill?” A resolved organization display name
  is used only from the existing owner-scoped store; unresolved/unsafe names
  fall back to “your organization” without guessing a name.
- Recipient action order: **View Details**, **Not Now**, **Mute Skill
  Recommendations**, **Install Skill**. Not Now is present only when a real
  deferral is available; install navigation is absent for blocked/pending/
  unavailable security checks. Details display the exact version/hash and
  material findings; “Looks good.” returns without granting consent.
- **Install Skill** opens the existing update-policy chooser, followed by the
  existing exact-version package review and explicit installation consent.
  Advice, details, and fabricated `confirm` callbacks cannot authorize installation.
- Recipient mute and sharing **Snooze Collective Wisdom** open the existing
  native notification settings on the original private owner route. Settings
  explicitly disclose that mute applies to all Collective Wisdom recommendations
  for that account in the organization, not a skill/category. Opening settings
  changes no mute state.
- Actual local preparation failures now produce typed **Share Packaging failed**
  cards with **Retry Packaging**. Exceptions are not printed or persisted in the
  card. Retrying still requires exact-package publication review; this failure
  card intentionally has no settings action.

## Authority and compatibility

Existing native callback binding is reused: organization/enrolment, profile,
actor, session, scope, chat, thread, original message, expiry and adapter lease.
Ready assessment delivery binds controls only after its existing delivery claim,
freshness and settlement checks. Independently delivered recommendations do not
supersede each other's controls. Existing read-only “Looks good.” behavior,
silent dismissal, publication hashes, complete-check aggregation and separate
setup consent remain unchanged.

The advisory projection moved unchanged from `native_runtime` into `native_review`
(the runtime imports it), avoiding a circular import when rendering bound advice.
No host edits, services, schema migration or new authorization system.

## Regression commands

```sh
python -m pytest tests/test_native_recommendation_controls.py tests/test_native_surfaces.py tests/test_native_review.py tests/test_native_reference_parity.py tests/test_mediation.py
python -m pytest
python -m pylint hermes_wisdom_plugin tests
node --experimental-vm-modules --test tests/desktop/plugin.test.mjs
python -m build
```

Tests use disposable profiles, SQLite stores and service/platform doubles; no
real skill installation, publication or connector credentials are required.
The implementation run used an isolated allowlisted environment, blocked external
Python sockets, permitted loopback fixture servers, and an offline wheelhouse for
packaging subprocesses. Optional pinned-host/SDK tests report skips when their
explicit prerequisites are not supplied.

## Explicit exclusions

Upfront sharing authorization **before** packaging is not changed. Candidate
qualification/fallback advice and general digest copy remain on their existing
paths. No gallery, external document, private review corpus, deployment, merge,
Jira update or broader UI redesign is included.
