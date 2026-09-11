# Hermes Collective Wisdom

Private development repository for extracting Collective Wisdom from Hermes
Agent into an external, opt-in plugin.

**Status: first-release extraction in progress; not ready for use.** The native
entrypoint remains intentionally inert until the required host and authorization
boundaries are integrated. It registers no commands, tools, hooks, background
tasks, or UI and does not read credentials, contact services, or change skills.

Work is on `codex/first-release-foundation`. See the
[execution tasks](docs/FIRST-RELEASE.md) and
[reference parity ledger](docs/PARITY.md); extracted unit tests are not live
feature acceptance.

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
checks. Enabling this scaffold intentionally exposes no Wisdom functionality.

No plugin has been installed into the local demo as part of milestone 0.

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
