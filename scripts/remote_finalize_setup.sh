#!/usr/bin/env bash
set +e

OPENROUTER_KEY="${OPENROUTER_KEY:-}"
LIGHTPANDA_WSS_URL="${LIGHTPANDA_WSS_URL:-}"

run() {
  echo "[CMD] $*"
  eval "$@"
  local code=$?
  echo "[EXIT:$code]"
  return 0
}

echo "[INFO] Finalizing remote setup"

run "openclaw config set agents.defaults.model.primary \"nemotron-4:14b\""
if [[ -n "${OPENROUTER_KEY}" ]]; then
  run "openclaw config set env.OPENROUTER_API_KEY \"${OPENROUTER_KEY}\""
fi
run "openclaw config set agents.research.model \"nemotron-4:14b\""
run "openclaw config set agents.guardian.model \"nemotron-4:14b\""
run "openclaw config set runtime.sandbox \"openshell\""
run "openclaw config set agents.guardian.killThreshold \"0.25\""
run "openclaw config set audit.immutableLog \"true\""
run "openclaw config set changes.requireHumanApproval \"true\""

for pkg in \
  lossless-claw guardian-executor research-agent lightpanda-browser \
  webull-browser kalshi-browser news-sentinel agent-watcher claude-watchdog
do
  run "clawhub install ${pkg}"
done

if [[ -n "${LIGHTPANDA_WSS_URL}" ]]; then
  run "openclaw config set agents.defaults.browser.engine \"lightpanda-cloud\""
  run "openclaw config set agents.defaults.browser.wssEndpoint \"${LIGHTPANDA_WSS_URL}\""
  run "openclaw config set agents.defaults.browser.fallback \"chrome\""
fi

run "openclaw config set channels.telegram.enabled \"true\""
run "openclaw config set channels.telegram.configWrites \"true\""
run "openclaw config set channels.telegram.capabilities.inlineButtons \"all\""
run "openclaw config set execApprovals.enabled \"true\""

run "mkdir -p ~/.openclaw/workspace"
run "cp /tmp/finalImplemenatationSoul.md ~/.openclaw/workspace/SOUL.md"

tmp="$(mktemp)"
crontab -l 2>/dev/null | awk '
  $0 !~ /openclaw run-agent trading-daily-review/ &&
  $0 !~ /openclaw run-agent trading-scan/ &&
  $0 !~ /openclaw run-agent research-cycle/ &&
  $0 !~ /openclaw run-agent evolutionary-tune/
' > "$tmp"
cat >> "$tmp" <<'EOF'
0 9 * * * openclaw run-agent trading-daily-review
*/5 * * * * openclaw run-agent trading-scan
0 8,20 * * * openclaw run-agent research-cycle
0 3 * * 0 openclaw run-agent evolutionary-tune
EOF
crontab "$tmp"
rm -f "$tmp"

run "sudo systemctl restart openclaw-gateway"
run "openclaw health"
run "systemctl is-active openclaw-gateway"
run "crontab -l"
run "rg -n \"FIRST-MINUTE MOMENTUM FILTER|COPY-TRADING FILTER|BOT FARMING DEFENSE\" ~/.openclaw/workspace/SOUL.md"

echo "[INFO] Remote finalization complete"
