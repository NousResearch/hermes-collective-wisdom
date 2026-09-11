# Host API Map

Audited against Agent `31d0a2428e9db346d6781da66f5b37ff3e12def2`.
See [SOURCES.md](SOURCES.md) for immutable source notation. Proposed interfaces
below are **not present or implemented** by this repository today.

A partial command-only experiment exists on the rejected Agent PR #108645:
[historical H1 evidence](verification/H1-commands.md). It is not a dependency.
The 2026-09-12 simplification decision requires plugin-owned checks and unchanged
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
complete host integration. Registration stays inert until real adapters and
their denial/expiry/profile-switch tests are in place. The early read-only
slice does not waive disclosure/registration prerequisites from the old flow.

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
