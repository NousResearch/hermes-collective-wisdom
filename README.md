# Hermes Collective Wisdom

Private development repository for extracting Collective Wisdom from Hermes
Agent into an external, opt-in plugin.

**Status: development slice, terminal enrollment, discovery, manual installation,
skill setup, named-skill manual updates/publication, and local native Slack/Telegram
browse/install/update/setup/publication acceptance.** The native
entrypoint registers `hermes wisdom` and `/wisdom`, including while signed out.
Credentials are resolved on invocation. Loading starts no polling or proactive
delivery and does not change installed skills. Registered model tools remain
unavailable without a current admitted private human turn.

In an admitted private Slack or Telegram conversation, `/wisdom settings`
opens an owner-bound settings card. `Agent decides` enables eligible proactive
recommendations; `Fixed notifications` disables proactive generation/delivery
while keeping manual reviews available. Each choice makes one explicit change
and updates the original card. Mute uses existing Gateway revision/sync controls;
an unavailable Gateway is not assumed unmuted. Mode controls are omitted when
the host supplies no config setter. Reopen settings to make another change.

After enabling the plugin and logging in with Wisdom access, run:

```bash
hermes wisdom setup
hermes wisdom status --json
hermes wisdom maintenance --refresh-policy --json
hermes wisdom maintenance --apply-automatic --json
hermes wisdom notifications --json
hermes wisdom notifications --mark-seen --json
hermes wisdom inbox --json
hermes wisdom mute status --json
hermes wisdom mute 1w --json
hermes wisdom sync status --json
hermes wisdom sync retry --json
hermes wisdom browse "incident"
hermes wisdom show <skill-id> --json
hermes wisdom versions <skill-id>
hermes wisdom install <skill-id>@v1 --plan --json
hermes wisdom install --apply-receipt <receipt> --json
hermes wisdom installed --json
hermes wisdom check <skill-id> --json
hermes wisdom update <skill-id> --plan --json
hermes wisdom update --apply-receipt <receipt> --json
hermes wisdom uninstall <skill-id> --yes --json
```

Setup requires explicit disclosure acceptance and read/install scopes, but does
not install a skill. Noninteractive setup requires `--accept-disclosure`.
`/wisdom browse`, `/wisdom show`, and `/wisdom versions` provide read-only,
shared-chat-safe cards. Detail/version cards preserve exact source descriptions
and trusted Portal links. An admitted private Slack/Telegram install can review
every exact package byte and approve the bound receipt; shared chats and other
slash install/installed/uninstall paths remain terminal-only. `/wisdom setup`
only points to the terminal; it cannot accept disclosure. Terminal installation
requires an exact, enrolment-bound review receipt, valid
for 24 hours before starting. Interactive install also supports a plain terminal
confirmation. Re-run `install --plan` to recheck an expired or changed review.
Retry interrupted installation or update with the same `--apply-receipt`;
status and `check` do not silently resume mutation. Manual update revalidates
owner, Gateway authority, exact incoming bytes, compatibility, and local scan.
Local edits block by default; `update --plan --preserve-modified` binds an exact
unmanaged preservation destination and acknowledgement into the receipt before
replacement. Explicit `maintenance --apply-automatic` applies only freshly authorized
`AUTO_WITH_NOTICE` or `REQUIRED` updates. `AUTO_WITH_NOTICE` refuses local edits;
`REQUIRED` preserves the exact edited tree as an unmanaged copy before replacement.
`MANUAL` never mutates automatically. Uninstall moves files to recoverable,
undiscoverable trash.

Private native advisory delivery is implemented locally. Native settings, install, update,
setup, and publication mutate only after exact owner-bound native review; publication
status/reopen and trusted Portal links edit the original card. Native update `Not Now` and
explicit command reopen are implemented locally; this is not cadence suppression.
`--accept-partial` acknowledges setup/compatibility limitations; it never
authorizes setup commands. Installed files are not proof of verified readiness.
An update clears readiness across versions even when setup guidance bytes are
identical; version-two setup/verification must be reviewed separately. The
recorded update policy does not start a background update worker; mutation occurs only
through the explicit maintenance flag.
This development slice verifies installation on Linux/macOS. Secure receipt
operations fail closed on platforms without descriptor-relative, no-follow
filesystem support; Windows installation is not yet supported.

See the [execution tasks](docs/FIRST-RELEASE.md) and
[reference parity ledger](docs/PARITY.md); extracted unit tests are not live
feature acceptance.

Agent PR #108645 is not a dependency. Keep integration in this plugin, including
entitlement and proactive delivery; report any insurmountable API gap before
proposing another Agent change. Existing host command surfaces stay unchanged.

## Local qualification and weekly evidence

Enrolled profiles observe the host's successful skill-load and mutation facts
through `on_skill_lifecycle`. Both reference qualification paths are local:
consecutive business-day usage and independent meaningful refinements with
stability/recent-use checks. These are not claims of verified task success.
Signed-out or mismatched owners do not collect evidence; reenrollment cannot
inherit another account's counters. Session completion processes bounded due
stability jobs without counting another use.

`hermes wisdom review-week --dry-run --json` inspects eligible weekly evidence;
without `--dry-run` it queues local evidence using current server policy. Neither
form calls a model or sends a recommendation. `maintenance` is the explicit feed
and policy network entrypoint. `notifications` and `inbox` read only persisted,
owner-bound local state; they do not poll, fetch metadata, invoke a model, send a
card, or apply an update. `notifications` is the durable feed; `inbox` projects
queued assessments, unresolved native reviews, and installed setup references.
The model tool restricts reviews to its admitted private conversation; terminal
inspection remains bound to the exact enrolled owner. Fixed mode hides proactive
assessments without hiding explicit reviews. Expired and uncertain reviews are
reported without renewal or execution; installed files do not imply readiness.
Feed cursors and rows are fenced to the exact account,
organization, installation, Gateway route, and checkpoint generation. Explicit
maintenance caches at most eight exact-version metadata records per pass. Advice
fails closed until that local metadata verifies the exact skill/version, and then
excludes a version published by the current member while private activity retains it.
Explicit maintenance queues bounded owner-bound feed and qualified-week references only
after fresh policy, mute, and suppression checks. Already queued feed rows are skipped
before the eight-item projection bound, so later feed rows and policy-capped weekly evidence
remain reachable across retries; reported counts are new durable rows, not deduplicated
replays. Queueing does not mark the native feed seen or delivered. It does not call a model
or send a notification. Automatic completion is projected as one durable local `updated`
feed notice; automatic-update notice presentation remains a later checkpoint.
Ambiguous edits remain conservatively non-meaningful without an authorized
classifier. The optional refinement review uses the host's auxiliary task
`hermes_collective_wisdom_refinement`, deferred to `on_session_end` after skill
operations—not a new background scheduler. Clear structural/trivial edits stay
local and model-free.

Enrollment alone does **not** permit transmitting candidate text. To opt in for
the active profile:

```bash
hermes config set plugins.entries.hermes-collective-wisdom.settings.qualification.allow_model_review true
```

This uses the host's plugin LLM trust controls; the plugin-owned task needs no
provider/model override grant. The model-review setting is disabled by default.
Enabling it permits exact before/after `SKILL.md` text
(up to 16,000 characters each) and its structural delta to be sent to the
host-configured auxiliary model after a successful owning private human turn.
No conversation history or other skill files are sent. Cron, subagent, shared-chat,
unadmitted, failed, and interrupted turns cannot trigger it. Revoking the setting
stops queued calls and prevents in-flight results from being applied; already-sent
text cannot be recalled. Current owner and actual on-disk content are checked
before sending and again before accepting a result.

Model failure or invalid output stays conservative; there is no plugin-owned
provider fallback. A meaningful label supplies refinement evidence only:
thresholds, stability, recent use, and owner checks still gate qualification, and
publication still requires its separate exact-content consent. See the
[refinement gate](docs/TESTING.md#authorized-ambiguous-refinement-classifier--2026-09-14)
for local/pinned-host evidence and release limits.

Preparation may include a frozen, editable “Publisher usage (client-reported)”
section before hashing. Upload/retry never adds or refreshes those claims.

## Queued recommendation assessments

In the default `agent` notification mode, the next owning CLI/TUI or admitted
private Slack/Telegram turn processes up to eight queued references through the
registered `pre_llm_call` hook. This is a foreground bounded pass, not a poller,
new worker, or delivery schedule. Explicit maintenance remains the producer;
reading `inbox` still performs no assessment or network request.

Shared arrivals use one structured request through the active owning model
(`task=None`, no auxiliary route or provider override). Its bounded input contains
published version metadata, installed shared-skill summaries, prior introduction
records, and recent user/assistant conversation text—not system/tool messages or
local candidate files. The request timeout is 45 seconds; host/provider retries
can add latency. Missing or mismatched context-local main-model routing waits
without spending a model attempt, rather than using global/profile fallback.
This narrowly reads the existing host context-local route; compatibility is
covered by the pinned-host gate, not a claim of a public route-inspection API.

Owner, private admission, queue leases, delivery mode, policy, preferences and
current version facts gate generation and result acceptance. Structured results
must cover the exact batch; invalid or failed results retry within the existing
queue budget and become fallback state, never invented recommendation prose.
Unread ready assessments do not starve later pending work.

Local contribution candidates stay distinct from installation recommendations.
They require their real unread qualification event and matching on-disk snapshot;
the inbox retains that event ID for the existing candidate inspection flow.
Their explanation uses local qualification evidence. Security review remains
required and unavailable professionalism review is labeled as such; this path
sends no candidate body to a model and creates no consent or publication draft.

To stop assessment generation and hide proactive assessment entries while keeping
explicit reviews available:

```bash
hermes config set plugins.entries.hermes-collective-wisdom.settings.notifications.delivery_mode fixed
```

Set the same key to `agent` to restore the default behavior. Mode/admission changes
also fence in-flight results; already-transmitted model context cannot be recalled.
Advice is visible through `hermes wisdom inbox --json` and the admitted private
`wisdom_inbox` tool. Assessment does not mark anything seen, introduced, delivered,
or approved. On admitted private Slack/Telegram turns, a separate bounded native
drain delivers up to eight eligible ready/fallback entries through the connected
SDK. It also reaches earlier ready work without reassessing it or requiring new
model output. There is no autonomous polling schedule.

Delivery reserves durable intent first, then rechecks ownership, private admission,
mode, preferences, source and lease before sending. Verified SDK receipts complete
the durable delivery; an unknown outcome is never blindly resent. Advisory cards
point to exact `wisdom_inspect` inputs and create no approval or publication
consent. Unavailable assessments remain explicitly labeled, not invented prose.
CLI/TUI retain the manual inbox: their proactive presentation requires a separate
[host API decision](docs/HOST-API.md#verified-local-presentation-gap--2026-09-14).
See the [native delivery gate](docs/TESTING.md#native-ready-advice-delivery-gate--2026-09-14)
for local/recording-SDK evidence and the still-unverified live boundaries.

## Private contribution preparation (terminal)

`hermes wisdom suggest <local-skill> --json` prepares an owner-private local
review with exact files, manifest/hashes and local security findings. It does
not upload or publish. Source files remain unchanged; repeated preparation
preserves an existing edited overlay rather than replacing it.

The prepared draft can then be exported as one complete save-ready JSON object,
edited in a local editor, saved with stale-writer protection, and re-reviewed:

```bash
hermes wisdom prepared inspect <local-draft-id> --json > /tmp/wisdom-draft.json
# Edit author_description and the complete desired files array in /tmp/wisdom-draft.json.
hermes wisdom prepared save /tmp/wisdom-draft.json --json
hermes wisdom prepared review <local-draft-id> --json
```

The terminal editor saves the complete desired file set: add an entry to add a
supported file, omit an optional file to remove it, or change its path to rename
it. Keep `SKILL.md` and `skill.manifest.json`; canonical path, size and security
checks still apply. The original local skill is unchanged. Every save replaces
the private draft snapshot using the previous exact hashes; old publication
reviews cannot approve the new bytes. Saving and local re-review do not upload,
execute, submit, or authorize publication. Rich native file editing remains open.

There is no client-selectable publication visibility field. Drafts remain private
until explicit submission, and Gateway policy determines direct versus moderated
handling. The review displays that policy; an editor cannot override it.

`--allow-model-review` explicitly permits sending review content to the configured
model provider for editorial/professionalism assessment. Without it, preparation
makes no model request and unavailable assessments remain unavailable. Model
assessment is not security certification or publication consent. No publisher
usage claims are invented when the local evidence ledger is absent.

Publication is a separate, explicit terminal step. Review prints the exact file-byte snapshot, hashes, findings, owner identity, and current Gateway policy, then creates one short-lived receipt. Submit consumes that receipt once:

```bash
hermes wisdom publication review <local-draft-id> --json
hermes wisdom publication submit <publication-receipt> --json
hermes wisdom publication status <operation-id> --json
hermes wisdom publication reopen <operation-id> --json
# Optional cancellation of an existing remote draft:
hermes wisdom publication decline <operation-id> --json
```

The uploaded CAS is built from the reviewed immutable snapshot, not from a second filesystem walk. Submit rechecks owner, source fingerprint, exact hashes, the current local security guard, and current Gateway policy before the first remote effect. Gateway policy alone chooses open, managed, or moderated publication; the client cannot request a bypass. If policy changes before an uncommitted effect, the same operation requires a fresh review/receipt rather than creating another remote draft.

Publication intent, unknown-outcome phases, decline intent, lock ownership, and final local/remote projections are durable. `reopen` consults exact authoritative remote state before retrying; it never treats an ambiguous exception as proof that no remote mutation occurred. Terminal projection repair is atomic and idempotent. The same exact-review/status/reopen contract is covered locally through the real
Slack and Telegram SDKs; oversized native publication reviews remove approval and
hand off to the complete terminal review. This is not live deployed publication
acceptance. Full file-manifest UI editing and live delivery acceptance remain later slices;
local qualification and private native advisory delivery are implemented as described above.
See [publication evidence](docs/TESTING.md#policy-routed-publication-gate).

## Installed-skill setup (terminal)

`hermes wisdom setup` still enrolls the profile. `installed-setup` is the separate
workflow for an already installed, owner-bound skill with valid hash-covered
`refs/wisdom-setup.md` guidance:

```bash
hermes wisdom installed-setup inspect <skill-id> --version 1 --json
hermes wisdom installed-setup plan <skill-id> --version 1 --phase setup --index 0 --command '<exact command>' --json
# Read the complete receipt before explicitly applying it:
hermes wisdom installed-setup apply <setup-receipt> --json
hermes wisdom installed-setup status <skill-id> --json
```

Indices are zero-based. `--phase prerequisite --index N` reviews a non-executing
acknowledgement; missing locally checkable prerequisites cannot be acknowledged
away. `--phase verify --command '<exact verification command>'` creates another
separate receipt after prerequisites and setup steps are satisfied. Publisher
instructions are guidance, not automatically executable commands. Do not put
credentials in commands; use the intended local environment/credential handoff.

Execution uses the configured **local** Hermes terminal backend and its existing
approval guards. The command is spawned once, with its validated process handle
recorded before waiting. Install and setup receipts are not interchangeable.
Uncertain execution blocks further managed mutations; it is never blindly rerun.
Inspect `installed-setup recover <skill-id> <operation-id> --json`; only after
checking effects and stopping any remaining process may you add
`--acknowledge-effects-checked-and-stopped` to abandon that operation. This does
not undo effects, rerun the command, or declare the skill ready. A fresh receipt
is required for another attempt. A restored host result whose ownership or exact
command cannot be verified remains unknown.

`/wisdom installed-setup` is a terminal handoff in CLI/TUI. For an update started
from an admitted Slack/Telegram `/wisdom update`, the owning later human turn can
prepare one setup or verification command for separate native approval. No model
proposal executes by itself. If a native surface cannot show the complete guidance
and exact command without truncation, it disables native approval and gives an
explicit terminal inspect/plan/apply handoff; a forged callback is still refused.

## Release Scope and Sequence

- Implementation sequencing starts with CLI, TUI, Telegram, and Slack, then the
  shared UI backend, Desktop, and Dashboard.
- Release requires all six surfaces, including Desktop and Dashboard.
- Release acceptance includes the deployed endpoints exercised with Shannon's
  Nous Portal login; mocks, component stories, and local scripts do not suffice.
- Fresh installation only; no migration from the reverted core implementation.
- Gateway and Portal remain responsible for server authorization, organization
  policy, publication, moderation, and manager email delivery.
- Gateway PR [255](https://github.com/NousResearch/gateway-gateway/pull/255)
  and Portal PR [1022](https://github.com/NousResearch/hermes-portal/pull/1022)
  are verified merged; immutable revisions remain recorded in
  [the source baseline](docs/SOURCES.md).

Start with the [feature tracker](docs/FEATURES.md). It is the source of truth for
implementation, automated verification, and live acceptance. The
[migration plan](docs/MIGRATION.md) defines sequencing. The original planning
baseline was **66 points for the core-surface sequence, 31 for UI completion,
97 total, or 120-125 with contingency**. These are historical estimates, not
remaining-work or delivery promises.

## Development

Use Python 3.11-3.13 in an isolated environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pylint hermes_wisdom_plugin tests
.venv/bin/python -m pytest
.venv/bin/python -m build
```

No running Hermes instance or live credentials are needed for isolated checks.
An editable Python install alone does not install the native Hermes plugin.

To additionally verify the development-only OpenAPI artifact from an existing
Gateway checkout containing the pinned commit:

```bash
WISDOM_GATEWAY_DIR=/path/to/gateway .venv/bin/python -m pytest
```

Without that optional checkout, pytest explicitly skips the source-artifact
check. The runtime schema/vector digest checks still run. This is not the
four-repository release smoke, which remains a separate V1 gate.

The [manual installation evidence](docs/verification/manual-installation.md)
records the slice's extraction, host dependencies, and verification boundaries.
The [account compatibility probe](docs/verification/F1-account.md) additionally
accepts `WISDOM_AGENT_DIR` pointing to an unmodified Agent checkout. It uses
synthetic credentials in a temporary profile, never the current user's login.
Without that checkout it explicitly skips; ordinary CI is not host/live E2E.

## Native Plugin Loading

The repository uses the native Hermes directory layout: `plugin.yaml` and a
root `__init__.py` exposing `register(ctx)`. Development and eventual releases
use the host's reviewed Git install path with an immutable commit:

```bash
hermes plugins install NousResearch/hermes-collective-wisdom --ref <full-40-character-commit>
```

Private repository access must already be configured for the installer. Do not
put a GitHub token in the plugin config or clone URL. Installation, plugin
enablement, Wisdom entitlement, and disclosure/feature opt-in are distinct
checks. Account or organization changes require explicit terminal setup again.

No plugin has been installed into a live profile as part of this slice.

## Documents

- [Feature tracker](docs/FEATURES.md): individual deliverables and evidence.
- [Migration plan](docs/MIGRATION.md): milestones, dependencies, scope, estimates.
- [Architecture](docs/ARCHITECTURE.md): ownership, data flow, and surface adapters.
- [Host API map](docs/HOST-API.md): existing interfaces versus proposed additions.
- [Security](SECURITY.md): authority, consent, isolation, and reporting.
- [Testing](docs/TESTING.md): focused checks and live acceptance scenarios.
- [Source baseline](docs/SOURCES.md): immutable revisions and provenance.
- [Contributing](CONTRIBUTING.md): small PRs and completion criteria.

Inherited Hermes code retains its upstream MIT attribution; see [LICENSE](LICENSE).
