# Collective Wisdom features

This document describes the current plugin-only implementation. Historical milestone
inventories and superseded reliable-delivery designs are intentionally omitted.

## Release scope

The current release is manual skill sharing: a person chooses to share a skill, and
teammates choose to install it. `hermes_wisdom_plugin/release_scope.py` names the
surfaces the plugin registers (`ACTIVE = MANUAL_SHARING`).

Registered: `hermes wisdom` setup, status, catalog, `top`, `share` and its
preparation, publication and recovery commands, installation, updates, setup
approval and uninstall; private `/wisdom` help, status, catalog, `top`, `share`,
install and update reviews with terminal handoffs; the private `/wisdom` dispatch hook; and the Desktop
catalog, installed and contribute pages.

Implemented but not registered in this release: qualification capture and Hermes
sharing suggestions, proactive notices and recommendations, notification settings
and mute, automatic update maintenance, the three model tools, the refinement
auxiliary task, and model-written editorial copy. Preparation uses the skill's own
name and description. Widening `ACTIVE` to `FULL` restores these surfaces; their
tests run under the `full_release_scope` fixture.

## Supported workflows

- Enroll, inspect entitlement, and accept the explicit data-use disclosure.
- Browse and inspect the organization catalog.
- Install a checked exact version with one explicit Install action; inspect details
  optionally, choose whether to run separately disclosed setup commands when present,
  and remove managed installs.
- Check, review, and apply updates while protecting local edits and sensitive
  capability changes. Explicit bounded maintenance may apply only policy-authorized
  automatic updates.
- Record deterministic local usage/refinement signals and qualify contribution
  candidates using fixed thresholds (not registered in this release).
- Prepare owner-private contribution drafts, edit and re-review their exact files and
  hashes, submit them according to server policy, inspect status, reopen conflicts,
  and decline private drafts.
- Terminal `share <skill>` composes preparation, exact-hash review and submission
  behind one Share choice, with separate scanner-warning acknowledgement and
  prompt-free `--yes --json` automation. Retained unfinished operations are not resent.
- Private Slack/Telegram `/wisdom share <local-skill>` opens the same owner-bound
  preparation and review flow for non-CLI operators. It never publishes from the
  command itself; the explicit native **Share My Skill** action remains required.
- `hermes wisdom top` and private `/wisdom top [page]` rank the owner's own local
  skills by Hermes-recorded usage (uses, edits, last used, name) and label skills
  already shared. Group chats are refused without naming skills. Usage is read
  lazily from Hermes core `tools.skill_usage` behind a capability probe.
- After exact successful publication proof, register the unchanged author source in
  place for managed teammate updates. Failed local registration remains separately
  recoverable and never changes the remote publication result.
- Use Desktop management pages and private Slack/Telegram `/wisdom` commands for
  explicit catalog, install, update, setup, and publication reviews. Settings
  reviews are not registered in this release.

Every effect rechecks profile/account/organization ownership, entitlement, and the
reviewed content. A notification, model message, button opening a review, or prior
setup approval cannot authorize a later effect.

## Best-effort proactive notices

Not registered in the manual skill-sharing release. On supported private Desktop and messaging surfaces, an end-of-turn hook may perform
a bounded feed poll on each eligible turn and emit one deterministic human-facing notice.
CLI/TUI instead append one candidate-only suggestion to the current answer after
a verified skill load qualifies. This transform precedes final turn outcome: a
subsequently failed recovered answer may still contain the suggestion. It does not
claim whole-turn success or grant publication consent. Other unsupported clients
receive no proactive notice; their explicit commands remain available.

The notice path:

1. returns early when no renderer is available;
2. uses an owner-local cursor, bounded deduplication, and last-offered markers;
3. reads Gateway policy, mute, and suppression before rendering;
4. preserves the cursor on feed failure and never advances past unconsumed pagination;
5. calls the surface directly, without model assessment or history injection; and
6. treats renderer failure as a missed notice, never as a failed agent turn.

There is no delivery claim, settlement, outcome report, delivery/operation outbox,
advisory lease, liveness registry, rescue queue, retry worker, synthetic continuation,
or exact cross-device delivery cap. Opening a notice starts the ordinary explicit
review. It never grants consent.

## Local state

The plugin uses the host-injected PluginState namespace. Each retained domain owns a
small JSON record:

- direct enrollment/setup/publication records;
- qualification counters and candidates;
- notice cursor and bounded offer markers;
- explicit native review records; and
- managed install/update records owned by the install subsystem.

Shared timestamps use one short aggregate read-modify-write lock. No network call,
rendering wait, or package operation occurs while that lock is held. The plugin has no
SQLite schema, migration, importer, compatibility store, or dual-write path.

## Surface boundaries

- Terminal commands are the complete explicit workflow and recovery surface.
- Slack and Telegram support private `/wisdom` commands and actor/route-bound review
  callbacks. Group-sensitive effects are rejected.
- Desktop uses its supported turn event, plugin REST namespace, notification API,
  and Collective Wisdom page. Discovery slash commands open an SDK dialog through
  composer middleware, reusing the same catalog and exact package review components.
- CLI/TUI chat offers newly qualified local candidates in answer text, with a
  terminal-only private draft review handoff. Unsupported/non-gateway clients do
  not receive proactive notices.
- Dashboard/full embedded parity, Agent/Gateway core changes, and a separate daemon
  are out of scope.

## Reviewed native flow

Private Slack/Telegram notices now open the existing actionable review flow. Sharing
and install/update invitations separate read-only details from exact-content consent;
complete review pages stay within provider limits without trimming approved objects.
See [restoration scope and evidence](REVIEW_RESTORATION.md) for the per-item matrix,
remaining limitations, and the explicit file-complexity tradeoff.

## Verification boundary

Automated tests are offline and disposable. Live Gateway publication, installation,
or credentialed messaging journeys require separate authorization and evidence.
See `docs/TESTING.md` for current commands and recorded acceptance boundaries.
