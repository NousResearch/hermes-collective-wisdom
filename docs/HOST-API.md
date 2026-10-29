# Host API Map

## Current replacement boundary

Use the injected public `ctx.state` namespace for domain records and
`ctx.get_config` / `ctx.set_config` for settings. The end-turn hook emits only
best-effort supported-surface notices; no presentation-receipt extension,
SQLite database, delivery reservation or custom TUI bridge is required.
Model tools require a current private admission; exact review callbacks retain
separate actor, original-message, owner and expiry checks.

The inventory and development checkpoints below are historical API evidence,
not dependencies or acceptance claims for this replacement. In particular, the
local presentation and TUI experiments are not part of the shipped plugin.
See [Architecture](ARCHITECTURE.md) and [Verification](TESTING.md).

## Historical host audits

Audited against Agent `31d0a2428e9db346d6781da66f5b37ff3e12def2`.
The historical API inventory below describes those pinned hosts, not unpublished
host development.

## Local development checkpoint — 2026-09-15

A separately authorized, generic CLI/TUI presentation implementation now exists on
an unpublished Agent development branch based on
`1ad89ac018f26a4f21817ebf37bb09f508656d63`. It provides
`ctx.register_presentation_handler()` and correlated `rendered`, `not_rendered`,
and `uncertain` receipts. It is not committed, published, or part of the pinned
host used by the accepted plugin artifact tests.

The Wisdom plugin now optionally registers an interactive cadence consumer. It reuses
owner-scoped assessment leases, policy checks and the Gateway reservation/outcome
protocol, then requests literal local output. Registration is inert and older
hosts retain manual inbox access. An uncertain result stops the batch without
resending; untouched ready leases are released.
The consumer may perform bounded production and assessment before delivery.
It consumes a single-use owner-bound capture of the real pre-LLM Python context;
it does not invent model routing, inject messages, or grant approval. Local
pre-LLM alone no longer assesses or claims work. See the
[current gate](TESTING.md#simple-interactive-cadence--2026-09-15).

Local CLI/TUI read-tool handoffs now also use the existing pre-LLM hook: an
expiring, bounded grant binds the exact task/session and active model route to the
profile/account/installation owner. `wisdom_inspect` and `wisdom_inbox` validate
that binding against the service instance used for the read. Replacement turns,
route loss, unload and changed ownership reject old grants. This does not enable
native consent cards on local surfaces or authorize install/setup/publication.

Assessment cancellation releases the presentation callback without waiting for a
synchronous provider to stop. The worker owns its own service lifetime and loses
admission on cancellation; its already-started call may finish, but its result may
not trigger stale delivery. The underlying provider thread is not forcibly killed.

[Local turn journeys](TESTING.md#local-turn-journeys--2026-09-15) now cover the
real host pre-LLM hook and classic CLI PTY offer/render, plus TUI `prompt.submit`
through RPC/Ink and new-input cancellation. Both renderer journeys follow the
inspection handoff through the scoped tool registry. Gateway/model results are
recording fixtures; full live CLI/TUI and mutation UX acceptance remain open.
Do not infer a released minimum host version or deployed acceptance. The historical
gap below still applies to old pinned hosts, not this development capability.


The same isolated host now also provides an optional generic TUI review bridge
through `ctx.request_review()`, `ctx.can_request_review()` before protected
preparation, and `ctx.is_review_current()` after selection and final owner reads.
The latter does not renew the one-shot request or grant consent. It binds an explicit
slash invocation to the exact plugin registration, session, transport and input
generation. A literal, scrollable body and allowlisted named actions cross actual
RPC/Ink; initial Enter cancels, and display acknowledgement is never selection.
The host owns no Wisdom receipt or execution policy. Wisdom's `tui_actions.py`
uses the existing install/update/setup kernels; unsupported hosts fail safely to
the terminal handoff. See [the development TUI commands](../README.md#development-tui-review-and-actions).

A partial command-only experiment on a rejected Agent PR is not a dependency.
The plugin requires plugin-owned checks and unchanged
host command surfaces. Exhaust existing APIs for proactive delivery as well;
report any insurmountable gap before proposing another Agent PR.
Targeted re-audit: Agent `bf51fee548ceaa28d2c299a41c0f65066c04c061`.

## Reuse Existing Interfaces

| Need | Existing API / source | Constraint |
| --- | --- | --- |
| Native loading | `plugin.yaml`, root `register(ctx)`; `hermes_cli/plugins_manifest.py` | Manifest v2; enabled only by host opt-in |
| CLI commands | `PluginContext.register_cli_command` | Plugin owns the Wisdom argument parser |
| Slash commands | `PluginContext.register_command` | CLI, TUI and messaging dispatch exist; ordinary result is text |
| Tools | `register_tool`, `dispatch_tool` | Keep schemas stable; recheck authorization in handlers |
| Settings | `get_config`, `set_config` | Namespaced settings, not credential copies |
| Durable local directory | `ctx.state.data_dir`; `plugins_state.py` | Profile-local SQLite for transactional work |
| Model review | `ctx.llm.complete_structured`, auxiliary task registration | Advisory output; auxiliary tasks are not a scheduler |
| Skill observation | `on_skill_lifecycle`, pre/post tool hooks | Successful facts only; no substring guessing from chat prose |
| Supervised async work | `spawn_task`, `on_unload` | Requires running loop; cancellation is not durable recovery |
| Telegram handlers | `register_platform_handler`, `register_telegram_handler` | Native factory; scope callback patterns so core handlers still work |
| Slack actions | `register_slack_action_handler`, platform handler factory | Acknowledge promptly; plugin owns callback authorization |
| Desktop | `@hermes/plugin-sdk`, routes/panes/nav, `ctx.rest` / `ctx.socket` | External ESM; current supported exports only |
| Dashboard | `window.__HERMES_PLUGIN_SDK__`, registry, manifest/bundle | Separate SDK; supplied React and authenticated helpers |
| Shared UI API | `dashboard/plugin_api.py` exports FastAPI router | Backend enablement is separate from renderer enablement |

Private immutable Git install is already supported. `requires_hermes` can enforce
the minimum tested host version. Manifest/API metadata
does not substitute for runtime checks: an unavailable mandatory hook must
disable the affected feature with a clear diagnostic, not activate a bypass.

## Plugin-First Integration

### H1: Ordinary Commands and Native Actions

Use `register_cli_command` and `register_command`. Ordinary replies return via
the host's calling session; rely on host admission instead of adding a second
identity/routing layer. Shared slash handlers receive raw arguments, not actor
context. Do not infer identity from arguments or process-global last-user state.

Use native Telegram/Slack handler inputs for callback actor/message binding and
group privacy. Keep host admission/pairing intact when adding native handlers.
Account entitlement and callback ownership are different checks; an entitled
bot account does not authorize every person who can click a shared card.
Audit proactive/tool delivery independently; ordinary reply routing alone does
not prove that a subagent or background task has a human-facing destination.

### Command Availability: Plugin-Owned Gate

Only installed/enabled plugins register Wisdom features. Installed but
unavailable commands may remain visible and return a concise denial. No core
help/menu/completion changes or mid-conversation toolset rebuild is required.
Check current entitlement at initialization and before every protected action;
background gates recheck local state without refresh or model calls. Cache and
pending-action invalidation must cover account/profile/org changes and expiry.

The current hook list has session/lifecycle hooks but no dedicated auth or
org-switch events. Use suitable existing hooks as optimizations, not a required
login-event contract. Per-operation reads provide enforcement independently of
periodic checks. Existing supervised tasks are sufficient for local polling.

### Account and Request Boundary: Narrow Plugin Adapter

The follow-up audit at the same Agent baseline found no supported plugin-facing
account snapshot or authenticated request API. `get_config`/`set_config` and
`ctx.state` are not credential APIs. An invocation's admitted messaging actor is
also not an organization access token.

Host-owned building blocks already exist:

- `hermes_cli/auth_nous.py:get_nous_auth_status_local` is refresh-free, but is an
  internal display projection without the organization/scope fields needed here.
- `hermes_cli/nous_account.py:get_nous_portal_account_info` can fall through to a
  refresh. It is not safe to call from completion or availability predicates.
- Token decoding and `resolve_nous_runtime_credentials` remain host internals.
  Local token claims are availability hints, not a substitute for Gateway's
  verification and execution-time authorization.

The absence of a plugin facade does not mandate a new host API. First implement
a narrow plugin-side adapter over the existing host account/credential readers,
with compatibility tests against the pinned Agent revision. The local status
reader's `logged_in` flag alone is not entitlement: it can mean a refresh token
exists despite an unusable access token. Preserve the existing claims/expiry
checks and fail closed on missing inputs. Document any internal API coupling.
Bind reads and requests to the current profile/account; do not persist refresh
tokens or accept model-provided identity. Do not add a generic network framework,
extra credential store, or replacement JWT parser/OAuth flow.
An actual authorized request may use the host's existing refresh policy;
visibility and completion must never initiate refresh or network work.

The extracted plugin client uses explicit providers in tests. That does not
complete workflow integration. The account adapter
now passes denial/expiry/profile-switch checks against unmodified upstream.
It uses `get_provider_auth_state` and the existing host decoder, not a new API.
CLI/slash registration now supports explicit terminal onboarding and safe
discovery. Credentials are resolved per invocation; slash mutation requests
are static terminal handoffs. Neither registration nor status starts recovery.

### Manual Installation: Narrow Host Guard Adapter

The terminal installation slice uses the pinned host's `scan_skill` and
`should_allow_install(force=False)`, plus its existing target validators. These
are tested internal imports, not a new public host API. Static capability reads
do not execute commands, refresh credentials, load plugins, or probe networks.
Unavailable guards fail closed. The destructive Hub replacement installer is
not used.

The plugin owns exact-content receipts, journals, atomic promotion and
recoverable removal. Staging under `.hub/collective-wisdom` and trash under
`.archive/collective-wisdom` use directories excluded by the actual host skill
scanner; committed `_wisdom/<org>/<slug>` skills remain discoverable. Plain
terminal confirmation does not use remembered tool approvals or private TUI
modals.

### H3: Post-Delivery and Idle-Session Integration

First audit existing hooks and native delivery results for post-response facts
and safe idle execution. Only a demonstrated missing capability justifies a
small generic host extension. Preserve session/operation fencing without
requiring the draft universal H1 context. A generated response is
not necessarily a delivered response. A queued event is not a delivery receipt.

Support typed plugin workflow events without synthesizing human-authored input.
The host preserves session busy/idle exclusion, rejects stale or unauthorized
routes, and never lets a plugin resolve a human approval through event injection.
Expose startup/shutdown opportunity sufficient to use supervised plugin tasks.

Existing `inject_message` is not this interface: it creates conversational
input, and its queued return does not prove delivery. Core adapter
post-delivery/idle internals are investigation evidence, not permission to patch
core routing or call private adapter internals by default.

### Verified Local Presentation Gap — 2026-09-14

The delivery audit against the pinned `bf51fee548ceaa28d2c299a41c0f65066c04c061`
host found no public CLI/TUI notification-and-acknowledgement callback.
`pre_llm_call` observes a turn (or injects context); `inject_message` creates
conversation input. Neither acknowledges human-facing notification delivery.
`PluginContext.emit` publishes namespaced subscriber events, not a local renderer
receipt. Direct printing would bypass TUI rendering and cannot justify a
`transport_accepted` receipt.

Slack/Telegram already expose connected native SDK factories and real send
acknowledgements. Their plugin delivery connection is locally verified; CLI/TUI
proactive presentation remains blocked, with manual inbox intact.
No fake continuation, fabricated receipt, core patch, or new host API is added.
A generic local notification interface requires a separate scope/design decision.

The continuation audit also checked `agent/turn_finalizer.py:627-643` at the same
pin: `on_session_end` fires before the conversation result returns to its caller.
It is a completion observation, not proof that the caller delivered the reply or
that its UI is idle. The public hook summary in the current official plugin docs
likewise supplies no idle/presentation acknowledgement. Therefore connecting the
weekly producer to that hook must not be described as post-delivery cadence.

The minimal proposed host scope is two related capabilities, not a new workflow
engine: (1) a host-owned eligible human-surface opportunity after actual reply
rendering or an idle transition, fenced to the current profile/session/turn;
(2) bounded plain notification rendering with a renderer acknowledgement and
known-not-rendered versus uncertain outcomes. The plugin retains durable queue,
quota/cooldown, content checks, and dedupe ownership. No notification is a user
message, model continuation, or approval. The host must invalidate opportunities
on session replacement, new work, unload, or lost admission. A neutral host test
must prove those transitions and CLI/TUI rendering before plugin delivery can
claim a receipt. Exact public signatures and upstream implementation remain
unapproved; no speculative host API calls have been added here.

### UI Follow-Up

Desktop has dynamic contribution visibility and disposal. Dashboard's registry
and manifest have different lifecycle semantics. In M9, ensure both can hide
unavailable contributions on expiry/profile change. If a lifecycle/visibility
hook is missing, add a small generic extension with a neutral example; do not
patch a Wisdom condition into host navigation or core routes.

### Desktop management host boundary

The external page calls `ctx.rest` in `/api/plugins/collective-wisdom`.
Git installation includes `dashboard/manifest.json` and `plugin_api.py`; the latter
loads its adjacent Python package under a synthetic namespace, independent of
the host working directory or a stale pip installation. The host owns HTTP
authentication. No `api_context: "native"`, private manager lookup, or invented
Hermes conversation identity is used.

The installed host does not automatically scope plugin routes by `?profile=`.
The plugin adapter therefore uses the existing host `_config_profile_scope`
context manager and checks the selected profile's enable/disable configuration.
This is a **version-tested host-internal helper**, not a promised public SDK API.
It scopes config, credentials and plugin data together without mutating process
environment variables. Missing support fails route loading rather than silently
using another profile. `tests/test_desktop_host_mount.py` verifies actual stock
host discovery, authentication, A/B/A reads, cross-profile mutation denial and
disable enforcement. Run it against the host revision intended for deployment.
On the tested stock host, API discovery/import occurs at backend startup and
requires the plugin enabled in that backend's launch profile. Enabling only the
Desktop renderer or only a subsequently selected profile cannot mount an absent
backend. After a new backend installation, enable it in the host launch profile
and restart that backend through supported controls; selected profiles still
need their own Agent-plugin enablement and account authority. The plugin does
not bypass that host import gate.

The renderer's connection/profile values segregate caches, not authority. Fresh
account/enrollment reads and shared workflow receipts govern effects. Complete
private draft save/re-review and read-only update checks reuse the shared workflow
service with live-view binding. Dashboard UI and setup execution/uncertain-operation
recovery controls remain deferred. Earlier proposed host additions above are
historical, not current implementation tasks.

## Deliberately Not Building

- No replacement Telegram/Slack platform plugins and no universal messaging
  framework. Thin plugin adapters can use the existing native SDK factories.
- No new durable job service. Checkpointed plugin polling uses supervised tasks
  and the lifecycle bridge; offline hours are reconciled after restart.
- No new JWT claims, hardcoded organization allowlist, or replacement OAuth flow.
- No speculative Desktop thread renderer fork or Dashboard page replacement.

## Upstream Acceptance

Each host PR must contain neutral fixtures, positive and negative authorization
cases, cleanup tests, and proof that existing plugin handlers still work. Add
hooks together with working call sites, not inert declarations. Record merged
host revisions in the feature tracker before enabling dependent plugin features.
