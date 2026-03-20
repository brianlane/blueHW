#!/usr/bin/env bash
set -euo pipefail

# Local precheck for required artifacts before remote setup.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

check_file() {
  local f="$1"
  if [[ -f "${ROOT_DIR}/${f}" ]]; then
    printf "[OK] %s\n" "${f}"
  else
    printf "[MISSING] %s\n" "${f}"
    return 1
  fi
}

echo "Running local precheck in ${ROOT_DIR}"

status=0
check_file "blueHWbotTodoChecklist.md" || status=1
check_file "finalImplemenatation.md" || status=1
check_file "finalImplemenatationSoul.md" || status=1
check_file "open_router_key.txt" || status=1
check_file ".kalshiKey" || status=1

if [[ ! -f "${ROOT_DIR}/.env.tradingbot" ]]; then
  echo "[MISSING] .env.tradingbot (copy from .env.tradingbot.example)"
  status=1
else
  echo "[OK] .env.tradingbot"
fi

if [[ "${status}" -ne 0 ]]; then
  echo "Precheck failed. Resolve missing items."
  exit 1
fi

echo "Precheck passed."
