#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
WORKSPACE="${CODEX_AUTOMATE_WORKSPACE:-$ROOT_DIR}"
POLL_SECONDS="${CODEX_AUTOMATE_POLL_SECONDS:-5}"

CHECK_ARGS=(
  -m
  codex_automate
  worker-check
  --workspace
  "$WORKSPACE"
  --quiet
)

"$PYTHON_BIN" "${CHECK_ARGS[@]}"

CMD=(
  "$PYTHON_BIN"
  -m
  codex_automate
  serve-workers
  --workspace
  "$WORKSPACE"
  --poll-seconds
  "$POLL_SECONDS"
)

if [[ -n "${CODEX_AUTOMATE_MAX_CYCLES:-}" ]]; then
  CMD+=(--max-cycles "$CODEX_AUTOMATE_MAX_CYCLES")
fi

if [[ -n "${CODEX_AUTOMATE_GOAL_ID:-}" ]]; then
  CMD+=(--goal-id "$CODEX_AUTOMATE_GOAL_ID")
fi

if [[ "${CODEX_AUTOMATE_STOP_WHEN_IDLE:-0}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  CMD+=(--stop-when-idle)
fi

IFS=',' read -r -a AGENT_NAMES <<< "${CODEX_AUTOMATE_AGENT_NAMES:-}"
for agent_name in "${AGENT_NAMES[@]}"; do
  if [[ -n "$agent_name" ]]; then
    CMD+=(--agent "$agent_name")
  fi
done

exec "${CMD[@]}"
