#!/usr/bin/env bash
set -euo pipefail

# Deploy canonical SOUL and configure cron jobs.
# Run this ON the target Lightsail OpenClaw host.

log() { printf "\n[%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_SOUL="${ROOT_DIR}/finalImplemenatationSoul.md"
TARGET_SOUL="${HOME}/.openclaw/workspace/SOUL.md"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script must run on the Linux Lightsail instance."
  exit 1
fi

if [[ ! -f "${SOURCE_SOUL}" ]]; then
  echo "Missing source SOUL file: ${SOURCE_SOUL}"
  exit 1
fi

if ! command -v openclaw >/dev/null 2>&1; then
  echo "openclaw command not found."
  exit 1
fi

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab command not found."
  exit 1
fi

log "Deploying canonical SOUL.md"
mkdir -p "$(dirname "${TARGET_SOUL}")"
cp "${SOURCE_SOUL}" "${TARGET_SOUL}"

log "Installing cron jobs"
tmp_cron="$(mktemp)"
crontab -l 2>/dev/null | awk '
  $0 !~ /openclaw run-agent trading-daily-review/ &&
  $0 !~ /openclaw run-agent trading-scan/ &&
  $0 !~ /openclaw run-agent research-cycle/ &&
  $0 !~ /openclaw run-agent evolutionary-tune/
' > "${tmp_cron}" || true
cat >> "${tmp_cron}" <<'EOF'
0 9 * * * openclaw run-agent trading-daily-review
*/5 * * * * openclaw run-agent trading-scan
0 8,20 * * * openclaw run-agent research-cycle
0 3 * * 0 openclaw run-agent evolutionary-tune
EOF
crontab "${tmp_cron}"
rm -f "${tmp_cron}"

log "Restarting gateway"
sudo systemctl restart openclaw-gateway
openclaw status || true

log "SOUL + cron deployment complete"
