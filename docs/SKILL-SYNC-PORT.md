# Skill Sync port: from Hermes core to the Collective Wisdom plugin

Hermes core removed Skill Sync (`hermes sync`) and the org skill mirror in
`64ad33e32dadbdb462468c7384aa38e5c99a1b7a` ("refactor: remove Skill Sync (`hermes sync`) and the org skill
mirror from core", 2026-10-03). The plugin owns everything it needs from that code. This page tracks the port
piece by piece against the last core commit that still had it.

- **Baseline:** `9cf7960f274ea2bdfe672d64d26389e9954dfeb6`, the parent of the removal, tagged
  `wisdom-skill-sync-baseline`. The last Hermes release that ships Skill Sync is `v2026.9.24`; `v0.21.6` and
  later do not.
- **Archive:** the removed code is also preserved in the private `NousResearch/hermes-skill-sync` repository.
- **Parity bar:** the plugin behaves the same on the baseline Hermes and on Hermes main.

## Status

| Removed from core (baseline path) | What it did | Plugin status |
|---|---|---|
| `tools/skills_sync_client.py` `resolve_sync_base_url` | Gateway base URL: `HERMES_SYNC_BASE_URL`, then `sync.base_url`, then `https://gateway-gateway.nousresearch.com` | **Ported (v0).** `hermes_wisdom_plugin/gateway_endpoint.py`, same precedence, values and default. The plugin read it from core until this port, so every Wisdom command failed on Hermes main |
| `tools/skills_sync_client_wire.py` | Content-addressed objects, trees, commits, manifest bytes, three-way merge helpers | **Already in the plugin** for what Wisdom uses: `wire.py`, vendored and verified in `docs/verification/F2-wire.md`. Merge, commit and ref helpers are not vendored |
| `tools/skills_sync_client.py` `resolve_identity` and the `tool_gateway_admin` gate | Bearer and claims for sync; sync ran only for Nous admins | **Superseded.** `account.py` reads the Hermes Nous login directly; access is the Wisdom scopes minted for teams on the `hermes-wisdom` flag |
| `tools/skills_sync_client_org.py` `propose_skill`, `hermes sync propose` | Share a skill with the org through an org proposal | **Superseded** by `hermes wisdom share` and `/wisdom share` (review, scan, publish) |
| `tools/skills_sync_client_org.py` `pull_org_skills`, the `skills/_org/<org_id>/` mirror, `.active_org` index gating, `[org-shared]` labels, org provenance header and `org_provenance` on `skill_view`, the org delete guard, the auto-propose note on `skill_manage` | Mirror every org skill into the profile and mark it read-only | **Superseded** by opt-in installs: `hermes wisdom install`, `check`, `update`, `uninstall` with managed targets and provenance (`managed_target.py`). Wisdom never mirrors a whole catalog |
| `tools/skills_sync_client.py` `push_skills`, `pull_skills`, `sync_status`, sync manifest, conflict refs, device id and label; `hermes sync status/pull/push/now/enable/disable/device` | Personal cross-device sync of opted-in skills | **Not ported.** Personal sync is not part of Wisdom v0 |
| `tools/skill_usage.py` `set_sync`, `is_sync_enabled` (the per-skill `sync` flag in `.usage.json`) | Local opt-in for personal sync | **Not ported.** Needed only with personal sync; it would live in plugin state, not in `.usage.json` |
| Pull ticks (gateway housekeeping, classic CLI startup, `hermes serve` maintenance) and the debounced post-write push in `skill_manage` | Background reconcile | **Not ported.** Needs plugin lifecycle hooks; Wisdom v0 runs only when the person asks |
| Config knobs `HERMES_SYNC_ENABLED` / `sync.enabled`, `sync.default_opt_in`, `sync.org_auto_propose` | Feature switches for personal and org sync | **Not ported.** `sync.base_url` and `HERMES_SYNC_BASE_URL` are kept and read by the plugin |

## Rules

- The plugin imports no Skill Sync core module. `tests/test_gateway_endpoint.py` fails if any plugin file
  imports `tools.skills_sync_client*` or `hermes_cli.subcommands.sync`.
- A ported piece keeps the baseline's names, precedence and defaults unless a decision record says otherwise,
  so existing profiles keep working without edits.
- Host compatibility is checked against both the baseline and Hermes main with the host integration tests
  (`WISDOM_AGENT_DIR=<checkout>`).
