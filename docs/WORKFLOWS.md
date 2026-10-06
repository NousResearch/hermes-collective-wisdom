# Detailed workflows and compatibility notes

Start with the [README](../README.md). This reference describes current plugin-only
behavior. Local tests do not prove deployed-service or live connector acceptance.

## Enrollment and ownership

`hermes wisdom setup` accepts the current disclosure and creates an owner-bound
installation through the Gateway. The plugin stores only the returned identity and
small local domain records in the host-provided PluginState namespace. Every protected
operation rechecks profile, account, organization, installation, route, and
entitlement. Switching any owner component invalidates pending authority.

## Catalog, installation, and updates

Catalog commands are read-only:

```bash
hermes wisdom list --json
hermes wisdom browse incident --json
hermes wisdom show <skill-id> --json
```

Installation and update use exact review receipts. Review the package bytes, hashes,
compatibility, scanner result, and requested capabilities before applying the receipt:

```bash
hermes wisdom install <skill-id>@v1 --plan --json
hermes wisdom install --apply-receipt <receipt> --json
hermes wisdom update <skill-id> --plan --json
hermes wisdom update --apply-receipt <receipt> --json
```

Changed or expired content requires a new review. Installation does not approve or run
setup. Local edits block replacement unless the explicit preservation workflow is
reviewed. Automatic updates run only from the explicit bounded maintenance command and
only under freshly verified server policy. The maintenance command is not registered
in the manual skill-sharing release:

```bash
hermes wisdom maintenance --refresh-policy --json
hermes wisdom maintenance --apply-automatic --json
```

No scheduler or notice-repair worker is started.

## Local qualification

Not registered in the manual skill-sharing release. The plugin observes successful host `on_skill_lifecycle` load/mutation facts. Fixed
local thresholds qualify repeated business-day use or repeated meaningful refinement
with stability and recent-use checks. Session completion processes due stability jobs
but is not itself usage evidence. Signed-out, mismatched-owner, shared, background, or
subagent activity cannot inherit another owner's counters.

Ambiguous refinement classification is disabled by default. If explicitly enabled, it
may send bounded exact before/after `SKILL.md` text to the configured auxiliary model
after an admitted successful private owning-human turn. Model output supplies only one
piece of refinement evidence; it never qualifies by itself and never grants publication
consent.

`hermes wisdom notifications --json` and `hermes wisdom inbox --json` (not registered
in the manual skill-sharing release) are explicit local reads. They do not poll the feed, call a model, send a notice, or execute an
action.

## Best-effort end-turn notices

Not registered in the manual skill-sharing release.

Supported private Desktop and messaging routes may check for a notice at turn end.
There is no proactive notice for plain CLI/TUI chat or unsupported clients.

Each eligible completed turn checks one bounded cursor-based feed page, combines
eligible local qualified candidates, and reads current policy, mute, and suppression.
It offers at most one deterministic
human-facing notice. Cursor advancement never skips unconsumed pagination; a failed
feed preserves the cursor. Renderer failure is swallowed so it cannot fail the agent
turn. A missed notice stays missed.

The notice path has no LLM presentation, prompt/history injection, synthetic user turn,
delivery claim, settlement, operation outcome, advisory lease, liveness registry,
outbox, durable popup, or retry/rescue worker. Opening a notice starts the ordinary
explicit review and never approves an effect.

## Contribution and publication

`hermes wisdom suggest <local-skill> --json` prepares an owner-private local draft. It
never uploads or publishes. Inspect/edit/re-review the complete draft and hashes:

```bash
hermes wisdom prepared inspect <draft-id> --json
hermes wisdom prepared save <draft-file.json> --json
hermes wisdom prepared review <draft-id> --json
```

Publication is a separate review and confirmation:

```bash
hermes wisdom publication review <draft-id> --json
hermes wisdom publication submit <publication-receipt> --json
hermes wisdom publication status <operation-id> --json
hermes wisdom publication reopen <operation-id> --json
hermes wisdom publication enroll <local-skill-name> --json
hermes wisdom publication decline <operation-id> --json
```

Submit rechecks owner, entitlement, exact hashes, source fingerprint, security guard,
and current Gateway policy before the first remote effect. Gateway policy chooses direct
or moderated publication. Confirmed conflicts require explicit re-review. For a
genuinely uncertain write, the plugin keeps only the small evidence needed to avoid a
duplicate submission and reconciles authoritative status before another effect.
Current Share authorization timing is unchanged.

Enrollment recovery resolves the exact current local source by name and requires a
unique retained published proof for that source fingerprint and owner. It neither
republishes nor selects an older historical operation.

Optional editorial/model review is not security certification and is never publication
consent. Package scripts are not executed during preparation or review.

## Installed-skill setup

Installed setup is separate from enrollment and file installation:

```bash
hermes wisdom installed-setup inspect <skill-id> --version 1 --json
hermes wisdom installed-setup plan <skill-id> --version 1 --phase setup --index 0 --command '<exact command>' --json
hermes wisdom installed-setup apply <setup-receipt> --json
hermes wisdom installed-setup status <skill-id> --json
```

Publisher instructions are guidance. Each exact command needs its own owner-bound
review and host terminal approval. Unknown execution outcomes block blind replay; use
the explicit recovery/status workflow after checking effects. Setup receipt authority
is never shared with install/update/publication receipts.

## Surface behavior

- Terminal: complete explicit review, action, status, and recovery workflows.
- Slack/Telegram: private `/wisdom` catalog and explicit actor/route-bound review
  callbacks; sensitive group actions are rejected.
- Desktop: supported plugin REST routes, completed-turn event, direct notification,
  and existing Collective Wisdom management page.
- Plain CLI/TUI chat and unsupported clients: explicit tools/handoffs only, no
  proactive notice.

Desktop, Slack, and Telegram controls remain review surfaces. A button or notification
never substitutes for current effect consent.
