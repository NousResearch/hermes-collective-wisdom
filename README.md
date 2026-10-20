# Collective Wisdom for Hermes Agent

> Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin from Nous Research.

Collective Wisdom lets a team share the Hermes skills that work for them. An author shares a local
skill after one exact review, and it appears in the team's skill library in Nous Portal. Teammates
find it in the Portal or in Hermes and install that exact version. Hermes can rank your own most-used
skills and, when you ask, suggest which ones are worth sharing. Sharing itself always stays your
decision.

You stay in control at every step:

- Installing the plugin uploads nothing and enrolls nothing.
- Every share is one review of the exact files that will be published.
- Every install is one exact version, and declared setup commands never run without a separate approval.
- Nothing happens in the background: Collective Wisdom acts only when you run one of its commands.

## Requirements

- Hermes Agent 0.21.5 or newer.
- A Nous account in a team that has Collective Wisdom enabled. Personal accounts do not have access.
- Linux or macOS for installing skills. Windows is not supported for managed skill installation.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install collective-wisdom
```

To install one exact commit from the repository instead:

```bash
hermes plugins install NousResearch/hermes-collective-wisdom --ref <40-character-commit-sha> --enable
```

Install prepares the plugin's Python dependencies and asks before enabling it, then shows the next
step, `hermes wisdom setup`. Installing or enabling the plugin never runs setup. Plugins are installed
per profile: use `hermes -p <profile> plugins install collective-wisdom` for another profile. Start a
new Hermes session afterwards. If you use the messaging gateway, also run `hermes gateway restart`.

In Hermes Desktop, enable Collective Wisdom in **Capabilities → Plugins** (also available through the
command palette).

## Sign in and enable

Collective Wisdom uses the Nous sign-in Hermes already has. Check it, and sign in if needed:

```bash
hermes auth status nous
hermes auth add nous          # only if you are not signed in
```

Then enable Collective Wisdom for this profile:

```bash
hermes wisdom setup
```

Setup shows what data leaves your machine (see [Privacy and data use](#privacy-and-data-use)) and
registers the profile with your team after you accept. Changing account or team means running setup
again. Until a profile is set up, every other `hermes wisdom` and `/wisdom` command (except `status`,
`help`, `top` and `share-suggest`) replies with this step and changes nothing. In Telegram and Slack
the reply names the terminal command: setup is accepted only in the terminal.

## Verify

```bash
hermes wisdom status          # "active" when the profile is enrolled
hermes wisdom browse          # your team's shared skills; empty until someone shares
```

## What you can do

### Find what is worth sharing

`hermes wisdom top` ranks your own local skills by how often Hermes used them, then by edits, then by
last use. Skills that came with Hermes, from the Skills Hub or from your team are left out, and skills
you already shared are labelled **Shared**.

`/wisdom share-suggest` starts a normal Hermes turn in the current conversation with up to 30 of your
top skills. Hermes answers with up to 10 ranked suggestions, each with a short reason and the exact
command to share it. It never shares anything itself.

### Share a skill

```bash
hermes wisdom share <skill>
```

Share builds the exact package and shows one review: the team, who will see it, the version, the
description, every file, and the local security checks. You can open a file, edit the description or
the files, then choose **Share** or cancel. In a private Telegram chat or Slack DM, `/wisdom share
<skill>` opens the same review as a card, and only the **Share My Skill** button publishes.

The shared name and description come from the skill's `SKILL.md` frontmatter. A skill needs both,
with a description of up to 320 characters, before it can be shared.

The result is one of: shared (with the version, a Portal link and the install command for teammates),
already shared, blocked by a security check, name already owned by another author, not entitled,
invalid package, or uncertain. An uncertain result names a status command to check; do not share
again until you have checked it.

### Publish a new version

Edit the skill and run the same share command. The review shows the version change, for example v1 to
v2. Only the author of a skill can publish new versions under its name.

### Discover skills

In the Portal, the team library lists every shared skill with search by name, author and keywords,
and highlights skills published or updated since you last looked. A skill's page shows its
description, security checks, version history, requirements and the exact install commands.

In Hermes:

```bash
hermes wisdom browse [query]      # search the team library
hermes wisdom show <skill>        # one skill
hermes wisdom versions <skill>    # its published versions
```

The same reads work as `/wisdom browse`, `/wisdom show` and `/wisdom versions` in any Hermes chat.

### Install a skill

```bash
hermes wisdom install <skill>             # the latest version
hermes wisdom install <skill>@v2          # an exact version
hermes wisdom install <skill> --plan      # review without installing
```

`<skill>` is the skill's name or its ID, both shown in the Portal. A clean package installs straight
away into your profile's skills. If the Hermes skills scanner reports warnings, the install shows them
and waits for your acknowledgement. Commands a skill declares for setup never run during install;
review and approve them separately with `hermes wisdom installed-setup`.

In a private Telegram chat or Slack DM, `/wisdom install <skill>` installs from the chat. In Hermes
Desktop, install from the Collective Wisdom page or from the dialog that `/wisdom browse` opens.

### Keep installed skills current

```bash
hermes wisdom installed           # skills installed through Collective Wisdom
hermes wisdom check <skill>       # is a newer version available?
hermes wisdom update <skill>      # review and apply one update
hermes wisdom uninstall <skill>   # move an installed skill to recoverable trash
```

Updates are always reviewed and applied by you. If you edited an installed skill locally, the update
stops rather than overwrite your changes.

### Unpublish, take down and restore

These actions live on the skill's page in the Portal, under **Manage skill**. Each one asks for a
reason.

- **Unpublish** hides a skill from the library. Its author can unpublish their own skill and publish
  it again later.
- **Take down** is the security removal for team owners and admins: only admins can still see the
  skill. Admins can also restore it, and find retired skills with the library's status filter.

Both stop new installs and updates. Copies already installed keep working, and `hermes wisdom check`
shows the skill's new state.

## Where it works

| Surface | What you get |
|---|---|
| Terminal | Every `hermes wisdom` command. |
| Hermes CLI chat and TUI | Reads (`browse`, `show`, `versions`, `status`, `help`), `top` and `share-suggest` answer in place. Commands that change something reply with the terminal command to run. |
| Hermes Desktop | A Collective Wisdom page with **Catalog**, **Installed** and **Contribute** tabs. In chat, `/wisdom browse` and `/wisdom show` open the catalog in a dialog with Install. |
| Telegram private chat, Slack DM | Review and confirmation cards for share, install and update, catalog cards, `top` and `share-suggest`. Every card is bound to the person who started it. |
| Group chats and channels | Private actions are refused with a pointer to a private chat. No skill names or files are shown. |
| Other platforms | Use the terminal. |

## Commands

| Command | What it does |
|---|---|
| `setup` | Accept the data-use disclosure and enroll this profile with your team. |
| `status` | Show local and service readiness. |
| `list`, `browse [query]` | Read the team library. |
| `show <skill>`, `versions <skill>` | One skill and its published versions. |
| `top` | Rank your own skills by usage (`--page`, `--per-page`, `--json`). |
| `share-suggest` | Ask Hermes which of your skills are worth sharing (chat command). |
| `share <skill>` | Review and share a skill or a new version. |
| `install <skill>[@vN]` | Install an exact version; `--plan` reviews without installing. |
| `installed`, `check <skill>` | Installed skills and update availability. |
| `update <skill>` | Review and apply one update. |
| `installed-setup` | Review and approve a skill's declared setup commands. |
| `uninstall <skill>` | Move an installed skill to recoverable trash. |
| `suggest`, `prepared`, `publication` | The individual draft and publication steps behind `share`. |
| `help` | Command help. |

Every command takes `--help`, and most take `--json` for scripts. In chat, use `/wisdom <command>`.
In scripts, `--yes` confirms a share; scanner warnings additionally need `--accept-warnings`.

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `HERMES_SYNC_BASE_URL` or `sync.base_url` in `config.yaml` | `https://gateway-gateway.nousresearch.com` | The Collective Wisdom service. Change it only when your team runs its own. |
| `plugins.entries.collective-wisdom.allow_gateway_injection` | `false` | Lets `/wisdom share-suggest` start its Hermes turn in the TUI, Desktop and messaging chats. The classic CLI chat does not need it. |

```bash
hermes config set plugins.entries.collective-wisdom.allow_gateway_injection true
```

Without that setting, `/wisdom share-suggest` replies with your ranked list and a ready-to-send prompt
instead.

## Access and roles

Collective Wisdom is enabled per team. Every member of an enabled team can browse, install and share.
Team owners and admins can also take down and restore skills. Access is checked each time Hermes
refreshes your Nous sign-in, so a change to the team's access applies without reinstalling anything.

## Security checks

Every share is checked twice before anything is published:

- **On your machine, before upload:** private keys, live credentials and secret-like assignments
  block the share. Warnings from the Hermes skills scanner need your acknowledgement, and editing the
  package resets it.
- **On the service, before publishing:** the package and its description are scanned against the
  built-in checks and your team's content rules, and scanned again at publish time against the
  current rules. A blocked package is not published.

Packages can contain the skill's `SKILL.md`, supporting files, `references/`, `scripts/`,
`templates/` and `assets/`, up to 10 MiB in total. Compiled executables, symlinks, hard links,
credentials and private keys are refused. Scripts in a package are never run during sharing, review
or install.

## Privacy and data use

- **Network calls:** Collective Wisdom talks to the Collective Wisdom service (see
  [Configuration](#configuration)) and uses your existing Nous sign-in. It asks for no other
  credentials.
- **What leaves your machine:** only what you approve: the package files and description of a skill
  you share, its compatibility metadata, and the record of which skills you installed through
  Collective Wisdom.
- **What stays local:** usage signals for your skills, read from Hermes's own skill usage records,
  stay on your profile. `top` reads them locally.
- **Models:** `/wisdom share-suggest` sends the names, descriptions and usage counts of up to 30 of
  your skills to the model of the conversation you run it in, and that model may read those skills'
  files. No other command calls a model.
- **No background activity:** there is no telemetry, no polling and no background process. The plugin
  acts only when you run a Collective Wisdom command.

## Troubleshooting

- **"Collective Wisdom credentials are unavailable":** run `hermes auth status nous` and sign in again
  if your session expired.
- **The service does not offer Collective Wisdom:** your team does not have it enabled yet. Signing in
  alone does not enable it.
- **Setup asks for `--accept-disclosure`:** run `hermes wisdom setup` in an interactive terminal to
  read and accept the disclosure. Use the flag in automation only after it has been accepted.
- **A share is blocked:** remove the flagged content, then run the same share command again.
  Credential and private-key findings always block; never disable the scanner.
- **An uncertain result:** run the status command it names. Do not repeat the share or install until
  you know whether the first one completed.
- **A share command is refused in a group chat:** sharing happens in a private chat or the terminal.

## Disable or remove

```bash
hermes plugins disable collective-wisdom    # keep it installed, stop it loading
hermes plugins remove collective-wisdom     # uninstall the plugin
```

Removing the plugin does not delete the skills it installed. They stay in your profile's skills
directory as ordinary skills.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m pylint hermes_wisdom_plugin tests
npm ci --ignore-scripts && npm test
```

Tests run without live credentials. [Testing](docs/TESTING.md) covers the host-compatibility checks
against Hermes releases. [Architecture](docs/ARCHITECTURE.md), [host APIs](docs/HOST-API.md),
[workflows](docs/WORKFLOWS.md) and [features](docs/FEATURES.md) document the implementation, and
[Contributing](CONTRIBUTING.md) the development expectations. See [Security](SECURITY.md) to report a
vulnerability.

## License

MIT. See [LICENSE](LICENSE).
