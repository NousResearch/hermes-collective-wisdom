#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${WISDOM_GATEWAY_DIR:?Set WISDOM_GATEWAY_DIR to the pinned Gateway checkout}"
: "${WISDOM_AGENT_DIR:?Set WISDOM_AGENT_DIR to the pinned Agent checkout}"
: "${WISDOM_AGENT_PYTHON:?Set WISDOM_AGENT_PYTHON to a host-compatible Python executable}"
: "${WISDOM_PORTAL_DIR:?Set WISDOM_PORTAL_DIR to the Git checkout containing the Portal pin}"
: "${WISDOM_PORTAL_REVISION:?Set WISDOM_PORTAL_REVISION to the full Portal release commit}"
: "${WISDOM_OUTPUT_DIR:?Set WISDOM_OUTPUT_DIR to a fresh evidence directory}"

PLUGIN_PYTHON="${WISDOM_PLUGIN_PYTHON:-$ROOT/.venv/bin/python}"
PNPM_BIN="${WISDOM_PNPM:-$(command -v pnpm || true)}"
[[ -n "$PNPM_BIN" ]] || {
  echo "pnpm is required (set WISDOM_PNPM to its absolute path)" >&2
  exit 2
}

exec "$PLUGIN_PYTHON" "$ROOT/scripts/test_wisdom_offline_acceptance.py" \
  --plugin-dir "$ROOT" \
  --gateway-dir "$WISDOM_GATEWAY_DIR" \
  --portal-dir "$WISDOM_PORTAL_DIR" \
  --portal-revision "$WISDOM_PORTAL_REVISION" \
  --agent-dir "$WISDOM_AGENT_DIR" \
  --plugin-python "$PLUGIN_PYTHON" \
  --agent-python "$WISDOM_AGENT_PYTHON" \
  --pnpm "$PNPM_BIN" \
  --output-dir "$WISDOM_OUTPUT_DIR"
