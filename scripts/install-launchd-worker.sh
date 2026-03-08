#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
APP_SUPPORT_DIR="${HOME}/Library/Application Support/CodexAutomate"
LOG_DIR="${HOME}/Library/Logs/CodexAutomate"
PLIST_TEMPLATE="${ROOT_DIR}/deploy/launchd/com.alex.codex-automate-worker.plist"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.alex.codex-automate-worker.plist"
ENV_TARGET="${APP_SUPPORT_DIR}/worker.env"
LABEL="com.alex.codex-automate-worker"
UID_VALUE="$(id -u)"

mkdir -p "${LAUNCH_AGENTS_DIR}" "${APP_SUPPORT_DIR}" "${LOG_DIR}"

if [[ ! -f "${ENV_TARGET}" ]]; then
  cp "${ROOT_DIR}/deploy/worker.env.example" "${ENV_TARGET}"
fi

sed \
  -e "s|/Users/alex/Projects/git/Codex Automate|${ROOT_DIR}|g" \
  -e "s|/Users/alex/Library/Application Support/CodexAutomate|${HOME}/Library/Application Support/CodexAutomate|g" \
  -e "s|/Users/alex/Library/Logs/CodexAutomate|${HOME}/Library/Logs/CodexAutomate|g" \
  "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

chmod 644 "${PLIST_TARGET}"

if launchctl print "gui/${UID_VALUE}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/${UID_VALUE}" "${PLIST_TARGET}" || true
fi

launchctl bootstrap "gui/${UID_VALUE}" "${PLIST_TARGET}"
launchctl enable "gui/${UID_VALUE}/${LABEL}"
launchctl kickstart -k "gui/${UID_VALUE}/${LABEL}"

echo "Installed launchd worker: ${LABEL}"
echo "plist: ${PLIST_TARGET}"
echo "env:   ${ENV_TARGET}"
echo "logs:  ${LOG_DIR}"
