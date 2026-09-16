# Terminal Setup and Safe Discovery

Development slice on plugin PR #1. Foundation: `84d6fca`. Behavioral reference:
Agent `0924f7946b937c2ed02d083fc26752d577c54b3b`. Host integration target:
unmodified Agent `bf51fee548ceaa28d2c299a41c0f65066c04c061`.
Integrated implementation: `d89674d` (including the preceding extraction and
adaptation commits). Documentation changes after that revision do not change
the tested runtime.

## Scope and State

One synchronous CLI entry and one read-only slash handler use existing native
plugin APIs. Registration reads only the profile/data-root context. It does not
read credentials, register tools, start polling, enrol, or scan skills.

The account adapter uses provider state first and the host's refresh-free local
credential-pool status only if provider state is absent. Existing host claim
decoding remains authoritative for the local projection; Gateway authenticates
requests. Setup requires read/install scopes; discovery requires read.

`schema_meta.terminal_setup` contains profile/account/org/endpoint binding,
disclosure acceptance, reserved installation ID and setup state. No credentials
are persisted. The existing installation identity and operation journal are
reused; there is no schema migration or second configuration ledger. Activation
follows server acknowledgement and a fresh owner check. A new account in the
same organization still requires explicit setup. Interrupted calls reuse their
reserved ID; a hard process crash leaves a bounded 120-second lease. Expired or
superseded attempts cannot activate over a newer setup attempt. There is no
unsafe takeover of a live operation.

CLI retains diagnostic JSON. Every slash invocation, including TUI, receives
the shared-chat-safe text projection: published metadata, declared requirements,
check states and Portal links, never installation records or private findings.
Setup through slash only points to terminal instructions. Native install/update
controls are deliberately absent, not implied by prose.

A slash command owns a short-lived read-only subprocess, run off the event loop.
This bounds the entire command at 25 seconds even for stalled DNS, slow response
streams or many pages. Cancellation/unload kills and reaps that process. No
bearer is serialized into its request or argv; it rereads host credentials and
checks the non-secret binding. The calling process rechecks ownership before
returning any output. The synchronous handler captures the host ContextVar scope
before returning its awaitable; this also works through the host's helper-thread
resolver. Only the child's environment receives the captured profile home.
This introduces no Agent extension or background scheduler.

## Reference Dispositions

| Reference source/tests | Disposition in this slice |
| --- | --- |
| `hermes_wisdom/service.py`: disclosure and setup enrolment; `tests/wisdom/test_service.py` explicit acceptance, repeated identity, changed org, failure-before-activation | Selectively adapted to account-bound metadata and journal; scope, same-org account, timeout/retry and acknowledgement tests added |
| Setup's marker/scanning/install-consumption reconciliation and their tests | Deferred to installation/qualification/recovery slices; terminal setup has none of these side effects |
| `service.py`: list/search/resolve/show/versions | Copied coherent read methods, adapted to injected client and owner checks; installed/compatibility annotations deferred |
| `hermes_cli/subcommands/wisdom.py`: parser, emit, setup prompt and command error handling | Adapted to an already-created native argparse subparser; unsupported workflow commands excluded until implemented |
| `gateway/wisdom_command.py` and `tests/gateway/test_wisdom_command.py`: quoted search, list alias, pages/Back, public check projection and readable command fallback | Adapted to read-only text; native callback encodings and rich controls deferred |
| Private TUI compatibility/install views and mutating group/callback tests | Deferred; all slash output uses the stricter shared projection because existing registration lacks caller metadata |
| Old builtin CLI/TUI registration tests | Replaced by real external plugin loading and dispatch on the unmodified host |
| Legacy profile migration and old demo tokens | Intentionally excluded by fresh-start requirement |

The ledger does not claim install, setup execution, publishing, qualification,
native Telegram/Slack cards or proactive delivery parity. Those feature records
remain open, as do all real-account/live acceptance checks.

## Verification

Local assembled result: **331 passed**, including both real-host probes and the
pinned Gateway OpenAPI artifact check. Pylint: **10.00/10**. The full run includes
building and inspecting the source distribution and wheel, runtime-resource
digests, and native synthetic-namespace loading. `git diff --check` is clean.

The native command probe uses the actual plugin loader, CLI argument parser,
TUI `slash.exec`, and running-event-loop resolver on the pinned unmodified host.
It verifies signed-out loading/help, denied reads without HTTP or database
creation, login after load, explicit setup against the fixture, CLI JSON reads,
TUI discovery/navigation, scoped profile execution, nonmutating slash setup,
unload/reload and removal of owned registrations. It explicitly rejects imports
of the old in-core Wisdom module. The account probe additionally covers actual
host credential-pool fallback and invalid-provider precedence without refresh.

Tests use temporary HOME/HERMES_HOME, synthetic JWTs and a loopback Gateway.
No live account, demo process, production service or existing skill is changed.
The fixture validates request shape and local boundaries, not production JWT
authorization. Directory discovery is tested; the private Git installer and
four-repository release smoke remain V1 acceptance, not claimed here.

Reproduce the full local run:

```bash
WISDOM_AGENT_DIR=/path/to/unmodified/agent-bf51fee548 \
WISDOM_HOST_PYTHON=/path/to/host/venv/bin/python \
WISDOM_HOST_DEPENDENCIES=/path/to/host/site-packages \
WISDOM_GATEWAY_DIR=/path/to/pinned/gateway \
.venv/bin/python -m pytest -q
.venv/bin/python -m pylint hermes_wisdom_plugin tests
```

Normal Python 3.11/3.13 CI does not provision Agent or Gateway. It runs the full
plugin tests and builds distributions, explicitly skipping the three optional
source/host probes when their inputs are absent. CI status is attached to PR #1;
it must not be called real-account or four-surface acceptance.

### Named Reference Cases

Read-command tests preserve/adapt these cases from
`tests/gateway/test_wisdom_command.py` at the reference revision:

- `test_parse_supports_quoted_search_and_cli_aliases`
- `test_public_discovery_checks_preserve_state_without_private_findings`
- `test_group_browse_and_page_navigation_do_not_read_device_state`
- `test_unknown_keyword_returns_focused_help`
- `test_help_explains_every_workflow_and_includes_examples` (only implemented commands)
- `test_setup_disclosure_precedes_profile_mutation`
- `test_status_reports_local_and_degraded_state_without_raw_error`
- `test_group_show_removes_install_and_keeps_portal_link`
- `test_group_versions_pagination_never_reintroduces_install_controls`
- `test_browse_navigation_can_step_back_through_show_and_versions`
- `test_text_fallback_keeps_portal_links_but_not_callback_payloads`
- `test_local_browse_renders_show_by_skill_name_instead_of_action_token`

Navigation is intentionally stateless: Show returns to Browse, Versions returns
to Show, and list pages provide previous/next commands. Rich callback history
and restoring a previous search after Show are deferred with native controls;
there is no global navigation cache that can mix users in a shared handler.
`test_private_show_includes_requirements_and_installation_state` is partial:
public requirements are covered, private local-state annotations are deferred.
Old tests requiring builtin registration or hiding unentitled help are replaced
by native registration and explicit denial tests under the approved plugin-only
requirement. Other workflow tests remain in the main parity ledger, not deleted.
