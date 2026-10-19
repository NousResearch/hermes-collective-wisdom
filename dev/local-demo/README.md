# Collective Wisdom: local demo

Runs Collective Wisdom end to end on one Mac. Two fake teammates, **alice**
(team owner) and **bob** (member), share and install skills through a local
Gateway, optionally with Hermes Desktop and the real Portal. Nothing is sent
off the machine: every process runs under a macOS sandbox profile that only
allows loopback (`no-network.sb`), and Ctrl-C deletes everything it created.

```bash
./dev/local-demo/launch.sh --smoke             # quick check: alice shares, bob installs, tear down
./dev/local-demo/launch.sh                     # Gateway + alice/bob in the terminal
./dev/local-demo/launch.sh --portal --desktop  # everything: Portal and Desktop windows too
```

## Setup

### 1. Check out the repos side by side

The launcher lives in this repo and expects the other repos next to it:

```
<workspace>/
  collective-wisdom/       this repo (the plugin)
  gateway-gateway/         NousResearch/gateway-gateway
  nous-account-service/    NousResearch/hermes-portal (only needed for --portal)
```

Put every repo on the same feature branch. To use other paths, set
`CW_GATEWAY_DIR` and `CW_PORTAL_DIR`.

### 2. Install the tools (once)

| Tool | Why |
|---|---|
| macOS | the network block uses `sandbox-exec` |
| Node 24+, npm, pnpm | Gateway, Portal, the small local fakes |
| Docker (Docker Desktop or Colima) | throwaway databases |
| Python 3 | small launcher helpers |
| Hermes Agent, normal install (`~/.hermes/hermes-agent`) | alice, bob and Desktop run on it |

If Hermes lives somewhere else, set `HERMES_SRC` (the checkout) and
`HERMES_PYTHON` (its venv python).

### 3. Download dependencies (once, needs network)

```bash
docker pull postgres:16-alpine                   # Gateway database
docker pull timescale/timescaledb:2.17.2-pg16    # Portal database  (--portal only)
docker pull redis:7.4.1-alpine                   # Portal redis     (--portal only)

(cd ../gateway-gateway && npm ci)
(cd ../nous-account-service && pnpm install)                                         # --portal only
(cd ../nous-account-service/apps/nous-account-service && npx playwright install chromium)  # --portal windows only
```

These are official images and the repos' own packages. The launcher itself
never downloads anything: Docker runs with `--pull never` and Hermes lazy
installs are off.

## What you get

- **Terminal:** the launcher prints a `PATH` line. With it, `alice …` and
  `bob …` run Hermes as each user, for example:
  ```bash
  alice wisdom share deploy-checklist --folder DevOps
  bob wisdom list
  bob wisdom install <skill-id>
  ```
- **Desktop (`--desktop`):** two Hermes Desktop windows, one per user. In each
  one, turn the plugin on once: **Capabilities → Plugins → Collective Wisdom →
  Desktop**. **Collective Wisdom** then shows in the sidebar. Every run starts
  fresh, so you turn it on again each run.
- **Portal (`--portal`):** the real Portal from `../nous-account-service`,
  with alice and bob as real accounts in "Local Test Team". Two browser windows
  open already signed in at `/orgs/cw-local/wisdom`. The Portal library is
  read-only: sharing and installing happen in Hermes.

To try an update, edit alice's skill file (the launcher prints its path), share
it again, and bob sees **Updated**. If bob changed his copy, he sees
**Needs a look** with Combine / Keep mine / Use new version.

## Known limits

- **No real AI model.** Chat answers with one fixed line from a local fake, so
  **Combine** and folder suggestions (which need a model) don't work offline.
  Keep mine and Use new version do.
- The Portal's first page load takes about 20 seconds while Next.js compiles.
- `--desktop` needs a Hermes checkout with Desktop built
  (`apps/desktop/dist` and `apps/desktop/node_modules/electron`).

## What runs (all on 127.0.0.1)

| Piece | What it is |
|---|---|
| Postgres (`postgres:16-alpine`) | Gateway database, throwaway container |
| `fake-s3.mjs` | in-memory S3 for skill files |
| `fake-portal.mjs` | sign-in for alice/bob without `--portal` (random in-memory key); also the canned local model |
| Gateway | `../gateway-gateway` with the Collective Wisdom registry on |
| alice, bob | sandboxed Hermes homes under `.run/`, with this plugin installed |
| `--portal`: Portal, timescale, redis | `../nous-account-service` plus official images |
| `--portal`: `fake-upstash.mjs`, `fake-qstash.mjs` | stand-ins for the Upstash images the Portal's docker-compose uses |
| `--portal`: Privy, LiteLLM, Stripe, SES, relay mocks | the Portal's own `e2e/mocks` |
| `--desktop`: `open-desktop.sh` | one `hermes serve` backend + Desktop window per user |

Your real `~/.hermes` is never read or written; each user has its own home
under `.run/`. All keys and tokens are generated per run and deleted on exit.

## Troubleshooting

- If something fails, logs are kept in `dev/local-demo/last-logs/`.
- "another launch is running": stop it with Ctrl-C in its terminal.
- If Docker isn't running and Colima is installed, the launcher starts Colima,
  and stops it again on exit.

## Tests

`./dev/local-demo/test.sh` runs only the Collective Wisdom test suites across
the Gateway, this plugin and the Portal, then cleans up. `--help` lists the
options.
