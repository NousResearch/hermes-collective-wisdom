#!/usr/bin/env bash
# test.sh: run the Collective Wisdom v1 checks locally across the
# Gateway, the plugin and the Portal, then clean up everything it created.
#
#   ./dev/local-demo/test.sh                  all three repos
#   ./dev/local-demo/test.sh gateway plugin   only some (gateway | plugin | portal)
#   ./dev/local-demo/test.sh --no-db          Gateway without the Postgres-backed tests
#   ./dev/local-demo/test.sh --keep-deps      keep node_modules this run installed
#   ./dev/local-demo/test.sh --keep-logs      keep the per-step logs after exit
#
# Runs only the Collective Wisdom suites (never Hermes core tests). Commits
# nothing. Cleanup runs on any exit, including Ctrl-C and failures, and only
# removes what this run created: the throwaway Postgres container, Colima (if
# this run started it), the plugin virtualenv, the plugin's egg-info and Python
# caches, node_modules it installed, and the log directory.

set -uo pipefail

# Repos are sibling checkouts of collective-wisdom (see launch.sh).
PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(dirname "$PLUGIN")"
GATEWAY="${CW_GATEWAY_DIR:-$ROOT/gateway-gateway}"
if [[ -z "${CW_PORTAL_DIR:-}" ]]; then
  CW_PORTAL_DIR="$ROOT/nous-account-service"
  [[ -d "$CW_PORTAL_DIR" ]] || CW_PORTAL_DIR="$ROOT/hermes-portal"
fi
PORTAL="$CW_PORTAL_DIR"
PORTAL_APP="$PORTAL/apps/nous-account-service"

WANT=()
NO_DB=0
KEEP_DEPS=0
KEEP_LOGS=0
for arg in "$@"; do
  case "$arg" in
    gateway|plugin|portal) WANT+=("$arg") ;;
    --no-db) NO_DB=1 ;;
    --keep-deps) KEEP_DEPS=1 ;;
    --keep-logs) KEEP_LOGS=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done
[[ ${#WANT[@]} -eq 0 ]] && WANT=(gateway plugin portal)
want() { local x; for x in "${WANT[@]}"; do [[ "$x" == "$1" ]] && return 0; done; return 1; }

# ── State for cleanup ───────────────────────────────────────────────────────
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cw-v1-test.XXXXXX")"
PG_CONTAINER=""
STARTED_COLIMA=0
VENV_DIR=""
PLUGIN_EGG_PREEXISTED=1
INSTALLED_NODE_MODULES=()
CLEANED=0

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

cleanup() {
  [[ $CLEANED -eq 1 ]] && return
  CLEANED=1
  echo
  say "Cleaning up"
  if [[ -n "$PG_CONTAINER" ]]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 && note "removed container $PG_CONTAINER"
  fi
  if [[ $STARTED_COLIMA -eq 1 ]]; then
    colima stop >/dev/null 2>&1 && note "stopped Colima (this run started it)"
  fi
  if [[ -n "$VENV_DIR" && -d "$VENV_DIR" ]]; then
    rm -rf "$VENV_DIR" && note "removed plugin virtualenv"
  fi
  if [[ -d "$PLUGIN" ]]; then
    if [[ $PLUGIN_EGG_PREEXISTED -eq 0 ]]; then
      rm -rf "$PLUGIN"/*.egg-info && note "removed plugin egg-info"
    fi
    find "$PLUGIN" -name __pycache__ -type d -prune -not -path '*/node_modules/*' \
      -exec rm -rf {} + 2>/dev/null
    rm -rf "$PLUGIN/.pytest_cache"
  fi
  if [[ $KEEP_DEPS -eq 0 ]]; then
    local dir
    for dir in "${INSTALLED_NODE_MODULES[@]+"${INSTALLED_NODE_MODULES[@]}"}"; do
      rm -rf "$dir/node_modules" && note "removed node_modules in ${dir#$ROOT/}"
      # pnpm workspaces also install per-package node_modules.
      [[ "$dir" == "$PORTAL" ]] && find "$PORTAL/apps" "$PORTAL/packages" -maxdepth 2 \
        -name node_modules -type d -prune -exec rm -rf {} + 2>/dev/null
    done
  fi
  if [[ $KEEP_LOGS -eq 1 ]]; then
    note "logs kept in $LOG_DIR"
  else
    rm -rf "$LOG_DIR"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ── Step runner and summary ─────────────────────────────────────────────────
RESULTS=()
FAILED=0
step() {  # step <label> <command...>; runs in the current directory
  local label="$1"; shift
  local log="$LOG_DIR/$(echo "$label" | tr ' /:' '___').log"
  local start=$SECONDS
  printf '    %-44s ' "$label"
  if "$@" >"$log" 2>&1; then
    printf '\033[32mok\033[0m (%ss)\n' "$((SECONDS - start))"
    RESULTS+=("PASS  $label")
    return 0
  fi
  printf '\033[31mFAILED\033[0m (%ss)\n' "$((SECONDS - start))"
  RESULTS+=("FAIL  $label")
  FAILED=1
  echo "    ── last 40 lines of $label ──"
  tail -n 40 "$log" | sed 's/^/    │ /'
  return 1
}

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 2; }
}

# ── Gateway ─────────────────────────────────────────────────────────────────
gateway_db_up() {
  if ! docker info >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1; then
      note "starting Colima for Docker"
      colima start >/dev/null 2>&1 || return 1
      STARTED_COLIMA=1
    else
      return 1
    fi
  fi
  local port
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  PG_CONTAINER="cw-v1-test-pg-$$"
  docker run -d --rm --name "$PG_CONTAINER" \
    -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway -e POSTGRES_DB=cw_test \
    -p "127.0.0.1:$port:5432" postgres:16-alpine >/dev/null || return 1
  local i
  for i in $(seq 1 60); do
    docker exec "$PG_CONTAINER" psql -U gateway -d cw_test -c 'select 1' >/dev/null 2>&1 && break
    sleep 1
  done
  export CW_TEST_DB_URL="postgresql://gateway:gateway@127.0.0.1:$port/cw_test"
}

run_gateway() {
  say "Gateway ($GATEWAY)"
  require node; require npx
  cd "$GATEWAY" || return
  if [[ ! -d node_modules ]]; then
    step "gateway: npm ci" npm ci --no-audit --no-fund || return
    INSTALLED_NODE_MODULES+=("$GATEWAY")
  fi
  step "gateway: prisma generate" npx prisma generate || return
  step "gateway: type-check" npx tsc --noEmit -p .
  local suites=(test/sync/wisdom test/sync/publicationCoordinator test/openapi)
  if [[ $NO_DB -eq 1 ]]; then
    step "gateway: wisdom tests (in-memory)" npx vitest run "${suites[@]}"
    return
  fi
  if ! step "gateway: start throwaway Postgres" gateway_db_up; then
    note "Docker unavailable; running in-memory tests only (use --no-db to skip this attempt)"
    step "gateway: wisdom tests (in-memory)" npx vitest run "${suites[@]}"
    return
  fi
  step "gateway: prisma migrate deploy" env DATABASE_URL="$CW_TEST_DB_URL" npx prisma migrate deploy || return
  step "gateway: wisdom tests (+ Postgres)" env TEST_DATABASE_URL="$CW_TEST_DB_URL" \
    npx vitest run "${suites[@]}"
}

# ── Plugin ──────────────────────────────────────────────────────────────────
pick_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 1)' 2>/dev/null; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}

plugin_venv() {
  VENV_DIR="$LOG_DIR/plugin-venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q -p "$PY" "$VENV_DIR" &&
      uv pip install -q -p "$VENV_DIR/bin/python" -e "$PLUGIN[dev]"
  else
    "$PY" -m venv "$VENV_DIR" &&
      "$VENV_DIR/bin/pip" install -q -e "$PLUGIN[dev]"
  fi
}

run_plugin() {
  say "Plugin ($PLUGIN)"
  PY="$(pick_python)" || { echo "need Python 3.11-3.14 for the plugin" >&2; RESULTS+=("FAIL  plugin: python"); FAILED=1; return; }
  cd "$PLUGIN" || return
  compgen -G "$PLUGIN/*.egg-info" >/dev/null || PLUGIN_EGG_PREEXISTED=0
  step "plugin: virtualenv + deps" plugin_venv || return
  PYTHONDONTWRITEBYTECODE=1 step "plugin: pytest" \
    "$VENV_DIR/bin/python" -m pytest -q -p no:cacheprovider
}

# ── Portal ──────────────────────────────────────────────────────────────────
run_portal() {
  say "Portal ($PORTAL_APP)"
  require pnpm
  cd "$PORTAL" || return
  if [[ ! -d node_modules ]]; then
    step "portal: pnpm install" pnpm install --frozen-lockfile || return
    INSTALLED_NODE_MODULES+=("$PORTAL")
  fi
  cd "$PORTAL_APP" || return
  step "portal: type-check" node --max-old-space-size=6144 \
    node_modules/typescript/bin/tsc --noEmit --incremental false
  # CI runs Portal jest under SKIP_ENV_VALIDATION; it needs no real secrets.
  step "portal: wisdom + agents tests" env SKIP_ENV_VALIDATION=1 npx jest --silent \
    'src/app/\(portal-v2\)/orgs/\[orgSlug\]/(wisdom|agents)' \
    src/server/agents src/server/sync src/ui/hermes-wisdom \
    'src/app/\(portal-v2\)/wisdom'
}

# ── Main ────────────────────────────────────────────────────────────────────
for repo in gateway plugin portal; do
  if want "$repo"; then
    dir_var="$(echo "$repo" | tr '[:lower:]' '[:upper:]')"
    [[ -d "${!dir_var}" ]] || { echo "missing checkout: ${!dir_var}" >&2; exit 2; }
  fi
done

START=$SECONDS
want gateway && run_gateway
want plugin && run_plugin
want portal && run_portal

echo
say "Summary ($((SECONDS - START))s)"
for line in "${RESULTS[@]}"; do
  if [[ "$line" == PASS* ]]; then printf '    \033[32m%s\033[0m\n' "$line"
  else printf '    \033[31m%s\033[0m\n' "$line"; fi
done
exit $FAILED
