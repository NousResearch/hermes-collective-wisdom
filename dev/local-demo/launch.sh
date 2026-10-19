#!/usr/bin/env bash
# launch.sh: run Collective Wisdom end to end on this Mac, and tear
# everything down when you press Ctrl-C.
#
#   ./dev/local-demo/launch.sh            start everything and wait (Ctrl-C stops it all)
#   ./dev/local-demo/launch.sh --smoke    start, run a scripted alice-shares/bob-installs
#                                   check, then tear down (exit code = result)
#   ./dev/local-demo/launch.sh --desktop  also open Hermes Desktop signed in as alice
#                                   (and bob), sandboxed like the CLI users
#   ./dev/local-demo/launch.sh --portal   also run the real Portal (../nous-account-service)
#                                   and open it signed in as alice and bob.
#                                   alice and bob are then real Portal accounts.
#   Flags combine: ./dev/local-demo/launch.sh --portal --desktop
#
# What it starts (all on 127.0.0.1, nothing reaches a real Nous service):
#   - Postgres in a throwaway Docker container (official postgres image; Colima if needed)
#   - a tiny in-memory fake S3 (dev/local-demo/fake-s3.mjs) for skill files
#   - a fake Portal that signs in two fake users, alice (team owner) and bob
#     (member), in one fake team. Its signing key is random and lives only in
#     memory
#   - the Gateway from ../gateway-gateway with the Collective Wisdom registry on
#   - two sandboxed Hermes homes with the plugin from this repo
#     installed, plus `alice` and `bob` commands to drive them
#
# With --portal, the Portal runs from your checkout with the official
# timescale/timescaledb and redis images (already pulled), plus two tiny local
# stand-ins (dev/local-demo/fake-upstash.mjs, fake-qstash.mjs) instead of the
# third-party images its docker-compose.yaml uses. Sign-in is the Portal's own
# local Privy mock. The browser windows block every non-local request.
#
# Every process runs under dev/local-demo/no-network.sb (loopback only, no DNS).
#
# Your real ~/.hermes is never read or written: each user gets its own
# HERMES_HOME and shared-auth dir under dev/local-demo/.run, and Portal/inference
# URLs point at the local fake so a stray request cannot reach production.
# Everything under dev/local-demo/.run is deleted on exit.

set -uo pipefail

# This script lives in collective-wisdom/dev/local-demo. The other repos are
# expected as sibling checkouts of collective-wisdom (override with env vars):
#   <workspace>/collective-wisdom        this repo (the plugin)
#   <workspace>/gateway-gateway          CW_GATEWAY_DIR
#   <workspace>/nous-account-service     CW_PORTAL_DIR (or hermes-portal; only for --portal)
# Hermes itself comes from your normal install (HERMES_SRC / HERMES_PYTHON).
HELPERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$(cd "$HELPERS/../.." && pwd)"
WORKSPACE="$(dirname "$PLUGIN")"
GATEWAY="${CW_GATEWAY_DIR:-$WORKSPACE/gateway-gateway}"
if [[ -z "${CW_PORTAL_DIR:-}" ]]; then
  CW_PORTAL_DIR="$WORKSPACE/nous-account-service"
  [[ -d "$CW_PORTAL_DIR" ]] || CW_PORTAL_DIR="$WORKSPACE/hermes-portal"
fi
RUN="$HELPERS/.run"
SMOKE=0
DESKTOP=0
PORTAL=0
NAS="$CW_PORTAL_DIR/apps/nous-account-service"
for arg in "$@"; do
  case "$arg" in
    --smoke) SMOKE=1 ;;
    --desktop) DESKTOP=1 ;;
    --portal) PORTAL=1 ;;
  esac
done
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { sed -n '2,/^set -uo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0; }

TAG="cw-local-$$"
PG_CONTAINER=""
NAS_PG=""
NAS_REDIS=""
STARTED_COLIMA=0
PIDS=()
UI_PIDS=()  # Desktop/browser windows: stopped on exit, not watched
CLEANED=0

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
# Every process this script starts runs under macOS sandbox-exec with
# dev/local-demo/no-network.sb: loopback only, no DNS. Children inherit it.
SB="$HELPERS/no-network.sb"
nonet() { sandbox-exec -f "$SB" "$@"; }

cleanup() {
  [[ $CLEANED -eq 1 ]] && return
  CLEANED=1
  echo
  say "Shutting down and cleaning up"
  local pid
  for pid in "${UI_PIDS[@]+"${UI_PIDS[@]}"}" "${PIDS[@]+"${PIDS[@]}"}"; do
    pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "${UI_PIDS[@]+"${UI_PIDS[@]}"}" "${PIDS[@]+"${PIDS[@]}"}"; do
    pkill -KILL -P "$pid" 2>/dev/null; kill -KILL "$pid" 2>/dev/null
  done
  [[ ${#PIDS[@]} -gt 0 ]] && note "stopped Gateway, fake Portal and fake S3"
  [[ -n "$PG_CONTAINER" ]] && docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 && note "removed Postgres container"
  [[ -n "$NAS_PG" ]] && docker rm -f "$NAS_PG" >/dev/null 2>&1 && note "removed Portal database container"
  [[ -n "$NAS_REDIS" ]] && docker rm -f "$NAS_REDIS" >/dev/null 2>&1 && note "removed Portal redis container"
  if [[ $STARTED_COLIMA -eq 1 ]]; then
    colima stop >/dev/null 2>&1 && note "stopped Colima (this run started it)"
  fi
  if [[ "${KEEP_LOGS:-0}" == 1 && -d "$RUN/logs" ]]; then
    rm -rf "$HELPERS/last-logs" && cp -R "$RUN/logs" "$HELPERS/last-logs"
    note "kept logs in dev/local-demo/last-logs"
  fi
  rm -rf "$RUN" && note "deleted sandboxed Hermes homes, logs and alice/bob commands"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
wait_http() {  # wait_http <url> <seconds>
  local i
  for i in $(seq 1 "$2"); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# ── Preflight ───────────────────────────────────────────────────────────────
for tool in node npx docker curl python3; do
  command -v "$tool" >/dev/null || die "missing required tool: $tool"
done
HERMES_SRC="${HERMES_SRC:-$HOME/.hermes/hermes-agent}"
# Use the exact interpreter and dependency set your installed Hermes runs on
# (from Hermes's install records), so the sandbox needs nothing installed.
if [[ -z "${HERMES_PYTHON:-}" ]]; then
  HERMES_VENV="$(python3 "$HELPERS/find-hermes-env.py" "$HERMES_SRC")" ||
    die "could not find your Hermes dependency environment (set HERMES_PYTHON)"
  HERMES_PY="$HERMES_VENV/bin/python"
  # That python is the shared base interpreter; Hermes's packages load from the
  # venv's site-packages via PYTHONPATH, exactly as your running gateway does.
  HERMES_PYTHONPATH="$HERMES_SRC:$(echo "$HERMES_VENV"/lib/python3.*/site-packages)"
else
  HERMES_PY="$HERMES_PYTHON"
fi
[[ -x "$HERMES_PY" && -d "$HERMES_SRC/hermes_cli" ]] ||
  die "Hermes not found (set HERMES_PYTHON and HERMES_SRC to a Hermes checkout and its venv python)"
HERMES_PYTHONPATH="${HERMES_PYTHONPATH:-$HERMES_SRC}"
export HERMES_PY HERMES_SRC HERMES_PYTHONPATH
[[ -d "$GATEWAY/src" ]] || die "Gateway checkout not found at $GATEWAY (clone gateway-gateway next to collective-wisdom, or set CW_GATEWAY_DIR)"
[[ -d "$GATEWAY/node_modules" ]] || die "run 'npm ci' in $GATEWAY first"
[[ -d "$PLUGIN/hermes_wisdom_plugin" ]] || die "plugin checkout not found at $PLUGIN"
if [[ $PORTAL -eq 1 ]]; then
  [[ -d "$NAS" ]] || die "Portal checkout not found at $CW_PORTAL_DIR (clone it next to collective-wisdom, or set CW_PORTAL_DIR)"
  [[ -d "$NAS/node_modules" ]] || die "run 'pnpm install' in $CW_PORTAL_DIR first"
fi
if [[ -e "$RUN" ]]; then
  # Only refuse if another launcher is actually running; otherwise it is a
  # leftover from an interrupted run and safe to clear.
  other="$(cat "$RUN/launcher.pid" 2>/dev/null)"
  if [[ -n "$other" ]] && kill -0 "$other" 2>/dev/null; then
    die "another launch.sh (pid $other) is running; stop it first (Ctrl-C in its terminal)"
  fi
  rm -rf "$RUN"
fi
mkdir -p "$RUN/logs" "$RUN/bin"
echo $$ >"$RUN/launcher.pid"

# ── Docker: Postgres ────────────────────────────────────────────────────────
say "Starting throwaway Postgres"
if ! docker info >/dev/null 2>&1; then
  command -v colima >/dev/null || die "Docker is not running"
  note "starting Colima"
  colima start >/dev/null 2>&1 || die "colima start failed"
  STARTED_COLIMA=1
fi
PG_PORT="$(free_port)"
PG_CONTAINER="$TAG-pg"
docker run -d --rm --pull never --name "$PG_CONTAINER" -p "127.0.0.1:$PG_PORT:5432" \
  -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway -e POSTGRES_DB=cw_local \
  postgres:16-alpine >/dev/null || die "could not start Postgres (needs the postgres:16-alpine image cached: docker pull postgres:16-alpine)"
for _ in $(seq 1 60); do
  docker exec "$PG_CONTAINER" pg_isready -U gateway -d cw_local >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U gateway -d cw_local >/dev/null 2>&1 || die "Postgres did not come up"
if [[ $PORTAL -eq 1 ]]; then
  for image in timescale/timescaledb:2.17.2-pg16 redis:7.4.1-alpine; do
    docker image inspect "$image" >/dev/null 2>&1 ||
      die "--portal needs the official $image image (docker pull $image)"
  done
fi
DB_URL="postgresql://gateway:gateway@127.0.0.1:$PG_PORT/cw_local"
note "Postgres on 127.0.0.1:$PG_PORT"

# ── Fake S3 ─────────────────────────────────────────────────────────────────
say "Starting the in-memory fake S3"
nonet node "$HELPERS/fake-s3.mjs" >"$RUN/logs/s3.json" 2>"$RUN/logs/s3.err" &
PIDS+=($!)
for _ in $(seq 1 30); do [[ -s "$RUN/logs/s3.json" ]] && break; sleep 0.3; done
S3_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$RUN/logs/s3.json")" ||
  die "fake S3 did not start: $(cat "$RUN/logs/s3.err")"
note "fake S3 on $S3_URL"

# ── Portal (real, --portal) or fake Portal ──────────────────────────────────
# The fake Portal always runs: it serves the canned local model. Without
# --portal it also signs in alice and bob.
say "Starting the fake Portal (local model; sign-in unless --portal)"
nonet node "$HELPERS/fake-portal.mjs" >"$RUN/logs/portal.json" 2>"$RUN/logs/portal.err" &
PIDS+=($!)
for _ in $(seq 1 30); do [[ -s "$RUN/logs/portal.json" ]] && break; sleep 0.3; done
FAKE_PORTAL_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$RUN/logs/portal.json")" ||
  die "fake Portal did not start: $(cat "$RUN/logs/portal.err")"
PORTAL_URL="$FAKE_PORTAL_URL"
note "fake Portal on $FAKE_PORTAL_URL"

nas_run() {  # nas_run <log> <cmd...>: run in the Portal dir with its env, offline
  local log="$1"; shift
  (cd "$NAS" && exec sandbox-exec -f "$SB" env -i PATH="$PATH" HOME="$RUN" \
    bash -c 'set -a; source "$0"; set +a; exec "$@"' "$RUN/nas.env" "$@") >"$log" 2>&1
}

if [[ $PORTAL -eq 1 ]]; then
  say "Starting the real Portal (nous-account-service)"
  NAS_PG="$TAG-nas-pg"; NAS_REDIS="$TAG-nas-redis"
  NAS_PG_PORT="$(free_port)"; NAS_REDIS_PORT="$(free_port)"
  docker run -d --rm --pull never --name "$NAS_PG" -p "127.0.0.1:$NAS_PG_PORT:5432" \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=local -e POSTGRES_DB=nous-account-service \
    timescale/timescaledb:2.17.2-pg16 postgres -c max_connections=1000 \
    -c timescaledb.max_background_workers=0 >/dev/null || die "could not start the Portal database"
  docker run -d --rm --pull never --name "$NAS_REDIS" -p "127.0.0.1:$NAS_REDIS_PORT:6379" \
    redis:7.4.1-alpine >/dev/null || die "could not start the Portal redis"
  for _ in $(seq 1 60); do
    docker exec "$NAS_PG" pg_isready -U postgres -d nous-account-service >/dev/null 2>&1 && break
    sleep 1
  done
  nonet node "$HELPERS/fake-upstash.mjs" "$NAS_REDIS_PORT" >"$RUN/logs/upstash.json" 2>"$RUN/logs/upstash.err" &
  PIDS+=($!)
  nonet node "$HELPERS/fake-qstash.mjs" >"$RUN/logs/qstash.json" 2>"$RUN/logs/qstash.err" &
  PIDS+=($!)
  for _ in $(seq 1 30); do [[ -s "$RUN/logs/upstash.json" && -s "$RUN/logs/qstash.json" ]] && break; sleep 0.3; done
  UPSTASH_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$RUN/logs/upstash.json")" ||
    die "fake Upstash did not start: $(cat "$RUN/logs/upstash.err")"
  QSTASH_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$RUN/logs/qstash.json")" ||
    die "fake QStash did not start: $(cat "$RUN/logs/qstash.err")"

  NAS_APP_PORT="$(free_port)"; NAS_PRIVY_PORT="$(free_port)"; NAS_LITELLM_PORT="$(free_port)"
  NAS_RELAY_PORT="$(free_port)"; NAS_STRIPE_PORT="$(free_port)"; NAS_SES_PORT="$(free_port)"
  NAS_GW_PORT="$(free_port)"
  NAS_URL="http://127.0.0.1:$NAS_APP_PORT"
  NAS_DB="postgresql://postgres:local@127.0.0.1:$NAS_PG_PORT/nous-account-service"
  # The Portal's own e2e env (placeholder keys, logging muzzled), with every
  # URL rewritten to this run's loopback ports. Nothing points off-machine.
  (
    cd "$NAS" || exit 1
    env -i PATH="$PATH" bash -c '
      set -a; source e2e/browser/env.source; set +a
      export NODE_ENV=development NEXT_TELEMETRY_DISABLED=1 CHECKPOINT_DISABLE=1 DO_NOT_TRACK=1
      export PRISMA_HIDE_UPDATE_MESSAGE=1 AWS_EC2_METADATA_DISABLED=true SKIP_ENV_VALIDATION=1
      export BASE_URL="$1" DATABASE_URL="$2" DATABASE_URL_DIRECT="$2" LITELLM_RO_DB_URL="$2"
      export UPSTASH_REDIS_REST_URL="$3" LITELLM_REDIS_URL="$3"
      export QSTASH_BASE_URL="$4" QSTASH_CALLBACK_BASE_URL="$1"
      export PRIVY_API_URL="http://127.0.0.1:$5" LITELLM_URL="http://127.0.0.1:$6" LITELLM_ADMIN_URL="http://127.0.0.1:$6"
      export HERMES_RELAY_CONNECTOR_URL="http://127.0.0.1:$7" STRIPE_API_URL="http://127.0.0.1:$8"
      export AWS_ENDPOINT_URL_SESV2="http://127.0.0.1:$9"
      export AUTO_TOPUP_HEARTBEAT_URL="$1/api/auto-topup/heartbeat"
      export HERMES_WISDOM_LOCAL=1 HERMES_SYNC_PLANE_URL="http://127.0.0.1:${10}"
      export -p | sed -E "s/^declare -x //" | grep -vE "^(PATH|PWD|SHLVL|OLDPWD|_)(=|$)"
    ' _ "$NAS_URL" "$NAS_DB" "$UPSTASH_URL" "$QSTASH_URL" "$NAS_PRIVY_PORT" "$NAS_LITELLM_PORT" \
      "$NAS_RELAY_PORT" "$NAS_STRIPE_PORT" "$NAS_SES_PORT" "$NAS_GW_PORT"
  ) >"$RUN/nas.env" || die "could not prepare the Portal environment"
  chmod 600 "$RUN/nas.env"
  grep -E "^(HERMES_|NODE_ENV|BASE_URL|SKIP_ENV)" "$RUN/nas.env" >"$RUN/logs/nas-env-summary.txt"
  for mock in privy litellm relay stripe ses; do
    nas_run "$RUN/logs/nas-mock-$mock.log" node_modules/.bin/tsx "e2e/mocks/$mock/main.ts" &
    PIDS+=($!)
  done
  migrated=0
  for _ in $(seq 1 10); do
    if nas_run "$RUN/logs/nas-migrate.log" node_modules/.bin/prisma migrate deploy; then migrated=1; break; fi
    grep -q P1001 "$RUN/logs/nas-migrate.log" || break
    sleep 3
  done
  [[ $migrated -eq 1 ]] || die "Portal migrations failed: $(tail -5 "$RUN/logs/nas-migrate.log")"
  nas_run "$RUN/logs/nas-seed.log" node_modules/.bin/tsx scripts/seed-local.ts ||
    die "Portal seed failed: $(tail -5 "$RUN/logs/nas-seed.log")"
  note "Portal database migrated and seeded"
  (ulimit -n 65536; nas_run "$RUN/logs/nas-app.log" node_modules/.bin/next dev --hostname 127.0.0.1 --port "$NAS_APP_PORT") &
  PIDS+=($!)
  note "Portal starting on $NAS_URL (first compile takes a minute or two)"
  code=""
  for _ in $(seq 1 150); do
    code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' -H 'Authorization: Bearer x' "$NAS_URL/api/oauth/account" 2>/dev/null)"
    [[ "$code" == "401" ]] && break
    sleep 2
  done
  [[ "$code" == "401" ]] || die "Portal did not come up: $(tail -15 "$RUN/logs/nas-app.log")"
  note "Portal is up"

  say "Creating Portal accounts alice and bob in one team"
  for user in alice bob; do
    case "$user" in alice) display=Alice ;; *) display=Bob ;; esac
    nas_run "$RUN/$user.account.out" node_modules/.bin/tsx scripts/mint-account.ts \
      --email "$user@cw-local.test" --name "$display" ||
      die "could not create Portal account $user: $(tail -5 "$RUN/$user.account.out")"
    python3 - "$RUN/$user.account.out" "$RUN/$user.account.json" <<'PY' || die "mint-account printed no account for $user"
import json, sys
text = open(sys.argv[1]).read()
start = text.index("\n{") + 1 if not text.startswith("{") else 0
data, _ = json.JSONDecoder().raw_decode(text[start:])
json.dump(data, open(sys.argv[2], "w"))
PY
  done
  nas_run "$RUN/logs/team.json" node_modules/.bin/tsx "$HELPERS/portal-team.mts" "$NAS" \
    "$RUN/alice.account.json" "$RUN/bob.account.json" ||
    die "could not set up the team: $(tail -8 "$RUN/logs/team.json")"
  python3 - "$RUN/logs/team.json" "$RUN/logs/portal.json" "$NAS_URL" <<'PY' || die "could not read the team tokens: $(tail -5 "$RUN/logs/team.json")"
import json, sys
team = json.loads([line for line in open(sys.argv[1]) if line.startswith("{")][-1])
fake = json.load(open(sys.argv[2]))
fake["tokens"] = {name: tok["access_token"] for name, tok in team["tokens"].items()}
fake["refresh"] = {name: tok["refresh_token"] for name, tok in team["tokens"].items()}
json.dump(fake, open(sys.argv[2], "w"))
PY
  PORTAL_URL="$NAS_URL"
  note "team \"Local Test Team\": alice (owner), bob (member)"
fi

# ── Gateway ─────────────────────────────────────────────────────────────────
say "Starting the Gateway with the Collective Wisdom registry"
(cd "$GATEWAY" && DATABASE_URL="$DB_URL" CHECKPOINT_DISABLE=1 PRISMA_HIDE_UPDATE_MESSAGE=1 nonet node_modules/.bin/prisma migrate deploy) >"$RUN/logs/migrate.log" 2>&1 ||
  die "migrations failed (see output above): $(tail -5 "$RUN/logs/migrate.log")"
GW_PORT="${NAS_GW_PORT:-$(free_port)}"
(
  cd "$GATEWAY" || exit 1
  # env -i: start from an empty environment so nothing from your shell (prod
  # URLs, tokens, OTEL collectors) leaks into the local Gateway.
  exec sandbox-exec -f "$SB" env -i PATH="$PATH" HOME="$RUN" NODE_ENV=development PORT="$GW_PORT" \
    DATABASE_URL="$DB_URL" NOUS_PORTAL_URL="$PORTAL_URL" \
    SYNC_ENABLED=1 SYNC_OBJECT_STORE_REGION=us-east-1 SYNC_OBJECT_STORE_BUCKET=cw-local \
    SYNC_OBJECT_STORE_ACCESS_KEY_ID=local-fake SYNC_OBJECT_STORE_SECRET_ACCESS_KEY=local-fake \
    SYNC_OBJECT_STORE_ENDPOINT="$S3_URL" WISDOM_REGISTRY=1 \
    node_modules/.bin/tsx src/index.ts
) >"$RUN/logs/gateway.log" 2>&1 &
PIDS+=($!)
GW_URL="http://127.0.0.1:$GW_PORT"
if ! wait_http "$GW_URL/healthz" 90; then
  tail -30 "$RUN/logs/gateway.log" | sed 's/^/    │ /'
  die "Gateway did not become healthy"
fi
grep -q "Collective Wisdom registry ENABLED" "$RUN/logs/gateway.log" ||
  die "Gateway is up but the wisdom registry is not enabled"
note "Gateway on $GW_URL (registry enabled)"

# ── Sandboxed Hermes users ──────────────────────────────────────────────────
say "Creating sandboxed Hermes users alice and bob"
make_user() {  # make_user <name>
  local name="$1" home="$RUN/$1"
  mkdir -p "$home/plugins" "$home/shared-auth" "$home/skills"
  # Copy (not link) the plugin so the sandbox can never write into your checkout.
  rsync -a --exclude .git --exclude .venv --exclude node_modules --exclude '__pycache__' \
    --exclude '*.egg-info' --exclude .pytest_cache "$PLUGIN/" "$home/plugins/collective-wisdom/"
  # The model is the fake Portal's canned local endpoint, so Hermes and
  # Desktop count as set up without any real provider or network access.
  cat >"$home/config.yaml" <<YAML
model:
  provider: custom
  base_url: $FAKE_PORTAL_URL/v1
  api_key: local-demo
  default: local-demo
plugins:
  enabled:
    - collective-wisdom
sync:
  base_url: $GW_URL
YAML
  python3 - "$home/auth.json" "$name" "$PORTAL_URL" "$RUN/logs/portal.json" <<'PY'
import json, sys
path, name, portal, issued = sys.argv[1:]
data = json.load(open(issued))
state = {"access_token": data["tokens"][name], "portal_base_url": portal, "auth_method": "oauth"}
if data.get("refresh", {}).get(name):
    state.update(refresh_token=data["refresh"][name], client_id="hermes-cli")
json.dump({"version": 1, "active_provider": "nous", "providers": {"nous": state}},
    open(path, "w"), indent=2)
PY
  chmod 600 "$home/auth.json"
  cat >"$RUN/bin/$name" <<SH
#!/usr/bin/env bash
# Runs Hermes as $name inside the local sandbox. Example: $name wisdom list
exec sandbox-exec -f "$SB" env HERMES_HOME="$home" HERMES_SHARED_AUTH_DIR="$home/shared-auth" \\
  HERMES_DISABLE_LAZY_INSTALLS=1 \\
  HERMES_SYNC_BASE_URL="$GW_URL" HERMES_PORTAL_BASE_URL="$PORTAL_URL" \\
  NOUS_INFERENCE_BASE_URL="$FAKE_PORTAL_URL" PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="$HERMES_PYTHONPATH" \\
  "$HERMES_PY" -m hermes_cli.main "\$@"
SH
  chmod +x "$RUN/bin/$name"
}
make_user alice
make_user bob

# A skill for alice to share.
mkdir -p "$RUN/alice/skills/devops/deploy-checklist"
cat >"$RUN/alice/skills/devops/deploy-checklist/SKILL.md" <<'MD'
---
name: deploy-checklist
description: Walk through the team's pre-deploy checks before shipping to production.
---

# Deploy checklist

1. Confirm CI is green on the release commit.
2. Check the migration plan and the rollback step.
3. Post in #deploys with the commit and expected impact.
MD

export PATH="$RUN/bin:$PATH"
(cd "$HERMES_SRC" && alice wisdom --help) >/dev/null 2>"$RUN/logs/alice-help.err" ||
  die "the plugin did not load in the sandbox: $(tail -5 "$RUN/logs/alice-help.err")"
for user in alice bob; do
  (cd "$HERMES_SRC" && "$user" wisdom setup --accept-disclosure) >"$RUN/logs/$user-setup.log" 2>&1 ||
    die "wisdom setup failed for $user: $(tail -5 "$RUN/logs/$user-setup.log")"
done
note "alice: team owner (can share, install, moderate, admin)"
note "bob:   team member (can share and install)"

# ── Smoke check ─────────────────────────────────────────────────────────────
if [[ $SMOKE -eq 1 ]]; then
  say "Smoke check: alice shares, bob installs"
  cd "$HERMES_SRC" || exit 1
  ok=1
  run() { echo "    \$ $*"; "$@" 2>&1 | sed 's/^/    │ /'; return "${PIPESTATUS[0]}"; }
  run alice wisdom share deploy-checklist --folder DevOps --yes </dev/null || ok=0
  run bob wisdom list || ok=0
  skill_id="$(bob wisdom list --json 2>/dev/null | python3 -c '
import json,sys
data=json.load(sys.stdin)
rows=data.get("skills", data) if isinstance(data, dict) else data
print(next((r.get("skill_id") or r.get("id") for r in rows if "deploy" in json.dumps(r)), ""))' 2>/dev/null)"
  if [[ -z "$skill_id" ]]; then
    note "bob cannot see alice's skill"; ok=0
  else
    run bob wisdom install "$skill_id" </dev/null || ok=0
    run bob wisdom installed || ok=0
    ls -d "$RUN/bob/skills/_wisdom"/*/deploy-checklist >/dev/null 2>&1 ||
      { note "skill files not found in bob's sandbox"; ok=0; }
  fi
  if [[ $PORTAL -eq 1 && -n "$skill_id" ]]; then
    # The Portal's own library page, fetched as bob (signed-in cookies), must
    # list alice's skill. This is the page the browser windows open.
    cookie="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["curlHeader"])' "$RUN/bob.account.json")"
    page="$RUN/logs/portal-library.html"
    for _ in 1 2 3; do
      code="$(curl -s -m 180 -o "$page" -w '%{http_code}' -H "$cookie" "$NAS_URL/orgs/cw-local/wisdom")"
      [[ "$code" == "200" ]] && grep -q "Deploy Checklist" "$page" && break
      sleep 5
    done
    if [[ "$code" == "200" ]] && grep -q "Deploy Checklist" "$page"; then
      note "Portal library page lists deploy-checklist (as bob)"
    else
      note "Portal library page did not list the skill (HTTP $code; saved to logs/portal-library.html)"
      ok=0
    fi
  fi
  if [[ $ok -eq 1 ]]; then say "Smoke check passed"; exit 0; fi
  KEEP_LOGS=1
  say "Smoke check FAILED (Gateway log tail below)"
  tail -20 "$RUN/logs/gateway.log" | sed 's/^/    │ /'
  exit 1
fi

# ── Desktop ─────────────────────────────────────────────────────────────────
if [[ $DESKTOP -eq 1 ]]; then
  say "Opening Hermes Desktop as alice and bob"
  for user in alice bob; do
    chmod +x "$HELPERS/open-desktop.sh"
    "$HELPERS/open-desktop.sh" "$RUN/$user" "$GW_URL" "$PORTAL_URL" "$RUN" "$HERMES_SRC" "$HERMES_PY" \
      >"$RUN/logs/desktop-$user.log" 2>&1 &
    UI_PIDS+=($!)
  done
  note "two Desktop windows: HermesCWLocal-alice and HermesCWLocal-bob"
  note "open Collective Wisdom in the sidebar of each"
fi

# ── Portal browser windows ──────────────────────────────────────────────────
PORTAL_LIB=""
if [[ $PORTAL -eq 1 ]]; then
  PORTAL_LIB="$NAS_URL/orgs/cw-local/wisdom"
  if [[ $SMOKE -ne 1 ]]; then
    say "Opening the Portal as alice and bob"
    # Playwright's own downloaded Chrome for Testing (under your real home;
    # the Portal processes run with HOME set to the sandbox).
    chrome="$(ls -d "$HOME"/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"Google Chrome for Testing.app" 2>/dev/null |
      tail -1)"
    [[ -n "$chrome" ]] && printf "CW_CHROME=%q\n" "$chrome/Contents/MacOS/Google Chrome for Testing" >>"$RUN/nas.env"
    nas_run "$RUN/logs/portal-browser.log" node "$HELPERS/open-portal.mjs" "$NAS" "$NAS_URL" \
      "http://127.0.0.1:$NAS_PRIVY_PORT" "/orgs/cw-local/wisdom" \
      "alice=$RUN/alice.account.json" "bob=$RUN/bob.account.json" &
    UI_PIDS+=($!)
    note "two browser windows, signed in as alice and bob: $PORTAL_LIB"
  fi
fi

# ── Ready ───────────────────────────────────────────────────────────────────
cat <<EOF

$(printf '\033[1;32m')Collective Wisdom is running locally.$(printf '\033[0m') Press Ctrl-C here to stop and clean up everything.

In another terminal tab:

  export PATH="$RUN/bin:\$PATH"
  cd "$HERMES_SRC"

  alice wisdom share deploy-checklist --folder DevOps    # alice shares a skill
  bob wisdom list                                        # bob sees it
  bob wisdom install <skill-id>                          # bob installs it
  bob wisdom installed

  # To try an update, edit alice's copy, share again, then:
  #   \$EDITOR "$RUN/alice/skills/devops/deploy-checklist/SKILL.md"
  #   alice wisdom share deploy-checklist
  #   bob wisdom update <skill-id>

  Logs: $RUN/logs/gateway.log
$( [[ -n "$PORTAL_LIB" ]] && printf '\n  Portal (Collective Wisdom library): %s\n  Use the two browser windows the script opened; they are already signed in.\n' "$PORTAL_LIB" )

EOF
UI_WARNED=""
while true; do
  for pid in "${UI_PIDS[@]+"${UI_PIDS[@]}"}"; do
    if ! kill -0 "$pid" 2>/dev/null && [[ " $UI_WARNED " != *" $pid "* ]]; then
      UI_WARNED="$UI_WARNED $pid"
      note "a Desktop or browser window closed (pid $pid); everything else keeps running."
      note "logs: $RUN/logs/desktop-*.log, portal-browser.log"
    fi
  done
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      KEEP_LOGS=1
      say "A background service (pid $pid) exited unexpectedly; last Gateway log lines:"
      tail -20 "$RUN/logs/gateway.log" | sed 's/^/    │ /'
      exit 1
    fi
  done
  sleep 2
done
