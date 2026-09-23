# Migration and replacement boundaries

## Current decision

Collective Wisdom is a replacement-only, plugin-only implementation. It does not
require Hermes Agent or Gateway source changes, a native-session bridge, a daemon, or
a separate authentication system.

Guaranteed advisory delivery is not a release requirement. The plugin uses a simple
best-effort end-of-turn notice on supported private Desktop and messaging surfaces.
The unresolved Gateway delivery-attempt behavior is intentionally out of scope. If a
future product requirement needs exact delivery guarantees, it must be designed and
accepted as a new change rather than recovered from compatibility code here.

## Removed architecture

The following former responsibilities are not migrated:

- SQLite schemas and migration/import code;
- advisory mediation, model-generated presentation, and history injection;
- delivery and operation outboxes;
- delivery claims, settlements, and outcome reporting used only for notices;
- advisory leases, session-liveness or human-presence taxonomies;
- durable popup/retry/rescue queues and synthetic continuation turns; and
- compatibility wrappers, fallback engines, no-op adapters, and dual writes.

Existing live data is not deleted by this code. There is no automatic importer from an
older database. Test/demo state resets are separate explicitly authorized operations.

## Retained state and safety

Retained local data is stored directly in the host-provided PluginState namespace at
its domain owner: enrollment/setup/publication, qualification, notice cursor/dedup,
native explicit reviews, and managed install/update records. Account, profile, and
organization changes invalidate authority. Effects continue to require fresh
entitlement and exact review-bound consent.

Publication remains direct: prepare, review, submit, then record the server result.
Only small evidence needed to avoid duplicating a genuinely uncertain write is kept.
A confirmed conflict requires explicit re-review and retry. Current Share
authorization timing remains unchanged.

## Notice behavior

A supported end-turn handler checks a bounded feed on each eligible turn and renders one
deterministic notice. Failure preserves the cursor and cannot fail the agent turn.
Missing renderers are successful no-ops. A missed notice stays missed. View opens
read-only details; an explicit Install action authorizes checked installation.
Notification delivery alone never approves an effect. Install approval includes
explicitly declared setup commands; manual setup remains separate.

Plain CLI/TUI chat and unsupported clients receive no proactive notice. Explicit
catalog, contribution, install, publish, settings, and recovery commands remain.

## Operational boundary

Do not delete or rewrite user state to adopt this architecture. Validate new builds in
disposable profiles. Live credentialed, publication, installation, update, and
messaging acceptance requires separate authorization; local mocks do not prove it.
