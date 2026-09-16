# Hermes Collective Wisdom

Private development repository for extracting Collective Wisdom from Hermes
Agent into an external, opt-in plugin.

**Status: development slice, terminal setup, discovery and manual installation.** The native
entrypoint registers `hermes wisdom` and `/wisdom`, including while signed out.
Credentials are resolved on invocation. Loading starts no polling, model tools,
qualification, or proactive delivery, and does not change installed skills.

After enabling the plugin and logging in with Wisdom access, run:

```bash
hermes wisdom setup
hermes wisdom status --json
hermes wisdom browse "incident"
hermes wisdom show <skill-id> --json
hermes wisdom versions <skill-id>
hermes wisdom install <skill-id>@v1 --plan --json
hermes wisdom install --apply-receipt <receipt> --json
hermes wisdom installed --json
hermes wisdom uninstall <skill-id> --yes --json
```

Setup requires explicit disclosure acceptance and read/install scopes, but does
not install a skill. Noninteractive setup requires `--accept-disclosure`.
`/wisdom browse`, `/wisdom show`, and `/wisdom versions` provide read-only,
shared-chat-safe text. `/wisdom setup` only points to the terminal; it cannot
accept disclosure. Slash install/installed/uninstall only provide terminal
instructions; they never inspect private installation state or mutate files.
Terminal installation requires an exact, enrolment-bound review receipt, valid
for 24 hours before starting. Interactive install also supports a plain terminal
confirmation. Re-run `install --plan` to recheck an expired or changed review.
Retry interrupted installation with the same `--apply-receipt`; status does not
silently resume it. Uninstall moves files to recoverable, undiscoverable trash.

Skill setup execution, updates, rich cards and proactive delivery remain later
slices. `--accept-partial` acknowledges setup/compatibility limitations; it never
authorizes setup commands. Installed files are not proof of verified readiness.
The recorded update policy does not start an automatic update worker yet.
This development slice verifies installation on Linux/macOS. Secure receipt
operations fail closed on platforms without descriptor-relative, no-follow
filesystem support; Windows installation is not yet supported.

Work is on `codex/first-release-foundation`. See the
[execution tasks](docs/FIRST-RELEASE.md) and
[reference parity ledger](docs/PARITY.md); extracted unit tests are not live
feature acceptance.

Agent PR #108645 is not a dependency. Keep integration in this plugin, including
entitlement and proactive delivery; report any insurmountable API gap before
proposing another Agent change. Existing host command surfaces stay unchanged.

## Roadmap

- First release: CLI, TUI, Telegram, and Slack.
- Follow-up releases: Desktop and Dashboard, both included in the estimate.
- Fresh installation only; no migration from the reverted core implementation.
- Gateway and Portal remain responsible for server authorization, organization
  policy, publication, moderation, and manager email delivery.

Start with the [feature tracker](docs/FEATURES.md). It is the source of truth for
implementation, automated verification, and live acceptance. The
[migration plan](docs/MIGRATION.md) defines sequencing and estimates:
**66 points for the first release; 97 points overall; 120-125 with contingency.**

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
