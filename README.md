# Hermes Collective Wisdom

**Share useful Hermes skills with your team—and discover skills your teammates have shared.**

Collective Wisdom is an opt-in plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It connects your local skills to your organization's catalog through Nous Portal
and Gateway. Review a skill before installing it, prepare your own skills for
sharing, and manage updates from Hermes.

You stay in control: installing the plugin does not upload your skills or enroll
your profile. Sharing requires approval. Installing a shared skill adds only the reviewed
files; declared external setup commands require a separate Run setup choice.

> **Early access:** production enrollment and catalog reads have been exercised.
> End-to-end sharing, installation, and platform delivery are still being
> dogfooded. See [testing](docs/TESTING.md) for verification scope.

## What you can do

- **Discover:** browse your organization's skills and inspect published versions.
- **Share:** prepare a private local draft, review its files and checks, then
  submit it under your organization's publication policy.
- **Install and update:** review compatibility and approve an exact version.
- **Manage:** inspect your inbox, choose a notification mode, and mute recommendations.

## Get started

You'll need Hermes Agent, a Nous account with Wisdom access, and an organization
whose Gateway has Wisdom enabled. Managed skill installation currently supports
Linux and macOS; Windows installation is not yet supported.

Run commands on the intended Hermes host/profile. For a named profile, add
`--profile <name>` immediately after `hermes`.

### 1. Install the plugin

Replace `<commit-sha>` with a reviewed commit's full 40-character SHA:

```bash
hermes plugins install NousResearch/hermes-collective-wisdom --ref <commit-sha> --enable
```

For private-repository access through SSH, use
`git@github.com:NousResearch/hermes-collective-wisdom.git` instead of the repository
shorthand. Never put a token in the clone URL.

Start a fresh Hermes session. If you use the messaging gateway, run
`hermes gateway restart` from a separate terminal. No Agent source patch is needed.

### 2. Sign in and enable Wisdom

Already signed in to Nous? Check with `hermes auth status nous`. Otherwise, run
`hermes auth add nous` and complete the browser sign-in. You do not need to change
your conversation's model provider.

```bash
hermes wisdom setup
hermes wisdom status --json
```

Setup presents the data-use disclosure and registers this profile after you
accept. Successful status shows `"configured": true` and `"state": "active"`.
Installation alone does not complete this step.

### 3. Explore your team's skills

```bash
hermes wisdom list --json
hermes wisdom browse "incident"
```

An empty catalog is normal if nobody in your organization has published a skill yet.
Catalog rows and details also show owner/profile-local installation status: a managed
installed version (and a newer catalog version when known), no local copy, an
unmanaged same-name local copy, or unavailable inspection. This label is advisory;
the installed version is the recorded revision, not a claim that local files are
unchanged or setup is complete. Local edits are checked by the existing review flow;
install, update, and removal still use their separately reviewed authority checks.

## Where to use it

- **Terminal:** use `hermes wisdom` for enrollment, browsing, sharing, installation,
  updates, and skill setup.
- **Slack / Telegram:** use `/wisdom` for browsing and private review flows.
  Sensitive actions require a private conversation and explicit approval.
  Live platform acceptance is still in progress.
- **CLI / TUI chat:** after an owning turn loads a skill and it newly qualifies,
  a best-effort local sharing suggestion may be appended to the answer. It names
  the checked private draft and gives a terminal review command; it never shares
  automatically. The current Agent transform hook cannot distinguish a later
  failed final outcome, so a suggestion may remain in a recovered failed answer.
- **Desktop:** enable the package's Desktop half in **Capabilities → Plugins**
  (also available through **⌘K → Plugins**), then open **Collective Wisdom**.
  Catalog, Installed, Activity & Settings, and Contributions call the existing
  workflows on the selected host/profile. Install runs the checks and applies a
  clean package in one click; details remain available before acting. Material
  warnings, removal, and sharing retain their explicit review controls.

In Desktop chat, `/wisdom`, `/wisdom list`, `/wisdom browse [query]`, and
`/wisdom show <skill-id>` open the same catalog/review workflow in a themed dialog.
View is read-only. Install authorizes the checked installation without another
confirmation; material warnings still require acknowledgement. Other Wisdom
commands keep their normal route. The current Desktop
middleware API leaves the submitted command in the composer; closing the dialog
does not send it. A focused-chat/active-profile mismatch refuses requests rather
than silently switching owners.

The Desktop page also offers explicit enrollment after disclosure. At completed
turns it checks a bounded feed without a polling delay and may show a direct
best-effort notification. Contextual skill classification is deferred on stock
hosts: `useful_only` does not promote catalog feed rows without a grounded match.
A missed or failed notification is not queued or retried, and opening one starts a
review rather than approving any effect. Contributions supports complete private
draft editing and save-and-re-review, including text-file add/remove/rename and
inert opaque-byte export. Catalog exposes Install; Installed exposes Check for
updates and a separate exact-version Review update. Neither installs nor draft
edits authorize publication. Install writes only the reviewed files; a declared
setup command requires a separate Run setup choice. Manual setup and uncertain
operation recovery remain in the existing native controls.
Dashboard UI and full six-surface parity remain deferred.

## Common tasks

Use `hermes wisdom --help` for the full command list. Add `--help` to any
subcommand for its options.

### Install a skill

```bash
hermes wisdom install <skill-name>
```

The explicit install command runs the checks and installs a clean package without
another approval prompt. `--json` changes output formatting, not that intent.
To inspect without installing, add `--plan`; an inspected receipt can also be applied:

```bash
hermes wisdom install --apply-receipt <receipt> --json
```

Approval is bound to the reviewed version, files, and declared setup commands.
A changed or expired plan needs another review. Setup instructions not explicitly
declared as commands remain in the [manual setup flow](docs/WORKFLOWS.md#installed-skill-setup-terminal).
If the Hermes scanner reports reviewable warnings, the interactive terminal
shows the findings and offers explicit acknowledgement before final approval.
Scripted reviews use `--accept-warnings` after authorized review; this is distinct
from compatibility `--accept-partial`. An unavailable scan cannot be accepted.

### Prepare a skill to share

```bash
hermes wisdom suggest <local-skill-name>
```

This prepares a local draft without uploading, publishing, or changing the
original skill. Review the description, files, generated compatibility manifest,
and security findings. See the [sharing workflow](docs/WORKFLOWS.md#private-contribution-preparation-terminal)
for editing and the separate publication review/submission steps.

After a publication is proven successful, Wisdom registers the unchanged original
source as a managed installation in place; it does not copy, move, reinstall, or run
setup. Teammate versions then use the ordinary explicit update review and local-edit
protections. If remote publication succeeds but local registration does not, recover
the retained proof with `hermes wisdom publication enroll <local-skill-name>`. That
command performs read-only catalog/detail/content discovery and, after exact proof,
may register the installation with Gateway and local managed state. It never replays
publication or overwrites the source during enrollment.

On an interactive terminal, choose to inspect files and warnings, edit the draft,
continue to sharing review, or keep it for later. Sharing requires a separate
final confirmation. `--json` selects noninteractive machine output instead; it
never approves an action or opens a menu.

Native Hermes skill packages may contain ordinary source/configuration files,
`references/`, `refs/`, `scripts/`, `templates/`, `assets/`, and an inert
`.gitignore`. Text, executable script intent, and common opaque resources retain
their exact bytes. Packages have a 10 MiB aggregate payload limit and a separate
bounded path/tree metadata budget; there is no independent 32-file or tiny
per-file limit. Symlinks, hard links, special files, traversal/collisions,
credentials/private keys, unsafe modes, and compiled executable payloads remain
blocked. Package scripts are never executed during preparation or review.

**Service compatibility:** a Gateway enforcing the older instruction-only,
smaller-package contract still rejects these expanded packages. Updating this
plugin alone does not update Gateway or Portal limits. Keep the complete local
draft until the service supports it; the plugin never drops files to make a
submission fit. Local preparation is not proof of successful publication.

Optional model review requires `--allow-model-review`, which permits sending the
draft to your configured model provider. Without it, that assessment is reported
as unavailable, not passed.

### Manage updates and notifications

| Task | Command |
| --- | --- |
| List managed installations | `hermes wisdom installed --json` |
| Check for an update | `hermes wisdom check <skill-id> --json` |
| Review an update | `hermes wisdom update <skill-id> --plan --json` |
| Recover management of an already-published original | `hermes wisdom publication enroll <local-skill-name> --json` |
| Inspect notification settings | `hermes wisdom settings --json` |
| Mute proactive notifications for a week | `hermes wisdom mute 1w --json` |

After approving an update plan, apply it with
`hermes wisdom update --apply-receipt <receipt> --json`. Local edits block
replacement by default. Advanced preservation, maintenance, and recovery options
are in the [workflow reference](docs/WORKFLOWS.md).

The [review restoration guide](docs/REVIEW_RESTORATION.md) describes private native
invitations, complete bounded inspection, quiet dismissal, and the gaps that remain.

## Privacy and approval

Local candidate signals and notice cursors are small owner-bound PluginState
records; this plugin has no SQLite database, advisory outbox, delivery lease, or
retry worker. Enrollment is not consent to upload
local skill contents. Draft submission, installation, and setup execution each
have their own approval; a model recommendation cannot approve an action.

Proactive notices are deterministic and never add model messages, system-prompt
text, or synthetic conversation turns. See [Security](SECURITY.md) for details.

Organization policy determines direct versus moderated publication. An unavailable
check is never reported as passed. Changing accounts or organizations requires
setup again. After an uncertain upload or setup result, inspect recovery status
rather than blindly repeating the action.

## Troubleshooting setup

- **Credentials unavailable:** check `hermes auth status nous`. An expired or
  stale development login may need a fresh login. For production, use
  `hermes auth add nous --portal-url https://portal.nousresearch.com --inference-url https://inference-api.nousresearch.com/v1`.
  Do not import stale development credentials if offered; check intentional
  environment overrides before changing routes.
- **Gateway does not advertise Collective Wisdom:** ask your administrator to
  enable Wisdom on the intended Gateway. Login alone does not enable the service.
- **Setup requires `--accept-disclosure`:** run interactively to read and accept
  the disclosure. Automation may use the flag only after explicit acceptance.
- **No admitted private session:** verify enrollment with `hermes wisdom status
  --json`, then use a supported main-agent/private session. There is no separate
  user-facing admission switch.
- **Scanner warnings:** inspect the original verdict and findings, then explicitly
  acknowledge the exact review with `--accept-warnings` if appropriate. New
  findings invalidate that acknowledgement. Credential/private-key findings and
  unavailable scans remain hard stops; never disable the scanner.
  The local credential preflight recognizes only complete, delimited synthetic
  OpenAI/Anthropic-style example keys: a suffix of `test`, `dummy`, `example`,
  or `placeholder`, optionally followed by `key` and independently by the literal
  sequence `1234` (each optional part may have one `-` or `_` separator;
  case-insensitive).
  This applies to standalone key shapes and API-key assignments, not passwords,
  generic secrets, or token assignments. A `test` substring, a Markdown code
  fence, or a test filename grants no exemption. Unknown/extended key values
  remain blocked; redaction, host warning review, and Gateway checks are unchanged.
  Opaque-file checks retain their byte-preserving ASCII coverage. The synthetic
  classifier is shared, but non-ASCII word boundaries and line locations are not
  guaranteed to match decoded text; invalid bytes are never silently discarded.
- **Gateway rejects a native package:** the deployed service may still have the
  older instruction-only limits. Keep the local draft and retry after the service
  contract is upgraded; do not remove supporting files to force acceptance.

## Development and further reading

Use Python 3.11–3.13 in an isolated environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pylint hermes_wisdom_plugin tests scripts/test_wisdom_offline_acceptance.py
.venv/bin/python -m pytest
npm ci --ignore-scripts
npm test
.venv/bin/python -m build
```

Isolated tests need no live credentials. An editable Python install alone does
not install the native Hermes plugin. Optional host/Gateway checks and live
acceptance requirements are documented separately:

- [Detailed workflows](docs/WORKFLOWS.md): advanced commands and host limitations.
- [Testing](docs/TESTING.md): automated and live acceptance boundaries.
- [Architecture](docs/ARCHITECTURE.md) and [host APIs](docs/HOST-API.md): implementation.
- [Feature tracker](docs/FEATURES.md) and [release tasks](docs/FIRST-RELEASE.md): remaining work.
- [Migration history](docs/MIGRATION.md) and [source baseline](docs/SOURCES.md): decisions and provenance.
- [Contributing](CONTRIBUTING.md): development expectations.

Inherited Hermes code retains its upstream MIT attribution; see [LICENSE](LICENSE).
