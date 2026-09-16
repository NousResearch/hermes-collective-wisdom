# Plugin-Only Delivery Audit

Read-only source audit at upstream Agent
`bf51fee548ceaa28d2c299a41c0f65066c04c061`, 2026-09-12.
No delivery acceptance or Agent changes performed.

## Existing Building Blocks

- `hermes_cli/plugins.py:register_platform_handler` supplies the native SDK and
  read-only adapter. Telegram and Slack can send plugin-owned cards through
  their native SDKs and return message references. The plugin can own receipts
  for those sends; it does not need a new receipt hook for a core-owned reply.
- `register_telegram_handler` and `register_slack_action_handler` supply native
  callback integration. Preserve actor/message binding and host admission.
- `ctx.llm`, `spawn_task`, and `on_unload` cover advisory generation, supervised
  asynchronous work and cancellation. They do not themselves select a recipient.
- Existing session/tool lifecycle hooks can provide observations. Availability
  of a hook alone does not prove its payload is an authenticated delivery target.
- `inject_message` is not a delivery receipt: it can start or interrupt a turn.
  Do not use it to fabricate human-authored input or claim a send succeeded.

## Next Proof, Not a New Host Dependency

Start with plugin-owned native sends, message references and narrowly admitted
platform targets. Verify target freshness, profile/account changes, busy/idle
timing, reconnect/unload, duplicate sends and subagent-triggered presentation.
Investigate existing lifecycle and callback inputs only as each path needs them;
do not require a universal host invocation/receipt object.

The audit did not establish an end-to-end plugin-only active-session selection
path. That is outstanding integration evidence, not yet an insurmountable gap.
The absence of a single API bundling identity, idleness and receipt ownership
does not by itself justify a new Agent PR. If an essential behavior cannot be
preserved through existing APIs, report the exact failing path and smallest
alternative to the project owner before proposing host work.
