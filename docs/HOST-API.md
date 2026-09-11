# Host API Map

Audited against Agent `31d0a2428e9db346d6781da66f5b37ff3e12def2`.
See [SOURCES.md](SOURCES.md) for immutable source notation. Proposed interfaces
below are **not present or implemented** by this repository today.

A partial command-only implementation now exists on the separate Agent draft
branch, not in released Agent: [H1 evidence](verification/H1-commands.md).
It does not implement the full H1/H2 boundary and does not enable this plugin.

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
the minimum released host version once required hooks ship. Manifest/API metadata
does not substitute for runtime checks: an unavailable mandatory hook must
disable the affected feature with a clear diagnostic, not activate a bypass.

## Required Generic Host Work

### H1: Trusted Invocation Context

Expose a host-authored immutable context to plugin command/tool/lifecycle
handlers: profile/account ownership, session key, surface, authenticated actor
and route, conversation generation, and execution origin (root user-facing,
subagent, background). Missing or ambiguous context fails closed for delivery.

Keep existing handlers backward-compatible. Do not require plugins to inspect
private manager fields, mutable process-wide environment variables, adapter
internals, or model-supplied arguments to determine identity. Tests must exercise
real dispatch sites, including delegated tools and background workers.

### H2: Command Availability

Provide a generic availability predicate in command metadata and apply it to
discovery/completion/menu paths as well as direct dispatch. Plugins use it to
hide unavailable capabilities and reject expired credentials without rebuilding
mid-conversation prompts/toolsets. Handler/service authorization remains required.

The predicate is cheap and read-only. It must not refresh credentials or contact
a model. Stale native platform menus cannot confer authority on an old command.
Scope any polling/refresh to affected clients, not global configuration churn.

### Account and Request Boundary: Confirmed H1 Gap

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

H1 still needs a narrow, host-owned, refresh-free account projection and a
supported way to make authenticated Gateway requests. Bind both to the current
profile/account; do not return refresh tokens or accept model-provided identity.
Review the smallest host facade before selecting its public names. Do not add a
generic network framework, extra credential store, or a plugin JWT parser.
An actual authorized request may use the host's existing refresh policy;
visibility and completion must never initiate refresh or network work.

The extracted plugin client uses explicit providers in tests. That does not
complete host integration. Registration stays inert until real facades and
their denial/expiry/profile-switch tests are in place. The early read-only
slice does not waive disclosure/registration prerequisites from the old flow.

### H3: Post-Delivery and Idle-Session Integration

Expose host-owned post-response delivery facts and a safe idle-session work
boundary, carrying H1 context and generation fencing. A generated response is
not necessarily a delivered response. A queued event is not a delivery receipt.

Support typed plugin workflow events without synthesizing human-authored input.
The host preserves session busy/idle exclusion, rejects stale or unauthorized
routes, and never lets a plugin resolve a human approval through event injection.
Expose startup/shutdown opportunity sufficient to use supervised plugin tasks.

Existing `inject_message` is not this interface: it creates conversational
input, and its queued return does not prove delivery. Core adapter
post-delivery/idle internals are evidence to extract a generic interface, not
private APIs for this plugin to call directly.

### UI Follow-Up

Desktop has dynamic contribution visibility and disposal. Dashboard's registry
and manifest have different lifecycle semantics. In M9, ensure both can hide
unavailable contributions on expiry/profile change. If a lifecycle/visibility
hook is missing, add a small generic extension with a neutral example; do not
patch a Wisdom condition into host navigation or core routes.

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
