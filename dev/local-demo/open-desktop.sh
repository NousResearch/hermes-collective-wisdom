#!/usr/bin/env bash
# open-desktop.sh <hermes-home> <gateway-url> <portal-url> <run-dir> <hermes-src> <hermes-python>
#
# Opens Hermes Desktop as one sandboxed launcher user (alice or bob):
#   1. starts that user's Hermes backend (`hermes serve`) on a loopback port,
#      with the user's HERMES_HOME, so it has the plugin and the local login;
#   2. opens the prebuilt Desktop app (from your local Hermes install) and
#      connects it to that backend, with its own Electron data dir and app
#      name, so your normal Hermes.app and ~/.hermes are never touched.
# Both run under dev/local-demo/no-network.sb (loopback only, no DNS).
set -uo pipefail
HOME_DIR="$1"; GW_URL="$2"; PORTAL_URL="$3"; RUN="$4"; HERMES_SRC="$5"; HERMES_PY="$6"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SB="$HERE/no-network.sb"
DESKTOP="$HERMES_SRC/apps/desktop"
ELECTRON="$DESKTOP/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
[[ -x "$ELECTRON" && -f "$DESKTOP/dist/electron-main.mjs" ]] || {
  echo "prebuilt Hermes Desktop not found under $DESKTOP (needs dist/ and node_modules/electron)" >&2
  exit 1
}
NAME="$(basename "$HOME_DIR")"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
mkdir -p "$RUN/desktop-$NAME" "$RUN/desktop-bin"
# /bin/ps is setuid and cannot run inside sandbox-exec; Desktop only needs a
# process-state probe, which ps-shim.py answers.
ln -sfn "$HERE/ps-shim.py" "$RUN/desktop-bin/ps" 2>/dev/null || true

BACKEND_PID=""
stop() {
  [[ -n "$BACKEND_PID" ]] && { pkill -TERM -P "$BACKEND_PID" 2>/dev/null; kill "$BACKEND_PID" 2>/dev/null; }
}
trap stop EXIT
trap 'exit 130' INT TERM

# 1. The user's backend.
(
  cd "$HERMES_SRC" || exit 1
  exec sandbox-exec -f "$SB" env PATH="$RUN/desktop-bin:$PATH" \
    HERMES_HOME="$HOME_DIR" HERMES_SHARED_AUTH_DIR="$HOME_DIR/shared-auth" \
    HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_DASHBOARD_SESSION_TOKEN="$TOKEN" \
    HERMES_SYNC_BASE_URL="$GW_URL" HERMES_PORTAL_BASE_URL="$PORTAL_URL" \
    NOUS_INFERENCE_BASE_URL="$PORTAL_URL" PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="${HERMES_PYTHONPATH:-$HERMES_SRC}" \
    "$HERMES_PY" -m hermes_cli.main serve --host 127.0.0.1 --port "$PORT" --skip-build --isolated
) >"$RUN/logs/backend-$NAME.log" 2>&1 &
BACKEND_PID=$!
for _ in $(seq 1 90); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/status" -H "X-Hermes-Session-Token: $TOKEN" 2>/dev/null && break
  kill -0 "$BACKEND_PID" 2>/dev/null || { echo "backend for $NAME exited; see $RUN/logs/backend-$NAME.log" >&2; exit 1; }
  sleep 1
done
echo "backend for $NAME on http://127.0.0.1:$PORT"
echo "$PORT $TOKEN" >"$RUN/desktop-$NAME/backend"

# 2. Desktop, connected to that backend. Chromium's own process sandbox cannot
# start inside sandbox-exec, so it is turned off; the network block still applies.
# Desktop plugins only get a connection id from a saved connection, not from
# the HERMES_DESKTOP_REMOTE_* env route, so save this backend as the primary
# connection in this window's own (throwaway) data dir.
python3 - "$RUN/desktop-$NAME/connections.json" "$NAME" "http://127.0.0.1:$PORT" "$TOKEN" <<'PY'
import json, sys
path, name, url, token = sys.argv[1:]
json.dump({
    "version": 2, "primary": "cw-local", "launchMode": "primary", "lastUsed": "cw-local",
    "connections": [
        {"id": "local", "kind": "local", "label": "This Mac"},
        {"id": "cw-local", "kind": "remote", "label": f"CW local ({name})", "url": url,
         "authMode": "token", "token": {"encoding": "plain", "value": token}},
    ],
}, open(path, "w"), indent=2)
PY
chmod 600 "$RUN/desktop-$NAME/connections.json"
cd "$DESKTOP" || exit 1
sandbox-exec -f "$SB" env PATH="$RUN/desktop-bin:$PATH" \
  HERMES_HOME="$HOME_DIR" HERMES_SHARED_AUTH_DIR="$HOME_DIR/shared-auth" \
  HERMES_DESKTOP_USER_DATA_DIR="$RUN/desktop-$NAME" \
  HERMES_DESKTOP_APP_NAME="HermesCWLocal-$NAME" \
  HERMES_DISABLE_LAZY_INSTALLS=1 ELECTRON_DISABLE_SANDBOX=1 \
  "$ELECTRON" --no-sandbox "$DESKTOP"
