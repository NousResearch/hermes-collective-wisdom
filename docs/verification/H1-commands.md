# H1 Command Context Slice

Date: 2026-09-12. Agent base:
`31d0a2428e9db346d6781da66f5b37ff3e12def2`.
Reviewed command slice: `2ad6e3190d1558760d495f0b72e391aa2404a73f`.
Draft upstream PR: https://github.com/NousResearch/hermes-agent/pull/108645

## Available on the Draft Branch Only

- `PluginContext.invocation`: host-created, invocation-local context. Missing
  or expired access raises `PluginInvocationContextUnavailable`.
- `register_command(..., availability=predicate)`: synchronous literal-True
  predicate, denied for missing/invalid/expired contexts, exceptions, awaitables,
  or any other result. Existing ungated commands retain their behavior.
- CLI slash, TUI catalog/dispatch, and admitted gateway command call sites bind
  profile/session/surface/routing facts and revoke contexts on completion.
- Gateway preserves chat and thread separately. Internal messages do not acquire
  a human actor. Supervised background tasks lose the command context.
- Discovery-only contexts expire. A registered but unavailable command does not
  fall through into model handling. Availability errors omit exception messages
  and tracebacks from logs.

The plugin native entrypoint does not yet consume this API. No released host
minimum is claimed from the existence of an unmerged development branch.

## Verification

The integrator reran the focused tests with Python 3.12.14, pytest 9.1.1,
pytest-asyncio 1.3.0, and Ruff 0.15.10 (matching the host's dev pins). Test tools
were installed in the isolated host worktree's `.venv`; runtime dependencies
were read from the existing reference runtime. Current worktree source had
priority on PYTHONPATH. The reference environment was not modified.

```bash
PYTHONPATH="$PWD:/path/to/reference-runtime/lib/python3.12/site-packages" \
  .venv/bin/python -m pytest -q \
  tests/hermes_cli/test_plugin_invocation.py \
  tests/tui_gateway/test_plugin_command_context.py \
  tests/gateway/test_unknown_command.py::test_plugin_command_receives_admitted_gateway_context \
  tests/gateway/test_unknown_command.py::test_unavailable_registered_plugin_command_never_reaches_model \
  tests/gateway/test_unknown_command.py::test_internal_gateway_context_is_background_and_anonymous \
  tests/hermes_cli/test_commands.py \
  tests/hermes_cli/test_completer_config_reads.py
```

Result: **78 passed**. Pinned Ruff passed on all changed Python files;
`git diff --check` also passed. An initial integrator attempt selected the wrong
Python/site-packages combination and failed collection; the matched Python 3.12
rerun above supersedes it. This is focused local evidence, not a fresh locked
host environment, full Agent CI, or live messaging test.

## Remaining H1/H2 Work

Tool context/availability, CLI subcommand availability, subagent tool bindings,
and local authenticated-actor projection are not implemented. CLI/TUI actor
fields remain absent rather than fabricating identity; actor-dependent consent
must fail closed. Static platform command publication cannot evaluate a user
context and hides gated commands. H2 post-delivery/idle lifecycle work is not
implemented. H1 features therefore remain in progress, not accepted.
