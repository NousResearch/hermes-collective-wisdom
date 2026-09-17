# Agent Guide

Read README.md, docs/FEATURES.md, and docs/MIGRATION.md before starting a slice.

## Scope

This is an external Hermes plugin, not a Codex plugin or an Agent fork. Keep
Wisdom-specific code here. Generic host extensions require separate, small
Hermes Agent PRs. Do not copy the original 80k-line PR wholesale.

Current delivery is stock-Agent-compatible native workflows plus external
Desktop management pages using the existing authenticated plugin REST namespace.
No Hermes Agent source changes, native-session HTTP bridge, new daemon or separate
authentication system. Dashboard/full six-surface parity are deferred.
Offline tests do not replace installed Desktop or service acceptance.
Work only on the requested slice; planning documents do not authorize the roadmap.
Use `shannonsands/<feature>` for new branches.

## Implementation

- Choose a ready feature ID and satisfy its explicit dependencies first.
- Prefer existing host APIs and the shared workflow service. No monkey patches,
  replacement platform plugins, or direct imports of desktop app internals.
- Use relative imports within the Python package: native Hermes directory
  discovery loads the root entrypoint under a synthetic package namespace.
- Preserve fresh consent, entitlement checks, profile/session ownership, and
  idempotency. Model-written prose never authorizes an operation.
- Do not trigger recommendations or consent from subagents/background actors.
- Superseded legacy code need not be retained. Replace it with the current
  implementation and meaningful tests, not compatibility wrappers or dead copies
  (including the old `ConsentController.pending`). Keep necessary attribution.
- Keep reviewable PRs narrow. Separate mechanical extraction from behavior
  changes. Do not delete safety tests solely to reduce line count.
- Use apply_patch for manual edits. Preserve unrelated working-tree changes.

## Delegation and Evidence

The project owner requests gpt-5.6-luna for bounded investigation and
gpt-5.6-sol for implementation. Delegate only disjoint, concrete work; review
the result before integration. These roles do not replace live acceptance.

Update the feature's implementation, automated, and live status independently.
Record commands, immutable revisions, results, and any blocked checks. Never
mark an old core test result as proof that the plugin works.

Never reset a demo profile, publish a skill, install a team skill, restart a
gateway, enable emails, or change production flags as an incidental test.
Obtain explicit authorization for the particular live action.
