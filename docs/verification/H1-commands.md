# Rejected Host-Context Experiment (Historical)

Agent PR #108645 was rejected. Its APIs and tests are not plugin dependencies
or evidence of current plugin compatibility. Do not resume this host extension.

- Experiment base: `31d0a2428e9db346d6781da66f5b37ff3e12def2`.
- Reviewed head: `c4032ae225adc1bfa042c5a458ba38891baeddf9`.
- Historical local check on 2026-09-12: 102 focused command/context tests passed
  with Python 3.12.14. That result does not verify native callbacks, idle work,
  tool context, or live messaging. Earlier full-CI results are not a release gate.

The experiment tried host invocation contexts and availability predicates.
Current integration instead uses existing host registration and a plugin-owned
account adapter; see [terminal-discovery.md](terminal-discovery.md) and
[the delivery audit](plugin-only-delivery-audit.md). Full experiment details and
commands remain in this file's Git history and the immutable Agent revision.
