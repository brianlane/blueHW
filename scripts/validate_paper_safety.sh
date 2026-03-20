#!/usr/bin/env bash
set -euo pipefail

# Validate Phase 4/5 prerequisites on the target host.

failures=0
pass() { printf "[PASS] %s\n" "$*"; }
fail() { printf "[FAIL] %s\n" "$*"; failures=$((failures + 1)); }

need() {
  if command -v "$1" >/dev/null 2>&1; then
    pass "command available: $1"
  else
    fail "command missing: $1"
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Run on the Linux Lightsail host."
  exit 1
fi

need openclaw
need crontab
need systemctl

SOUL_PATH="${HOME}/.openclaw/workspace/SOUL.md"
if [[ -f "${SOUL_PATH}" ]]; then
  pass "SOUL file exists: ${SOUL_PATH}"
else
  fail "SOUL file missing: ${SOUL_PATH}"
fi

for phrase in \
  "FIRST-MINUTE MOMENTUM FILTER" \
  "COPY-TRADING FILTER" \
  "BOT FARMING DEFENSE" \
  "LOSSLESS CLAW + NEMOCLAW + GUARDIAN-EXECUTOR + TAILSCALE + OLLAMA"
do
  if rg -q "${phrase}" "${SOUL_PATH}" 2>/dev/null; then
    pass "SOUL contains: ${phrase}"
  else
    fail "SOUL missing section: ${phrase}"
  fi
done

if crontab -l 2>/dev/null | rg -q "openclaw run-agent trading-daily-review"; then
  pass "daily-review cron exists"
else
  fail "missing daily-review cron"
fi

if crontab -l 2>/dev/null | rg -q "openclaw run-agent trading-scan"; then
  pass "trading-scan cron exists"
else
  fail "missing trading-scan cron"
fi

if crontab -l 2>/dev/null | rg -q "openclaw run-agent research-cycle"; then
  pass "research-cycle cron exists"
else
  fail "missing research-cycle cron"
fi

if crontab -l 2>/dev/null | rg -q "openclaw run-agent evolutionary-tune"; then
  pass "evolutionary-tune cron exists"
else
  fail "missing evolutionary-tune cron"
fi

if systemctl is-active --quiet openclaw-gateway; then
  pass "openclaw-gateway service active"
else
  fail "openclaw-gateway service inactive"
fi

printf "\nManual Telegram checks still required:\n"
printf "  1) /status\n"
printf "  2) paper-mode full scan\n"
printf "  3) research cycle recall + proposal\n"
printf "  4) worst-loss autopsy output\n"

if [[ "${failures}" -gt 0 ]]; then
  printf "\nValidation failed with %d issue(s).\n" "${failures}"
  exit 1
fi

printf "\nValidation passed.\n"
