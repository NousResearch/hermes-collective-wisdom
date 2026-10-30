# Collective Wisdom features

This document describes the current plugin-only implementation. Historical milestone
inventories and superseded reliable-delivery designs are intentionally omitted.

## Release scope

The current release is manual skill sharing: a person chooses to share a skill, and
teammates choose to install it. `hermes_wisdom_plugin/release_scope.py` names the
surfaces the plugin registers (`ACTIVE = AUTO_SUGGEST`: manual sharing plus
automatic share suggestions).

Registered: `hermes wisdom` setup, status, catalog, `top`, `share-suggest`,
`share` and its preparation, publication and recovery commands, installation,
updates, setup approval and uninstall; private `/wisdom` help, status, catalog,
`top`, `share-suggest`, `share`, install, update and uninstall reviews with terminal handoffs; the private `/wisdom` dispatch hook; and the Desktop
catalog, installed and contribute pages. On a host with `ctx.fork_session` or
`ctx.side_question`, also
`hermes wisdom suggestions`, private `/wisdom suggestions`, the `on_skill_lifecycle`,
`post_tool_call` and `post_llm_call` hooks and the `wisdom_share_suggestion` tool;
on a host with `ctx.fork_session`, also the auxiliary task
`hermes_collective_wisdom_share_suggest`.

Implemented but not registered in this release: qualification capture and the
candidate-based proactive sharing suggestions, proactive notices and recommendations, notification settings
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
  already shared. Only the CLI, TUI, Desktop and private messaging chats are
  answered; groups, channels and webhook/API routes are refused without naming
  skills. Usage and candidate skills are read lazily from Hermes core
  `tools.skill_usage` behind a capability probe, and every listed skill resolves
  through the same profile-fenced resolver `share` uses.
- Private `/wisdom share-suggest`, only when the user runs it, starts one normal
  Hermes turn in the same conversation through `PluginContext.inject_message`,
  listing up to 30 top skills and asking for up to 10 ranked suggestions with the
  exact share command for that surface (`/wisdom share <skill>` in admitted
  Telegram/Slack private chats, otherwise `hermes wisdom share <skill>`). The model
  may read skill files and is told not to run share or publish commands.
  Without injection (no `allow_gateway_injection`, or no identifiable session) it
  replies with the ranked list and a ready-to-send prompt. The terminal command
  prints the first `top` page and points to the chat command.
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

## Automatic share suggestions

`hermes_wisdom_plugin/auto_suggest.py` and `auto_suggest_card.py`. Registered only
when the release scope has `auto_suggest` and the host offers `ctx.fork_session`
(Hermes with plugin session forks) or `ctx.side_question` (plugin side questions);
otherwise nothing is registered and manual sharing is unchanged. Both methods are
detected at register time. `plugin.yaml` declares
`capabilities: [llm.session_fork, llm.side_question]`, so one consent prompt covers
either path; Hermes releases without session forks ignore the unknown id. Hermes
asks for the grants on install, enable and update (only when the declared set
changed), on an interactive terminal only, and stores them as
`plugins.entries.<id>.granted_capabilities`. Core issues the `post_llm_call`
`side_question_token` (it backs one side question or one fork) only to a loaded
plugin that declares and was granted one of them, and never on unattended turns.

Which path runs is decided per turn, from the grants:

| `ctx.fork_session` + `llm.session_fork` | `ctx.side_question` + `llm.side_question` | Path |
|---|---|---|
| yes | any | session fork |
| no | yes | side question |
| no | no | off |

When the host refuses a fork (`fork_session` returns None), the same turn asks by side
question when `llm.side_question` is granted, with the same token. A refused fork may already
have spent that token: no answer then leaves the skill pending, not declined. The process uses
the side question from then on; a restart tries the fork again.

Cadence:

1. Off when `skills.creation_nudge_interval <= 0`,
   `auxiliary.background_review.enabled` is false, the plugin setting
   `auto_suggest` is false, or neither path above is available.
2. Candidates come only from `on_skill_lifecycle` facts with provenance `local` or
   `agent_created` (never team-installed `org:skill` names, bundled or Hub skills):
   created or patched in the session, or loaded and among the owner's top 30 by
   usage. `InvocationService.share_candidate` must resolve the skill as the owner's
   own and fingerprint it under the share path rules; skills already shared are
   skipped (update suggestions are a separate policy layer).
3. Fire point: `post_llm_call` of a later attended turn of the same session that
   carries `side_question_token`. When the hook also carries `review_signals`,
   the check fires only on a turn where `skill_review_due` is true,
   `background_review_enabled` is true and `skill_nudge_interval` is positive: the
   exact decision core uses for its own skill review. Without `review_signals`
   (older hosts) it fires after at least `creation_nudge_interval` tool calls
   (counted with `post_tool_call`) since the last check in that session. At most
   `auto_suggest_daily_limit` (default 1) per profile per day; a skill revision
   (content hash) is asked about once; a suppressed skill is never asked about.
4. Session fork: on a daemon thread, `ctx.fork_session(token, prompt,
   tools=("wisdom_share_suggestion",), max_iterations=4, timeout=300,
   task="hermes_collective_wisdom_share_suggest")`. The prompt lists up to three
   viable candidates (name, why, description, usage counts) and asks the fork to
   call the tool for at most one. The auxiliary task is registered with
   `inherit_from="background_review"`; `auxiliary.hermes_collective_wisdom_share_suggest.*`
   overrides it. Inside the fork the tool receives `turn_origin="plugin_fork:<id>"`
   and only then may propose: only a skill the running fork was offered, and only
   once per fork; any other origin, a second call or an unoffered skill is refused.
   The proposal records a yes verdict with the fork's reason and goes to step 5. A
   fork that ends `ok` without a proposal records no for every offered revision; a
   `cancelled`, `timeout`, `budget_exhausted`, `refused:*`, `None` or still-`deferred`
   fork (waited 330 s, then cancelled) records `not_asked` and leaves the skills for
   a later due turn. Core cancels the fork when the session's next live turn starts.
   Side question: the token is redeemed on a daemon thread with a 20 s timeout. The
   question carries only the first candidate's name, description and usage counts
   and asks for JSON `{"share": bool, "reason": str}`; anything but the boolean
   `true` is no.
5. Yes injects one user turn (origin for messaging, `session_key` for TUI/Desktop,
   the REPL queue for the CLI) asking the model to call `wisdom_share_suggestion`.
   Called by the live agent (no `turn_origin`), the tool never proposes anything.
   That tool claims the one pending suggestion of the same conversation exactly
   once and shows the card: a native Slack/Telegram card in private chats, or a
   text card in the CLI, TUI and Desktop. When injection is denied or unavailable,
   nothing is recorded as dismissed and no further attempt is made that day.

The card offers exactly **Confirm share**, **Don't share** and **Never for this
skill**. Confirm turns the card into the normal `/wisdom share` review in place;
Don't share dismisses that content hash; Never suppresses the skill until
`/wisdom suggestions allow <skill>`. Choices are owner-bound and applied once
through the native review record; stale or forged controls are refused. Group chats
never get a suggestion.

Every step (candidate, asked, verdict, injected, shown, not shown, check stopped,
choice) is recorded with a timestamp in the plugin state key `wisdom-auto-suggest`, read by
`hermes wisdom suggestions` and private `/wisdom suggestions`.

## Local state

The plugin uses the host-injected PluginState namespace. Each retained domain owns a
small JSON record:

- direct enrollment/setup/publication records;
- qualification counters and candidates;
- notice cursor and bounded offer markers;
- explicit native review records;
- automatic share suggestion choices and decisions (`wisdom-auto-suggest`); and
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

## Verification boundary

Automated tests are offline and disposable. Live Gateway publication, installation,
or credentialed messaging journeys require separate authorization and evidence.
See `docs/TESTING.md` for current commands and recorded acceptance boundaries.
